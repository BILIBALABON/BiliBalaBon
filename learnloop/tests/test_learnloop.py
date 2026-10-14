"""自動測試（不呼叫模型、不需要網路）。執行：python3 -m unittest discover -s tests -v

涵蓋：閘門判定、外層迴圈重試、審核退回、處置單、預算上限、中斷續跑、Stop hook 腳本、
成品清洗，以及監控介面 API（含路徑穿越與跨站請求防護）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from learnloop import gates  # noqa: E402
from learnloop.config import load_config  # noqa: E402
from learnloop.pipeline import Pipeline  # noqa: E402
from learnloop.project import Project, create_project, list_projects, safe_filename  # noqa: E402
from learnloop.render import render_html, sanitize_svg  # noqa: E402

HOOK = ROOT / ".claude" / "hooks" / "stop-gate.sh"

# 語音辨識引擎的替身：介面與 faster-whisper 相同，在行程內「辨識」（可設定要花幾秒），不下載模型
FAKE_WHISPER = '''
import os, time


class _Segment:
    def __init__(self, start, text):
        self.start, self.text = start, text


class WhisperModel:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, path, language=None, vad_filter=True):
        assert os.path.exists(path)
        if os.environ.get("FAKE_WHISPER_MARK"):
            open(os.environ["FAKE_WHISPER_MARK"], "w").close()
        time.sleep(float(os.environ.get("FAKE_WHISPER_SLEEP", "0")))
        return iter([_Segment(0.0, " 今天談熱傳導 "), _Segment(75.2, "接著是熱阻"), _Segment(80.0, "  ")]), None
'''


def install_fake_whisper(root: Path) -> Path:
    d = root / "fakelibs"
    (d / "faster_whisper").mkdir(parents=True, exist_ok=True)
    (d / "faster_whisper" / "__init__.py").write_text(FAKE_WHISPER, encoding="utf-8")
    return d
DOC = """# 熱傳導基礎

## 傅立葉定律
熱通量與溫度梯度成正比：q = -k dT/dx。k 為熱傳導係數，單位 W/(m·K)。
銅的熱傳導係數約為 400 W/(m·K)，空氣約 0.026 W/(m·K)。

## 熱阻
平板的熱阻 R = L/(kA)。多層材料串聯時熱阻相加，與電阻串聯相同。
"""


class Base(unittest.TestCase):
    ENV: dict[str, str] = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lp-test-"))
        self._old = dict(os.environ)
        os.environ.update({"LP_PROJECTS_DIR": str(self.tmp / "projects"), "LP_MOCK": "1", "LP_MOCK_DELAY": "0",
                           "LP_MOCK_COST": "0.02", "LP_INCIDENT_TIMEOUT": "1", "LP_MOCK_FAIL": "", **self.ENV})
        for k in ("LP_CONFIG", "LP_MODEL", "LP_EFFORT", "LP_LINKCHECK"):
            os.environ.pop(k, None)
        # 測試不受使用者在 config.toml 設的 models.override 影響（文件建議試跑時這樣設）
        import re
        cfg_text = re.sub(r"(?m)^override\s*=.*$", 'override = ""', (ROOT / "config.toml").read_text(encoding="utf-8"))
        (self.tmp / "config.toml").write_text(cfg_text, encoding="utf-8")
        os.environ["LP_CONFIG"] = str(self.tmp / "config.toml")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_project(self, n_docs: int = 2, **settings) -> Project:
        p = create_project("測試專案", settings or None)
        for i in range(n_docs):
            f = self.tmp / f"doc{i}.tmp"
            f.write_text(DOC + f"\n第 {i + 1} 份。", encoding="utf-8")
            p.add_source(f"講義{i + 1}.md", f)
        return p

    def run_pipeline(self, p: Project, from_stage: str | None = None) -> str:
        return asyncio.run(Pipeline(Project(p.id, load_config())).run(from_stage=from_stage))

    def events(self, p: Project) -> list[dict]:
        return p.read_events(0)[0]


class TestPipeline(Base):
    def test_full_run_passes_every_gate(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")
        st = p.state
        self.assertTrue(all(s["status"] == "passed" for s in st["stages"].values()), st["stages"])
        for name in ("deck.html", "notes.md", "references.md", "report.json"):
            self.assertTrue((p.output / name).exists(), name)
        cfg = load_config()
        for g in ("ingest", "plan", "organize", "factcheck", "enrich", "integrate", "compose", "review", "publish"):
            self.assertTrue(gates.check(p.dir, g, None, cfg).ok, g)
        # 花費有累計，且以設定的匯率換算
        self.assertGreater(st["cost_usd"], 0)
        report = json.loads((p.output / "report.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(report["cost_ntd"], round(st["cost_usd"] * cfg.usd_twd_rate, 2), places=2)

    def test_ingest_runs_one_worker_per_source(self):
        p = self.make_project(n_docs=3)
        self.run_pipeline(p)
        workers = p.state["stages"]["ingest"]["workers"]
        self.assertEqual(sorted(workers), ["s01", "s02", "s03"])
        self.assertTrue(all(w["status"] == "passed" for w in workers.values()))
        self.assertEqual(sorted(p.state["stages"]["verify"]["workers"]), ["enrich", "factcheck"])


class TestReruns(Base):
    """重跑與續跑不能被上一次留下的檔案或狀態騙過。"""

    def test_rerun_cannot_pass_on_files_left_by_a_previous_run(self):
        from learnloop.mock import MockRunner
        from learnloop.runner import RunResult
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")

        class DoesNothing(MockRunner):  # 每個 session 都失敗、什麼都沒寫
            async def run(self, **kw):
                return RunResult(ok=False, session_id="dead", cost_usd=0.0, subtype="error_max_turns", error="max turns")

        cfg = load_config()
        final = asyncio.run(Pipeline(Project(p.id, cfg), cfg, runner=DoesNothing(cfg)).run(from_stage="plan"))
        self.assertEqual(final, "stopped")
        st = p.state["stages"]
        self.assertNotEqual(st["plan"]["status"], "passed", "上次的 outline.json 不能讓這次的失敗變成通過")
        self.assertEqual(st["plan"]["attempts"], cfg.max_attempts)
        self.assertFalse((p.work / "outline.json").exists())
        self.assertFalse((p.work / "deck.json").exists())
        self.assertEqual(st["ingest"]["status"], "passed")  # 指定階段之前的不受影響
        self.assertTrue((p.work / "sources" / "s01.md").exists())

    def test_new_source_added_after_a_run_is_picked_up(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")
        f = self.tmp / "extra.tmp"
        f.write_text(DOC + "\n第三份，之後才加入。", encoding="utf-8")
        p.add_source("補充講義.md", f)
        mark = len(self.events(p))
        self.assertEqual(self.run_pipeline(p), "done")
        new = self.events(p)[mark:]
        self.assertTrue(next(e for e in new if e["type"] == "pipeline_started")["sources_changed"])
        ingested = [e["worker"] for e in new if e["type"] == "worker_started" and e["stage"] == "ingest"]
        self.assertEqual(ingested, ["s03"], "只有新檔案需要重新擷取")
        self.assertTrue((p.raw / "s03" / "meta.json").exists())
        self.assertIn("s03", json.dumps(json.loads((p.work / "outline.json").read_text(encoding="utf-8"))))
        self.assertIn("plan", [e["stage"] for e in new if e["type"] == "stage_started"])

    def test_source_ids_are_never_reused(self):
        p = self.make_project()
        p.remove_source("s02")
        f = self.tmp / "again.tmp"
        f.write_text(DOC, encoding="utf-8")
        self.assertEqual(p.add_source("新的.md", f)["id"], "s03")

    def test_cleared_slide_count_falls_back_to_default(self):
        p = self.make_project()
        p.update_settings({"slide_target": None})
        self.assertEqual(p.settings["slide_target"], 12)
        p.update_settings({"slide_target": ""})
        self.assertEqual(p.settings["slide_target"], 12)
        self.assertEqual(self.run_pipeline(p), "done")

    def test_skipped_factcheck_is_not_rerun_or_recharged_on_resume(self):
        # 查證被略過、補充還沒過就停止 → 續跑時「查證與補充」這個階段會再進來一次
        os.environ.update({"LP_MOCK_FAIL": "factcheck:99,enrich:99", "LP_INCIDENT_TIMEOUT": "30"})
        p = self.make_project()

        async def go():
            task = asyncio.ensure_future(Pipeline(Project(p.id, load_config())).run())
            while not task.done():
                for inc in p.incidents(pending_only=True):
                    p.respond_incident(inc["id"], "skip" if inc["target"] == "factcheck" else "stop")
                await asyncio.sleep(0.05)
            return await task

        self.assertEqual(asyncio.run(go()), "stopped")
        before = p.state["stages"]["verify"]["workers"]["factcheck"]
        self.assertEqual(before["status"], "skipped")
        self.assertNotIn("running", [s["status"] for s in p.state["stages"].values()])
        os.environ.update({"LP_MOCK_FAIL": "", "LP_INCIDENT_TIMEOUT": "1"})
        self.assertEqual(self.run_pipeline(p), "done")
        after = p.state["stages"]["verify"]["workers"]["factcheck"]
        self.assertEqual((after["status"], after["attempts"]), ("skipped", before["attempts"]))
        self.assertFalse((p.work / "factcheck.json").exists())
        self.assertIn("未經外部查證", (p.output / "deck.html").read_text(encoding="utf-8"))


class TestPreprocess(Base):
    def test_text_encodings_and_subtitles(self):
        from learnloop.preprocess import _read_text_file, _subtitle_to_md
        f = self.tmp / "utf16.txt"
        f.write_text("熱傳導係數 k", encoding="utf-16")
        self.assertEqual(_read_text_file(f), "熱傳導係數 k")
        f.write_bytes("傅立葉定律".encode("cp950"))
        self.assertEqual(_read_text_file(f), "傅立葉定律")
        srt = "1\n00:00:01,000 --> 00:00:03,000\n答案是\n42\n\n2\n00:01:05,000 --> 00:01:08,000\n<i>當 a < b 且 c > d</i> 時成立\n"
        md, segs = _subtitle_to_md(srt)
        self.assertEqual([s["start"] for s in segs], [1, 65])
        self.assertEqual(segs[0]["text"], "答案是 42")           # 純數字的字幕內容要留著
        self.assertEqual(segs[1]["text"], "當 a < b 且 c > d 時成立")  # 只去掉標記，不吃掉比較符號
        self.assertTrue(md.startswith("[00:00:01]"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
    def test_audio_goes_through_the_speech_engine_and_keeps_timestamps(self):
        from learnloop.preprocess import preprocess_source
        fake = install_fake_whisper(self.tmp)
        sys.path.insert(0, str(fake))
        sys.modules.pop("faster_whisper", None)
        try:
            audio = self.tmp / "a.mp3"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)], check=True)
            p = create_project("錄音")
            src = p.add_source("課堂錄音.mp3", audio)
            meta = preprocess_source(p, src)
        finally:
            sys.path.remove(str(fake))
            sys.modules.pop("faster_whisper", None)
        text = (p.raw / src["id"] / "transcript.md").read_text(encoding="utf-8")
        self.assertEqual(text, "[00:00:00] 今天談熱傳導\n\n[00:01:15] 接著是熱阻")
        self.assertTrue(meta["has_timestamps"])
        self.assertFalse((p.raw / src["id"] / "audio.wav").exists(), "轉檔用的暫存音軌要清掉")
        self.assertTrue(gates.check(p.dir, "preprocess", None, load_config()).ok)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
    def test_video_without_audio_track_and_reuse(self):
        from learnloop.preprocess import is_fresh, preprocess_source
        video = self.tmp / "silent.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=5:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
        p = create_project("無聲影片")
        src = p.add_source("示範.mp4", video)
        self.assertFalse(is_fresh(p, src))
        meta = preprocess_source(p, src)   # 沒有音軌不該失敗，也不需要語音辨識引擎
        self.assertEqual(meta["transcript_from"], "none")
        self.assertGreaterEqual(len(meta["frames"]), 1)
        for fr in meta["frames"]:
            self.assertTrue((p.raw / src["id"] / fr["file"]).exists())
        self.assertTrue(gates.check(p.dir, "preprocess", None, load_config()).ok)
        self.assertTrue(is_fresh(p, src))
        os.utime(p.uploads / "示範.mp4", (time.time() + 5, time.time() + 5))  # 檔案被換過
        self.assertFalse(is_fresh(p, src))


class TestOuterLoop(Base):
    ENV = {"LP_MOCK_FAIL": "compose:1"}

    def test_gate_failure_is_fed_back_and_retried(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")
        self.assertEqual(p.state["stages"]["compose"]["attempts"], 2)
        results = [e for e in self.events(p) if e["type"] == "gate_result" and e["stage"] == "compose"]
        self.assertEqual([e["ok"] for e in results], [False, True])
        self.assertIn("條列", results[0]["errors"][0])


class TestReviewLoop(Base):
    ENV = {"LP_MOCK_FAIL": "review:1"}

    def test_failed_review_sends_deck_back_then_passes(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")
        st = p.state["stages"]
        self.assertEqual(st["review"]["round"], 2)
        self.assertEqual(st["compose"]["attempts"], 2)  # 第一次編排 + 一次依審核意見修改
        types = [e["type"] for e in self.events(p)]
        self.assertIn("revision_started", types)
        review = json.loads((p.work / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["verdict"], "pass")


class TestReviewOverride(Base):
    ENV = {"LP_MOCK_FAIL": "review:9", "LP_INCIDENT_TIMEOUT": "30"}

    def test_user_can_publish_despite_failed_review_and_it_is_flagged(self):
        p = self.make_project()

        async def go():
            task = asyncio.ensure_future(Pipeline(Project(p.id, load_config())).run())
            for _ in range(400):
                pending = p.incidents(pending_only=True)
                if pending:
                    self.assertEqual(pending[0]["kind"], "review_failed")
                    p.respond_incident(pending[0]["id"], "publish")
                    break
                await asyncio.sleep(0.05)
            return await task

        self.assertEqual(asyncio.run(go()), "done")
        report = json.loads((p.output / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["flags"]["review_overridden"])
        self.assertIn("審核未通過", (p.output / "deck.html").read_text(encoding="utf-8"))


class TestIncidents(Base):
    ENV = {"LP_MOCK_FAIL": "ingest-s01:99"}

    def test_unanswered_incident_takes_the_safe_default_and_stops(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "stopped")
        inc = p.incidents()[0]
        self.assertEqual(inc["kind"], "gate_failed")
        self.assertEqual(inc["response"]["choice"], "stop")
        self.assertEqual(inc["response"]["by"], "timeout")
        self.assertEqual(p.state["stages"]["ingest"]["workers"]["s01"]["attempts"], load_config().max_attempts)
        self.assertEqual(p.state["stages"]["ingest"]["workers"]["s02"]["status"], "passed")  # 另一個 worker 不受影響

    def test_user_can_skip_a_failing_source_and_continue(self):
        os.environ["LP_INCIDENT_TIMEOUT"] = "30"
        p = self.make_project()

        async def go():
            task = asyncio.ensure_future(Pipeline(Project(p.id, load_config())).run())
            for _ in range(400):
                pending = p.incidents(pending_only=True)
                if pending:
                    self.assertEqual(p.state["status"], "waiting")
                    p.respond_incident(pending[0]["id"], "skip")
                    break
                await asyncio.sleep(0.05)
            return await task

        self.assertEqual(asyncio.run(go()), "done")
        report = json.loads((p.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["flags"]["skipped_sources"], ["s01"])
        outline = json.loads((p.work / "outline.json").read_text(encoding="utf-8"))
        self.assertNotIn("s01", json.dumps(outline))

    def test_resume_continues_from_first_unfinished_stage(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "stopped")
        os.environ["LP_MOCK_FAIL"] = ""
        self.assertEqual(self.run_pipeline(p), "done")
        started = [e["stage"] for e in self.events(p) if e["type"] == "stage_started"]
        self.assertEqual(started.count("preprocess"), 1)  # 已通過的階段不重跑
        self.assertEqual(started.count("ingest"), 2)
        self.assertEqual(p.state["stages"]["ingest"]["workers"]["s02"]["attempts"], 1)  # 已通過的 worker 也不重跑


class TestBudget(Base):
    ENV = {"LP_MOCK_COST": "0.2"}

    def test_budget_in_ntd_stops_the_run(self):
        p = self.make_project(budget_ntd=10)  # 10 元 ÷ 32 ≈ 0.31 美元，第二個 session 後就超過
        self.assertEqual(self.run_pipeline(p), "stopped")
        inc = [i for i in p.incidents() if i["kind"] == "budget_exhausted"]
        self.assertEqual(len(inc), 1)
        self.assertLessEqual(p.state["cost_usd"], 0.2 * 3)
        self.assertNotEqual(p.state["stages"]["publish"]["status"], "passed")


class TestLinkCheck(Base):
    """對本機的假網站測試連結分類（200／404／403），不需要外部網路。"""

    def setUp(self):
        super().setUp()
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                code = {"/ok": 200, "/forbidden": 403}.get(self.path.split("?")[0], 404)
                self.send_response(code)
                self.end_headers()
            do_GET = do_HEAD

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_dead_links_fail_blocked_links_warn(self):
        from learnloop.linkcheck import check_urls
        urls = [f"{self.base}/ok", f"{self.base}/missing", f"{self.base}/forbidden", "not-a-url"]
        errors, warnings, offline = check_urls(urls, self.tmp, "strict", 5, allow_private=True)
        self.assertFalse(offline)
        self.assertEqual(len(errors), 1)
        self.assertIn("/missing", errors[0])
        self.assertIn("HTTP 404", errors[0])
        self.assertEqual(len(warnings), 1)
        self.assertIn("/forbidden", warnings[0])
        # warn 模式：失效連結降為警告；off：完全不檢查
        errors, warnings, _ = check_urls(urls, self.tmp, "warn", 5, allow_private=True)
        self.assertEqual((len(errors), len(warnings)), (0, 2))
        self.assertEqual(check_urls(urls, self.tmp, "off", 5), ([], [], False))

    def test_internal_addresses_are_never_probed(self):
        from learnloop.linkcheck import check_one
        hits = []
        orig = self.httpd.RequestHandlerClass.do_HEAD
        self.httpd.RequestHandlerClass.do_HEAD = self.httpd.RequestHandlerClass.do_GET = \
            lambda h: (hits.append(h.path), orig(h))[1]
        status, detail = check_one(f"{self.base}/ok", 5)  # 模型給的網址指向本機
        self.assertEqual(status, "dead")
        self.assertIn("內部網路", detail)
        self.assertEqual(hits, [], "不應該真的連過去")
        self.assertEqual(check_one("http://169.254.169.254/latest/meta-data/", 2)[0], "dead")

    def test_non_ascii_urls_are_encoded_not_reported_dead(self):
        from learnloop.linkcheck import check_one, to_ascii_url
        self.assertEqual(to_ascii_url("https://zh.wikipedia.org/wiki/熱傳導"),
                         "https://zh.wikipedia.org/wiki/%E7%86%B1%E5%82%B3%E5%B0%8E")
        self.assertEqual(to_ascii_url("https://example.com/a%20b?q=1&r=x"), "https://example.com/a%20b?q=1&r=x")
        status, detail = check_one(f"{self.base}/ok?標題=熱傳導", 5, allow_private=True)
        self.assertEqual(status, "ok", detail)


class TestOffline(Base):
    ENV = {"LP_INCIDENT_TIMEOUT": "30"}

    def test_offline_link_check_asks_the_user_instead_of_burning_attempts(self):
        from learnloop import linkcheck
        p = self.make_project()
        cfg = load_config()
        cfg.linkcheck_mode = "strict"  # 模擬模式預設關閉連結檢查，這裡特地打開
        old = (linkcheck.check_one, linkcheck.is_online)
        linkcheck.check_one = lambda url, *a, **k: ("unreachable", "無法連線（測試）")
        linkcheck.is_online = lambda *a, **k: False

        async def go():
            task = asyncio.ensure_future(Pipeline(Project(p.id, cfg), cfg).run())
            answered = 0
            while not task.done():
                for inc in p.incidents(pending_only=True):
                    self.assertEqual(inc["kind"], "gate_unavailable")
                    p.respond_incident(inc["id"], "nolinks")
                    answered += 1
                await asyncio.sleep(0.05)
            return await task, answered

        try:
            final, answered = asyncio.run(go())
        finally:
            linkcheck.check_one, linkcheck.is_online = old
        self.assertEqual(final, "done")
        self.assertGreaterEqual(answered, 1)
        workers = p.state["stages"]["verify"]["workers"]
        self.assertEqual(workers["factcheck"]["attempts"], 1, "沒有網路時不該反覆重跑 agent")
        report = json.loads((p.output / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["flags"]["linkcheck_skipped"])
        self.assertIn("連結未經檢查", (p.output / "deck.html").read_text(encoding="utf-8"))


class TestBudgetParallel(Base):
    ENV = {"LP_MOCK_COST": "0.2", "LP_MOCK_FAIL": "ingest:1"}

    def test_parallel_workers_hitting_the_cap_ask_only_once(self):
        # 兩個擷取 worker 第一輪都沒過；第二輪開始前兩個同時發現預算用完
        p = self.make_project(budget_ntd=10)
        self.assertEqual(self.run_pipeline(p), "stopped")
        asked = [e for e in self.events(p) if e["type"] == "incident_opened"]
        self.assertEqual(len(asked), 1, [e["title"] for e in asked])
        self.assertEqual(p.incidents()[0]["kind"], "budget_exhausted")


class TestGates(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.assertEqual(self.run_pipeline(self.p), "done")
        self.cfg = load_config()

    def edit(self, name: str, fn) -> None:
        f = self.p.work / name
        data = json.loads(f.read_text(encoding="utf-8"))
        fn(data)
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def errors(self, gate: str, target: str | None = None) -> str:
        r = gates.check(self.p.dir, gate, target, self.cfg)
        self.assertFalse(r.ok, f"{gate} 應該要擋下來")
        return "\n".join(r.errors)

    def test_ingest_requires_every_heading(self):
        f = self.p.work / "sources" / "s01.md"
        f.write_text(f.read_text(encoding="utf-8").replace("## 重點整理", "## 重點"), encoding="utf-8")
        self.assertIn("缺少段落標題「## 重點整理」", self.errors("ingest", "s01"))
        self.assertTrue(gates.check(self.p.dir, "ingest", "s02", self.cfg).ok)

    def test_plan_rejects_unknown_and_unused_sources(self):
        self.edit("outline.json", lambda d: d["sections"][0].update(source_refs=["s09"]))
        self.assertIn("s09 不是這個專案的來源代號", self.errors("plan"))

        def only_first(d):
            for sec in d["sections"]:
                sec["source_refs"] = ["s01"]
        self.edit("outline.json", only_first)
        self.assertIn("教材 s02 沒有被任何章節引用", self.errors("plan"))

    def test_notes_need_citations(self):
        f = self.p.work / "notes.md"
        import re
        f.write_text(re.sub(r"\[s\d+\]", "", f.read_text(encoding="utf-8")), encoding="utf-8")
        self.assertIn("沒有標示來源", self.errors("organize"))

    def test_factcheck_needs_evidence_and_corrections(self):
        def breakit(d):
            d["claims"][0]["sources"] = []
            d["claims"][1].pop("correction", None)
        self.edit("factcheck.json", breakit)
        e = self.errors("factcheck")
        self.assertIn("卻沒有可查的來源", e)
        self.assertIn("必須填 correction", e)

    def test_material_cannot_be_cited_as_its_own_evidence(self):
        def cite_material(d):
            for c in d["claims"]:
                if c["verdict"] != "unverifiable":
                    c["sources"] = [{"title": "教材本身", "ref": "s01"}]
        self.edit("factcheck.json", cite_material)
        e = self.errors("factcheck")
        self.assertIn("不是上傳的參考資料", e)
        self.assertIn("卻沒有可查的來源", e)
        # 把 s02 標成參考資料之後，引用它才算數
        self.p.set_source_role("s02", "reference")

        def cite_reference(d):
            for c in d["claims"]:
                if c["verdict"] != "unverifiable":
                    c["sources"] = [{"title": "上傳的教科書章節", "ref": "s02"}]
        self.edit("factcheck.json", cite_reference)
        self.assertTrue(gates.check(self.p.dir, "factcheck", None, self.cfg).ok)

    def test_enrich_sources_must_be_real_references(self):
        def vague(d):
            d["supplements"][0]["sources"] = [{"title": "常識", "ref": "common knowledge"}]
        self.edit("enrich.json", vague)
        self.assertIn("必須附上出處", self.errors("enrich"))

    def test_placeholder_check_does_not_trip_on_ordinary_words(self):
        f = self.p.work / "sources" / "s01.md"
        base = f.read_text(encoding="utf-8")
        f.write_text(base + "\n講者提到 Mastodon 與 todos 清單，並引用 Stbd 與 lorem 等字。\n", encoding="utf-8")
        self.assertTrue(gates.check(self.p.dir, "ingest", "s01", self.cfg).ok)
        f.write_text(base + "\nTODO: 補上例子\n", encoding="utf-8")
        self.assertIn("佔位文字", self.errors("ingest", "s01"))

    def test_svg_gate_uses_an_allowlist_and_checks_legibility(self):
        from learnloop.svgsafe import check_svg
        head = '<svg viewBox="0 0 960 540" xmlns="http://www.w3.org/2000/svg">'
        self.assertIsNone(check_svg(head + '<g font-size="24"><text x="1" y="30">可以</text></g></svg>'))
        cases = {
            "<meta>": head + '<meta http-equiv="refresh" content="0"/><text font-size="24">x</text></svg>',
            "<div>": head + '<div><form><button formaction="javascript:alert(1)">x</button></form></div><text font-size="24">x</text></svg>',
            "style 屬性": head + '<text font-size="24" style="font-size:2px">x</text></svg>',
            "文字太小": head + '<text font-size="9">x</text></svg>',
            "外部連結": head + '<rect width="9" height="9" fill="url(https://evil.example/a.svg#p)"/><text font-size="24">x</text></svg>',
            "viewBox": '<svg xmlns="http://www.w3.org/2000/svg"><text font-size="24">x</text></svg>',
        }
        for expected, svg in cases.items():
            self.assertIn(expected, check_svg(svg) or "", expected)
        # 沒寫字級 = 瀏覽器預設 16，一樣太小
        self.assertIn("文字太小", check_svg(head + "<text>x</text></svg>"))

    def test_image_path_with_illegal_characters_is_a_content_error(self):
        def breakit(d):
            s = next(x for x in d["slides"] if x["layout"] == "diagram")
            s["visual"] = {"type": "image", "path": "raw/s01/\x00.jpg"}
        self.edit("deck.json", breakit)
        r = gates.check(self.p.dir, "compose", None, self.cfg)
        self.assertIsNone(r.system, "應該回報給 agent 修，而不是當成系統錯誤")
        self.assertIn("找不到圖片", "\n".join(r.errors))

    def test_enrich_needs_a_video(self):
        self.edit("enrich.json", lambda d: d.update(media=[]))
        self.assertIn("至少要有 1 支說明影片", self.errors("enrich"))

    def test_integrate_must_account_for_every_finding(self):
        self.edit("integration.json", lambda d: d.update(claims=[]))
        self.assertIn("沒有交代處理方式", self.errors("integrate"))

    def test_deck_limits(self):
        def breakit(d):
            s = next(x for x in d["slides"] if x["layout"] == "content")
            s["bullets"] = ["重點"] * 9
            s["notes"] = ""
            d["slides"] = [x for x in d["slides"] if x["layout"] != "quiz"]
        self.edit("deck.json", breakit)
        e = self.errors("compose")
        self.assertIn("上限 6 個", e)
        self.assertIn("講解備註", e)
        self.assertIn("layout=quiz", e)

    def test_deck_rejects_script_in_svg(self):
        def breakit(d):
            s = next(x for x in d["slides"] if x["layout"] == "diagram")
            s["visual"]["svg"] = '<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><text>x</text></svg>'
        self.edit("deck.json", breakit)
        self.assertIn("不可包含 <script>", self.errors("compose"))

    def test_deck_image_path_cannot_leave_the_project(self):
        def breakit(d):
            s = next(x for x in d["slides"] if x["layout"] == "diagram")
            s["visual"] = {"type": "image", "path": "../../../../etc/hostname"}
        self.edit("deck.json", breakit)
        self.assertIn("找不到圖片", self.errors("compose"))

    def test_review_thresholds(self):
        self.edit("review.json", lambda d: d["scores"].update(accuracy=3))
        self.assertIn("正確性評分 3 分", self.errors("review"))

    def test_unknown_urls_are_link_checked_in_strict_mode(self):
        from learnloop import linkcheck
        cfg = load_config()
        cfg.linkcheck_mode = "strict"

        def breakit(d):
            s = next(x for x in d["slides"] if x["layout"] == "references")
            s["references"].append({"title": "編出來的網址", "url": "http://learnloop-no-such-host.invalid/page"})
        self.edit("deck.json", breakit)
        old = (linkcheck.check_one, linkcheck.is_online)
        try:
            linkcheck.check_one = lambda url, *a, **k: ("unreachable", "無法連線（測試）")
            # 有網路、但這個網址連不上 → 是內容問題，要退回給 agent 改
            linkcheck.is_online = lambda *a, **k: True
            r = gates.check(self.p.dir, "compose", None, cfg)
            self.assertFalse(r.ok)
            self.assertIsNone(r.system)
            self.assertIn("連結無法開啟", "\n".join(r.errors))
            # 整台電腦沒有網路 → 是系統問題，不該叫 agent 改內容
            linkcheck.is_online = lambda *a, **k: False
            r = gates.check(self.p.dir, "compose", None, cfg)
            self.assertFalse(r.ok)
            self.assertEqual(r.system, "offline")
            self.assertNotIn("連結無法開啟", "\n".join(r.errors))
            cfg.linkcheck_mode = "warn"
            r = gates.check(self.p.dir, "compose", None, cfg)
            self.assertTrue(r.ok)
            self.assertIn("無法檢查連結", "\n".join(r.warnings))
        finally:
            linkcheck.check_one, linkcheck.is_online = old

    def test_cli_exit_codes(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        ok = subprocess.run([sys.executable, "-m", "learnloop.gates", str(self.p.dir), "compose"], env=env, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        (self.p.work / "deck.json").write_text("{not json", encoding="utf-8")
        bad = subprocess.run([sys.executable, "-m", "learnloop.gates", str(self.p.dir), "compose"], env=env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("不是合法的 JSON", bad.stderr)
        usage = subprocess.run([sys.executable, "-m", "learnloop.gates", str(self.p.dir), "nope"], env=env, capture_output=True, text=True)
        self.assertEqual(usage.returncode, 2)


class TestStopHook(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.assertEqual(self.run_pipeline(self.p), "done")

    def hook(self, stage: str | None, session: str = "sess-1", max_blocks: int = 2) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LP_") or k in ("LP_PROJECTS_DIR", "LP_MOCK")}
        if stage:
            env.update({"LP_PROJECT_DIR": str(self.p.dir), "LP_STAGE": stage, "LP_STOP_MAX_BLOCKS": str(max_blocks)})
        payload = json.dumps({"session_id": session, "hook_event_name": "Stop", "stop_hook_active": False})
        return subprocess.run(["bash", str(HOOK)], input=payload, env=env, capture_output=True, text=True, timeout=60)

    def test_passes_when_gate_passes(self):
        self.assertEqual(self.hook("compose").returncode, 0)

    def test_allows_stop_outside_the_pipeline(self):
        (self.p.work / "deck.json").unlink()
        self.assertEqual(self.hook(None).returncode, 0)

    def test_blocks_with_exit_2_then_gives_up_after_max_blocks(self):
        (self.p.work / "deck.json").unlink()
        first = self.hook("compose")
        self.assertEqual(first.returncode, 2)
        self.assertIn("deck.json 不存在", first.stderr)
        self.assertEqual(self.hook("compose").returncode, 2)
        self.assertEqual(self.hook("compose").returncode, 0, "達到攔截上限後必須放行，否則會無限迴圈")
        # 計數以 session 為單位：新的 session 重新計算
        self.assertEqual(self.hook("compose", session="sess-2").returncode, 2)

    def test_gate_exit_code_3_means_system_problem_and_is_not_blocked(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        code = ("import sys; from learnloop import gates; "
                "gates.GATES['compose'] = lambda *a: (_ for _ in ()).throw(RuntimeError('boom')); "
                "sys.exit(gates.main(sys.argv[1:]))")
        r = subprocess.run([sys.executable, "-c", code, str(self.p.dir), "compose"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("系統問題", r.stderr)

    def test_crashing_interpreter_is_not_mistaken_for_a_content_failure(self):
        fake = self.tmp / "fakepython"
        fake.write_text("#!/bin/sh\necho 'Traceback (most recent call last):' >&2\n"
                        "echo \"ModuleNotFoundError: No module named 'jsonschema'\" >&2\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        (self.p.work / "deck.json").unlink()
        os.environ["LP_PYTHON"] = str(fake)
        try:
            env = {**os.environ, "LP_PROJECT_DIR": str(self.p.dir), "LP_STAGE": "compose"}
            r = subprocess.run(["bash", str(HOOK)], input="{}", env=env, capture_output=True, text=True, timeout=60)
        finally:
            del os.environ["LP_PYTHON"]
        self.assertEqual(r.returncode, 0, "直譯器當掉時不可以把 agent 擋住")
        self.assertIn("jsonschema", (self.p.state_dir / "hook.log").read_text(encoding="utf-8"))

    def test_block_counter_restarts_each_outer_round(self):
        (self.p.work / "deck.json").unlink()
        self.assertEqual(self.hook("compose", max_blocks=1).returncode, 2)
        self.assertEqual(self.hook("compose", max_blocks=1).returncode, 0)   # 這一輪已達上限
        os.environ["LP_ROUND"] = "2"
        try:
            env = {**os.environ, "LP_PROJECT_DIR": str(self.p.dir), "LP_STAGE": "compose", "LP_STOP_MAX_BLOCKS": "1"}
            r = subprocess.run(["bash", str(HOOK)], input=json.dumps({"session_id": "sess-1"}), env=env,
                               capture_output=True, text=True, timeout=60)
        finally:
            del os.environ["LP_ROUND"]
        self.assertEqual(r.returncode, 2, "同一個 session 被 resume 進入下一輪時，hook 要重新開始把關")

    def test_broken_gate_does_not_trap_the_agent(self):
        self.assertEqual(self.hook("no-such-gate").returncode, 0)
        self.assertIn("閘門無法執行", (self.p.state_dir / "hook.log").read_text(encoding="utf-8"))


class TestRenderAndStore(Base):
    def test_svg_is_sanitised(self):
        dirty = ('<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg" onload="x()"><script>alert(1)</script>'
                 '<a href="https://evil.example"><text>gone</text></a><foreignObject><b>x</b></foreignObject>'
                 '<meta http-equiv="refresh" content="0;url=https://evil.example"/>'
                 '<div><form><button style="position:fixed;inset:0" formaction="javascript:alert(1)">x</button></form></div>'
                 '<rect width="5" height="5" fill="url(https://evil.example/x.svg#p)" style="position:fixed"/>'
                 '<defs><marker id="m"><path d="M0,0"/></marker></defs>'
                 '<line x1="0" y1="0" x2="9" y2="9" marker-end="url(#m)"/><text onclick="y()" font-size="3">hi</text></svg>')
        clean = sanitize_svg(dirty, id_prefix="fig-p3")
        for bad in ("script", "onload", "onclick", "evil.example", "foreignObject", "meta", "form", "button",
                    "style", "javascript", "gone", "<a", "<div"):
            self.assertNotIn(bad, clean)
        self.assertIn("hi", clean)
        # 圖內的 id 加上前綴並同步改寫引用：不同投影片的圖不會互撞，也蓋不到頁面本身的元素
        self.assertIn('id="fig-p3-m"', clean)
        self.assertIn("url(#fig-p3-m)", clean)
        self.assertEqual(sanitize_svg("<div>not svg</div>"), "")
        self.assertEqual(sanitize_svg("<svg><unclosed"), "")

    def test_text_is_escaped_and_bad_urls_neutralised(self):
        deck = {"meta": {"title": "<img src=x onerror=alert(1)>"}, "slides": [
            {"id": "p1", "layout": "title", "title": "<script>alert(1)</script>"},
            {"id": "p2", "layout": "media", "title": "t", "visual": {"type": "link", "url": "javascript:alert(1)", "title": "x"}}]}
        html = render_html(deck, self.tmp)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("javascript:alert", html)

    def test_filenames_and_project_listing(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\Users\\a\\課堂 錄影.mp4"), "課堂 錄影.mp4")
        p = self.make_project(n_docs=1)
        f = self.tmp / "dup.tmp"
        f.write_text("x", encoding="utf-8")
        self.assertEqual(p.add_source("講義1.md", f)["filename"], "講義1-2.md")  # 同名不覆蓋
        self.assertEqual([s["id"] for s in list_projects()], [p.id])
        with self.assertRaises(Exception):
            Project("../../etc")


class ServerBase(Base):
    """啟動真正的伺服器行程。"""

    def setUp(self):
        super().setUp()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = {**os.environ, "PYTHONPATH": str(ROOT), "LP_INCIDENT_TIMEOUT": "0",
               "LP_MOCK_DELAY": os.environ.get("LP_MOCK_DELAY_SERVER", "0.05"), **self.server_env()}
        self.proc = subprocess.Popen([sys.executable, "-m", "learnloop", "serve", "--port", str(self.port)],
                                     cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.call("GET", "/api/system")
                break
            except Exception:
                time.sleep(0.1)

    def server_env(self) -> dict:
        return {}

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        super().tearDown()

    def call(self, method: str, path: str, body=None, headers: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers={**h, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if "json" in r.headers.get("content-type", "") else payload), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def upload(self, pid: str, name: str, content: bytes):
        b = "----lpboundary"
        raw = (f'--{b}\r\nContent-Disposition: form-data; name="role"\r\n\r\nmaterial\r\n'
               f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
        return self.call("POST", f"/api/projects/{pid}/files", raw=raw,
                         headers={"Content-Type": f"multipart/form-data; boundary={b}"})

    def wait_status(self, pid: str, wanted: set[str], seconds: float = 40) -> dict:
        end = time.time() + seconds
        while time.time() < end:
            snap = self.call("GET", f"/api/projects/{pid}")[1]
            if snap["state"]["status"] in wanted:
                return snap
            time.sleep(0.2)
        self.fail(f"等不到狀態 {wanted}，目前是 {snap['state']['status']}")



class TestServer(ServerBase):
    def test_project_lifecycle(self):
        code, proj, _ = self.call("POST", "/api/projects", {"title": "API 測試"})
        self.assertEqual(code, 200)
        pid = proj["id"]
        # 草稿暫存
        code, snap, _ = self.call("PATCH", f"/api/projects/{pid}", {"title": "改過的標題", "settings": {"audience": "新進同仁", "budget_ntd": 120}})
        self.assertEqual((snap["title"], snap["settings"]["audience"], snap["budget_ntd"]), ("改過的標題", "新進同仁", 120))
        # 沒有資料不能開始
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 400)
        # 上傳：不支援的類型被拒絕，其餘加入
        code, up, _ = self.upload(pid, "講義.md", DOC.encode())
        self.assertEqual([a["id"] for a in up["added"]], ["s01"])
        code, up, _ = self.upload(pid, "virus.exe", b"MZ")
        self.assertEqual(up["rejected"][0]["reason"], "不支援的檔案類型")
        # 開始 → 完成
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 409)  # 不可重複啟動
        self.assertEqual(self.call("DELETE", f"/api/projects/{pid}")[0], 400)         # 執行中不可刪除
        snap = self.wait_status(pid, {"done"})
        self.assertEqual([o["name"] for o in snap["outputs"]][:1], ["deck.html"])
        # 成品放在沙箱裡提供
        code, body, headers = self.call("GET", f"/api/projects/{pid}/artifact/output/deck.html")
        self.assertEqual(code, 200)
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        # 路徑穿越：目標檔案都確實存在，擋得住才算數。用 http.client 送出未經正規化的原始路徑。
        import http.client
        self.assertTrue((self.tmp / "projects" / pid / "project.json").exists())
        for bad in ("output/../project.json", "work/../state.json", "output/%2e%2e/project.json",
                    "work/..%2fproject.json", "uploads/" + urllib.request.quote("講義.md"), "project.json"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", f"/api/projects/{pid}/artifact/{bad}")
            self.assertEqual(conn.getresponse().status, 404, bad)
            conn.close()
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/artifact/work/outline.json")[0], 200)
        # work/review.json 是 agent 寫得到的檔案：內容不合格式就不送到介面（避免夾帶 HTML）
        review = self.tmp / "projects" / pid / "work" / "review.json"
        self.assertIsNotNone(self.call("GET", f"/api/projects/{pid}")[1]["review"])
        data = json.loads(review.read_text(encoding="utf-8"))
        data["scores"]["accuracy"] = "<img src=x onerror=alert(1)>"
        review.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.call("GET", f"/api/projects/{pid}")[1]["review"])
        # 刪除
        self.assertEqual(self.call("DELETE", f"/api/projects/{pid}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}")[0], 404)

    def test_stop_and_incident_roundtrip(self):
        pid = self.call("POST", "/api/projects", {"title": "處置測試"})[1]["id"]
        self.upload(pid, "講義.md", DOC.encode())
        # 沒有語音辨識引擎時，音檔會開出處置單（真實行為，不是模擬）
        self.upload(pid, "錄音.mp3", b"not really audio")
        self.call("POST", f"/api/projects/{pid}/start", {})
        snap = self.wait_status(pid, {"waiting", "done"})
        if snap["state"]["status"] == "waiting":
            inc = snap["incidents"][0]
            self.assertIn(inc["kind"], ("missing_dependency", "preprocess_error"))
            self.assertEqual(self.call("POST", f"/api/projects/{pid}/incidents/{inc['id']}/respond", {"choice": "nope"})[0], 400)
            self.assertEqual(self.call("POST", f"/api/projects/{pid}/incidents/{inc['id']}/respond", {"choice": "skip"})[0], 200)
            snap = self.wait_status(pid, {"done"})
            self.assertEqual(snap["skipped_sources"], ["s02"])

    def test_cross_site_requests_are_refused(self):
        self.assertEqual(self.call("GET", "/api/projects", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/projects", {"title": "x"}, headers={"Origin": "https://evil.example"})[0], 403)
        # 同一台電腦上「別的連接埠」的網頁也不行
        self.assertEqual(self.call("POST", "/api/projects", {"title": "x"}, headers={"Origin": "http://127.0.0.1:3000"})[0], 403)
        self.assertEqual(self.call("POST", "/api/projects", {"title": "x"}, headers={"Origin": "http://localhost:3000"})[0], 403)
        self.assertEqual(self.call("POST", "/api/projects", {"title": "x"}, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("POST", "/api/projects", {"title": "x"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 200)

    def test_long_cjk_filename_is_accepted(self):
        pid = self.call("POST", "/api/projects", {"title": "長檔名"})[1]["id"]
        code, up, _ = self.upload(pid, "熱" * 100 + ".md", DOC.encode())
        self.assertEqual(code, 200)
        self.assertEqual(len(up["added"]), 1)
        self.assertLessEqual(len(up["added"][0]["filename"].encode("utf-8")), 200)


class TestServerStop(ServerBase):
    """停止：行程真的結束、狀態是 stopped、之後可以再啟動。用慢速的模擬 session 確保按下去時還在執行。"""
    ENV = {"LP_MOCK_DELAY_SERVER": "5"}

    def test_stop_really_stops(self):
        pid = self.call("POST", "/api/projects", {"title": "停止測試"})[1]["id"]
        self.upload(pid, "講義.md", DOC.encode())
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)
        end = time.time() + 20
        while time.time() < end:  # 等到第一個 agent session 開始（之後每個 session 要 5 秒）
            snap = self.call("GET", f"/api/projects/{pid}")[1]
            if snap["state"]["stages"]["ingest"]["status"] == "running":
                break
            time.sleep(0.1)
        self.assertEqual(snap["state"]["status"], "running")
        orch = snap["state"]["pid"]
        t0 = time.time()
        code, snap, _ = self.call("POST", f"/api/projects/{pid}/stop")
        self.assertEqual(code, 200)
        self.assertEqual(snap["state"]["status"], "stopped")
        self.assertIsNone(snap["state"]["pid"])
        self.assertLess(time.time() - t0, 8, "停止不該等到 session 自己跑完")
        with self.assertRaises(ProcessLookupError):
            os.kill(orch, 0)  # 行程確實不在了
        self.assertNotIn("running", [s["status"] for s in snap["state"]["stages"].values()])
        self.assertEqual(snap["incidents"], [])
        # 停止後可以從中斷處再啟動，而且不會出現兩個行程
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 409)
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/stop")[1]["state"]["status"], "stopped")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
class TestServerStopDuringPreprocess(ServerBase):
    """停止時前處理正卡在背景執行緒（這裡用一道要跑 8 秒的語音辨識指令代替）：
    行程必須馬上結束，而且不留下還在跑的子行程。"""
    MARK = "8.125"

    def server_env(self) -> dict:
        cfg = self.tmp / "config.toml"
        cfg.write_text('[transcribe]\nengine = "command"\n'
                       f'command = "sh -c \'sleep {self.MARK}; echo 逐字稿\'"\n', encoding="utf-8")
        return {"LP_CONFIG": str(cfg)}

    def test_stop_while_a_worker_thread_is_busy(self):
        audio = self.tmp / "a.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)], check=True)
        pid = self.call("POST", "/api/projects", {"title": "停止測試"})[1]["id"]
        self.upload(pid, "錄音.mp3", audio.read_bytes())
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)
        end = time.time() + 20
        while time.time() < end:  # 等到語音辨識指令真的在跑
            if subprocess.run(["pgrep", "-f", f"sleep {self.MARK}"], capture_output=True).returncode == 0:
                break
            time.sleep(0.1)
        else:
            self.fail("語音辨識指令沒有啟動")
        orch = self.call("GET", f"/api/projects/{pid}")[1]["state"]["pid"]
        t0 = time.time()
        code, snap, _ = self.call("POST", f"/api/projects/{pid}/stop")
        took = time.time() - t0
        self.assertEqual((code, snap["state"]["status"]), (200, "stopped"))
        self.assertLess(took, 4, "不該等背景工作做完才結束")
        with self.assertRaises(ProcessLookupError):
            os.kill(orch, 0)
        self.assertNotEqual(subprocess.run(["pgrep", "-f", f"sleep {self.MARK}"], capture_output=True).returncode, 0,
                            "停止後不該留下還在跑的子行程")
        raw = self.tmp / "projects" / pid / "raw" / "s01"
        time.sleep(1.0)
        self.assertFalse((raw / "transcript.md").exists(), "停止後不該再寫入檔案")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg")
class TestServerStopDuringInProcessWork(ServerBase):
    """停止時語音辨識正在行程內的背景執行緒裡跑（訊號叫不停它）：行程仍然要馬上結束。"""

    def server_env(self) -> dict:
        fake = install_fake_whisper(self.tmp)
        return {"PYTHONPATH": f"{fake}{os.pathsep}{ROOT}", "FAKE_WHISPER_SLEEP": "8",
                "FAKE_WHISPER_MARK": str(self.tmp / "asr-started")}

    def test_stop_does_not_wait_for_the_recogniser(self):
        audio = self.tmp / "a.mp3"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)], check=True)
        pid = self.call("POST", "/api/projects", {"title": "停止測試"})[1]["id"]
        self.upload(pid, "錄音.mp3", audio.read_bytes())
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)
        end = time.time() + 20
        while time.time() < end and not (self.tmp / "asr-started").exists():
            time.sleep(0.1)
        self.assertTrue((self.tmp / "asr-started").exists(), "語音辨識沒有啟動")
        orch = self.call("GET", f"/api/projects/{pid}")[1]["state"]["pid"]
        t0 = time.time()
        code, snap, _ = self.call("POST", f"/api/projects/{pid}/stop")
        self.assertEqual((code, snap["state"]["status"]), (200, "stopped"))
        self.assertLess(time.time() - t0, 4, "不該等語音辨識跑完才結束")
        with self.assertRaises(ProcessLookupError):
            os.kill(orch, 0)
        time.sleep(1.0)
        self.assertFalse((self.tmp / "projects" / pid / "raw" / "s01" / "transcript.md").exists(), "停止後不該再寫入檔案")
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/start", {})[0], 200)  # 可以再啟動
        self.call("POST", f"/api/projects/{pid}/stop")


class TestModelSelection(Base):
    """每個 agent 的模型選型：明確指定、符合任務特性，而且真的傳到 SDK 與 CLI。"""

    @staticmethod
    def tier(model: str) -> int:
        return ("haiku", "sonnet", "opus", "fable").index(model.split("-")[1])

    def test_every_agent_pins_a_model_and_an_effort(self):
        from learnloop.agents import EFFORTS, load_agents
        for name, d in load_agents().items():
            # 完整 model ID：別名會隨版本漂移；effort 明確寫：各模型的預設值不同（Opus 5.5／Haiku 5.5 是 medium，Sonnet 5.5 是 high）
            self.assertRegex(d.model or "", r"^claude-[a-z]+-\d+-\d+$", name)
            self.assertIn(d.effort, EFFORTS, name)
            for extra in (d.fallbackModel, d.visionModel):
                if extra:
                    self.assertRegex(extra, r"^claude-[a-z]+-\d+-\d+$", name)
            self.assertNotEqual(d.fallbackModel, d.model, f"{name}：備援模型不能和主要模型相同")

    def test_selection_matches_each_task(self):
        from learnloop.agents import EFFORTS, load_agents
        a = load_agents()
        producers = [n for n in a if n != "reviewer"]
        # 逐檔平行、純文字擷取：所有 agent 中最便宜的一級；但要看圖的來源會升級
        self.assertEqual(self.tier(a["ingest-document"].model), min(self.tier(d.model) for d in a.values()))
        self.assertGreater(self.tier(a["ingest-document"].visionModel), self.tier(a["ingest-document"].model))
        # 審核者是最後一道判斷：不比任何產出者弱、思考深度不低於任何產出者，而且不會悄悄換成較弱的備援模型
        self.assertGreaterEqual(self.tier(a["reviewer"].model), max(self.tier(a[n].model) for n in producers))
        self.assertGreaterEqual(EFFORTS.index(a["reviewer"].effort), max(EFFORTS.index(a[n].effort) for n in producers))
        self.assertIsNone(a["reviewer"].fallbackModel)
        # 大綱決定後面所有階段的結構：規劃者不比整理筆記的 agent 弱
        self.assertGreaterEqual(self.tier(a["planner"].model), self.tier(a["organizer"].model))

    def test_choose_model_rules(self):
        from learnloop.agents import AgentDefinition, choose_model
        d = AgentDefinition(description="", prompt="", model="claude-haiku-5-5", effort="high",
                            fallbackModel="claude-sonnet-5-5", visionModel="claude-opus-5-5", visionEffort="medium")
        c = choose_model(d)
        self.assertEqual((c.model, c.effort, c.fallback, c.vision), ("claude-haiku-5-5", "high", "claude-sonnet-5-5", False))
        c = choose_model(d, vision=True)
        self.assertEqual((c.model, c.effort, c.fallback, c.vision), ("claude-opus-5-5", "medium", "claude-sonnet-5-5", True))
        # 統一覆寫（測試省錢用）：不走看圖路線、不設備援，避免悄悄換回貴的模型；effort 照舊
        c = choose_model(d, override="claude-haiku-5-5", vision=True)
        self.assertEqual((c.model, c.effort, c.fallback, c.vision, c.override), ("claude-haiku-5-5", "high", None, False, True))
        # 備援與主要模型相同時不設（CLI 不接受）
        d.fallbackModel = "claude-opus-5-5"
        self.assertIsNone(choose_model(d, vision=True).fallback)
        with self.assertRaises(ValueError):
            from learnloop.agents import _effort
            _effort("extreme", "x.md")

    def test_pipeline_routes_each_session_to_its_model(self):
        from learnloop.mock import MockRunner
        p = self.make_project(n_docs=1)
        img = self.tmp / "board.tmp"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
        p.add_source("板書.png", img)  # 圖片：前處理標成 needs_vision
        cfg = load_config()
        runner = MockRunner(cfg)
        self.assertEqual(asyncio.run(Pipeline(Project(p.id, cfg), runner=runner).run()), "done")
        seen = {(role, target): c for role, target, c in runner.choices}
        self.assertEqual(seen[("ingest-document", "s01")].model, "claude-haiku-5-5")
        self.assertFalse(seen[("ingest-document", "s01")].vision)
        self.assertEqual(seen[("ingest-document", "s01")].fallback, "claude-sonnet-5-5")
        self.assertEqual(seen[("ingest-document", "s02")].model, "claude-opus-5-5")
        self.assertTrue(seen[("ingest-document", "s02")].vision)
        self.assertEqual((seen[("reviewer", None)].model, seen[("reviewer", None)].effort), ("claude-opus-5-5", "high"))
        self.assertEqual(seen[("fact-checker", None)].model, "claude-sonnet-5-5")
        # 狀態檔記下每個 worker 與階段實際用的模型，監控介面據此顯示
        st = Project(p.id, cfg).state["stages"]
        self.assertEqual(st["ingest"]["workers"]["s02"]["model"], "claude-opus-5-5")
        self.assertEqual(st["compose"]["model"], "claude-sonnet-5-5")
        self.assertEqual(st["plan"]["model"], "claude-opus-5-5")
        started = [e for e in self.events(p) if e["type"] == "attempt_started"]
        self.assertTrue(started and all(e.get("model") and e.get("effort") for e in started))

    def test_override_applies_to_every_session(self):
        from learnloop.mock import MockRunner
        os.environ["LP_MODEL"] = "claude-haiku-5-5"
        p = self.make_project(n_docs=1)
        img = self.tmp / "board.tmp"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
        p.add_source("板書.png", img)
        cfg = load_config()
        runner = MockRunner(cfg)
        self.assertEqual(asyncio.run(Pipeline(Project(p.id, cfg), runner=runner).run()), "done")
        self.assertTrue(all(c.model == "claude-haiku-5-5" and c.fallback is None and not c.vision
                            for _, _, c in runner.choices))

    def test_ingest_made_by_another_model_is_redone(self):
        # 先用統一覆寫的模型試跑，之後改回正式選型（文字講義用 Haiku 5.5）並加入新檔：別的模型做的整理不能沿用
        from learnloop.mock import MockRunner
        os.environ["LP_MODEL"] = "claude-sonnet-5-5"
        p = self.make_project(n_docs=1)
        self.assertEqual(self.run_pipeline(p), "done")
        os.environ.pop("LP_MODEL")
        f = self.tmp / "extra.tmp"
        f.write_text(DOC, encoding="utf-8")
        p.add_source("講義2.md", f)
        cfg = load_config()
        runner = MockRunner(cfg)
        self.assertEqual(asyncio.run(Pipeline(Project(p.id, cfg), runner=runner).run()), "done")
        redone = {t for role, t, _ in runner.choices if role == "ingest-document"}
        self.assertEqual(redone, {"s01", "s02"})
        # 模型沒變時照常沿用：再加一個檔，只整理新的那個
        g = self.tmp / "extra2.tmp"
        g.write_text(DOC, encoding="utf-8")
        p.add_source("講義3.md", g)
        runner = MockRunner(cfg)
        self.assertEqual(asyncio.run(Pipeline(Project(p.id, cfg), runner=runner).run()), "done")
        self.assertEqual({t for role, t, _ in runner.choices if role == "ingest-document"}, {"s03"})

    def test_frontmatter_comments_follow_yaml(self):
        from learnloop.agents import _parse_frontmatter
        fm, _ = _parse_frontmatter('---\nmodel: claude-haiku-5-5\t# 註解\ndescription: "a # b"\n# 整行註解\neffort: high  # x\n---\n')
        self.assertEqual(fm, {"model": "claude-haiku-5-5", "description": "a # b", "effort": "high"})

    def test_doctor_flags_an_sdk_that_does_not_know_the_models(self):
        try:
            import claude_agent_sdk
        except ImportError:
            self.skipTest("沒有安裝 claude-agent-sdk")
        from unittest import mock
        from learnloop.cli import doctor_report
        os.environ.pop("LP_MOCK")
        with mock.patch.object(claude_agent_sdk, "__version__", "0.2.159"):
            row = next(r for r in doctor_report() if r["name"] == "Claude Agent SDK")
        self.assertFalse(row["ok"])
        self.assertIn("0.2.165", row["detail"])
        with mock.patch.object(claude_agent_sdk, "__version__", "0.10.0"):
            self.assertTrue(next(r for r in doctor_report() if r["name"] == "Claude Agent SDK")["ok"])

    def test_sdk_session_gets_model_effort_and_fallback(self):
        try:
            import claude_agent_sdk as sdk
        except ImportError:
            self.skipTest("沒有安裝 claude-agent-sdk")
        from unittest import mock
        from learnloop.agents import choose_model
        from learnloop.runner import SdkRunner
        captured = []

        async def fake_query(*, prompt, options):
            captured.append(options)
            if False:
                yield None

        cfg = load_config()
        runner = SdkRunner(cfg)
        kw = dict(prompt="x", project_dir=self.tmp, budget_usd=0.5, emit=lambda *a, **k: None)
        with mock.patch.object(sdk, "query", fake_query):
            asyncio.run(runner.run(role="reviewer", gate=None, **kw))
            asyncio.run(runner.run(role="ingest-document", gate="ingest", target="s01",
                                   choice=choose_model(runner.agents["ingest-document"], vision=True), **kw))
            cfg.model_override = "claude-haiku-5-5"
            asyncio.run(runner.run(role="slide-composer", gate="compose", **kw))
        got = [(o.model, o.effort, o.fallback_model) for o in captured]
        self.assertEqual(got, [("claude-opus-5-5", "high", None),
                               ("claude-opus-5-5", "medium", "claude-sonnet-5-5"),
                               ("claude-haiku-5-5", "high", None)])
        self.assertNotIn("Write", captured[0].tools)  # 審核者仍是唯讀

    def test_headless_loop_passes_the_same_selection_to_the_cli(self):
        if not shutil.which("jq"):
            self.skipTest("沒有 jq")
        fakebin = self.tmp / "bin"
        fakebin.mkdir()
        (fakebin / "claude").write_text(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$FAKE_CLAUDE_ARGS"\n'
            'printf "%s|%s" "${CLAUDE_CODE_SESSION_ID-unset}" "${CLAUDE_CODE_REMOTE_SESSION_ID-unset}" > "$FAKE_CLAUDE_ARGS.env"\n'
            'echo \'{"session_id":"s1","subtype":"success","is_error":false,"total_cost_usd":0}\'\n', encoding="utf-8")
        (fakebin / "claude").chmod(0o755)
        proj = self.tmp / "proj"
        (proj / "raw" / "s01").mkdir(parents=True)

        def run(agent: str, gate: str, vision: bool, **env) -> list[str]:
            (proj / "raw" / "s01" / "meta.json").write_text(json.dumps({"needs_vision": vision}), encoding="utf-8")
            args = self.tmp / "args.txt"
            e = {**os.environ, "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}", "MAX_ROUNDS": "1",
                 "FAKE_CLAUDE_ARGS": str(args), "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_REMOTE_SESSION_ID": "parent",
                 "MODEL": "gpt-x", "EFFORT": "max",  # 一般名稱的環境變數不可影響選型
                 **env}
            cmd = ["bash", str(ROOT / "scripts" / "loop.sh"), str(proj), gate, agent, "任務"]
            subprocess.run(cmd + (["--target", "s01"] if gate == "ingest" else []), env=e,
                           capture_output=True, text=True, timeout=120)
            return args.read_text(encoding="utf-8").splitlines()

        def flag(argv: list[str], name: str) -> str | None:
            return argv[argv.index(name) + 1] if name in argv else None

        a = run("ingest-document", "ingest", vision=False)
        self.assertEqual((flag(a, "--model"), flag(a, "--effort"), flag(a, "--fallback-model")),
                         ("claude-haiku-5-5", "high", "claude-sonnet-5-5"))
        a = run("ingest-document", "ingest", vision=True)
        self.assertEqual((flag(a, "--model"), flag(a, "--effort")), ("claude-opus-5-5", "medium"))
        a = run("slide-composer", "compose", vision=False, LP_MODEL="claude-haiku-5-5", LP_EFFORT="low")
        self.assertEqual((flag(a, "--model"), flag(a, "--effort"), flag(a, "--fallback-model")),
                         ("claude-haiku-5-5", "low", None))
        # 在 Claude Code 裡執行時，不可沿用上層 session 的 ID
        self.assertEqual((self.tmp / "args.txt.env").read_text(encoding="utf-8"), "|")

    def test_agent_sessions_never_inherit_the_parent_session_id(self):
        # 從 Claude Code（雲端 session、/loop）裡啟動時，這兩個變數會讓所有 agent 共用上層的 session ID：
        # 平行的 agent 寫進同一份紀錄、重試時 resume 到別人的對話（實際執行時發現）
        from learnloop.runner import session_env
        env = session_env(load_config(), self.tmp, "compose", None)
        self.assertEqual(env.get("CLAUDE_CODE_SESSION_ID"), "")
        self.assertEqual(env.get("CLAUDE_CODE_REMOTE_SESSION_ID"), "")


class TestDeckLimits(Base):
    """大綱的章節數必須放得進允許的張數（實際執行時發現：期望 6 張卻排了 4 章，編排者怎麼改都過不了）。"""

    def test_chapter_cap_follows_the_slide_range(self):
        cfg = load_config()
        self.assertEqual(gates.deck_limits({"slide_target": 6}, cfg), (6, 9, 6, 2))
        self.assertEqual(gates.deck_limits({"slide_target": 12}, cfg), (6, 18, 12, 6))
        self.assertEqual(gates.deck_limits({"slide_target": 40}, cfg)[3], 8)  # 不超過大綱 schema 的 8 章
        self.assertEqual(gates.deck_limits({"slide_target": None}, cfg)[2], 12)

    def test_plan_gate_rejects_more_chapters_than_the_deck_can_hold(self):
        p = self.make_project()
        self.assertEqual(self.run_pipeline(p), "done")  # 模擬的大綱有 3 章，預設 12 張放得下
        cfg = load_config()
        p.update_settings({"slide_target": 6})          # 最多 9 張 → 只容得下 2 章
        g = gates.check(p.dir, "plan", None, cfg)
        self.assertFalse(g.ok)
        self.assertIn("只容得下 2 章", " ".join(g.errors))
        p.update_settings({"slide_target": 8})          # 最多 12 張 → 3 章
        self.assertTrue(gates.check(p.dir, "plan", None, cfg).ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
