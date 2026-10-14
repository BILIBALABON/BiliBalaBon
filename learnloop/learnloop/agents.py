"""讀取 .claude/agents/*.md，轉成 orchestrator 用的 agent 規格，並決定每個 session 用哪個模型。

agent 的定義只寫在 Markdown 檔這一處：互動式 Claude Code、headless 的 scripts/loop.sh、
以及 Agent SDK orchestrator 三邊共用，不會各寫一份而對不起來。

模型選型也寫在同一份 frontmatter（理由見 docs/模型選型.md）：
  model          主要模型（完整 model ID，不用會隨版本漂移的別名）
  effort         思考深度 low / medium / high / xhigh / max，每個 agent 明確指定
  fallbackModel  主要模型過載或無法使用時改用的模型（可省略）
  visionModel    來源必須「看圖」時改用的模型（掃描檔、板書照片），可省略
  visionEffort   搭配 visionModel 的思考深度，省略時沿用 effort

指令列用法（給 scripts/loop.sh 用）：
  python3 -m learnloop.agents --list                        → 每個 agent 的模型選型一覽
  python3 -m learnloop.agents --json                        → 全部 agent 的 --agents JSON
  python3 -m learnloop.agents --field <名稱> prompt|tools|model|effort|maxTurns|allow
  python3 -m learnloop.agents --choose <名稱> [--vision]     → 實際要用的 {model, effort, fallback}（含 models.override／LP_MODEL）
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT

AGENTS_DIR = ROOT / ".claude" / "agents"
SHARED_RULES = ROOT / "CLAUDE.md"

# agent 只能寫入專案的 work/；Bash 只開放「跑閘門」這一道指令。
WRITE_RULES = ["Write(/work/**)", "Edit(/work/**)"]
GATE_BASH_RULE = "Bash(python3 -m learnloop.gates *)"

EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass
class AgentDefinition:
    """一個 agent 的規格。前五個欄位與 Agent SDK 的 AgentDefinition 同名；其餘是本系統的選型欄位。"""
    description: str
    prompt: str
    tools: list[str] | None = None
    model: str | None = None
    maxTurns: int | None = None
    effort: str | None = None
    fallbackModel: str | None = None
    visionModel: str | None = None
    visionEffort: str | None = None


@dataclass(frozen=True)
class ModelChoice:
    """某一次 session 實際使用的模型。"""
    model: str | None
    effort: str | None
    fallback: str | None = None
    vision: bool = False      # 是否走了 visionModel 這條路
    override: bool = False    # 是否被 models.override／LP_MODEL 統一覆寫

    def as_dict(self) -> dict:
        return {"model": self.model, "effort": self.effort, "fallback": self.fallback,
                "vision": self.vision, "override": self.override}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ValueError("缺少 frontmatter")
    end = text.find("\n---", 3)
    if end < 0:
        raise ValueError("frontmatter 沒有結尾的 ---")
    fm: dict = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        quoted = re.match(r'^(["\'])(.*?)\1(\s+#.*)?$', v)
        # 和 YAML 一樣：引號內的 # 是內容；否則「空白＋#」之後是註解（空白可以是 tab）
        fm[k.strip()] = quoted.group(2) if quoted else re.sub(r"\s+#.*$", "", v)
    body = text[end + 4:].lstrip("\n")
    return fm, body


def shared_rules() -> str:
    """CLAUDE.md 是所有 agent 的共同規則。orchestrator 的 session 以專案資料夾為工作目錄、
    不載入檔案系統設定，所以由這裡把它接在每個 agent 的 system prompt 後面。"""
    try:
        return SHARED_RULES.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _effort(value: str | None, where: str) -> str | None:
    if not value:
        return None
    if value not in EFFORTS:
        raise ValueError(f"{where}：effort 必須是 {'／'.join(EFFORTS)} 之一，目前是「{value}」")
    return value


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
            effort=_effort(fm.get("effort"), p.name),
            fallbackModel=fm.get("fallbackModel") or None,
            visionModel=fm.get("visionModel") or None,
            visionEffort=_effort(fm.get("visionEffort"), p.name),
        )
    if not out:
        raise FileNotFoundError(f"在 {agents_dir or AGENTS_DIR} 找不到任何 agent 定義")
    return out


def choose_model(defn: AgentDefinition, override: str = "", vision: bool = False) -> ModelChoice:
    """決定一個 session 用哪個模型與思考深度。

    - override（config.toml 的 models.override 或 LP_MODEL）：所有 agent 統一改用它，用於測試省錢；
      此時不走看圖路線、也不設備援模型，避免「測試時」悄悄換回貴的模型。思考深度仍照各 agent 的設定。
    - vision：來源必須看圖（掃描檔、照片）且 agent 有 visionModel 時改用它。
    - 備援模型與主要模型相同時不設（CLI 不接受兩者相同）。
    """
    if override:
        return ModelChoice(model=override, effort=defn.effort, override=True)
    use_vision = bool(vision and defn.visionModel)
    model = defn.visionModel if use_vision else defn.model
    effort = (defn.visionEffort or defn.effort) if use_vision else defn.effort
    fallback = defn.fallbackModel if defn.fallbackModel and defn.fallbackModel != model else None
    return ModelChoice(model=model, effort=effort, fallback=fallback, vision=use_vision)


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
    """Claude Code --agents 的格式（只放 CLI 認得的欄位）。"""
    return json.dumps({
        name: {k: v for k, v in {"description": d.description, "prompt": d.prompt, "tools": d.tools,
                                 "model": d.model, "effort": d.effort, "maxTurns": d.maxTurns}.items()
               if v is not None}
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
        elif f in ("model", "effort", "fallbackModel", "visionModel", "visionEffort"):
            print(getattr(d, f) or "")
        elif f == "maxTurns":
            print(d.maxTurns or "")
        elif f == "allow":
            print("\n".join(allow_rules(d)))
        else:
            return 2
        return 0
    if len(args) in (2, 3) and args[0] == "--choose" and args[1] in agents and args[2:] in ([], ["--vision"]):
        from .config import load_config  # 統一覆寫：config.toml 的 models.override 或 LP_MODEL
        choice = choose_model(agents[args[1]], load_config().model_override, vision=args[2:] == ["--vision"])
        print(json.dumps(choice.as_dict()))
        return 0
    if not args or args == ["--list"]:
        print(f"{'agent':16s} {'model':18s} {'effort':7s} {'fallback':18s} 看圖時")
        for name, d in agents.items():
            vision = f"{d.visionModel}／{d.visionEffort or d.effort}" if d.visionModel else "-"
            print(f"{name:16s} {d.model or '-':18s} {d.effort or '-':7s} {d.fallbackModel or '-':18s} {vision}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
