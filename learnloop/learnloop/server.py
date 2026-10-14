"""監控介面後端（FastAPI）。

只做三件事：管理專案資料夾、啟動／停止 orchestrator 行程、把狀態與事件即時推給瀏覽器（SSE）。
管線本身在獨立行程（python -m learnloop run）裡跑，所以關掉或重啟這個伺服器不會中斷執行中的專案。

安全：預設只聽 127.0.0.1，沒有登入機制，請勿對外開放。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from jsonschema import Draft202012Validator
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from . import STAGE_LABELS, STAGES, __version__
from .cli import doctor_report
from .config import ROOT, load_config
from . import schemas
from .project import (ALLOWED_EXT, Project, ProjectError, create_project, delete_project, list_projects,
                      pid_alive, read_json)

app = FastAPI(title="LearnLoop", version=__version__, docs_url=None, redoc_url=None)
WEB = Path(__file__).parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
PROCS: dict[str, subprocess.Popen] = {}  # 這個伺服器啟動的 orchestrator 行程（專案代號 → 行程）


@app.middleware("http")
async def local_only(request: Request, call_next):
    """擋掉跨站請求：其他網頁不能代替使用者呼叫這個本機 API（CSRF／DNS rebinding）。"""
    hostport = request.headers.get("host") or ""
    host = hostport.split("]")[0] + "]" if hostport.startswith("[") else hostport.rsplit(":", 1)[0]
    allowed = LOCAL_HOSTS | set(filter(None, os.environ.get("LP_ALLOWED_HOSTS", "").split(",")))
    if host not in allowed:
        return JSONResponse({"detail": "只接受本機連線"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        # 寫入類請求只接受「這個介面自己」發出的：Origin 必須與本服務的 host:port 完全相同。
        # 只比主機名稱不夠——同一台電腦上其他連接埠的網頁也是 localhost。
        origin = request.headers.get("origin")
        if origin is not None and urlparse(origin).netloc != hostport:
            return JSONResponse({"detail": "來源不被允許"}, status_code=403)
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return JSONResponse({"detail": "來源不被允許"}, status_code=403)
        if request.url.path.endswith("/files"):
            limit = load_config().max_upload_mb * 1024 * 1024 * 8  # 一次拖進多個檔案的總量上限
            if int(request.headers.get("content-length") or 0) > limit:
                return JSONResponse({"detail": "一次上傳的總量太大，請分批上傳"}, status_code=413)
    return await call_next(request)


@app.exception_handler(ProjectError)
async def project_error(_request: Request, exc: ProjectError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def get_project(pid: str) -> Project:
    try:
        return Project(pid)
    except ProjectError as e:
        raise HTTPException(404, str(e))


def proc_running(pid: str) -> bool:
    """這個專案的 orchestrator 行程是否還活著（即使狀態檔已經寫成 stopped）。"""
    proc = PROCS.get(pid)
    if proc is None:
        return False
    if proc.poll() is None:
        return True
    PROCS.pop(pid, None)
    return False


def is_busy(p: Project) -> bool:
    return proc_running(p.id) or p.reconcile()["status"] in ("running", "waiting")


def load_review(p: Project) -> dict | None:
    """review.json 放在 agent 寫得到的 work/ 裡，送到介面前先確認格式與型別，不合就不顯示。"""
    data = read_json(p.work / "review.json")
    if not isinstance(data, dict) or list(Draft202012Validator(schemas.REVIEW).iter_errors(data)):
        return None
    return data


def snapshot(p: Project) -> dict:
    cfg = p.cfg
    st = p.reconcile()
    meta = p.meta
    outputs = []
    for name, label in (("deck.html", "簡報（HTML）"), ("deck.pptx", "簡報（PowerPoint）"), ("notes.md", "學習筆記"),
                        ("references.md", "參考資料"), ("report.json", "執行報告")):
        f = p.output / name
        if f.exists():
            outputs.append({"name": name, "label": label, "size": f.stat().st_size, "mtime": f.stat().st_mtime})
    return {
        "id": p.id, "title": meta.get("title"), "created_at": meta.get("created_at"),
        "settings": p.settings, "sources": meta.get("sources", []),
        "skipped_sources": read_json(p.state_dir / "skipped_sources.json", []) or [],
        "state": st,
        "cost_ntd": round(cfg.usd_to_ntd(st.get("cost_usd", 0.0)), 2),
        "budget_ntd": p.budget_ntd(),
        "incidents": p.incidents(pending_only=True) if st["status"] in ("running", "waiting") else [],
        "outputs": outputs,
        "review": load_review(p),
        "report": read_json(p.output / "report.json"),
    }


# ---- 頁面與系統資訊 -------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/api/system")
async def system():
    cfg = load_config()
    return {
        "version": __version__, "mock": cfg.mock,
        "stages": [{"key": s, "label": STAGE_LABELS[s]} for s in STAGES],
        "usd_twd_rate": cfg.usd_twd_rate, "default_budget_ntd": cfg.default_project_budget_ntd,
        "limits": {"max_attempts": cfg.max_attempts, "max_review_rounds": cfg.max_review_rounds,
                   "max_parallel": cfg.max_parallel, "max_upload_mb": cfg.max_upload_mb},
        "allowed_ext": sorted(ALLOWED_EXT),
        "doctor": await asyncio.to_thread(doctor_report),
    }


# ---- 專案總管 -------------------------------------------------------------

@app.get("/api/projects")
async def projects():
    return list_projects()


@app.post("/api/projects")
async def new_project(body: dict):
    p = create_project(str(body.get("title") or ""), body.get("settings") or None)
    return snapshot(p)


@app.get("/api/projects/{pid}")
async def project(pid: str):
    return snapshot(get_project(pid))


@app.patch("/api/projects/{pid}")
async def save_draft(pid: str, body: dict):
    """設定表單的自動暫存。執行中不可修改（避免跑到一半條件改變）。"""
    p = get_project(pid)
    if is_busy(p):
        raise HTTPException(409, "執行中無法修改設定，請先停止")
    p.update_settings(body.get("settings") or {}, title=body.get("title"))
    return snapshot(p)


@app.delete("/api/projects/{pid}")
async def remove_project(pid: str):
    get_project(pid)
    delete_project(pid)
    return {"ok": True}


# ---- 檔案上傳 -------------------------------------------------------------

@app.post("/api/projects/{pid}/files")
async def upload(pid: str, files: list[UploadFile] = File(...), role: str = Form("material")):
    p = get_project(pid)
    if is_busy(p):
        raise HTTPException(409, "執行中無法新增檔案，請先停止")
    limit = p.cfg.max_upload_mb * 1024 * 1024
    added, rejected = [], []
    for f in files:
        name = f.filename or "file"
        if Path(name).suffix.lower() not in ALLOWED_EXT:
            rejected.append({"filename": name, "reason": "不支援的檔案類型"})
            continue
        size = 0
        tmp = tempfile.NamedTemporaryFile(delete=False, dir=p.dir, prefix=".upload-")
        try:
            with tmp:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ProjectError(f"超過單檔上限 {p.cfg.max_upload_mb} MB")
                    tmp.write(chunk)
            if size == 0:
                raise ProjectError("檔案是空的")
            added.append(p.add_source(name, Path(tmp.name), role))
        except ProjectError as e:
            Path(tmp.name).unlink(missing_ok=True)
            rejected.append({"filename": name, "reason": str(e)})
        except OSError as e:  # 檔名不合法、磁碟空間不足…：只影響這一個檔案
            Path(tmp.name).unlink(missing_ok=True)
            rejected.append({"filename": name, "reason": f"無法儲存（{e.strerror or e}）"})
        except Exception:
            Path(tmp.name).unlink(missing_ok=True)
            raise
    if added:
        p.emit("files_added", files=[a["filename"] for a in added])
    return {"added": added, "rejected": rejected, "project": snapshot(p)}


@app.delete("/api/projects/{pid}/files/{sid}")
async def remove_file(pid: str, sid: str):
    p = get_project(pid)
    if is_busy(p):
        raise HTTPException(409, "執行中無法移除檔案，請先停止")
    p.remove_source(sid)
    return snapshot(p)


@app.patch("/api/projects/{pid}/files/{sid}")
async def set_role(pid: str, sid: str, body: dict):
    p = get_project(pid)
    if is_busy(p):
        raise HTTPException(409, "執行中無法修改，請先停止")
    p.set_source_role(sid, str(body.get("role")))
    return snapshot(p)


# ---- 開始／停止 -----------------------------------------------------------

@app.post("/api/projects/{pid}/start")
async def start(pid: str, body: dict | None = None):
    p = get_project(pid)
    if is_busy(p):
        raise HTTPException(409, "這個專案已經在執行中")
    if not p.content_sources():
        raise HTTPException(400, "請先上傳至少一份資料")
    from_stage = (body or {}).get("from_stage")
    if from_stage is not None and from_stage not in STAGES:
        raise HTTPException(400, f"沒有這個階段：{from_stage}")
    st = p.state
    if from_stage is None and st["status"] == "done" and not p.sources_changed():
        from_stage = STAGES[0]  # 已完成的專案再按開始 = 整個重跑（來源有變動時由 orchestrator 自行判斷要重做哪些）
    cmd = [sys.executable, "-m", "learnloop", "run", pid] + (["--from", from_stage] if from_stage else [])
    p.state_dir.mkdir(exist_ok=True)
    log = open(p.state_dir / "run.log", "ab")
    env = {**os.environ, "PYTHONPATH": str(ROOT) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=env)
    log.close()
    PROCS[pid] = proc
    st.update(status="running", pid=proc.pid, error=None)  # 行程自己也會寫，這裡先佔位避免重複啟動
    p.save_state(st)
    asyncio.get_running_loop().run_in_executor(None, proc.wait)  # 回收子行程，避免殭屍
    return snapshot(p)


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


@app.post("/api/projects/{pid}/stop")
async def stop(pid: str):
    p = get_project(pid)
    proc = PROCS.get(pid)
    # 以「行程本身」為準，不看狀態檔：orchestrator 會先把狀態寫成 stopped 才結束，中間有空窗
    proc_id = proc.pid if proc is not None and proc.poll() is None else p.state.get("pid")

    def alive() -> bool:
        return proc.poll() is None if proc is not None and proc.pid == proc_id else pid_alive(proc_id)

    if not proc_id or not alive():
        return snapshot(p)
    p.emit("stop_requested")
    _kill_group(int(proc_id), signal.SIGTERM)  # orchestrator 收到後會存檔並標成 stopped
    for _ in range(40):
        await asyncio.sleep(0.25)
        if not alive():
            break
    else:
        _kill_group(int(proc_id), signal.SIGKILL)  # 10 秒內沒停下來就強制結束（含 Claude CLI 子行程）
        for _ in range(20):
            await asyncio.sleep(0.1)
            if not alive():
                break
    if proc is not None and proc.pid == proc_id:
        # 即使正常結束，也清掉同一個行程群組裡殘留的子行程（轉檔到一半的 ffmpeg、Claude CLI）
        _kill_group(int(proc_id), signal.SIGKILL)
    return snapshot(p)


# ---- 處置單 ---------------------------------------------------------------

@app.get("/api/projects/{pid}/incidents")
async def incidents(pid: str):
    return get_project(pid).incidents()


@app.post("/api/projects/{pid}/incidents/{iid}/respond")
async def respond(pid: str, iid: str, body: dict):
    p = get_project(pid)
    p.respond_incident(iid, str(body.get("choice")), str(body.get("note") or ""))
    return snapshot(p)


# ---- 即時狀態（Server-Sent Events） ---------------------------------------

@app.get("/api/projects/{pid}/stream")
async def stream(pid: str, request: Request):
    p = get_project(pid)

    async def gen():
        offset, last_sig, last_beat = 0, None, time.monotonic()
        # 先送最近 300 筆紀錄（log_init：瀏覽器收到後「取代」現有紀錄，斷線重連才不會重複），之後只送新增的
        events, offset = p.read_events(0)
        yield f"event: log_init\ndata: {json.dumps(events[-300:], ensure_ascii=False)}\n\n"
        while True:
            if await request.is_disconnected():
                return
            if not p.dir.is_dir():
                yield "event: gone\ndata: {}\n\n"
                return
            events, offset = p.read_events(offset)
            if events:
                yield f"event: log\ndata: {json.dumps(events, ensure_ascii=False)}\n\n"
            snap = snapshot(p)
            sig = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            if sig != last_sig:
                last_sig = sig
                yield f"event: state\ndata: {sig}\n\n"
            if time.monotonic() - last_beat > 15:
                last_beat = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---- 成品與中間產物 -------------------------------------------------------

@app.get("/api/projects/{pid}/artifact/{path:path}")
async def artifact(pid: str, path: str, download: int = 0):
    p = get_project(pid)
    target = (p.dir / path).resolve()
    allowed_roots = [p.output.resolve(), p.work.resolve()]
    if not any(r == target or r in target.parents for r in allowed_roots) or not target.is_file():
        raise HTTPException(404, "找不到檔案")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix in (".md", ".json", ".txt"):
        mime = "text/plain; charset=utf-8"
    headers = {
        # 成品含有模型產生的內容：放進沙箱，讓它無法以本站身分呼叫 API
        "Content-Security-Policy": "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    }
    return FileResponse(target, media_type=mime, headers=headers,
                        filename=target.name if download else None,
                        content_disposition_type="attachment" if download else "inline")
