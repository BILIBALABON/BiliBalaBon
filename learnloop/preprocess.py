"""前處理（不用模型的確定性步驟）：把上傳檔轉成 agent 讀得懂的文字與圖片。

  書面 → raw/<sid>/text.md           （PDF / Word / PowerPoint / 純文字；掃描檔與圖片標記 needs_vision）
  音檔 → raw/<sid>/transcript.md     （逐字稿，含時間戳）
  影片 → raw/<sid>/transcript.md + raw/<sid>/frames/*.jpg（關鍵畫面）

Claude 不能直接「聽」或「看影片」，所以音檔與影片一定要先經過語音辨識與擷取畫面。
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .project import Project, atomic_write_json

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


class MissingDependency(Exception):
    """缺少外部工具（ffmpeg、語音辨識引擎…）。hint 會顯示在處置彈窗。"""

    def __init__(self, what: str, hint: str):
        super().__init__(f"缺少 {what}")
        self.what, self.hint = what, hint


def fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---- 書面 -----------------------------------------------------------------

def _read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16（Windows 記事本的「Unicode」）
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "cp950", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _pdf_to_md(path: Path) -> tuple[str, dict]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts, chars = [], 0
    for i, page in enumerate(reader.pages, 1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception:
            t = ""
        chars += len(t)
        parts.append(f"## 第 {i} 頁\n\n{t or '（此頁沒有可擷取的文字）'}")
    n = len(reader.pages)
    # 平均每頁不到 40 字 → 多半是掃描檔，交給 agent 直接用 Read 看原檔
    return "\n\n".join(parts), {"pages": n, "needs_vision": n > 0 and chars / n < 40}


def _docx_to_md(path: Path) -> tuple[str, dict]:
    import docx
    d = docx.Document(str(path))
    out = []
    for p in d.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        m = re.match(r"heading (\d)", style)
        if m:
            out.append("#" * min(int(m.group(1)) + 1, 6) + " " + t)
        elif style == "title":
            out.append("# " + t)
        elif "list" in style:
            out.append("- " + t)
        else:
            out.append(t)
    for ti, table in enumerate(d.tables, 1):
        out.append(f"\n**表格 {ti}**\n")
        for ri, row in enumerate(table.rows):
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            out.append("| " + " | ".join(cells) + " |")
            if ri == 0:
                out.append("|" + "---|" * len(cells))
    return "\n\n".join(out), {"needs_vision": False}


def _pptx_to_md(path: Path) -> tuple[str, dict]:
    from pptx import Presentation
    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        out.append(f"## 投影片 {i}：{title}".rstrip("："))
        for shape in slide.shapes:
            if shape == slide.shapes.title or not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                t = "".join(r.text for r in para.runs).strip()
                if t:
                    out.append("  " * para.level + "- " + t)
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                out.append(f"> 備忘稿：{note}")
    return "\n\n".join(out), {"slides": len(prs.slides), "needs_vision": False}


def _subtitle_to_md(text: str) -> tuple[str, list[dict]]:
    """把 .srt / .vtt 轉成帶時間戳的逐字稿。"""
    segs, cur = [], None
    ts = re.compile(r"(\d{1,2}:)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
    for line in text.splitlines():
        line = line.strip()
        m = ts.match(line)
        if m:
            h = int(m.group(1)[:-1]) if m.group(1) else 0
            cur = {"start": h * 3600 + int(m.group(2)) * 60 + int(m.group(3)), "text": ""}
            segs.append(cur)
        elif cur is not None and line:
            # 只去掉字幕標記（<i>、<c.yellow>、<00:00:01.000>），不要誤刪「a < b 且 c > d」這種內容
            clean = re.sub(r"</?[A-Za-z][^<>]*>|<\d{1,2}:[\d:.]+>", "", line)
            cur["text"] = (cur["text"] + " " + clean).strip()
        elif not line:
            cur = None
    segs = [s for s in segs if s["text"]]
    return _segments_to_md(segs), segs


def _segments_to_md(segs: list[dict]) -> str:
    """每約 60 秒併成一段，段首標 [hh:mm:ss]，方便 agent 引用時間點。"""
    lines, bucket, start = [], [], None
    for s in segs:
        if start is not None and s["start"] - start >= 60:  # 這一句已經超過一分鐘：先收掉上一段，時間標籤才對得上
            lines.append(f"[{fmt_ts(start)}] " + " ".join(bucket))
            bucket, start = [], None
        if start is None:
            start = s["start"]
        bucket.append(s["text"].strip())
    if bucket:
        lines.append(f"[{fmt_ts(start or 0)}] " + " ".join(bucket))
    return "\n\n".join(lines)


# ---- 音檔／影片 -----------------------------------------------------------

def _need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise MissingDependency(tool, f"請先安裝 {tool}（macOS：brew install ffmpeg；Windows：winget install ffmpeg；Ubuntu：apt install ffmpeg）")
    return path


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")


def media_duration(path: Path) -> float:
    """長度（秒）。只是參考資訊：沒有 ffprobe 或讀不出來就回 0，不擋流程。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    r = _run([ffprobe, "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nw=1:nk=1", str(path)], timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(path: Path) -> bool:
    r = _run([_need("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
              "-of", "csv=p=0", str(path)], timeout=120)
    return bool(r.stdout.strip())


def upload_sig(project: Project, source: dict) -> list:
    """原檔（與配對的字幕檔）的指紋：大小與修改時間。用來判斷前處理與擷取結果能不能沿用。"""
    sig = []
    for p in (project.uploads / source["filename"], _find_sidecar(project, source)):
        try:
            st = p.stat() if p else None
        except OSError:
            st = None
        sig.append([p.name, st.st_size, st.st_mtime_ns] if st else None)
    return sig + [source.get("kind"), source.get("role", "material")]


def is_fresh(project: Project, source: dict) -> bool:
    meta = load_raw_meta(project, source["id"])
    if not meta or meta.get("upload_sig") != upload_sig(project, source):
        return False
    return (project.raw / source["id"] / meta.get("text_file", "text.md")).exists()


def extract_audio(src: Path, dest: Path) -> None:
    r = _run([_need("ffmpeg"), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dest)])
    if r.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg 轉出音軌失敗：{r.stderr.strip()[:300]}")


def transcribe(audio: Path, out_dir: Path, cfg: Config) -> list[dict]:
    """回傳 [{start, text}]。找不到可用引擎時丟 MissingDependency。"""
    engine = cfg.transcribe_engine
    if engine == "faster-whisper":
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError:
            raise MissingDependency(
                "語音辨識引擎 faster-whisper",
                "安裝：pip install faster-whisper（第一次使用會下載模型）。"
                "也可以改上傳與音檔同檔名的 .srt／.vtt 字幕檔，系統會直接採用。")
        model = WhisperModel(cfg.transcribe_model, device="auto", compute_type="auto")
        segments, _info = model.transcribe(str(audio), language=cfg.transcribe_language or None, vad_filter=True)
        return [{"start": float(s.start), "text": s.text.strip()} for s in segments if s.text.strip()]
    if engine == "command":
        if not cfg.transcribe_command:
            raise MissingDependency("語音辨識指令", "請在 config.toml 的 [transcribe] command 填入指令")
        out_base = out_dir / "asr"
        cmd = [a.format(audio=str(audio), out=str(out_base)) for a in shlex.split(cfg.transcribe_command)]
        r = _run(cmd, timeout=6 * 3600)
        for ext in (".srt", ".vtt", ".txt"):
            cand = Path(str(out_base) + ext)
            if cand.exists():
                text = _read_text_file(cand)
                if ext == ".txt":
                    return [{"start": 0.0, "text": text.strip()}]
                return _subtitle_to_md(text)[1]
        if r.stdout.strip():
            return [{"start": 0.0, "text": r.stdout.strip()}]
        raise RuntimeError(f"語音辨識指令沒有產生輸出：{r.stderr.strip()[:300]}")
    raise MissingDependency("語音辨識引擎", "config.toml 的 [transcribe] engine 目前是 none。"
                            "請改成 faster-whisper 或 command，或上傳同檔名的 .srt／.vtt 字幕檔。")


def extract_frames(video: Path, frames_dir: Path, cfg: Config) -> list[dict]:
    """擷取關鍵畫面：先用場景變化偵測，沒抓到就平均取樣。回傳 [{file, t}]。"""
    ffmpeg = _need("ffmpeg")
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()
    dur = media_duration(video)
    scale = "scale='min(1280,iw)':-2"
    r = _run([ffmpeg, "-y", "-v", "info", "-i", str(video),
              "-vf", f"select='gt(scene,{cfg.video_scene_threshold})',showinfo,{scale}",
              "-vsync", "vfr", "-q:v", "4", str(frames_dir / "scene_%05d.jpg")])
    times = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    files = sorted(frames_dir.glob("scene_*.jpg"))
    picked = list(zip(files, times))
    # 開場畫面常是標題頁，場景偵測抓不到第一格，補上
    if dur > 0:
        first = frames_dir / "first.jpg"
        _run([ffmpeg, "-y", "-v", "error", "-ss", "0.5" if dur > 1 else "0", "-i", str(video),
              "-frames:v", "1", "-vf", scale, "-q:v", "4", str(first)])
        if first.exists():
            picked.insert(0, (first, 0.0))
    if len(picked) <= 1 and dur > 0:  # 畫面幾乎不變（例如只拍講者）→ 平均取樣
        n = max(1, min(cfg.video_max_frames, int(dur // 120) + 1))
        for i in range(1, n + 1):
            t = dur * i / (n + 1)
            f = frames_dir / f"uni_{i:03d}.jpg"
            _run([ffmpeg, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                  "-frames:v", "1", "-vf", scale, "-q:v", "4", str(f)])
            if f.exists():
                picked.append((f, t))
    if len(picked) > cfg.video_max_frames:  # 太多就等距抽樣
        step = len(picked) / cfg.video_max_frames
        keep = {int(i * step) for i in range(cfg.video_max_frames)}
        for i, (f, _t) in enumerate(picked):
            if i not in keep:
                f.unlink(missing_ok=True)
        picked = [p for i, p in enumerate(picked) if i in keep]
    out = []
    for f, t in sorted(picked, key=lambda x: x[1]):
        name = f"f_{int(t):06d}.jpg"
        target = frames_dir / name
        if target.exists() and target != f:
            f.unlink(missing_ok=True)
            continue
        f.rename(target)
        out.append({"file": f"frames/{name}", "t": round(t, 1), "ts": fmt_ts(t)})
    return out


def _find_sidecar(project: Project, source: dict) -> Path | None:
    stem = Path(source["filename"]).stem
    for s in project.sources:
        if s["kind"] == "sidecar" and Path(s["filename"]).stem == stem:
            return project.uploads / s["filename"]
    return None


# ---- 進入點 ---------------------------------------------------------------

def preprocess_source(project: Project, source: dict) -> dict:
    """處理單一來源，寫出 raw/<sid>/，回傳 meta。可重複執行（結果會覆蓋）。"""
    cfg = project.cfg
    sid, kind = source["id"], source["kind"]
    src = project.uploads / source["filename"]
    if not src.exists():
        raise FileNotFoundError(f"上傳檔不見了：{source['filename']}")
    out = project.raw / sid
    out.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    meta: dict = {"source_id": sid, "kind": kind, "filename": source["filename"], "role": source.get("role", "material")}

    if kind in ("document", "sidecar"):
        if ext == ".pdf":
            text, extra = _pdf_to_md(src)
        elif ext == ".docx":
            text, extra = _docx_to_md(src)
        elif ext == ".pptx":
            text, extra = _pptx_to_md(src)
        elif ext in IMAGE_EXT:
            text, extra = "（圖片檔，請直接用 Read 檢視原檔）", {"needs_vision": True}
        elif ext in (".srt", ".vtt"):
            text, segs = _subtitle_to_md(_read_text_file(src))
            extra = {"needs_vision": False, "has_timestamps": bool(segs)}
        else:
            text, extra = _read_text_file(src), {"needs_vision": False}
        meta.update(extra)
        meta["text_file"] = "text.md"
        meta["chars"] = len(text)
        (out / "text.md").write_text(text, encoding="utf-8")

    elif kind in ("audio", "video"):
        meta["duration_s"] = round(media_duration(src), 1)
        sidecar = _find_sidecar(project, source)
        if sidecar is not None:
            md, segs = _subtitle_to_md(_read_text_file(sidecar))
            meta["transcript_from"] = sidecar.name
        elif kind == "video" and not has_audio(src):
            md, segs = "（這支影片沒有音軌，請以畫面內容為主）", []
            meta["transcript_from"] = "none"
            meta["needs_vision"] = True  # 沒有逐字稿可讀，內容全靠關鍵畫面
        else:
            wav = out / "audio.wav"
            extract_audio(src, wav)
            try:
                segs = transcribe(wav, out, cfg)
            finally:
                wav.unlink(missing_ok=True)  # 轉檔用的暫存音軌很大，不留
            md = _segments_to_md(segs)
            meta["transcript_from"] = cfg.transcribe_engine
        if not md.strip():
            md = "（語音辨識沒有辨識出任何內容）"
        (out / "transcript.md").write_text(md, encoding="utf-8")
        atomic_write_json(out / "segments.json", segs)
        meta.update({"text_file": "transcript.md", "chars": len(md), "has_timestamps": bool(segs)})
        if kind == "video":
            frames = extract_frames(src, out / "frames", cfg)
            meta["frames"] = frames
    else:
        raise ValueError(f"未知的來源類型：{kind}")

    meta["upload_sig"] = upload_sig(project, source)
    atomic_write_json(out / "meta.json", meta)
    return meta


def load_raw_meta(project: Project, sid: str) -> dict:
    try:
        return json.loads((project.raw / sid / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
