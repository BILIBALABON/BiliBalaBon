"""讀取 config.toml，並提供新台幣↔美元換算。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # repo 根目錄（.claude/ 所在）

_DEFAULT_SHARE = {"ingest": 0.25, "plan": 0.08, "organize": 0.15, "verify": 0.30,
                  "integrate": 0.12, "compose": 0.20, "review": 0.12}


@dataclass
class Config:
    projects_dir: Path = ROOT / "projects"
    usd_twd_rate: float = 32.0
    default_project_budget_ntd: float = 600.0
    stage_share: dict = field(default_factory=lambda: dict(_DEFAULT_SHARE))
    max_attempts: int = 3
    max_review_rounds: int = 2
    max_parallel: int = 3
    max_concurrent_subagents: int = 4
    stop_hook_max_blocks: int = 3
    max_turns_default: int = 30
    incident_timeout_s: int = 0
    max_upload_mb: int = 500
    model_override: str = ""
    transcribe_engine: str = "faster-whisper"
    transcribe_model: str = "small"
    transcribe_language: str = ""
    transcribe_command: str = ""
    video_max_frames: int = 24
    video_scene_threshold: float = 0.30
    linkcheck_mode: str = "strict"
    linkcheck_timeout_s: int = 10
    review_min_score: int = 4
    min_claims: int = 5
    min_slides: int = 6
    max_bullets: int = 6
    max_bullet_chars: int = 80
    mock: bool = False

    def ntd_to_usd(self, ntd: float) -> float:
        return float(ntd) / self.usd_twd_rate

    def usd_to_ntd(self, usd: float) -> float:
        return float(usd) * self.usd_twd_rate


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None) -> Config:
    """讀設定檔。環境變數可覆寫少數開關（方便測試與 CI）：
    LP_MOCK=1、LP_LINKCHECK=off|warn|strict、LP_PROJECTS_DIR=<path>、LP_MODEL=<alias>、
    LP_INCIDENT_TIMEOUT=<秒>。
    """
    path = path or Path(os.environ.get("LP_CONFIG", ROOT / "config.toml"))
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    c = Config()
    g = raw.get
    paths, budget, limits = g("paths", {}), g("budget", {}), g("limits", {})
    c.projects_dir = (ROOT / paths.get("projects_dir", "projects")).resolve()
    c.usd_twd_rate = float(budget.get("usd_twd_rate", c.usd_twd_rate))
    c.default_project_budget_ntd = float(budget.get("default_project_budget_ntd", c.default_project_budget_ntd))
    c.stage_share = {**_DEFAULT_SHARE, **budget.get("stage_share", {})}
    for k in ("max_attempts", "max_review_rounds", "max_parallel", "max_concurrent_subagents",
              "stop_hook_max_blocks", "max_turns_default", "incident_timeout_s", "max_upload_mb"):
        setattr(c, k, int(limits.get(k, getattr(c, k))))
    c.model_override = str(g("models", {}).get("override", "") or "")
    t = g("transcribe", {})
    c.transcribe_engine = t.get("engine", c.transcribe_engine)
    c.transcribe_model = t.get("model", c.transcribe_model)
    c.transcribe_language = t.get("language", "") or ""
    c.transcribe_command = t.get("command", "") or ""
    v = g("video", {})
    c.video_max_frames = int(v.get("max_frames", c.video_max_frames))
    c.video_scene_threshold = float(v.get("scene_threshold", c.video_scene_threshold))
    lc = g("linkcheck", {})
    c.linkcheck_mode = lc.get("mode", c.linkcheck_mode)
    c.linkcheck_timeout_s = int(lc.get("timeout_s", c.linkcheck_timeout_s))
    c.review_min_score = int(g("review", {}).get("min_score", c.review_min_score))
    gt = g("gates", {})
    for k in ("min_claims", "min_slides", "max_bullets", "max_bullet_chars"):
        setattr(c, k, int(gt.get(k, getattr(c, k))))

    if c.usd_twd_rate <= 0:
        raise ValueError("config.toml：budget.usd_twd_rate 必須大於 0")

    # 環境變數覆寫
    if os.environ.get("LP_PROJECTS_DIR"):
        c.projects_dir = Path(os.environ["LP_PROJECTS_DIR"]).resolve()
    if os.environ.get("LP_LINKCHECK"):
        c.linkcheck_mode = os.environ["LP_LINKCHECK"]
    if os.environ.get("LP_MODEL"):
        c.model_override = os.environ["LP_MODEL"]
    if os.environ.get("LP_INCIDENT_TIMEOUT"):
        c.incident_timeout_s = int(os.environ["LP_INCIDENT_TIMEOUT"])
    c.mock = _truthy(os.environ.get("LP_MOCK"))
    if c.mock:
        c.linkcheck_mode = "off"  # 模擬內容的連結是假的，不檢查
    return c
