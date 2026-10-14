"""模擬 runner：不呼叫模型，直接寫出符合資料契約的假產出。

用途：在沒有 Claude 帳號或不想花錢的情況下，驗證外層迴圈、閘門、處置單與監控介面。
產出的內容都標有「模擬」字樣，連結是假的（模擬模式會關閉連結檢查）。

環境變數：
  LP_MOCK=1                 啟用
  LP_MOCK_DELAY=0.6         每個 session 模擬的執行秒數
  LP_MOCK_COST=0.02         每個 session 模擬的花費（美元）
  LP_MOCK_FAIL=compose:1,review:1,ingest-s02:9
      讓指定對象的前 N 次嘗試產出不合格的結果，用來測試重試與處置單。
      對象可寫階段名（ingest、plan、organize、factcheck、enrich、integrate、compose、review）或 ingest-<sid>。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import Config
from .runner import Emit, RunResult

SVG = (
    '<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">'
    '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto">'
    '<path d="M0,0 L10,5 L0,10 z" fill="#1B3A5C"/></marker></defs>'
    '<rect x="0" y="0" width="960" height="540" fill="#FFFFFF"/>'
    '{boxes}</svg>'
)


def _flow_svg(labels: list[str]) -> str:
    n = len(labels)
    w, gap = 200, (960 - 60 - 200 * n) / max(n - 1, 1)
    parts = []
    for i, lab in enumerate(labels):
        x = 30 + i * (w + gap)
        fill = "#1F8A70" if i == n - 1 else "#EEF3F8"
        color = "#FFFFFF" if i == n - 1 else "#1B3A5C"
        parts.append(f'<rect x="{x:.0f}" y="220" width="{w}" height="100" rx="12" fill="{fill}" stroke="#1B3A5C" stroke-width="2"/>')
        parts.append(f'<text x="{x + w / 2:.0f}" y="278" text-anchor="middle" font-family="sans-serif" font-size="26" fill="{color}">{lab}</text>')
        if i < n - 1:
            parts.append(f'<line x1="{x + w + 6:.0f}" y1="270" x2="{x + w + gap - 8:.0f}" y2="270" stroke="#1B3A5C" stroke-width="3" marker-end="url(#a)"/>')
    return SVG.format(boxes="".join(parts))


class MockRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.delay = float(os.environ.get("LP_MOCK_DELAY", "0.6"))
        self.cost = float(os.environ.get("LP_MOCK_COST", "0.02"))
        self.fail: dict[str, int] = {}
        for item in filter(None, os.environ.get("LP_MOCK_FAIL", "").split(",")):
            k, _, n = item.partition(":")
            self.fail[k.strip()] = int(n or 1)
        self.calls: dict[str, int] = {}
        self.choices: list = []  # 每次呼叫拿到的模型選型（測試用）

    def _should_fail(self, *keys: str) -> bool:
        """同一個對象的第 N 次呼叫，N ≤ 設定值時回傳 True。"""
        primary = keys[0]
        self.calls[primary] = self.calls.get(primary, 0) + 1
        return any(self.calls[primary] <= self.fail.get(k, 0) for k in keys)

    async def run(self, *, role: str, prompt: str, project_dir: Path, gate: str | None,
                  target: str | None = None, resume: str | None = None, budget_usd: float,
                  emit: Emit, output_schema: dict | None = None, label: str = "", round_no: int = 1,
                  choice=None) -> RunResult:
        self.choices.append((role, target, choice))
        model = f"（實際執行會用 {choice.model}／effort {choice.effort}）" if choice else ""
        emit("agent_activity", role=role, label=label, kind="text", text=f"（模擬）{role} 開始工作{model}")
        await asyncio.sleep(self.delay)
        pdir = Path(project_dir)
        work = pdir / "work"
        work.mkdir(exist_ok=True)
        meta = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
        res = RunResult(ok=True, session_id=resume or f"mock-{role}-{os.urandom(3).hex()}",
                        cost_usd=self.cost, subtype="success", num_turns=3, text="（模擬）完成")

        if role.startswith("ingest-"):
            bad = self._should_fail(f"ingest-{target}", "ingest")
            self._ingest(pdir, meta, target or "", bad)
        elif role == "planner":
            self._plan(pdir, meta, self._should_fail("plan"))
        elif role == "organizer":
            self._notes(pdir, "notes.md", self._should_fail("organize"))
        elif role == "fact-checker":
            self._factcheck(pdir, self._should_fail("factcheck"))
        elif role == "enricher":
            self._enrich(pdir, self._should_fail("enrich"))
        elif role == "integrator":
            self._integrate(pdir, self._should_fail("integrate"))
        elif role == "slide-composer":
            self._deck(pdir, meta, self._should_fail("compose"))
        elif role == "reviewer":
            res.structured = self._review(self._should_fail("review"))
        emit("agent_activity", role=role, label=label, kind="tool", text=f"（模擬）Write work/… 完成")
        return res

    # ---- 各角色的假產出 ---------------------------------------------------
    def _sources(self, pdir: Path, meta: dict) -> list[dict]:
        from .gates import _active_sources
        return _active_sources(pdir)

    def _ingest(self, pdir: Path, meta: dict, sid: str, bad: bool) -> None:
        src = next(s for s in meta["sources"] if s["id"] == sid)
        raw_dir = pdir / "raw" / sid
        raw_meta = json.loads((raw_dir / "meta.json").read_text(encoding="utf-8"))
        raw = (raw_dir / raw_meta.get("text_file", "text.md")).read_text(encoding="utf-8")
        excerpt = raw.strip()[:600] or "（原檔沒有文字）"
        ts = "[00:00:00] " if raw_meta.get("has_timestamps") else ""
        body = (
            f"---\nsource_id: {sid}\nkind: {src['kind'] if src['kind'] != 'sidecar' else 'document'}\n"
            f"title: （模擬）{Path(src['filename']).stem}\n---\n\n"
            f"## 摘要\n（模擬產出）這份來源「{src['filename']}」共約 {len(raw)} 字。以下內容由模擬 runner 產生，"
            "僅用來驗證流程，並非模型實際整理的結果。\n\n"
            f"## 重點整理\n- {ts}來源的第一個重點（模擬）。\n- {ts}來源的第二個重點（模擬）。\n- {ts}來源的第三個重點（模擬）。\n\n"
            f"## 詳細內容\n{ts}原始內容節錄如下，實際執行時這裡會是完整的結構化整理：\n\n{excerpt}\n\n"
            + "模擬內容補足長度，確保格式檢查能反映真實情況。" * 6 + "\n\n"
            "## 待查證事項\n- 來源提到的一個數字（模擬）。\n"
        )
        if bad:
            body = body.replace("## 待查證事項", "## 其他")  # 少一個必要標題 → 閘門會擋
        (pdir / "work" / "sources").mkdir(parents=True, exist_ok=True)
        (pdir / "work" / "sources" / f"{sid}.md").write_text(body, encoding="utf-8")

    def _plan(self, pdir: Path, meta: dict, bad: bool) -> None:
        srcs = self._sources(pdir, meta)
        mat = [s["id"] for s in srcs if s.get("role", "material") == "material"] or [s["id"] for s in srcs]
        names = ["核心概念", "運作方式", "應用與限制"]
        sections = [{"id": f"sec{i + 1}", "title": f"（模擬）{n}",
                     "key_points": [f"{n}的重點一", f"{n}的重點二"],
                     "source_refs": mat if i == 0 else [mat[i % len(mat)]]} for i, n in enumerate(names)]
        if bad:
            sections[0]["source_refs"] = ["s99"]
        data = {"title": f"（模擬）{meta.get('title', '學習資料')}",
                "audience": meta.get("settings", {}).get("audience") or "一般學習者",
                "language": "繁體中文",
                "objectives": ["說明核心概念", "比較不同作法的適用情境"],
                "sections": sections,
                "glossary": [{"term": "模擬", "definition": "不呼叫模型的測試模式"}],
                "open_questions": ["來源提到的數字是否正確"]}
        (pdir / "work" / "outline.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _notes(self, pdir: Path, filename: str, bad: bool, final: bool = False) -> None:
        outline = json.loads((pdir / "work" / "outline.json").read_text(encoding="utf-8"))
        parts = [f"# {outline['title']}\n"]
        for i, s in enumerate(outline["sections"]):
            if bad and i == len(outline["sections"]) - 1:
                continue  # 少一章 → 閘門會擋
            ref = s["source_refs"][0]
            parts.append(f"## [{s['id']}] {s['title']}\n")
            parts.append("這一章說明" + s["title"] + "。" + "（模擬內容）這裡會是依來源整理出的完整說明，包含定義、推導、例子與適用條件。" * 5 + f" [{ref}]\n")
            for kp in s["key_points"]:
                parts.append(f"- {kp}：模擬的說明文字。 [{ref}]")
            if final:
                parts.append("\n> 補充：（模擬）這是補充階段找到的背景說明。\n")
                parts.append("### 延伸資源\n- （模擬）說明影片：https://example.org/mock-video\n")
            parts.append("")
        parts.append("## 名詞對照\n- 模擬：不呼叫模型的測試模式\n")
        if final:
            parts.append("## 參考資料\n- （模擬）參考頁面一 https://example.org/mock-source-1\n- （模擬）參考頁面二 https://example.org/mock-source-2\n")
        (pdir / "work" / filename).write_text("\n".join(parts), encoding="utf-8")

    def _factcheck(self, pdir: Path, bad: bool) -> None:
        outline = json.loads((pdir / "work" / "outline.json").read_text(encoding="utf-8"))
        secs = [s["id"] for s in outline["sections"]]
        claims = []
        for i in range(max(self.cfg.min_claims, len(secs))):
            verdict = "partially_supported" if i == 1 else ("unverifiable" if i == 3 else "supported")
            c = {"id": f"c{i + 1}", "section_id": secs[i % len(secs)], "claim": f"（模擬）筆記中的主張 {i + 1}",
                 "verdict": verdict, "confidence": "medium",
                 "explanation": "（模擬）比對來源後的說明。",
                 "sources": [] if verdict == "unverifiable" else
                 [{"title": f"（模擬）參考頁面 {i % 2 + 1}", "url": f"https://example.org/mock-source-{i % 2 + 1}", "accessed": "2026-01-01"}]}
            if verdict == "partially_supported":
                c["correction"] = "（模擬）加上適用條件後的正確說法。"
            claims.append(c)
        if bad:
            claims[0]["sources"] = []  # supported 卻沒有來源 → 閘門會擋
        (pdir / "work" / "factcheck.json").write_text(
            json.dumps({"claims": claims, "summary": {"checked": len(claims)}}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _enrich(self, pdir: Path, bad: bool) -> None:
        outline = json.loads((pdir / "work" / "outline.json").read_text(encoding="utf-8"))
        secs = [s["id"] for s in outline["sections"]]
        types = ["background", "example", "misconception", "application"]
        sups = [{"id": f"e{i + 1}", "section_id": sid, "type": types[i % 4], "title": f"（模擬）補充 {i + 1}",
                 "content": "（模擬）補充階段找到的說明內容。",
                 "sources": [{"title": "（模擬）參考頁面二", "url": "https://example.org/mock-source-2"}]}
                for i, sid in enumerate(secs)]
        media = [] if bad else [{"id": "m1", "section_id": secs[min(1, len(secs) - 1)], "kind": "video",
                                 "title": "（模擬）概念說明影片", "url": "https://example.org/mock-video",
                                 "why": "（模擬）用動畫解釋核心概念", "start_seconds": 30}]
        (pdir / "work" / "enrich.json").write_text(
            json.dumps({"supplements": sups, "media": media}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _integrate(self, pdir: Path, bad: bool) -> None:
        self._notes(pdir, "notes.final.md", False, final=True)
        def load(name: str, empty: dict) -> dict:  # 查證或補充被略過時，檔案不存在
            f = pdir / "work" / name
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else empty
        fc, en = load("factcheck.json", {"claims": []}), load("enrich.json", {"supplements": []})
        claims = [{"id": c["id"], "action": "corrected" if c["verdict"] == "partially_supported" else "flagged",
                   "note": "（模擬）"} for c in fc["claims"] if c["verdict"] != "supported"]
        if bad:
            claims = claims[:-1]
        sups = [{"id": s["id"], "action": "inserted", "note": "（模擬）放在該章重點之後"} for s in en["supplements"]]
        (pdir / "work" / "integration.json").write_text(
            json.dumps({"claims": claims, "supplements": sups}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _deck(self, pdir: Path, meta: dict, bad: bool) -> None:
        outline = json.loads((pdir / "work" / "outline.json").read_text(encoding="utf-8"))
        secs = outline["sections"]
        slides: list[dict] = [
            {"id": "p1", "layout": "title", "title": outline["title"], "subtitle": "模擬模式產生的範例簡報"},
            {"id": "p2", "layout": "agenda", "title": "學完你會", "bullets": outline["objectives"]},
        ]
        n = 3
        for i, s in enumerate(secs):
            bullets = [f"{kp}（模擬）" for kp in s["key_points"]]
            if bad and i == 0:
                bullets = bullets * 5  # 條列過多 → 閘門會擋
            slides.append({"id": f"p{n}", "layout": "content", "section_id": s["id"],
                           "title": f"{s['title']}：一句話結論", "bullets": bullets,
                           "notes": "（模擬）講解備註：這裡補充條列沒有寫到的脈絡與例子。", "sources": s["source_refs"]})
            n += 1
            if i == 0:
                slides.append({"id": f"p{n}", "layout": "diagram", "section_id": s["id"], "title": "整體流程一張圖",
                               "visual": {"type": "svg", "svg": _flow_svg(["輸入", "處理", "查證", "成果"]), "caption": "（模擬）示意圖"},
                               "notes": "（模擬）由左到右閱讀，最後一格是學習者拿到的成果。"})
                n += 1
            if i == 1:
                slides.append({"id": f"p{n}", "layout": "media", "section_id": s["id"], "title": "看影片理解運作方式",
                               "visual": {"type": "video", "url": "https://example.org/mock-video",
                                          "title": "（模擬）概念說明影片", "caption": "建議從 0:30 開始看", "start_seconds": 30},
                               "bullets": ["留意影片如何解釋第一個步驟"], "notes": "（模擬）這支影片用動畫說明抽象的過程，適合在讀完本章文字後觀看。"})
                n += 1
            if i == len(secs) - 1:
                slides.append({"id": f"p{n}", "layout": "comparison", "section_id": s["id"], "title": "兩種作法怎麼選",
                               "columns": [{"heading": "作法 A", "bullets": ["適合小規模", "設定簡單"]},
                                           {"heading": "作法 B", "bullets": ["適合大規模", "需要維護"]}],
                               "notes": "（模擬）比較兩種作法時先看規模，再看長期的維護成本，最後才看導入難度。"})
                n += 1
        slides.append({"id": f"p{n}", "layout": "quiz", "section_id": secs[-1]["id"], "title": "想一想",
                       "question": "（模擬）下列哪一個敘述正確？", "options": ["敘述一", "敘述二", "敘述三"],
                       "answer_index": 1, "explanation": "（模擬）敘述二符合定義；其他兩個忽略了適用條件。"})
        slides.append({"id": f"p{n + 1}", "layout": "summary", "title": "重點回顧",
                       "bullets": [f"{s['title']}：{s['key_points'][0]}" for s in secs]})
        slides.append({"id": f"p{n + 2}", "layout": "references", "title": "參考資料",
                       "references": [{"title": "（模擬）參考頁面一", "url": "https://example.org/mock-source-1"},
                                      {"title": "（模擬）參考頁面二", "url": "https://example.org/mock-source-2"}]})
        deck = {"meta": {"title": outline["title"], "subtitle": "模擬模式", "language": "繁體中文",
                         "audience": outline["audience"]}, "slides": slides}
        (pdir / "work" / "deck.json").write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")

    def _review(self, bad: bool) -> dict:
        if bad:
            return {"verdict": "revise",
                    "scores": {"accuracy": 4, "completeness": 3, "readability": 4, "pedagogy": 4},
                    "issues": [{"severity": "major", "scope": "deck", "ref": "p3",
                                "problem": "（模擬）第一章漏掉了適用條件", "fix": "在條列加上適用條件，並在備註補一個例子"}],
                    "summary": "（模擬）整體可讀，但第一章需要補上適用條件。"}
        return {"verdict": "pass",
                "scores": {"accuracy": 5, "completeness": 4, "readability": 4, "pedagogy": 4},
                "issues": [{"severity": "minor", "scope": "deck", "ref": "p2",
                            "problem": "（模擬）學習目標可以更具體", "fix": "改用可檢驗的動詞"}],
                "summary": "（模擬）內容正確、結構清楚，可以出版。"}
