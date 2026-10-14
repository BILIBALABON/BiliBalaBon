"""Orchestrator：外層迴圈 + 獨立 agent + 驗證閘門。

每個階段的流程都一樣：

    啟動一個獨立的 agent session（有自己的 context、工具與預算）
        └─ session 內：agent 自己跑閘門；想結束時 Stop hook 再擋一次
    session 結束 → orchestrator 跑閘門（以這次結果為準）
        ├─ 通過 → 下一階段
        ├─ 沒過 → 帶著錯誤訊息 resume 同一個 session 再跑一輪（最多 max_attempts 輪）
        └─ 用完輪數 → 開處置單，等人在監控介面決定：再試／略過／停止

來源擷取（每個檔案一個 worker）與查證＋補充是平行的（asyncio.gather）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import traceback
from datetime import date
from pathlib import Path

from . import STAGE_LABELS, STAGES, gates, schemas
from .config import Config
from .preprocess import MissingDependency, is_fresh, preprocess_source, upload_sig
from .project import Project, atomic_write_json, blank_state, now_iso, read_json
from .render import references_md, render_html, render_pptx
from .runner import RunResult, make_runner


class Halt(Exception):
    """管線中止。status：stopped（人決定停）或 failed（無法繼續）。"""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# 每個階段的產出。指定「從某階段重跑」時先刪掉該階段與之後的產出，
# 否則 agent 這次什麼都沒寫，閘門也會因為上次留下的檔案而通過。
STAGE_OUTPUTS = {
    "ingest": ["work/sources"],
    "plan": ["work/outline.json"],
    "organize": ["work/notes.md"],
    "verify": ["work/factcheck.json", "work/enrich.json"],
    "integrate": ["work/notes.final.md", "work/integration.json"],
    "compose": ["work/deck.json"],
    "review": ["work/review.json"],
}

INGEST_ROLE = {"document": "ingest-document", "sidecar": "ingest-document",
               "audio": "ingest-audio", "video": "ingest-video"}

OPT_RETRY = {"key": "retry", "label": "再試一次", "desc": "沿用同一個 session 再跑一輪；可以在下方補充指示。"}
OPT_STOP = {"key": "stop", "label": "停止執行", "desc": "保留目前進度，之後可以從這個階段續跑。"}


class Pipeline:
    def __init__(self, project: Project, cfg: Config | None = None, runner=None):
        self.p = project
        self.cfg = cfg or project.cfg
        self.runner = runner or make_runner(self.cfg)
        self.st = project.state
        self._waiting = 0
        self._halting = False
        self._budget_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max(1, self.cfg.max_parallel))
        self.flags: dict = read_json(self.p.state_dir / "flags.json", {}) or {}

    # ---- 狀態與事件 -------------------------------------------------------
    def _save(self) -> None:
        self.p.save_state(self.st)

    def _stage(self, stage: str) -> dict:
        return self.st["stages"][stage]

    def _worker(self, stage: str, key: str) -> dict:
        return self._stage(stage)["workers"].setdefault(
            key, {"status": "pending", "attempts": 0, "cost_usd": 0.0, "gate": None, "label": key})

    def _set(self, stage: str, worker: str | None = None, **kw) -> None:
        (self._worker(stage, worker) if worker else self._stage(stage)).update(kw)
        self._save()

    def _add_cost(self, stage: str, usd: float, worker: str | None = None) -> None:
        self.st["cost_usd"] = round(self.st.get("cost_usd", 0.0) + usd, 6)
        s = self._stage(stage)
        s["cost_usd"] = round(s.get("cost_usd", 0.0) + usd, 6)
        if worker:
            w = self._worker(stage, worker)
            w["cost_usd"] = round(w.get("cost_usd", 0.0) + usd, 6)
        self._save()

    def _flag(self, key: str, value) -> None:
        self.flags[key] = value
        atomic_write_json(self.p.state_dir / "flags.json", self.flags)

    def emit(self, type_: str, **data) -> None:
        self.p.emit(type_, **data)

    # ---- 預算 -------------------------------------------------------------
    def remaining_usd(self) -> float:
        return self.p.budget_usd() - self.st.get("cost_usd", 0.0)

    def session_budget(self, stage: str, parallel: int = 1) -> float:
        """單一 session 的 max_budget_usd：該階段的預算比例 ÷ 平行數，且不超過專案剩餘預算。"""
        share = float(self.cfg.stage_share.get(stage, 0.15)) * self.p.budget_usd() / max(parallel, 1)
        return max(0.01, min(share, self.remaining_usd()))

    async def ensure_budget(self, stage: str) -> None:
        async with self._budget_lock:  # 平行的 worker 同時撞到上限時，只開一張處置單
            while self.remaining_usd() <= 0.005:
                if self._halting:  # 另一個平行的 worker 已經問過，使用者選了停止
                    raise Halt("stopped", "花費已達上限，已停止")
                spent = self.cfg.usd_to_ntd(self.st["cost_usd"])
                budget = self.p.budget_ntd()
                add = max(50, round(budget * 0.5 / 10) * 10)
                resp = await self.ask(
                    stage=stage, kind="budget_exhausted", title="花費已達上限",
                    message=f"這個專案已花費約 NT${spent:.0f}，達到設定的上限 NT${budget:.0f}。",
                    options=[{"key": "add", "label": f"追加 NT${add:.0f} 繼續", "desc": f"上限調整為 NT${budget + add:.0f}。"},
                             OPT_STOP],
                    recommended="stop", default="stop")
                if resp["choice"] != "add":
                    self._halting = True
                    raise Halt("stopped", "花費已達上限，已停止")
                self.p.update_settings({"budget_ntd": budget + add})
                self.emit("budget_added", stage=stage, budget_ntd=budget + add)

    # ---- 處置單（人工決定） ----------------------------------------------
    async def ask(self, **kw) -> dict:
        inc = self.p.open_incident(**kw)
        self._waiting += 1
        self.st["status"] = "waiting"
        self._save()
        t0 = time.monotonic()
        timeout = self.cfg.incident_timeout_s
        try:
            while True:
                cur = self.p.get_incident(inc["id"])
                if cur and cur.get("response"):
                    return cur["response"]
                if timeout > 0 and time.monotonic() - t0 > timeout:
                    # 無人值守：逾時就採用預設選項（一律是保守的那個）
                    return self.p.respond_incident(inc["id"], inc["default"], "逾時未回覆，採用預設選項", by="timeout")["response"]
                await asyncio.sleep(1.0)
        finally:
            self._waiting -= 1
            if self._waiting == 0 and self.st["status"] == "waiting":
                self.st["status"] = "running"
                self._save()

    # ---- 外層迴圈 ---------------------------------------------------------
    async def _attempts(self, *, stage: str, gate: str, role: str, prompt: str, target: str | None,
                        worker: str | None, session: str | None, note: str, parallel: int,
                        errors: list[str]) -> tuple[bool, list[str], str | None, RunResult | None, str | None]:
        """一次外層迴圈：最多跑 max_attempts 輪。errors 是上一回合留下的閘門錯誤（人工選擇「再試」時帶入）。"""
        last: RunResult | None = None
        label = worker or stage
        for attempt in range(1, self.cfg.max_attempts + 1):
            await self.ensure_budget(stage)
            holder = self._worker(stage, worker) if worker else self._stage(stage)
            holder["attempts"] = holder.get("attempts", 0) + 1
            if worker:
                self._stage(stage)["attempts"] = max(self._stage(stage).get("attempts", 0), holder["attempts"])
            self._save()
            self.emit("attempt_started", stage=stage, worker=worker, role=role,
                      attempt=attempt, max_attempts=self.cfg.max_attempts)
            if attempt == 1 and not session:
                text = prompt + (f"\n\n使用者補充的指示：{note}" if note else "")
            else:
                text = self._retry_prompt(gate, target, errors, note if attempt == 1 else "")
            async with self._sem:
                last = await self.runner.run(
                    role=role, prompt=text, project_dir=self.p.dir, gate=gate, target=target,
                    resume=session, budget_usd=self.session_budget(stage, parallel), emit=self._emit_for(stage, worker),
                    label=label, round_no=holder["attempts"])
            session = last.session_id or session
            self._add_cost(stage, last.cost_usd, worker)
            self.emit("cost", stage=stage, worker=worker, usd=last.cost_usd,
                      total_ntd=round(self.cfg.usd_to_ntd(self.st["cost_usd"]), 2))
            if not last.ok:
                self.emit("agent_error", stage=stage, worker=worker, subtype=last.subtype, error=last.error)
            if last.fatal:  # 登入、帳務這類問題重試也沒用，直接交給人處理
                return False, errors, session, last, None
            g = await asyncio.to_thread(gates.check, self.p.dir, gate, target, self.cfg)
            holder["gate"] = g.as_dict()
            self._save()
            self.emit("gate_result", stage=stage, worker=worker, gate=gate, ok=g.ok, attempt=attempt,
                      errors=g.errors[:20], warnings=g.warnings[:10])
            if g.ok:
                return True, [], session, last, None
            errors = g.errors
            if g.system:  # 沒有網路、閘門程式出錯：不是 agent 改得了的，不要再耗輪數
                return False, errors, session, last, g.system
        return False, errors, session, last, None

    def _retry_prompt(self, gate: str, target: str | None, errors: list[str], note: str) -> str:
        cmd = f"python3 -m learnloop.gates . {gate}" + (f" --target {target}" if target else "")
        lines = ["驗證閘門尚未通過。請修正下列問題，修完後再執行一次閘門確認："] if errors else \
                ["請繼續完成任務，完成後執行閘門確認："]
        lines += [f"- {e}" for e in errors[:25]]
        lines.append(f"\n閘門指令：{cmd}")
        if note:
            lines.append(f"\n使用者補充的指示：{note}")
        return "\n".join(lines)

    def _emit_for(self, stage: str, worker: str | None):
        def _emit(type_: str, **data):
            self.emit(type_, stage=stage, worker=worker, **data)
        return _emit

    async def run_gated(self, *, stage: str, gate: str, role: str, prompt: str, target: str | None = None,
                        worker: str | None = None, parallel: int = 1, skip: dict | None = None,
                        note: str = "") -> str:
        """跑到閘門通過為止。回傳 passed 或 skipped；使用者選擇停止時丟 Halt。"""
        session: str | None = None
        errors: list[str] = []
        if worker:
            self._set(stage, worker, status="running")
            self.emit("worker_started", stage=stage, worker=worker, role=role)
        recheck = False
        while True:
            if recheck:  # 剛排除的是系統問題（例如改成不檢查連結）：先重跑閘門，通過就不必再叫 agent
                recheck = False
                g = await asyncio.to_thread(gates.check, self.p.dir, gate, target, self.cfg)
                (self._worker(stage, worker) if worker else self._stage(stage))["gate"] = g.as_dict()
                self._save()
                self.emit("gate_result", stage=stage, worker=worker, gate=gate, ok=g.ok,
                          errors=g.errors[:20], warnings=g.warnings[:10])
                ok, errors, last, system = g.ok, g.errors, None, g.system
                if not ok and not system:
                    continue  # 檢查恢復正常了，剩下的是內容問題：交回給 agent 修
            else:
                ok, errors, session, last, system = await self._attempts(
                    stage=stage, gate=gate, role=role, prompt=prompt, target=target, worker=worker,
                    session=session, note=note, parallel=parallel, errors=errors)
            if ok:
                if worker:
                    self._set(stage, worker, status="passed")
                    self.emit("worker_done", stage=stage, worker=worker, status="passed")
                return "passed"
            if worker:
                self._set(stage, worker, status="waiting")
            options = [OPT_RETRY] + ([skip] if skip else []) + [OPT_STOP]
            details = list(errors[:15])
            if last and last.error:
                details.append(f"agent 執行訊息：{last.error}")
            who = f"{STAGE_LABELS[stage]}" + (f"（{worker}）" if worker else "")
            if system == "offline":
                resp = await self.ask(
                    stage=stage, target=worker, kind="gate_unavailable", title="無法檢查連結：連不到外部網站",
                    message="內容已經產出，但這台電腦目前連不上外部網站，無法確認來源與影片連結是否存在。"
                            "請檢查網路或代理設定後重試；也可以這次先不檢查連結。",
                    details=details,
                    options=[{"key": "retry", "label": "網路恢復了，重新檢查", "desc": "只重跑檢查，不會重新呼叫 agent。"},
                             {"key": "nolinks", "label": "這次不檢查連結，繼續", "desc": "連結問題只列為警告，成品會標示「連結未經檢查」。"},
                             OPT_STOP],
                    recommended="retry", default="stop")
                if resp["choice"] == "nolinks":
                    self.cfg.linkcheck_mode = "warn"
                    self._flag("linkcheck_skipped", True)
                if resp["choice"] in ("retry", "nolinks"):
                    recheck = True
                    if worker:
                        self._set(stage, worker, status="running")
                    continue
                raise Halt("stopped", "無法檢查連結，使用者選擇停止")
            if system == "exception":
                resp = await self.ask(
                    stage=stage, target=worker, kind="gate_unavailable", title=f"{who}的檢查程式發生錯誤",
                    message="這是系統本身的問題，不是內容問題。請把下面的訊息回報給維護者；排除後可以重新檢查。",
                    details=details, options=[{"key": "retry", "label": "重新檢查", "desc": "只重跑檢查。"}, OPT_STOP],
                    recommended="retry", default="stop")
                if resp["choice"] == "retry":
                    recheck = True
                    continue
                raise Halt("stopped", "檢查程式發生錯誤，使用者選擇停止")
            if last and last.fatal:
                resp = await self.ask(
                    stage=stage, target=worker, kind="agent_error", title="無法啟動 Claude agent",
                    message="請確認這台電腦的 Claude Code 已登入（執行 claude 後輸入 /login），"
                            "或已設定 ANTHROPIC_API_KEY，並確認帳號額度正常，再選擇重試。",
                    details=[last.error or ""], options=[OPT_RETRY, OPT_STOP], recommended="retry", default="stop")
            else:
                resp = await self.ask(
                    stage=stage, target=worker, kind="gate_failed", title=f"{who}連續 {self.cfg.max_attempts} 輪未通過檢查",
                    message="系統已自動重試到上限，仍有下列問題。建議先看問題是否能靠補充指示解決；若是來源檔本身的問題，可以略過或停止。",
                    details=details, options=options, recommended="retry", default="stop")
            if resp["choice"] == "retry":
                note = resp.get("note", "")
                if worker:
                    self._set(stage, worker, status="running")
                continue
            if skip and resp["choice"] == skip["key"]:
                if worker:
                    self._set(stage, worker, status="skipped")
                    self.emit("worker_done", stage=stage, worker=worker, status="skipped")
                return "skipped"
            raise Halt("stopped", f"{who}未通過檢查，使用者選擇停止")

    async def _gather(self, coros: list) -> list:
        """平行執行；任何一個丟出例外就取消其餘的。"""
        tasks = [asyncio.ensure_future(c) for c in coros]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # ---- 任務說明 ---------------------------------------------------------
    def _context(self) -> str:
        s, meta = self.p.settings, self.p.meta
        lines = [
            f"專案：{meta.get('title', '')}",
            f"撰寫語言：{s['language']}",
            f"對象：{s['audience'] or '未指定（以一般大學程度的學習者為準）'}",
            f"學習目的：{s['goal'] or '未指定'}",
            f"期望投影片張數：約 {int(s['slide_target'] or 12)} 張（目標值；允許 {self.cfg.min_slides}–"
            f"{max(int(int(s['slide_target'] or 12) * 1.5), self.cfg.min_slides + 2)} 張）",
            f"今天日期：{date.today().isoformat()}",
        ]
        if s.get("notes"):
            lines.append(f"使用者的額外指示：{s['notes']}")
        skipped = self._skipped_sources()
        if skipped:
            lines.append(f"已略過、不需處理的來源：{', '.join(sorted(skipped))}")
        if self.flags.get("factcheck_skipped"):
            lines.append("注意：查證階段被略過，work/factcheck.json 不存在；內容尚未經外部查證。")
        if self.flags.get("enrich_skipped"):
            lines.append("注意：補充階段被略過，work/enrich.json 不存在；沒有補充資料與推薦影片。")
        return "\n".join(lines)

    def _skipped_sources(self) -> set[str]:
        return set(read_json(self.p.state_dir / "skipped_sources.json", []) or [])

    def _skip_source(self, sid: str) -> None:
        skipped = self._skipped_sources() | {sid}
        atomic_write_json(self.p.state_dir / "skipped_sources.json", sorted(skipped))
        material_left = [s for s in self.p.content_sources()
                         if s["id"] not in skipped and s.get("role", "material") == "material"]
        if not material_left:
            raise Halt("failed", "所有教材來源都被略過了，沒有內容可以整理")

    def _active_sources(self) -> list[dict]:
        skipped = self._skipped_sources()
        return [s for s in self.p.content_sources() if s["id"] not in skipped]

    # ---- 各階段 -----------------------------------------------------------
    async def stage_preprocess(self) -> None:
        sources = self._active_sources()
        if not sources:
            raise Halt("failed", "專案裡沒有來源檔，請先上傳資料")
        for src in sources:
            sid = src["id"]
            self._worker("preprocess", sid)["label"] = src["filename"]
            if is_fresh(self.p, src):  # 原檔沒變就不重做轉檔與語音辨識（續跑、或只是新增了別的檔案）
                self._set("preprocess", sid, status="passed")
                continue
            self._set("preprocess", sid, status="running")
            self.emit("worker_started", stage="preprocess", worker=sid, role="前處理", file=src["filename"])
            while True:
                try:
                    meta = await asyncio.to_thread(preprocess_source, self.p, src)
                    self._set("preprocess", sid, status="passed")
                    self.emit("worker_done", stage="preprocess", worker=sid, status="passed",
                              chars=meta.get("chars"), frames=len(meta.get("frames", [])))
                    break
                except Exception as e:
                    missing = isinstance(e, MissingDependency)
                    self._set("preprocess", sid, status="waiting")
                    resp = await self.ask(
                        stage="preprocess", target=sid, kind="missing_dependency" if missing else "preprocess_error",
                        title=f"無法處理 {src['filename']}",
                        message=(f"{e}。{e.hint}" if missing else f"前處理時發生錯誤：{type(e).__name__}: {e}"),
                        options=[{"key": "retry", "label": "已處理好，重試", "desc": "安裝好工具或換好檔案後選這個。"},
                                 {"key": "skip", "label": "略過這個檔案", "desc": "其餘來源照常進行。"}, OPT_STOP],
                        recommended="retry" if missing else "skip", default="stop")
                    if resp["choice"] == "retry":
                        self._set("preprocess", sid, status="running")
                        continue
                    if resp["choice"] == "skip":
                        self._skip_source(sid)
                        self._set("preprocess", sid, status="skipped")
                        self.emit("worker_done", stage="preprocess", worker=sid, status="skipped")
                        break
                    raise Halt("stopped", f"{src['filename']} 無法處理，使用者選擇停止")
        self._final_gate("preprocess", "preprocess")

    def _final_gate(self, stage: str, gate: str) -> None:
        g = gates.check(self.p.dir, gate, None, self.cfg)
        self._stage(stage)["gate"] = g.as_dict()
        self._save()
        self.emit("gate_result", stage=stage, gate=gate, ok=g.ok, errors=g.errors[:20], warnings=g.warnings[:10])
        if not g.ok:
            raise Halt("failed", f"{STAGE_LABELS[stage]}未通過檢查：" + "；".join(g.errors[:3]))

    async def stage_ingest(self) -> None:
        sources = self._active_sources()
        done_path = self.p.state_dir / "ingested.json"
        done = read_json(done_path, {}) or {}
        todo = []
        for s in sources:
            sid = s["id"]
            self._worker("ingest", sid)["label"] = s["filename"]
            if done.get(sid) == upload_sig(self.p, s) and gates.check(self.p.dir, "ingest", sid, self.cfg).ok:
                self._set("ingest", sid, status="passed")  # 原檔沒變、整理結果也還合格：沿用
            else:
                todo.append(s)
        n = min(len(todo), self.cfg.max_parallel) or 1

        async def one(src: dict) -> None:
            sid = src["id"]
            self._worker("ingest", sid)["label"] = src["filename"]
            prompt = (f"{self._context()}\n\n請整理來源 {sid}：{src['filename']}（類型：{src['kind']}，"
                      f"角色：{'教材' if src.get('role', 'material') == 'material' else '參考資料'}）。\n"
                      f"前處理結果在 raw/{sid}/，原檔在 uploads/{src['filename']}。\n"
                      f"產出 work/sources/{sid}.md，完成後執行：python3 -m learnloop.gates . ingest --target {sid}")
            result = await self.run_gated(
                stage="ingest", gate="ingest", role=INGEST_ROLE[src["kind"]], prompt=prompt, target=sid, worker=sid,
                parallel=n, skip={"key": "skip", "label": "略過這個來源", "desc": "其餘來源照常進行，這份檔案不會出現在成品裡。"})
            if result == "skipped":
                self._skip_source(sid)
            else:
                done[sid] = upload_sig(self.p, src)
                atomic_write_json(done_path, done)

        await self._gather([one(s) for s in todo])
        self._final_gate("ingest", "ingest")

    async def stage_plan(self) -> None:
        await self.run_gated(
            stage="plan", gate="plan", role="planner",
            prompt=f"{self._context()}\n\n請讀完 work/sources/ 下所有來源整理，規劃學習目標與章節大綱，產出 work/outline.json。")

    async def stage_organize(self) -> None:
        await self.run_gated(
            stage="organize", gate="organize", role="organizer",
            prompt=f"{self._context()}\n\n請依 work/outline.json，把 work/sources/ 的內容整理成完整的學習筆記 work/notes.md。")

    async def stage_verify(self) -> None:
        async def factcheck() -> None:
            if self._worker("verify", "factcheck").get("status") in ("passed", "skipped"):
                return
            self._worker("verify", "factcheck")["label"] = "查證"
            r = await self.run_gated(
                stage="verify", gate="factcheck", role="fact-checker", worker="factcheck", parallel=2,
                prompt=f"{self._context()}\n\n請查證 work/notes.md 中可被檢驗的主張，產出 work/factcheck.json。",
                skip={"key": "skip", "label": "略過查證", "desc": "繼續往下做，但成品會標示「內容未經外部查證」。"})
            if r == "skipped":
                (self.p.work / "factcheck.json").unlink(missing_ok=True)
                self._flag("factcheck_skipped", True)

        async def enrich() -> None:
            if self._worker("verify", "enrich").get("status") in ("passed", "skipped"):
                return
            self._worker("verify", "enrich")["label"] = "補充"
            r = await self.run_gated(
                stage="verify", gate="enrich", role="enricher", worker="enrich", parallel=2,
                prompt=f"{self._context()}\n\n請為 work/notes.md 找補充資料與說明影片，產出 work/enrich.json。",
                skip={"key": "skip", "label": "略過補充", "desc": "繼續往下做，成品只包含原始教材的內容，沒有補充資料與影片。"})
            if r == "skipped":
                (self.p.work / "enrich.json").unlink(missing_ok=True)
                self._flag("enrich_skipped", True)

        await self._gather([factcheck(), enrich()])

    async def stage_integrate(self) -> None:
        await self.run_gated(
            stage="integrate", gate="integrate", role="integrator",
            prompt=f"{self._context()}\n\n請依 work/factcheck.json 更正 work/notes.md、併入 work/enrich.json 的補充資料，"
                   "產出 work/notes.final.md 與 work/integration.json。")

    async def stage_compose(self) -> None:
        await self.run_gated(
            stage="compose", gate="compose", role="slide-composer",
            prompt=f"{self._context()}\n\n請把 work/notes.final.md 編排成簡報，產出 work/deck.json。")

    async def _review_once(self, round_no: int, previous: dict | None) -> dict:
        prompt = (f"{self._context()}\n\n請審核 work/deck.json 與 work/notes.final.md（第 {round_no} 輪審核），"
                  "並以指定的結構化格式回傳結果。")
        if previous:
            issues = "\n".join(f"- [{i['severity']}] {i['ref']}：{i['problem']}" for i in previous.get("issues", []))
            prompt += f"\n\n上一輪提出的問題如下，請確認是否已修正，並重新完整審核一次（不要只看這些）：\n{issues}"
        last_err = ""
        for attempt in range(1, self.cfg.max_attempts + 1):
            await self.ensure_budget("review")
            self._stage("review")["attempts"] += 1
            self._save()
            self.emit("attempt_started", stage="review", role="reviewer", attempt=attempt,
                      max_attempts=self.cfg.max_attempts, round=round_no)
            async with self._sem:
                res = await self.runner.run(
                    role="reviewer", prompt=prompt, project_dir=self.p.dir, gate=None, budget_usd=self.session_budget("review"),
                    emit=self._emit_for("review", None), output_schema=schemas.REVIEW, label="review",
                    round_no=self._stage("review")["attempts"])
            self._add_cost("review", res.cost_usd)
            self.emit("cost", stage="review", usd=res.cost_usd, total_ntd=round(self.cfg.usd_to_ntd(self.st["cost_usd"]), 2))
            if res.fatal:
                last_err = f"無法啟動 Claude agent：{res.error}（請確認已登入或已設定 ANTHROPIC_API_KEY）"
                self.emit("agent_error", stage="review", subtype=res.subtype, error=last_err)
                break
            data = res.structured
            if data is None and res.text.strip().startswith("{"):
                try:
                    data = json.loads(res.text)
                except json.JSONDecodeError:
                    data = None
            if isinstance(data, dict):
                check = gates.GateResult()
                if gates._validate(data, schemas.REVIEW, "review.json", check):
                    return data
                last_err = "；".join(check.errors[:3])
            else:
                last_err = res.error or "審核者沒有回傳結構化結果"
            self.emit("agent_error", stage="review", subtype=res.subtype, error=last_err)
        resp = await self.ask(
            stage="review", kind="agent_error", title="審核者無法產出審核結果",
            message=f"連續 {self.cfg.max_attempts} 次都沒有拿到合格的審核結果。", details=[last_err],
            options=[OPT_RETRY, OPT_STOP], recommended="retry", default="stop")
        if resp["choice"] == "retry":
            return await self._review_once(round_no, previous)
        raise Halt("stopped", "審核無法完成，使用者選擇停止")

    async def stage_review(self) -> None:
        round_no, previous, extra_note = 0, None, ""
        while True:
            round_no += 1
            self.emit("review_round", stage="review", round=round_no)
            review = await self._review_once(round_no, previous)
            atomic_write_json(self.p.work / "review.json", review)  # 審核者唯讀，由 orchestrator 代寫
            ok, reasons = gates.review_passes(review, self.cfg)
            g = gates.check(self.p.dir, "review", None, self.cfg)
            self._stage("review")["gate"] = g.as_dict()
            self._stage("review")["round"] = round_no
            self._save()
            self.emit("gate_result", stage="review", gate="review", ok=ok, round=round_no,
                      errors=g.errors[:20], scores=review.get("scores"), summary=review.get("summary"))
            if ok:
                self._flag("review_overridden", False)
                return
            if round_no > self.cfg.max_review_rounds:
                self._set("review", status="waiting")
                resp = await self.ask(
                    stage="review", kind="review_failed", title=f"審核 {round_no} 輪後仍未通過",
                    message=review.get("summary") or "審核者認為內容還需要修改。",
                    details=reasons + [f"[{i['severity']}] {i['ref']}：{i['problem']}" for i in review.get("issues", [])][:12],
                    options=[{"key": "retry", "label": "再修改一輪", "desc": "把問題退回給編排者修正後重新審核；可以在下方補充指示。"},
                             {"key": "publish", "label": "照目前版本出版", "desc": "成品會附上審核意見，並標示「審核未通過」。"},
                             OPT_STOP],
                    recommended="retry", default="stop")
                if resp["choice"] == "publish":
                    self._flag("review_overridden", True)
                    return
                if resp["choice"] != "retry":
                    raise Halt("stopped", "審核未通過，使用者選擇停止")
                extra_note = resp.get("note", "")
                self._set("review", status="running")
            await self._revise(review, extra_note)
            previous = review

    async def _revise(self, review: dict, note: str) -> None:
        """把審核意見退回去修：筆記有錯先改筆記，再重排簡報。"""
        issues = review.get("issues", [])
        fmt = lambda xs: "\n".join(f"- [{i['severity']}] {i['ref']}：{i['problem']} → 建議：{i['fix']}" for i in xs)
        notes_issues = [i for i in issues if i.get("scope") == "notes" and i.get("severity") in ("blocker", "major")]
        self.emit("revision_started", stage="review", notes=len(notes_issues), deck=len(issues) - len(notes_issues))
        if notes_issues:
            self._set("integrate", status="running")
            await self.run_gated(
                stage="integrate", gate="integrate", role="integrator", note=note,
                prompt=f"{self._context()}\n\n審核者指出定稿筆記有下列問題，請修正 work/notes.final.md"
                       f"（必要時同步更新 work/integration.json）：\n{fmt(notes_issues)}")
            self._set("integrate", status="passed")
        self._set("compose", status="running")
        await self.run_gated(
            stage="compose", gate="compose", role="slide-composer", note=note,
            prompt=f"{self._context()}\n\n審核者對簡報提出下列意見，請修正 work/deck.json"
                   f"（內容以 work/notes.final.md 為準；minor 等級的建議也盡量處理）：\n{fmt(issues)}")
        self._set("compose", status="passed")

    async def stage_publish(self) -> None:
        out = self.p.output
        out.mkdir(exist_ok=True)
        deck = json.loads((self.p.work / "deck.json").read_text(encoding="utf-8"))
        outline = read_json(self.p.work / "outline.json", {})
        review = read_json(self.p.work / "review.json", {})
        badges = []
        if self.flags.get("factcheck_skipped"):
            badges.append("內容未經外部查證")
        if self.flags.get("review_overridden"):
            badges.append("審核未通過，僅供參考")
        if self.flags.get("linkcheck_skipped"):
            badges.append("連結未經檢查")
        if self.cfg.mock:
            badges.append("模擬模式產出，非實際內容")
        if badges and deck["slides"] and deck["slides"][0]["layout"] == "title":
            first = deck["slides"][0]
            first["subtitle"] = "｜".join(filter(None, [first.get("subtitle", "")] + [f"⚠ {b}" for b in badges]))
        (out / "deck.html").write_text(render_html(deck, self.p.dir, outline), encoding="utf-8")
        shutil.copyfile(self.p.work / "notes.final.md", out / "notes.md")
        (out / "references.md").write_text(references_md(self.p.dir), encoding="utf-8")
        warnings: list[str] = []
        try:
            warnings += await asyncio.to_thread(render_pptx, deck, self.p.dir, out / "deck.pptx")
        except ImportError:
            warnings.append("未安裝 python-pptx，沒有輸出 PowerPoint（pip install python-pptx）")
        except Exception as e:
            warnings.append(f"PowerPoint 輸出失敗：{type(e).__name__}: {e}")
        report = {
            "title": deck["meta"]["title"], "finished_at": now_iso(), "mock": self.cfg.mock,
            "cost_usd": round(self.st["cost_usd"], 4), "cost_ntd": round(self.cfg.usd_to_ntd(self.st["cost_usd"]), 2),
            "usd_twd_rate": self.cfg.usd_twd_rate, "slides": len(deck["slides"]),
            "review": {**{k: review.get(k) for k in ("verdict", "scores", "summary", "issues")},
                       "rounds": self._stage("review").get("round")},
            "flags": {"factcheck_skipped": bool(self.flags.get("factcheck_skipped")),
                      "enrich_skipped": bool(self.flags.get("enrich_skipped")),
                      "review_overridden": bool(self.flags.get("review_overridden")),
                      "linkcheck_skipped": bool(self.flags.get("linkcheck_skipped")),
                      "skipped_sources": sorted(self._skipped_sources())},
            "stages": {s: {"attempts": v["attempts"], "cost_usd": v["cost_usd"]} for s, v in self.st["stages"].items()},
            "warnings": warnings,
        }
        atomic_write_json(out / "report.json", report)
        for w in warnings:
            self.emit("warning", stage="publish", text=w)
        self._final_gate("publish", "publish")

    # ---- 進入點 -----------------------------------------------------------
    async def run(self, from_stage: str | None = None) -> str:
        st = self.st
        if from_stage and from_stage not in STAGES:
            raise ValueError(f"沒有這個階段：{from_stage}")
        # 來源清單和上次執行時不同（新增、移除、改了用途）→ 之前的大綱與筆記都不算數，從頭來過。
        # 沒變的檔案不會重新轉檔，也不會重新擷取。
        sig, sig_path = self.p.sources_signature(), self.p.state_dir / "sources_sig.json"
        sources_changed = read_json(sig_path) not in (None, sig)
        atomic_write_json(sig_path, sig)
        explicit = from_stage
        if sources_changed and (from_stage is None or STAGES.index(from_stage) > 0):
            from_stage = STAGES[0]
        if from_stage:
            idx = STAGES.index(from_stage)
            fresh = blank_state()["stages"]
            for s in STAGES[idx:]:
                st["stages"][s] = {**fresh[s], "cost_usd": st["stages"][s].get("cost_usd", 0.0)}
            # 使用者沒有明確要求重做擷取時（因來源變動而自動重來），保留沒變的來源整理，stage_ingest 會逐一確認
            keep_ingest = explicit is None or STAGES.index(explicit) > STAGES.index("ingest")
            for s in STAGES[idx:]:
                if s == "ingest" and keep_ingest:
                    continue
                for rel in STAGE_OUTPUTS.get(s, []):
                    target = self.p.dir / rel
                    shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
            if explicit and STAGES.index(explicit) <= STAGES.index("ingest"):
                (self.p.state_dir / "ingested.json").unlink(missing_ok=True)
            if explicit == STAGES[0]:  # 使用者明確要求整個重來：連轉檔結果與略過清單都清掉
                shutil.rmtree(self.p.raw, ignore_errors=True)
                (self.p.state_dir / "skipped_sources.json").unlink(missing_ok=True)
            (self.p.work / "sources").mkdir(parents=True, exist_ok=True)
            self.p.raw.mkdir(exist_ok=True)
            if idx <= STAGES.index("verify"):
                self.flags = {}
                atomic_write_json(self.p.state_dir / "flags.json", self.flags)
        for s in STAGES:  # 上次中斷時停在半路的階段，重新來過
            if st["stages"][s]["status"] not in ("passed", "skipped"):
                st["stages"][s]["status"] = "pending"
                for w in st["stages"][s]["workers"].values():
                    if w.get("status") not in ("passed", "skipped"):
                        w["status"] = "pending"
        self.p.close_stale_incidents()
        if self.flags.get("linkcheck_skipped"):
            self.cfg.linkcheck_mode = "warn" if self.cfg.linkcheck_mode == "strict" else self.cfg.linkcheck_mode
        st.update(status="running", pid=os.getpid(), started_at=now_iso(), ended_at=None, error=None)
        self._save()
        self.emit("pipeline_started", from_stage=from_stage, mock=self.cfg.mock,
                  budget_ntd=self.p.budget_ntd(), rate=self.cfg.usd_twd_rate, sources_changed=sources_changed)
        final, error = "done", None
        try:
            for stage in STAGES:
                if self._stage(stage)["status"] in ("passed", "skipped"):
                    continue
                st["current_stage"] = stage
                self._set(stage, status="running", started_at=now_iso(), ended_at=None)
                self.emit("stage_started", stage=stage)
                await getattr(self, f"stage_{stage}")()
                self._set(stage, status="passed", ended_at=now_iso())
                self.emit("stage_passed", stage=stage, attempts=self._stage(stage)["attempts"])
            st["current_stage"] = None
        except Halt as h:
            final, error = h.status, h.message
        except asyncio.CancelledError:
            final, error = "stopped", "已手動停止"
        except Exception as e:
            final, error = "failed", f"{type(e).__name__}: {e}"
            self.emit("error", text=traceback.format_exc()[-1500:])
        finally:
            if final != "done":
                cur = st.get("current_stage")
                for name, sd in st["stages"].items():
                    if sd["status"] in ("running", "waiting"):
                        sd["status"] = "failed" if (final == "failed" and name == cur) else "pending"
                    for w in sd["workers"].values():
                        if w.get("status") in ("running", "waiting"):
                            w["status"] = "pending"
                self.p.close_stale_incidents()
            st.update(status=final, pid=None, ended_at=now_iso(), error=error)
            self._save()
            self.emit("pipeline_finished", status=final, error=error,
                      cost_ntd=round(self.cfg.usd_to_ntd(st["cost_usd"]), 2))
        return final
