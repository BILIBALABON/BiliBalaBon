"""讀取 .claude/agents/*.md，轉成 Agent SDK 的 AgentDefinition。

agent 的定義只寫在 Markdown 檔這一處：互動式 Claude Code、headless 的 scripts/loop.sh、
以及 Agent SDK orchestrator 三邊共用，不會各寫一份而對不起來。

指令列用法（給 scripts/loop.sh 用）：
  python3 -m learnloop.agents --json            → 全部 agent 的 --agents JSON
  python3 -m learnloop.agents --field <名稱> prompt|tools|model|maxTurns|allow
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT

try:  # 模擬模式不需要 SDK，沒裝也能跑
    from claude_agent_sdk import AgentDefinition  # type: ignore
except ImportError:  # pragma: no cover
    @dataclass
    class AgentDefinition:  # type: ignore[no-redef]
        description: str
        prompt: str
        tools: list[str] | None = None
        model: str | None = None
        maxTurns: int | None = None

AGENTS_DIR = ROOT / ".claude" / "agents"
SHARED_RULES = ROOT / "CLAUDE.md"

# agent 只能寫入專案的 work/；Bash 只開放「跑閘門」這一道指令。
WRITE_RULES = ["Write(/work/**)", "Edit(/work/**)"]
GATE_BASH_RULE = "Bash(python3 -m learnloop.gates *)"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("缺少 frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("frontmatter 沒有結尾的 ---")
    fm: dict = {}
    for line in text[3:end].splitlines():
        line = line.split(" #", 1)[0].rstrip()  # 去掉行尾註解
        if not line.strip() or ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip().strip("\"'")
    body = text[end + 4:].lstrip("\n")
    return fm, body


def shared_rules() -> str:
    """CLAUDE.md 是所有 agent 的共同規則。orchestrator 的 session 以專案資料夾為工作目錄、
    不載入檔案系統設定，所以由這裡把它接在每個 agent 的 system prompt 後面。"""
    try:
        return SHARED_RULES.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def load_agents(agents_dir: Path | None = None, with_shared: bool = True) -> dict[str, AgentDefinition]:
    out: dict[str, AgentDefinition] = {}
    extra = ("\n\n---\n\n" + shared_rules()) if with_shared and shared_rules() else ""
    for p in sorted((agents_dir or AGENTS_DIR).glob("*.md")):
        fm, body = _parse_frontmatter(p.read_text(encoding="utf-8"))
        name = fm.get("name") or p.stem
        tools = [t.strip() for t in fm.get("tools", "").split(",") if t.strip()] or None
        out[name] = AgentDefinition(
            description=fm.get("description", name),
            prompt=body.strip() + extra,
            tools=tools,
            model=fm.get("model") or None,
            maxTurns=int(fm["maxTurns"]) if fm.get("maxTurns") else None,
        )
    if not out:
        raise FileNotFoundError(f"在 {agents_dir or AGENTS_DIR} 找不到任何 agent 定義")
    return out


def allow_rules(defn: AgentDefinition) -> list[str]:
    """把 agent 的工具清單換成 dontAsk 模式下的自動核准規則（最小權限）。

    Read / Grep / Glob 在工作目錄內本來就不需詢問；其他一律要有規則才會放行。
    """
    tools = set(defn.tools or [])
    rules: list[str] = []
    if tools & {"Write", "Edit"}:
        rules += WRITE_RULES
    if "Bash" in tools:
        rules.append(GATE_BASH_RULE)
    for t in ("WebSearch", "WebFetch"):
        if t in tools:
            rules.append(t)
    return rules


def to_agents_json(agents: dict[str, AgentDefinition]) -> str:
    return json.dumps({
        name: {k: v for k, v in {"description": d.description, "prompt": d.prompt, "tools": d.tools,
                                 "model": d.model, "maxTurns": d.maxTurns}.items() if v is not None}
        for name, d in agents.items()}, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    agents = load_agents()
    if args == ["--json"]:
        print(to_agents_json(agents))
        return 0
    if len(args) == 3 and args[0] == "--field" and args[1] in agents:
        d, f = agents[args[1]], args[2]
        if f == "prompt":
            print(d.prompt)
        elif f == "tools":
            print(",".join(d.tools or []))
        elif f == "model":
            print(d.model or "")
        elif f == "maxTurns":
            print(d.maxTurns or "")
        elif f == "allow":
            print("\n".join(allow_rules(d)))
        else:
            return 2
        return 0
    if not args or args == ["--list"]:
        for name, d in agents.items():
            print(f"{name:18s} {d.model or '-':8s} {','.join(d.tools or [])}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
