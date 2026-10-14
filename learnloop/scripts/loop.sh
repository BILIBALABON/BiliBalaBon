#!/usr/bin/env bash
# 外層迴圈（headless 版）：對「一個階段」重複執行 claude -p，直到驗證閘門通過或用完輪數。
#
# 這是 Python orchestrator（learnloop/pipeline.py）同一套迴圈的最小 shell 版本，
# 適合在 CI 或沒有監控介面時，手動重跑某一個階段。
#
# 用法：
#   scripts/loop.sh <專案資料夾> <閘門> <agent> "<任務說明>" [--target <代號>]
# 例：
#   scripts/loop.sh projects/20260101-120000-ab12 compose slide-composer \
#       "請把 work/notes.final.md 編排成簡報，產出 work/deck.json。"
#   scripts/loop.sh projects/20260101-120000-ab12 ingest ingest-document \
#       "請整理來源 s01，產出 work/sources/s01.md。" --target s01
#
# 環境變數：
#   MAX_ROUNDS=3      外層迴圈輪數上限
#   BUDGET_USD=1.00   每一輪的 --max-budget-usd（CLI 只收美元）
#   LP_MODEL=         統一覆寫模型（測試省錢用；同時停用備援模型與看圖路線）。也可寫在 config.toml 的 models.override
#   LP_EFFORT=        覆寫 agent 檔指定的思考深度（low / medium / high / xhigh / max）
#
# 模型、思考深度與備援模型照 .claude/agents/<agent>.md 的選型（見 docs/模型選型.md）；
# 擷取階段的來源若需要看圖（raw/<代號>/meta.json 的 needs_vision），會改用該 agent 的 visionModel。
#
# 三道上限：--max-turns（單輪回合數）、--max-budget-usd（單輪花費）、MAX_ROUNDS（輪數）。
# 在 Claude Code 裡執行這支腳本時，清空繼承來的 session ID 變數，確保每次都是自己的 session。
set -uo pipefail

if [ "$#" -lt 4 ]; then
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="$(cd "$1" && pwd)" || { echo "找不到專案資料夾：$1" >&2; exit 2; }
GATE="$2"; AGENT="$3"; TASK="$4"; shift 4
TARGET=""
if [ "${1:-}" = "--target" ]; then TARGET="${2:?--target 需要代號}"; fi

MAX_ROUNDS="${MAX_ROUNDS:-3}"
BUDGET_USD="${BUDGET_USD:-1.00}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

for bin in claude jq python3; do
  command -v "$bin" >/dev/null 2>&1 || { echo "缺少指令：$bin" >&2; exit 2; }
done

# agent 定義只有一份（.claude/agents/<agent>.md），這裡取出 system prompt、工具與核准規則
SYS_PROMPT="$(python3 -m learnloop.agents --field "$AGENT" prompt)" || { echo "沒有這個 agent：$AGENT" >&2; exit 2; }
TOOLS="$(python3 -m learnloop.agents --field "$AGENT" tools)"
VISION=""
if [ -n "$TARGET" ] && [ "$(jq -r '.needs_vision // false' "$PROJECT_DIR/raw/$TARGET/meta.json" 2>/dev/null)" = "true" ]; then
  VISION=1
fi
CHOICE="$(python3 -m learnloop.agents --choose "$AGENT" ${VISION:+--vision})" || exit 2
MODEL="$(jq -r '.model // empty' <<<"$CHOICE")"
EFFORT="${LP_EFFORT:-$(jq -r '.effort // empty' <<<"$CHOICE")}"
FALLBACK="$(jq -r '.fallback // empty' <<<"$CHOICE")"
MAX_TURNS="$(python3 -m learnloop.agents --field "$AGENT" maxTurns)"
ALLOW=()
while IFS= read -r rule; do [ -n "$rule" ] && ALLOW+=("$rule"); done < <(python3 -m learnloop.agents --field "$AGENT" allow)

# Stop hook 閘門：以絕對路徑掛上，因為 session 的工作目錄是專案資料夾而不是 repo
SETTINGS="$(jq -cn --arg c "\"$ROOT/.claude/hooks/stop-gate.sh\"" \
  '{hooks:{Stop:[{hooks:[{type:"command",command:$c,timeout:300}]}]}}')"

run_gate() {
  python3 -m learnloop.gates "$PROJECT_DIR" "$GATE" ${TARGET:+--target "$TARGET"}
}

ERR_FILE="$(mktemp)"; trap 'rm -f "$ERR_FILE"' EXIT
GATE_CMD="python3 -m learnloop.gates . $GATE${TARGET:+ --target $TARGET}"
SID=""; TOTAL_COST=0

for i in $(seq 1 "$MAX_ROUNDS"); do
  if [ -z "$SID" ]; then
    PROMPT="$TASK"$'\n\n'"完成後執行閘門確認：$GATE_CMD"
  else
    # 把上一輪閘門的錯誤訊息帶回去，而不是只說「還是失敗」
    PROMPT="驗證閘門尚未通過。請修正下列問題，修完後再執行一次閘門確認（$GATE_CMD）："$'\n'"$(cat "$ERR_FILE")"
  fi

  echo "── 第 $i/$MAX_ROUNDS 輪：$AGENT（${MODEL:-預設模型}／effort ${EFFORT:-預設}）→ 閘門 $GATE${TARGET:+（$TARGET）}" >&2
  OUT="$(cd "$PROJECT_DIR" && \
    LP_PROJECT_DIR="$PROJECT_DIR" LP_STAGE="$GATE" LP_TARGET="$TARGET" LP_ROUND="$i" \
    CLAUDE_CODE_SESSION_ID= CLAUDE_CODE_REMOTE_SESSION_ID= \
    claude -p "$PROMPT" ${SID:+--resume "$SID"} \
      --system-prompt "$SYS_PROMPT" \
      --tools "$TOOLS" \
      --permission-mode dontAsk \
      --max-turns "${MAX_TURNS:-30}" \
      --max-budget-usd "$BUDGET_USD" \
      ${MODEL:+--model "$MODEL"} \
      ${EFFORT:+--effort "$EFFORT"} \
      ${FALLBACK:+--fallback-model "$FALLBACK"} \
      --setting-sources "" \
      --settings "$SETTINGS" \
      --output-format json \
      ${ALLOW[@]+--allowedTools "${ALLOW[@]}"} < /dev/null)" || true

  NEW_SID="$(jq -r '.session_id // empty' <<<"$OUT" 2>/dev/null || true)"
  [ -n "$NEW_SID" ] && SID="$NEW_SID"
  COST="$(jq -r '.total_cost_usd // 0' <<<"$OUT" 2>/dev/null || echo 0)"
  SUBTYPE="$(jq -r '.subtype // "no_result"' <<<"$OUT" 2>/dev/null || echo no_result)"
  TOTAL_COST="$(python3 -c "print(round($TOTAL_COST + ${COST:-0}, 4))")"
  echo "   session 結束：$SUBTYPE，本輪 US\$$COST，累計 US\$$TOTAL_COST" >&2
  if [ "$SUBTYPE" = "no_result" ]; then
    echo "   claude 沒有回傳結果（未登入、CLI 版本不符或網路問題）：" >&2
    printf '%s\n' "$OUT" | head -c 600 >&2; echo >&2
  fi

  run_gate 2>"$ERR_FILE"; GATE_RC=$?
  if [ "$GATE_RC" -eq 0 ]; then
    echo "PASS：第 $i 輪通過閘門 $GATE（累計 US\$$TOTAL_COST）"
    exit 0
  fi
  sed 's/^/   /' "$ERR_FILE" >&2
  if [ "$GATE_RC" -ne 1 ]; then
    # 結束碼 3（沒有網路、閘門程式出錯）或其他：不是 agent 改得了的，不要再花錢重試
    echo "STOP：檢查無法進行（結束碼 $GATE_RC），這不是內容問題。排除後再重跑。" >&2
    exit 3
  fi
done

echo "FAIL：$MAX_ROUNDS 輪後仍未通過閘門 $GATE（累計 US\$$TOTAL_COST）" >&2
exit 1
