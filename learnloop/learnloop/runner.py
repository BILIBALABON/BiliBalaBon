"""執行單一 agent session。

SdkRunner：用 Claude Agent SDK 的 query() 啟動一個獨立 session（各有自己的 context）。
MockRunner：不呼叫模型的模擬版，用來測試迴圈、閘門與監控介面（LP_MOCK=1）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agents import AgentDefinition, allow_rules, load_agents
from .config import ROOT, Config

HOOK_SCRIPT = ROOT / ".claude" / "hooks" / "stop-gate.sh"

# 這些錯誤重試也沒用，要直接請人處理
FATAL_MARKERS = ("invalid api key", "authentication", "not logged in", "please run /login", "oauth",
                 "credit balance", "billing", "organization has been disabled", "cli not found")


@dataclass
class RunResult:
    ok: bool                       # session 正常結束（不代表閘門通過）
    session_id: str | None = None
    cost_usd: float = 0.0
    subtype: str = ""              # success / error_max_turns / error_max_budget_usd / error_during_execution…
    text: str = ""
    structured: Any = None
    error: str | None = None
    num_turns: int = 0
    fatal: bool = False            # 驗證、帳務、找不到 CLI 等重試無效的錯誤
    denials: int = 0


Emit = Callable[..., None]


def hook_settings_json(cfg: Config) -> str:
    """給 session 的 --settings：掛上 Stop hook 閘門。

    orchestrator 的 session 以專案資料夾為工作目錄，不讀 repo 的 .claude/settings.json，
    所以用絕對路徑指向同一支 hook 腳本。
    """
    return json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": f'"{HOOK_SCRIPT}"', "timeout": 300}]}]}})


def session_env(cfg: Config, project_dir: Path, gate: str | None, target: str | None,
                round_no: int = 1) -> dict[str, str]:
    env = {
        "PYTHONPATH": str(ROOT) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        # agent 與 hook 執行閘門時，要用到和 orchestrator 同一個 Python（裝了 jsonschema 的那個）
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
        "LP_PYTHON": sys.executable,
        "LP_ROUND": str(round_no),  # Stop hook 的攔截次數以「外層迴圈的每一輪」為單位重新計算
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(cfg.max_concurrent_subagents),
        "LP_STOP_MAX_BLOCKS": str(cfg.stop_hook_max_blocks),
        "LP_LINKCHECK": cfg.linkcheck_mode,
        # 沒指定閘門的 session（例如唯讀的審核者）把變數清空，hook 會直接放行
        "LP_PROJECT_DIR": str(project_dir) if gate else "",
        "LP_STAGE": gate or "",
        "LP_TARGET": target or "",
    }
    return env


class SdkRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.agents: dict[str, AgentDefinition] = load_agents()

    async def run(self, *, role: str, prompt: str, project_dir: Path, gate: str | None,
                  target: str | None = None, resume: str | None = None, budget_usd: float,
                  emit: Emit, output_schema: dict | None = None, label: str = "", round_no: int = 1) -> RunResult:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock,
                                      ToolUseBlock, query)

        defn = self.agents[role]
        options = ClaudeAgentOptions(
            system_prompt=defn.prompt,            # 角色定義來自 .claude/agents/<role>.md
            tools=list(defn.tools) if defn.tools else None,  # 限縮工具集；審核者沒有 Write／Edit／Bash
            allowed_tools=allow_rules(defn),       # dontAsk 下自動核准的規則（寫入僅限 work/）
            permission_mode="dontAsk",             # 無人值守：需要詢問的動作一律拒絕
            model=self.cfg.model_override or defn.model,
            max_turns=defn.maxTurns or self.cfg.max_turns_default,
            max_budget_usd=max(0.01, round(budget_usd, 4)),
            cwd=str(project_dir),
            setting_sources=[],                    # 不載入使用者／專案設定，行為只由這裡決定
            settings=hook_settings_json(self.cfg) if gate else None,
            env=session_env(self.cfg, project_dir, gate, target, round_no),
            resume=resume,
            output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
        )
        res = RunResult(ok=False, session_id=resume)
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            emit("agent_activity", role=role, label=label, kind="tool",
                                 text=_tool_brief(block.name, block.input))
                        elif isinstance(block, TextBlock) and block.text.strip():
                            emit("agent_activity", role=role, label=label, kind="text",
                                 text=block.text.strip().replace("\n", " ")[:240])
                elif isinstance(msg, ResultMessage):
                    res.session_id = msg.session_id or res.session_id
                    res.cost_usd = float(msg.total_cost_usd or 0.0)
                    res.subtype = msg.subtype
                    res.num_turns = msg.num_turns
                    res.text = msg.result or ""
                    res.structured = msg.structured_output
                    res.denials = len(msg.permission_denials or [])
                    res.ok = not msg.is_error
                    if msg.is_error:
                        res.error = "; ".join(msg.errors or []) or msg.subtype
        except asyncio.CancelledError:
            raise
        except Exception as e:  # SDK 在錯誤結果之後會再丟例外；ResultMessage 已先記下
            if not res.subtype:
                res.error = f"{type(e).__name__}: {e}"
                res.subtype = "error_during_execution"
            res.ok = False
        if res.error and any(m in res.error.lower() for m in FATAL_MARKERS):
            res.fatal = True
        return res


def _tool_brief(name: str, inp: dict) -> str:
    for key in ("file_path", "path", "pattern", "query", "url", "command"):
        if key in inp:
            v = str(inp[key])
            if key in ("file_path", "path"):
                v = "/".join(Path(v).parts[-3:])
            return f"{name} {v[:120]}"
    return name


def make_runner(cfg: Config):
    if cfg.mock:
        from .mock import MockRunner
        return MockRunner(cfg)
    return SdkRunner(cfg)
