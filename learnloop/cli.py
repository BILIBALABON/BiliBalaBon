"""指令列進入點。

  python3 -m learnloop serve [--port 8765]          啟動監控介面
  python3 -m learnloop new "標題" 檔案…              建立專案並加入來源檔
  python3 -m learnloop run <專案代號> [--from 階段]  執行管線（監控介面也是呼叫這個）
  python3 -m learnloop list                          列出專案
  python3 -m learnloop gate <專案代號> <閘門>        手動跑閘門
  python3 -m learnloop watch <收件匣資料夾>          自動觸發：新資料夾進來就建立專案並執行
  python3 -m learnloop doctor                        檢查環境
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import STAGES, __version__
from .config import load_config
from .project import Project, ProjectError, create_project, list_projects


def cmd_run(args) -> int:
    from .pipeline import Pipeline
    cfg = load_config()
    project = Project(args.project, cfg)
    st = project.reconcile()
    # 監控介面啟動時會先把狀態標成 running 並填入本行程的 pid；只有「別的」行程在跑才算重複啟動
    if st["status"] in ("running", "waiting") and st.get("pid") != os.getpid():
        print("這個專案已經在執行中", file=sys.stderr)
        return 2

    async def main() -> str:
        pipeline = Pipeline(project, cfg)
        task = asyncio.ensure_future(pipeline.run(from_stage=args.from_stage))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, task.cancel)  # 停止鍵 → 取消 → 狀態存成 stopped
            except NotImplementedError:  # Windows
                signal.signal(sig, lambda *_: task.cancel())
        final = await task
        print(f"結束：{final}", flush=True)
        if final != "done":
            # 狀態已經存檔。背景執行緒裡可能還有跑到一半的轉檔或語音辨識（asyncio.run 會等它們做完才結束），
            # 「已停止」卻還在寫檔、佔 CPU 是不對的，所以直接結束行程。
            sys.stderr.flush()
            os._exit(1)
        return final

    asyncio.run(main())
    return 0


def cmd_new(args) -> int:
    project = create_project(args.title)
    for f in args.files:
        src = Path(f)
        if not src.is_file():
            print(f"找不到檔案：{f}", file=sys.stderr)
            return 2
        with tempfile.NamedTemporaryFile(delete=False, dir=project.dir) as tmp:
            shutil.copyfile(src, tmp.name)
        try:
            rec = project.add_source(src.name, Path(tmp.name), role="reference" if args.reference else "material")
            print(f"  + {rec['id']} {rec['filename']}（{rec['kind']}）")
        except ProjectError as e:
            Path(tmp.name).unlink(missing_ok=True)
            print(f"  ! {e}", file=sys.stderr)
    print(project.id)
    return 0


def cmd_list(_args) -> int:
    for s in list_projects():
        print(f"{s['id']}  {s['status']:8s} {s['progress'] * 100:3.0f}%  NT${s['cost_ntd']:<8.2f} {s['title']}")
    return 0


def cmd_gate(args) -> int:
    from . import gates
    project = Project(args.project)
    argv = [str(project.dir), args.gate] + (["--target", args.target] if args.target else [])
    return gates.main(argv)


# Claude Code CLI 2.1.294 起才認得 .claude/agents/ 指定的 5.5 世代模型（舊版會照錯的單價計費）。
# orchestrator 用 SDK 內建的 CLI（SDK 0.2.165 內建 2.1.294）；scripts/loop.sh 用系統上的 claude。
MIN_SDK = "0.2.165"
MIN_CLI = "2.1.294"


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def doctor_report() -> list[dict]:
    """環境檢查，監控介面的「系統狀態」也用這份。"""
    cfg = load_config()
    out = []

    def add(name, ok, detail, need="必要"):
        out.append({"name": name, "ok": bool(ok), "detail": detail, "need": need})

    add("Python", sys.version_info >= (3, 11), sys.version.split()[0] + "（需要 3.11 以上）")
    try:
        import claude_agent_sdk
        v = str(getattr(claude_agent_sdk, "__version__", "") or "版本不明")
        ok = bool(_version_tuple(v)) and _version_tuple(v) >= _version_tuple(MIN_SDK)
        add("Claude Agent SDK", ok or cfg.mock, v if ok else
            f"{v}：需要 {MIN_SDK} 以上（pip install -U claude-agent-sdk）。舊版內建的 CLI 不認得 agent 檔指定的模型，花費與預算上限會算錯")
    except Exception:
        add("Claude Agent SDK", cfg.mock, "未安裝：pip install claude-agent-sdk" + ("（模擬模式不需要）" if cfg.mock else ""))
    claude = shutil.which("claude")
    if claude:
        try:
            v = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            v = "已安裝"
        new_enough = bool(_version_tuple(v)) and _version_tuple(v) >= _version_tuple(MIN_CLI)
        add("Claude Code CLI", new_enough, (v + "（SDK 內建 CLI；headless 腳本 scripts/loop.sh 會用到這個）") if new_enough else
            f"{v}：scripts/loop.sh 需要 {MIN_CLI} 以上（claude update），舊版不認得 agent 檔指定的模型，花費會算錯", "選用")
    else:
        add("Claude Code CLI", False, "找不到 claude 指令。SDK 內建一份，不影響監控介面；scripts/loop.sh 需要它", "選用")
    add("ffmpeg", shutil.which("ffmpeg") and shutil.which("ffprobe"), "音檔與影片前處理需要", "音檔／影片")
    try:
        import faster_whisper  # noqa: F401
        add("語音辨識", True, f"faster-whisper（模型 {cfg.transcribe_model}）", "音檔／影片")
    except ImportError:
        ok = cfg.transcribe_engine == "command" and bool(cfg.transcribe_command)
        add("語音辨識", ok, "已設定外部指令" if ok else
            "未安裝 faster-whisper。可改上傳同檔名的 .srt／.vtt 字幕檔，或 pip install faster-whisper", "音檔／影片")
    try:
        import pptx  # noqa: F401
        add("PowerPoint 輸出", True, "python-pptx", "選用")
    except ImportError:
        add("PowerPoint 輸出", False, "未安裝 python-pptx，只會輸出 HTML 簡報", "選用")
    try:
        import cairosvg  # noqa: F401
        add("示意圖轉 PowerPoint", True, "cairosvg", "選用")
    except Exception:
        add("示意圖轉 PowerPoint", False, "未安裝 cairosvg，PowerPoint 裡不會有示意圖（HTML 版不受影響）", "選用")
    return out


def cmd_doctor(_args) -> int:
    cfg = load_config()
    print(f"LearnLoop {__version__}｜專案資料夾：{cfg.projects_dir}｜匯率：1 USD = {cfg.usd_twd_rate} TWD"
          + ("｜模擬模式" if cfg.mock else ""))
    bad = 0
    for r in doctor_report():
        mark = "✓" if r["ok"] else ("✗" if r["need"] == "必要" else "–")
        bad += (not r["ok"]) and r["need"] == "必要"
        print(f"  {mark} {r['name']:14s} {r['detail']}（{r['need']}）")
    return 1 if bad else 0


def cmd_watch(args) -> int:
    """自動觸發：監看收件匣資料夾，裡面每個子資料夾視為一個新專案。

    無人值守：遇到需要人決定的事，等 LP_INCIDENT_TIMEOUT 秒（預設 60）後採用預設選項（一律是「停止」），
    專案會停在那個階段，之後可以在監控介面續跑。處理過的資料夾會移到 <收件匣>/_done/。
    """
    from .project import kind_of
    inbox = Path(args.inbox).resolve()
    done = inbox / "_done"
    inbox.mkdir(parents=True, exist_ok=True)
    print(f"監看 {inbox}（每 {args.interval} 秒檢查一次；Ctrl+C 結束）")
    while True:
        for d in sorted(x for x in inbox.iterdir() if x.is_dir() and not x.name.startswith(("_", "."))):
            files = sorted(f for f in d.iterdir() if f.is_file() and kind_of(f.name))
            if not files:
                continue
            if time.time() - max(f.stat().st_mtime for f in files) < args.settle:
                continue  # 檔案可能還在複製中，下一輪再看
            project = create_project(d.name)
            for f in files:
                with tempfile.NamedTemporaryFile(delete=False, dir=project.dir) as tmp:
                    shutil.copyfile(f, tmp.name)
                project.add_source(f.name, Path(tmp.name))
            print(f"[{time.strftime('%H:%M:%S')}] {d.name} → 專案 {project.id}（{len(files)} 個檔案），開始執行")
            env = {**os.environ, "LP_INCIDENT_TIMEOUT": os.environ.get("LP_INCIDENT_TIMEOUT", "60")}
            rc = subprocess.run([sys.executable, "-m", "learnloop", "run", project.id], env=env).returncode
            done.mkdir(exist_ok=True)
            shutil.move(str(d), str(done / f"{d.name}-{project.id}"))
            print(f"[{time.strftime('%H:%M:%S')}] {d.name}：{'完成' if rc == 0 else '未完成，請到監控介面查看'}")
        if args.once:
            return 0
        time.sleep(args.interval)


def cmd_serve(args) -> int:
    import uvicorn
    print(f"監控介面：http://{args.host}:{args.port}")
    uvicorn.run("learnloop.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="learnloop", description="學習資料整理迴圈系統")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve", help="啟動監控介面")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("new", help="建立專案")
    p.add_argument("title")
    p.add_argument("files", nargs="*")
    p.add_argument("--reference", action="store_true", help="這批檔案是查證用的參考資料")
    p.set_defaults(fn=cmd_new)
    p = sub.add_parser("run", help="執行管線")
    p.add_argument("project")
    p.add_argument("--from", dest="from_stage", choices=STAGES, help="從指定階段重跑")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("list", help="列出專案")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("gate", help="手動跑閘門")
    p.add_argument("project")
    p.add_argument("gate")
    p.add_argument("--target")
    p.set_defaults(fn=cmd_gate)
    p = sub.add_parser("watch", help="監看收件匣資料夾，自動建立專案並執行")
    p.add_argument("inbox")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--settle", type=int, default=20, help="檔案幾秒內沒有變動才開始處理")
    p.add_argument("--once", action="store_true", help="只檢查一次就結束（給 cron 用）")
    p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("doctor", help="檢查環境")
    p.set_defaults(fn=cmd_doctor)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ProjectError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
