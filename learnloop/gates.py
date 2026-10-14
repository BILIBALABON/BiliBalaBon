"""驗證閘門：每個階段「算不算做完」的確定性檢查（等同程式專案裡的 npm test）。

同一份檢查在三個地方被呼叫，結果一定一致：
  1. agent 自己完成前先跑（Bash 僅開放這一道指令）
  2. Stop hook（.claude/hooks/stop-gate.sh）：沒過就不准結束
  3. orchestrator 外層迴圈：以它的結果為準，決定通過、重試或開處置單

用法：python3 -m learnloop.gates <專案資料夾> <階段> [--target s01] [--json]
結束碼：0 通過；1 內容未通過（錯誤訊息印到 stderr）；2 用法錯誤；3 檢查無法進行（沒有網路、閘門程式出錯）。
只有結束碼 1 代表「內容需要修改」；Stop hook 只在 1 的時候攔截。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator

from . import schemas
from .config import Config, load_config
from .linkcheck import check_urls, is_http_url
from .svgsafe import check_svg

GATE_NAMES = ["preprocess", "ingest", "plan", "organize", "factcheck", "enrich", "verify",
              "integrate", "compose", "review", "publish"]

INGEST_HEADINGS = ["## 摘要", "## 重點整理", "## 詳細內容", "## 待查證事項"]
# 英文縮寫只認大寫整詞（否則 Mastodon、todos 都會被誤判）
_PLACEHOLDER = re.compile(r"(?<![A-Za-z])(?:TODO|TBD)(?![A-Za-z])|待補|（略）|\(略\)|[Ll]orem ipsum|請在此填入")
_TIMESTAMP = re.compile(r"\[(\d{1,2}:)?\d{1,2}:\d{2}\]")
_CITE = re.compile(r"\[s\d+(@[\d:]+)?\]")


@dataclass
class GateResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    system: str | None = None  # 非內容問題：offline（連不到網路，無法檢查連結）｜exception（閘門程式出錯）

    def err(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def merge(self, other: "GateResult") -> "GateResult":
        self.ok = self.ok and other.ok
        self.errors += other.errors
        self.warnings += other.warnings
        self.system = self.system or other.system
        return self

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "system": self.system}

    def links(self, label: str, urls: list[str], pdir: Path, cfg: Config) -> None:
        """連結檢查。本機沒有網路時不把連結判成失效（agent 改也沒用），改標成系統問題交給人處理。"""
        errors, warnings, offline = check_urls(urls, pdir, cfg.linkcheck_mode, cfg.linkcheck_timeout_s)
        if offline:
            msg = ("無法檢查連結：這台電腦目前連不到外部網站（多個常見網站都連不上）。"
                   "這是網路或代理設定的問題，不是內容問題，不需要修改內容。")
            if cfg.linkcheck_mode == "strict":
                self.err(msg)
                self.system = self.system or "offline"
            else:
                self.warn(msg)
            return
        for m in errors:
            self.err(f"{label}：{m}")
        self.warnings += warnings


# ---- 共用工具 -------------------------------------------------------------

def _load_json(path: Path, label: str, res: GateResult):
    if not path.exists():
        res.err(f"{label} 不存在，請產出這個檔案（路徑：{path.name if path.parent.name == 'work' else path}）")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        res.err(f"{label} 不是合法的 JSON：第 {e.lineno} 行第 {e.colno} 欄 {e.msg}")
        return None


def _zh(e) -> str:
    v, val = e.validator, e.validator_value
    if v == "required":
        m = re.search(r"'([^']+)' is a required", e.message)
        return f"缺少必要欄位「{m.group(1) if m else '?'}」"
    if v == "type":
        return f"型別應為 {val}"
    if v == "enum":
        return f"只能是 {val} 其中之一（目前是 {e.instance!r}）"
    if v == "minItems":
        return f"至少要有 {val} 項（目前 {len(e.instance)} 項）"
    if v == "maxItems":
        return f"最多 {val} 項（目前 {len(e.instance)} 項）"
    if v == "minLength":
        return "不可為空字串"
    if v == "pattern":
        return f"格式不符，需符合 {val}（目前是 {e.instance!r}）"
    if v == "additionalProperties":
        m = re.findall(r"'([^']+)'", e.message)
        return f"出現未定義的欄位：{', '.join(m) if m else e.message}"
    return e.message


def _validate(data, schema: dict, label: str, res: GateResult, limit: int = 12) -> bool:
    errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
    for e in errs[:limit]:
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path).lstrip(".")
        res.err(f"{label} › {path or '（最外層）'}：{_zh(e)}")
    if len(errs) > limit:
        res.err(f"{label} 另有 {len(errs) - limit} 個格式錯誤未列出")
    return not errs


def _project_meta(pdir: Path) -> dict:
    try:
        return json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _content_sources(pdir: Path) -> list[dict]:
    srcs = _project_meta(pdir).get("sources", [])
    media = {Path(s["filename"]).stem for s in srcs if s["kind"] in ("audio", "video")}
    return [s for s in srcs if not (s["kind"] == "sidecar" and Path(s["filename"]).stem in media)]


def _active_sources(pdir: Path) -> list[dict]:
    """扣掉使用者在處置單選擇「略過」的來源。"""
    try:
        skipped = set(json.loads((pdir / ".state" / "skipped_sources.json").read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        skipped = set()
    return [s for s in _content_sources(pdir) if s["id"] not in skipped]


def _reference_ids(pdir: Path) -> set[str]:
    return {s["id"] for s in _content_sources(pdir) if s.get("role") == "reference"}


def _outline(pdir: Path) -> dict | None:
    try:
        return json.loads((pdir / "work" / "outline.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _section_ids(pdir: Path) -> list[str]:
    o = _outline(pdir) or {}
    return [s.get("id") for s in o.get("sections", []) if isinstance(s, dict) and s.get("id")]


def _dupes(ids: list[str]) -> list[str]:
    seen, d = set(), []
    for i in ids:
        if i in seen and i not in d:
            d.append(i)
        seen.add(i)
    return d


def _sections_of(md: str) -> dict[str, str]:
    """把筆記依「## [secN] 標題」切段，回傳 {sec_id: 內文}。"""
    out, cur, buf = {}, None, []
    for line in md.splitlines():
        m = re.match(r"^##\s+\[(sec\d+)\]", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf)
            cur, buf = m.group(1), []
        elif re.match(r"^##\s+", line) and cur:
            out[cur] = "\n".join(buf)
            cur, buf = None, []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf)
    return out


def known_urls(pdir: Path) -> set[str]:
    """查證與補充階段已經出現（且通過連結檢查）的網址。"""
    urls: set[str] = set()
    for name in ("factcheck.json", "enrich.json"):
        try:
            data = json.loads((pdir / "work" / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        for item in data.get("claims", []) + data.get("supplements", []):
            for s in item.get("sources", []) or []:
                if is_http_url(s.get("url")):
                    urls.add(s["url"].strip())
        for m in data.get("media", []) or []:
            if is_http_url(m.get("url")):
                urls.add(m["url"].strip())
    return urls


# ---- 各階段閘門 -----------------------------------------------------------

def gate_preprocess(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    srcs = [s for s in _active_sources(pdir) if target in (None, s["id"])]
    if not srcs:
        res.err("專案裡沒有可處理的來源檔，請先上傳資料")
    for s in srcs:
        d = pdir / "raw" / s["id"]
        meta_p = d / "meta.json"
        if not meta_p.exists():
            res.err(f"{s['id']}（{s['filename']}）尚未完成前處理")
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        tf = d / meta.get("text_file", "text.md")
        if not tf.exists():
            res.err(f"{s['id']}：缺少 {tf.name}")
        elif not meta.get("needs_vision") and len(tf.read_text(encoding="utf-8").strip()) < 20:
            res.err(f"{s['id']}（{s['filename']}）幾乎沒有擷取到文字，請確認檔案內容")
        elif s["kind"] == "video" and meta.get("needs_vision") and not meta.get("frames"):
            res.err(f"{s['id']}（{s['filename']}）沒有音軌，也擷取不到任何畫面，請確認影片檔")
    return res


def gate_ingest(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    srcs = [s for s in _active_sources(pdir) if target in (None, s["id"])]
    if target and not srcs:
        res.err(f"找不到來源 {target}")
    for s in srcs:
        sid = s["id"]
        p = pdir / "work" / "sources" / f"{sid}.md"
        label = f"work/sources/{sid}.md"
        if not p.exists():
            res.err(f"{label} 不存在，請依規定格式寫出這份來源整理")
            continue
        text = p.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            res.err(f"{label}：檔案開頭要有 --- 包起來的 frontmatter（source_id、kind、title）")
            body, fm = text, {}
        else:
            body = text[m.end():]
            fm = dict(re.findall(r"^(\w+):\s*(.+?)\s*$", m.group(1), re.M))
            for k in ("source_id", "kind", "title"):
                if not fm.get(k):
                    res.err(f"{label}：frontmatter 缺少 {k}")
            if fm.get("source_id") and fm["source_id"].strip("\"'") != sid:
                res.err(f"{label}：frontmatter 的 source_id 應為 {sid}")
        for h in INGEST_HEADINGS:
            if not re.search(rf"^{re.escape(h)}\s*$", body, re.M):
                res.err(f"{label}：缺少段落標題「{h}」")
        raw_meta = {}
        try:
            raw_meta = json.loads((pdir / "raw" / sid / "meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        need = min(300, max(80, int(raw_meta.get("chars", 0) * 0.2)))
        if len(body.strip()) < need:
            res.err(f"{label}：內容太少（{len(body.strip())} 字，至少 {need} 字），請完整整理來源內容")
        if raw_meta.get("has_timestamps") and not _TIMESTAMP.search(body):
            res.err(f"{label}：來源有時間軸，重點需標註時間點，例如 [00:12:30]")
        ph = _PLACEHOLDER.search(body)
        if ph:
            res.err(f"{label}：還留著未完成的佔位文字「{ph.group(0)}」")
    return res


# 每份簡報固定要有的頁面：封面、學習目標、重點回顧、參考資料，以及（找到影片時）影片頁
FIXED_SLIDES = 5


def deck_limits(settings: dict, cfg: Config) -> tuple[int, int, int, int]:
    """回傳（最少張數, 最多張數, 目標張數, 大綱章節上限）。

    章節上限讓大綱與張數相容：每章至少一張內容與一題測驗，再加上固定頁面。
    沒有這道限制時，實測期望 6 張的專案被排了 4 章，簡報至少要 12 張，編排者怎麼改都過不了張數檢查。
    """
    target = int(settings.get("slide_target") or 12)
    lo, hi = cfg.min_slides, max(int(target * 1.5), cfg.min_slides + 2)
    return lo, hi, target, max(2, min(8, (hi - FIXED_SLIDES) // 2))


def gate_plan(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    data = _load_json(pdir / "work" / "outline.json", "work/outline.json", res)
    if data is None or not _validate(data, schemas.OUTLINE, "outline.json", res):
        return res
    ids = [s["id"] for s in data["sections"]]
    for d in _dupes(ids):
        res.err(f"outline.json：章節代號 {d} 重複")
    active = _active_sources(pdir)
    valid = {s["id"] for s in active}
    material = {s["id"] for s in active if s.get("role", "material") == "material"}
    used: set[str] = set()
    for s in data["sections"]:
        for r in s["source_refs"]:
            if r not in valid:
                res.err(f"outline.json › {s['id']}.source_refs：{r} 不是這個專案的來源代號（可用：{', '.join(sorted(valid))}）")
            used.add(r)
    for sid in sorted(material - used):
        res.err(f"outline.json：教材 {sid} 沒有被任何章節引用，請把它的內容排進大綱")
    _, hi, target_n, max_sec = deck_limits(_project_meta(pdir).get("settings", {}), cfg)
    if len(ids) > max_sec:
        res.err(f"outline.json：有 {len(ids)} 章，但期望約 {target_n} 張投影片（最多 {hi} 張）只容得下 {max_sec} 章"
                f"（每章至少一張內容與一題測驗，另有封面、學習目標、重點回顧、參考資料等固定頁）。"
                "請合併相關章節，次要內容留給筆記")
    return res


def gate_organize(pdir: Path, cfg: Config, target: str | None = None, filename: str = "notes.md") -> GateResult:
    res = GateResult()
    p = pdir / "work" / filename
    label = f"work/{filename}"
    if not p.exists():
        res.err(f"{label} 不存在")
        return res
    md = p.read_text(encoding="utf-8")
    secs = _sections_of(md)
    want = _section_ids(pdir)
    if not want:
        res.err("work/outline.json 不存在或沒有章節，無法檢查筆記")
    for sid in want:
        if sid not in secs:
            res.err(f"{label}：缺少章節「## [{sid}] …」（每個大綱章節都要有對應的二級標題，格式為 ## [{sid}] 標題）")
            continue
        body = secs[sid].strip()
        if len(body) < 200:
            res.err(f"{label} › {sid}：內容只有 {len(body)} 字，至少 200 字")
        if not _CITE.search(body):
            res.err(f"{label} › {sid}：沒有標示來源，請在重點後標上來源代號，如 [s01] 或 [s02@12:30]")
    valid = {s["id"] for s in _content_sources(pdir)}
    for c in sorted({m.group(0)[1:-1].split("@")[0] for m in _CITE.finditer(md)} - valid):
        res.err(f"{label}：引用了不存在的來源代號 [{c}]")
    ph = _PLACEHOLDER.search(md)
    if ph:
        res.err(f"{label}：還留著未完成的佔位文字「{ph.group(0)}」")
    return res


def gate_factcheck(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    data = _load_json(pdir / "work" / "factcheck.json", "work/factcheck.json", res)
    if data is None or not _validate(data, schemas.FACTCHECK, "factcheck.json", res):
        return res
    claims = data["claims"]
    for d in _dupes([c["id"] for c in claims]):
        res.err(f"factcheck.json：主張代號 {d} 重複")
    secs = _section_ids(pdir)
    need = max(cfg.min_claims, len(secs))
    if len(claims) < need:
        res.err(f"factcheck.json：只查證了 {len(claims)} 個主張，至少要 {need} 個（每個章節至少 1 個）")
    covered = {c["section_id"] for c in claims}
    for s in secs:
        if s not in covered:
            res.err(f"factcheck.json：章節 {s} 沒有任何被查證的主張")
    refs = _reference_ids(pdir)  # 只有標為「參考資料」的上傳檔能當證據；教材不能拿來證明自己
    urls = []
    for c in claims:
        if c["section_id"] not in secs:
            res.err(f"factcheck.json › {c['id']}.section_id：{c['section_id']} 不在大綱裡")
        good = [s for s in c["sources"] if is_http_url(s.get("url")) or s.get("ref") in refs]
        for s in c["sources"]:
            if s.get("url") and not is_http_url(s["url"]):
                res.err(f"factcheck.json › {c['id']}：來源網址格式不正確：{s['url']!r}")
            if s.get("ref") and s["ref"] not in refs:
                res.err(f"factcheck.json › {c['id']}：ref「{s['ref']}」不是上傳的參考資料"
                        f"（可用：{', '.join(sorted(refs)) or '無；請改用網路來源'}）。教材本身不能當作查證依據")
        if c["verdict"] != "unverifiable" and not good:
            res.err(f"factcheck.json › {c['id']}：判定為 {c['verdict']} 卻沒有可查的來源（需要 url，或 ref 指向上傳的參考資料）")
        if c["verdict"] in ("refuted", "partially_supported") and not (c.get("correction") or "").strip():
            res.err(f"factcheck.json › {c['id']}：判定為 {c['verdict']} 時必須填 correction（正確的說法）")
        urls += [s["url"] for s in c["sources"] if is_http_url(s.get("url"))]
    res.links("factcheck.json", urls, pdir, cfg)
    return res


def gate_enrich(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    data = _load_json(pdir / "work" / "enrich.json", "work/enrich.json", res)
    if data is None or not _validate(data, schemas.ENRICH, "enrich.json", res):
        return res
    sups, media = data["supplements"], data["media"]
    for d in _dupes([s["id"] for s in sups]) + _dupes([m["id"] for m in media]):
        res.err(f"enrich.json：代號 {d} 重複")
    secs = _section_ids(pdir)
    ref_ids = _reference_ids(pdir)
    need = max(2, len(secs))
    if len(sups) < need:
        res.err(f"enrich.json：補充資料只有 {len(sups)} 則，至少要 {need} 則")
    urls = []
    for s in sups:
        if s["section_id"] not in secs:
            res.err(f"enrich.json › {s['id']}.section_id：{s['section_id']} 不在大綱裡")
        if not any(is_http_url(x.get("url")) or x.get("ref") in ref_ids for x in s["sources"]):
            res.err(f"enrich.json › {s['id']}：補充資料必須附上出處（http(s) 網址，或 ref 指向上傳的參考資料）")
        urls += [x["url"] for x in s["sources"] if is_http_url(x.get("url"))]
    if not any(m["kind"] == "video" for m in media):
        res.err("enrich.json：media 至少要有 1 支說明影片（kind=video），請實際搜尋並確認影片存在")
    for m in media:
        if m["section_id"] not in secs:
            res.err(f"enrich.json › {m['id']}.section_id：{m['section_id']} 不在大綱裡")
        if not is_http_url(m["url"]):
            res.err(f"enrich.json › {m['id']}：url 格式不正確：{m['url']!r}")
        else:
            urls.append(m["url"])
    res.links("enrich.json", urls, pdir, cfg)
    return res


def gate_verify(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    if target == "factcheck":
        return gate_factcheck(pdir, cfg)
    if target == "enrich":
        return gate_enrich(pdir, cfg)
    return gate_factcheck(pdir, cfg).merge(gate_enrich(pdir, cfg))


def gate_integrate(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = gate_organize(pdir, cfg, filename="notes.final.md")
    final = pdir / "work" / "notes.final.md"
    if final.exists() and not re.search(r"^##\s+參考資料\s*$", final.read_text(encoding="utf-8"), re.M):
        res.err("work/notes.final.md：結尾要有「## 參考資料」段落，列出查證與補充用到的出處")
    data = _load_json(pdir / "work" / "integration.json", "work/integration.json", res)
    if data is None or not _validate(data, schemas.INTEGRATION, "integration.json", res):
        return res
    fc = json.loads((pdir / "work" / "factcheck.json").read_text(encoding="utf-8")) if (pdir / "work" / "factcheck.json").exists() else {"claims": []}
    en = json.loads((pdir / "work" / "enrich.json").read_text(encoding="utf-8")) if (pdir / "work" / "enrich.json").exists() else {"supplements": []}
    done = {c["id"]: c["action"] for c in data["claims"]}
    for c in fc.get("claims", []):
        if c.get("verdict") in ("refuted", "partially_supported", "unverifiable"):
            if c["id"] not in done:
                res.err(f"integration.json：查證結果 {c['id']}（{c['verdict']}）沒有交代處理方式（corrected／removed／flagged）")
            elif c["verdict"] == "refuted" and done[c["id"]] == "kept":
                res.err(f"integration.json › {c['id']}：已被查證為錯誤的內容不能原樣保留（kept），請更正或移除")
    sdone = {s["id"] for s in data["supplements"]}
    for s in en.get("supplements", []):
        if s["id"] not in sdone:
            res.err(f"integration.json：補充資料 {s['id']} 沒有交代是否採用（inserted／skipped）")
    return res


def gate_compose(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    data = _load_json(pdir / "work" / "deck.json", "work/deck.json", res)
    if data is None or not _validate(data, schemas.DECK, "deck.json", res):
        return res
    slides = data["slides"]
    for d in _dupes([s["id"] for s in slides]):
        res.err(f"deck.json：投影片代號 {d} 重複")
    settings = _project_meta(pdir).get("settings", {})
    lo, hi, target_n, _ = deck_limits(settings, cfg)
    if not lo <= len(slides) <= hi:
        res.err(f"deck.json：共 {len(slides)} 張投影片，需介於 {lo}–{hi} 張（目標 {target_n} 張）")
    layouts = [s["layout"] for s in slides]
    if layouts and layouts[0] != "title":
        res.err("deck.json：第一張必須是 layout=title 的封面")
    for need, name in (("agenda", "學習目標／大綱"), ("summary", "重點回顧"), ("references", "參考資料"), ("quiz", "自我檢測題")):
        if need not in layouts:
            res.err(f"deck.json：缺少 layout={need} 的投影片（{name}）")
    secs = _section_ids(pdir)
    covered = {s.get("section_id") for s in slides}
    for sid in secs:
        if sid not in covered:
            res.err(f"deck.json：大綱章節 {sid} 沒有對應的投影片（請在投影片填 section_id）")
    known = known_urls(pdir)
    to_check: list[str] = []
    n_diagram = 0
    for s in slides:
        tag = f"deck.json › {s['id']}"
        lay = s["layout"]
        if len(s["title"]) > 40:
            res.err(f"{tag}.title：標題 {len(s['title'])} 字太長，請控制在 40 字內")
        if s.get("section_id") and s["section_id"] not in secs:
            res.err(f"{tag}.section_id：{s['section_id']} 不在大綱裡")
        groups = [s.get("bullets") or []] + [c["bullets"] for c in s.get("columns") or []]
        for g in groups:
            if len(g) > cfg.max_bullets:
                res.err(f"{tag}：一欄有 {len(g)} 個條列，上限 {cfg.max_bullets} 個，請拆成兩張或精簡")
            for b in g:
                if len(b) > cfg.max_bullet_chars:
                    res.err(f"{tag}：條列「{b[:18]}…」有 {len(b)} 字，上限 {cfg.max_bullet_chars} 字")
        if lay in ("content", "agenda", "summary") and not s.get("bullets"):
            res.err(f"{tag}：layout={lay} 需要 bullets")
        if lay in ("content", "diagram", "media", "comparison") and len((s.get("notes") or "").strip()) < 20:
            res.err(f"{tag}.notes：需要至少 20 字的講解備註（補充條列沒寫到的脈絡）")
        if lay == "comparison" and not s.get("columns"):
            res.err(f"{tag}：layout=comparison 需要 columns（2–3 欄）")
        if lay == "quiz":
            opts = s.get("options") or []
            if not (s.get("question") or "").strip() or len(opts) < 2:
                res.err(f"{tag}：測驗題需要 question 與至少 2 個 options")
            elif not isinstance(s.get("answer_index"), int) or s["answer_index"] >= len(opts):
                res.err(f"{tag}：answer_index 必須是 0–{len(opts) - 1} 的整數")
            if not (s.get("explanation") or "").strip():
                res.err(f"{tag}：測驗題需要 explanation（為什麼是這個答案）")
        if lay == "references":
            refs = s.get("references") or []
            if not refs:
                res.err(f"{tag}：參考資料頁需要 references")
            for r in refs:
                if r.get("url"):
                    if not is_http_url(r["url"]):
                        res.err(f"{tag}：參考資料網址格式不正確：{r['url']!r}")
                    elif r["url"].strip() not in known:
                        to_check.append(r["url"])
        v = s.get("visual")
        if lay in ("diagram", "media") and not v:
            res.err(f"{tag}：layout={lay} 需要 visual")
        if v:
            t = v["type"]
            if t == "svg":
                n_diagram += 1
                problem = check_svg(v.get("svg") or "")
                if problem:
                    res.err(f"{tag}.visual：{problem}")
            elif t == "image":
                rel = v.get("path") or ""
                try:
                    p = (pdir / rel).resolve()
                    found = bool(rel) and pdir.resolve() in p.parents and p.is_file()
                except (ValueError, OSError):  # 路徑含有不合法字元
                    found = False
                if not found:
                    res.err(f"{tag}.visual.path：找不到圖片 {rel!r}（需為專案內的相對路徑，例如 raw/s02/frames/f_000123.jpg）")
                elif p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                    res.err(f"{tag}.visual.path：{rel} 不是圖片檔")
                elif p.stat().st_size > 4_000_000:
                    res.err(f"{tag}.visual.path：{rel} 超過 4 MB，無法放進簡報，請改用其他畫面")
            else:  # video / link
                url = v.get("url") or ""
                if not is_http_url(url):
                    res.err(f"{tag}.visual.url：需要完整的 http(s) 網址")
                elif url.strip() not in known:
                    to_check.append(url)
                if not (v.get("title") or "").strip():
                    res.err(f"{tag}.visual.title：請寫出影片／連結的標題")
    if n_diagram == 0:
        res.err("deck.json：至少要有 1 張示意圖（visual.type=svg），用圖解說明最重要的流程或關係")
    try:
        media = json.loads((pdir / "work" / "enrich.json").read_text(encoding="utf-8")).get("media", [])
    except (FileNotFoundError, json.JSONDecodeError):
        media = []
    if any(m.get("kind") == "video" for m in media) and not any(
            (s.get("visual") or {}).get("type") == "video" for s in slides):
        res.err("deck.json：補充階段找到了說明影片，請至少放 1 張 layout=media、visual.type=video 的投影片")
    res.links("deck.json", to_check, pdir, cfg)
    return res


def review_passes(review: dict, cfg: Config) -> tuple[bool, list[str]]:
    reasons = []
    scores = review.get("scores", {})
    names = {"accuracy": "正確性", "completeness": "完整性", "readability": "可讀性", "pedagogy": "學習效果"}
    for k, zh in names.items():
        v = scores.get(k)
        if not isinstance(v, int) or not 1 <= v <= 5:
            reasons.append(f"review.json › scores.{k}：需為 1–5 的整數")
        elif v < cfg.review_min_score:
            reasons.append(f"{zh}評分 {v} 分，未達 {cfg.review_min_score} 分")
    heavy = [i for i in review.get("issues", []) if i.get("severity") in ("blocker", "major")]
    if heavy:
        reasons.append(f"有 {len(heavy)} 個必須修正的問題（blocker／major）")
    if review.get("verdict") != "pass":
        reasons.append("審核結論為「需修改」")
    return (not reasons), reasons


def gate_review(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    data = _load_json(pdir / "work" / "review.json", "work/review.json", res)
    if data is None or not _validate(data, schemas.REVIEW, "review.json", res):
        return res
    ok, reasons = review_passes(data, cfg)
    for r in reasons:
        res.err(r)
    for i in data.get("issues", []):
        if i.get("severity") in ("blocker", "major"):
            res.err(f"［{i['severity']}］{i.get('ref', '')}：{i.get('problem', '')} → {i.get('fix', '')}")
    return res


def gate_publish(pdir: Path, cfg: Config, target: str | None = None) -> GateResult:
    res = GateResult()
    html = pdir / "output" / "deck.html"
    if not html.exists() or html.stat().st_size < 2000:
        res.err("output/deck.html 沒有成功產生")
    elif "<section" not in html.read_text(encoding="utf-8"):
        res.err("output/deck.html 裡沒有任何投影片")
    if not (pdir / "output" / "notes.md").exists():
        res.err("output/notes.md 沒有成功產生")
    return res


GATES = {
    "preprocess": gate_preprocess, "ingest": gate_ingest, "plan": gate_plan, "organize": gate_organize,
    "factcheck": gate_factcheck, "enrich": gate_enrich, "verify": gate_verify, "integrate": gate_integrate,
    "compose": gate_compose, "review": gate_review, "publish": gate_publish,
}


def check(project_dir: str | Path, stage: str, target: str | None = None, cfg: Config | None = None) -> GateResult:
    if stage not in GATES:
        raise ValueError(f"沒有這個閘門：{stage}（可用：{', '.join(GATE_NAMES)}）")
    pdir = Path(project_dir).resolve()
    try:
        return GATES[stage](pdir, cfg or load_config(), target)
    except Exception as e:  # 閘門自己出錯時要讓人看得出來，而不是默默放行
        res = GateResult(system="exception")
        res.err(f"閘門執行時發生例外（{type(e).__name__}: {e}）。這是系統問題，不是內容問題。")
        return res


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    if as_json:
        args.remove("--json")
    target = None
    if "--target" in args:
        i = args.index("--target")
        if i + 1 >= len(args):
            print("用法：python3 -m learnloop.gates <專案資料夾> <階段> [--target 代號] [--json]", file=sys.stderr)
            return 2
        target = args[i + 1]
        del args[i:i + 2]
    if len(args) != 2 or args[1] not in GATES:
        print("用法：python3 -m learnloop.gates <專案資料夾> <階段> [--target 代號] [--json]\n"
              f"階段：{', '.join(GATE_NAMES)}", file=sys.stderr)
        return 2
    res = check(args[0], args[1], target)
    if as_json:
        print(json.dumps(res.as_dict(), ensure_ascii=False, indent=2))
    else:
        if res.ok:
            print(f"閘門通過：{args[1]}" + (f"（{target}）" if target else ""))
        else:
            print(f"閘門未通過：{args[1]}" + (f"（{target}）" if target else "") + f"，共 {len(res.errors)} 個問題：", file=sys.stderr)
            for e in res.errors:
                print(f"  ✗ {e}", file=sys.stderr)
        for w in res.warnings:
            print(f"  ! {w}", file=sys.stderr)
    return 0 if res.ok else (3 if res.system else 1)


if __name__ == "__main__":
    sys.exit(main())
