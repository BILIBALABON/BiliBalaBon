"""專案儲存層：一個專案 = projects/<id>/ 一個資料夾。

所有狀態都落在檔案上（project.json / state.json / events.jsonl / incidents/），
監控介面與 orchestrator 是兩個行程，只透過這些檔案溝通，因此任一方重啟都不會遺失進度。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import STAGES
from .config import Config, load_config

DOC_EXT = {".pdf", ".docx", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SIDECAR_EXT = {".srt", ".vtt"}  # 與音檔／影片同檔名的字幕檔，視為現成逐字稿
ALLOWED_EXT = DOC_EXT | AUDIO_EXT | VIDEO_EXT | SIDECAR_EXT

_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9]{4}$")

DEFAULT_SETTINGS = {
    "language": "繁體中文",
    "audience": "",          # 例：大二修課學生、部門新進同仁
    "goal": "",              # 例：準備期中考、內部教育訓練
    "slide_target": 12,      # 期望投影片張數（實際允許 ±50%）
    "budget_ntd": None,      # None = 用 config 預設值
    "notes": "",             # 給 agent 的額外指示
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def kind_of(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in DOC_EXT:
        return "document"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return None


def safe_filename(name: str) -> str:
    """去掉路徑與危險字元，保留中文檔名。"""
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name).strip().strip(".") or "file"
    stem, ext = Path(name).stem, Path(name).suffix[:16]
    while len((stem + ext).encode("utf-8")) > 180 and stem:  # 檔名上限是 255「位元組」，中文一個字佔 3 個
        stem = stem[:-1]
    return (stem + ext) or "file"


def blank_state() -> dict:
    return {
        "status": "draft",  # draft | running | waiting | stopped | failed | done
        "pid": None,
        "started_at": None,
        "ended_at": None,
        "cost_usd": 0.0,
        "current_stage": None,
        "error": None,
        "stages": {s: {"status": "pending", "attempts": 0, "cost_usd": 0.0, "gate": None,
                       "workers": {}, "started_at": None, "ended_at": None} for s in STAGES},
    }


class ProjectError(Exception):
    pass


def pid_alive(pid) -> bool:
    """這個 pid 是不是還活著的 orchestrator 行程。

    當機後 pid 可能被別的程式重複使用，所以在有 /proc 的系統上會確認指令列裡有 learnloop，
    避免之後對不相干的行程送出停止訊號。
    """
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError):
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            if f.read().rsplit(")", 1)[1].split()[0] == "Z":  # 殭屍行程不算存活
                return False
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"learnloop" in f.read()
    except (OSError, IndexError):
        return True  # 沒有 /proc（macOS、Windows）：只能相信 pid


class Project:
    def __init__(self, pid: str, cfg: Config | None = None):
        if not _ID_RE.match(pid):
            raise ProjectError(f"專案代號格式不正確：{pid}")
        self.cfg = cfg or load_config()
        self.id = pid
        self.dir = (self.cfg.projects_dir / pid).resolve()
        if not self.dir.is_dir():
            raise ProjectError(f"找不到專案：{pid}")

    # ---- 路徑 -------------------------------------------------------------
    @property
    def uploads(self) -> Path: return self.dir / "uploads"
    @property
    def raw(self) -> Path: return self.dir / "raw"
    @property
    def work(self) -> Path: return self.dir / "work"
    @property
    def output(self) -> Path: return self.dir / "output"
    @property
    def state_dir(self) -> Path: return self.dir / ".state"
    @property
    def incidents_dir(self) -> Path: return self.dir / "incidents"
    @property
    def events_path(self) -> Path: return self.dir / "events.jsonl"

    # ---- 專案資料（project.json）：設定與來源清單 -------------------------
    @property
    def meta(self) -> dict:
        return read_json(self.dir / "project.json", {})

    def save_meta(self, meta: dict) -> None:
        meta["updated_at"] = now_iso()
        atomic_write_json(self.dir / "project.json", meta)

    @property
    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **self.meta.get("settings", {})}

    @property
    def sources(self) -> list[dict]:
        return self.meta.get("sources", [])

    def budget_ntd(self) -> float:
        b = self.settings.get("budget_ntd")
        return float(b) if b else self.cfg.default_project_budget_ntd

    def budget_usd(self) -> float:
        return self.cfg.ntd_to_usd(self.budget_ntd())

    def update_settings(self, patch: dict, title: str | None = None) -> dict:
        meta = self.meta
        s = {**DEFAULT_SETTINGS, **meta.get("settings", {})}
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS:
                s[k] = v
        try:
            s["slide_target"] = max(6, min(60, int(s["slide_target"])))
        except (TypeError, ValueError):  # 欄位被清空
            s["slide_target"] = DEFAULT_SETTINGS["slide_target"]
        if s.get("budget_ntd") not in (None, ""):
            s["budget_ntd"] = max(1.0, float(s["budget_ntd"]))
        else:
            s["budget_ntd"] = None
        meta["settings"] = s
        if title is not None and title.strip():
            meta["title"] = title.strip()[:120]
        self.save_meta(meta)
        return meta

    def add_source(self, filename: str, src_path: Path, role: str = "material") -> dict:
        """把已存到暫存位置的檔案收進專案。回傳來源紀錄。"""
        name = safe_filename(filename)
        kind = kind_of(name)
        if kind is None:
            raise ProjectError(f"不支援的檔案類型：{name}")
        meta = self.meta
        sources = meta.setdefault("sources", [])
        used = {s["filename"] for s in sources}
        stem, ext, n = Path(name).stem, Path(name).suffix, 1
        while name in used:  # 同名檔案加序號，不覆蓋
            n += 1
            name = f"{stem}-{n}{ext}"
        # 代號只增不減：刪掉 s02 再上傳新檔會拿到 s03，不會撞到 s02 留下的紀錄（略過清單、連結快取…）
        n = max([int(s["id"][1:]) for s in sources] + [int(meta.get("last_source_no", 0))]) + 1
        meta["last_source_no"] = n
        sid = f"s{n:02d}"
        self.uploads.mkdir(parents=True, exist_ok=True)
        dest = self.uploads / name
        shutil.move(str(src_path), dest)
        rec = {"id": sid, "filename": name, "kind": kind,
               "role": role if role in ("material", "reference") else "material",
               "size": dest.stat().st_size, "added_at": now_iso()}
        sources.append(rec)
        self.save_meta(meta)
        return rec

    def remove_source(self, sid: str) -> None:
        meta = self.meta
        keep, gone = [], None
        for s in meta.get("sources", []):
            if s["id"] == sid:
                gone = s
            else:
                keep.append(s)
        if gone is None:
            raise ProjectError(f"找不到來源：{sid}")
        meta["sources"] = keep
        self.save_meta(meta)
        (self.uploads / gone["filename"]).unlink(missing_ok=True)
        shutil.rmtree(self.raw / sid, ignore_errors=True)
        (self.work / "sources" / f"{sid}.md").unlink(missing_ok=True)
        skipped = read_json(self.state_dir / "skipped_sources.json", []) or []
        if sid in skipped:
            atomic_write_json(self.state_dir / "skipped_sources.json", [x for x in skipped if x != sid])

    def sources_changed(self) -> bool:
        """來源清單是否和上次執行時不同。"""
        return read_json(self.state_dir / "sources_sig.json") not in (None, self.sources_signature())

    def sources_signature(self) -> list:
        """來源清單的指紋。和上次執行時不同，就代表要從頭重新整理。"""
        return [[s["id"], s["filename"], s["size"], s.get("role", "material")] for s in self.sources]

    def set_source_role(self, sid: str, role: str) -> None:
        if role not in ("material", "reference"):
            raise ProjectError("role 只能是 material 或 reference")
        meta = self.meta
        for s in meta.get("sources", []):
            if s["id"] == sid:
                s["role"] = role
                self.save_meta(meta)
                return
        raise ProjectError(f"找不到來源：{sid}")

    def content_sources(self) -> list[dict]:
        """需要擷取的來源（字幕檔若已配給某個音檔／影片，就不單獨處理）。"""
        srcs = self.sources
        media_stems = {Path(s["filename"]).stem for s in srcs if s["kind"] in ("audio", "video")}
        return [s for s in srcs
                if not (s["kind"] == "sidecar" and Path(s["filename"]).stem in media_stems)]

    # ---- 執行狀態（state.json） ------------------------------------------
    @property
    def state(self) -> dict:
        st = read_json(self.dir / "state.json") or blank_state()
        for s in STAGES:  # 向前相容：補上新增的階段
            st["stages"].setdefault(s, blank_state()["stages"][s])
        return st

    def save_state(self, st: dict) -> None:
        st["updated_at"] = now_iso()
        atomic_write_json(self.dir / "state.json", st)

    def is_alive(self, pid: int | None = None) -> bool:
        return pid_alive(pid if pid is not None else self.state.get("pid"))

    def reconcile(self) -> dict:
        """state 說在跑、但行程已不在（當機或被強制結束）→ 改標成 stopped。"""
        st = self.state
        if st["status"] in ("running", "waiting") and not pid_alive(st.get("pid")):
            st = self.state  # 行程可能剛好在這一瞬間正常結束並寫入最終狀態：重讀一次，不要蓋掉它
            if st["status"] not in ("running", "waiting"):
                return st
            st["status"] = "stopped"
            st["pid"] = None
            st["error"] = st.get("error") or "執行行程已中斷，可按「開始」從上次通過的階段續跑"
            for s in st["stages"].values():
                if s["status"] in ("running", "waiting"):
                    s["status"] = "pending"
                for w in s.get("workers", {}).values():
                    if w.get("status") == "running":
                        w["status"] = "pending"
            self.save_state(st)
        return st

    # ---- 事件紀錄（events.jsonl，只增不改） -------------------------------
    def emit(self, type_: str, **data: Any) -> dict:
        ev = {"ts": now_iso(), "type": type_, **data}
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def read_events(self, offset: int = 0) -> tuple[list[dict], int]:
        """從位元組位移 offset 讀到檔尾，回傳 (事件, 新位移)。只回傳完整的行。"""
        try:
            with open(self.events_path, "rb") as f:
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            return [], 0
        end = data.rfind(b"\n")
        if end < 0:
            return [], offset
        out = []
        for line in data[: end + 1].splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out, offset + end + 1

    # ---- 處置單（incidents/） --------------------------------------------
    def open_incident(self, *, stage: str, kind: str, title: str, message: str,
                      options: list[dict], recommended: str, default: str,
                      details: list[str] | None = None, target: str | None = None) -> dict:
        iid = f"i{int(time.time() * 1000)}{secrets.token_hex(2)}"
        inc = {"id": iid, "stage": stage, "target": target, "kind": kind, "title": title,
               "message": message, "details": details or [], "options": options,
               "recommended": recommended, "default": default,
               "created_at": now_iso(), "response": None}
        atomic_write_json(self.incidents_dir / f"{iid}.json", inc)
        self.emit("incident_opened", stage=stage, target=target, incident=iid, title=title)
        return inc

    def incidents(self, pending_only: bool = False) -> list[dict]:
        out = []
        if self.incidents_dir.is_dir():
            for p in sorted(self.incidents_dir.glob("i*.json")):
                inc = read_json(p)
                if inc and (not pending_only or inc.get("response") is None):
                    out.append(inc)
        return out

    def get_incident(self, iid: str) -> dict | None:
        if not re.fullmatch(r"i[0-9a-f]+", iid):
            return None
        return read_json(self.incidents_dir / f"{iid}.json")

    def respond_incident(self, iid: str, choice: str, note: str = "", by: str = "user") -> dict:
        inc = self.get_incident(iid)
        if inc is None:
            raise ProjectError("找不到這張處置單")
        if inc.get("response") is not None:
            raise ProjectError("這張處置單已經回覆過了")
        if choice not in {o["key"] for o in inc["options"]}:
            raise ProjectError(f"沒有這個選項：{choice}")
        inc["response"] = {"choice": choice, "note": (note or "").strip()[:2000], "by": by, "at": now_iso()}
        atomic_write_json(self.incidents_dir / f"{iid}.json", inc)
        self.emit("incident_resolved", stage=inc["stage"], target=inc.get("target"),
                  incident=iid, choice=choice, by=by)
        return inc

    def close_stale_incidents(self) -> None:
        """重新開始前，把上次沒回覆的處置單標成已失效，避免彈窗殘留。"""
        for inc in self.incidents(pending_only=True):
            inc["response"] = {"choice": inc["default"], "note": "（執行已中斷，自動關閉）",
                               "by": "system", "at": now_iso()}
            atomic_write_json(self.incidents_dir / f"{inc['id']}.json", inc)

    # ---- 總覽 -------------------------------------------------------------
    def summary(self) -> dict:
        meta, st = self.meta, self.reconcile()
        done = sum(1 for s in st["stages"].values() if s["status"] in ("passed", "skipped"))
        return {
            "id": self.id, "title": meta.get("title", "未命名專案"),
            "created_at": meta.get("created_at"), "updated_at": st.get("updated_at") or meta.get("updated_at"),
            "status": st["status"], "current_stage": st.get("current_stage"),
            "progress": done / len(STAGES), "n_sources": len(meta.get("sources", [])),
            "cost_ntd": round(self.cfg.usd_to_ntd(st.get("cost_usd", 0.0)), 2),
            "budget_ntd": self.budget_ntd(),
            "has_output": (self.output / "deck.html").exists(),
        }


# ---- 模組層級函式 ---------------------------------------------------------

def create_project(title: str, settings: dict | None = None, cfg: Config | None = None) -> Project:
    cfg = cfg or load_config()
    cfg.projects_dir.mkdir(parents=True, exist_ok=True)
    pid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    d = cfg.projects_dir / pid
    for sub in ("uploads", "raw", "work/sources", "output", ".state", "incidents"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    meta = {"id": pid, "title": (title or "").strip()[:120] or "未命名專案",
            "created_at": now_iso(), "settings": dict(DEFAULT_SETTINGS), "sources": []}
    atomic_write_json(d / "project.json", meta)
    atomic_write_json(d / "state.json", blank_state())
    p = Project(pid, cfg)
    if settings:
        p.update_settings(settings)
    p.emit("project_created", title=meta["title"])
    return p


def list_projects(cfg: Config | None = None) -> list[dict]:
    cfg = cfg or load_config()
    out = []
    if cfg.projects_dir.is_dir():
        for d in cfg.projects_dir.iterdir():
            if d.is_dir() and _ID_RE.match(d.name) and (d / "project.json").exists():
                try:
                    out.append(Project(d.name, cfg).summary())
                except Exception:  # 壞掉的專案資料夾不該讓整個清單失敗
                    continue
    out.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "", reverse=True)
    return out


def delete_project(pid: str, cfg: Config | None = None) -> None:
    p = Project(pid, cfg)
    if p.reconcile()["status"] in ("running", "waiting"):
        raise ProjectError("專案正在執行，請先停止再刪除")
    shutil.rmtree(p.dir)

