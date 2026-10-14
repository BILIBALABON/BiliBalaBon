#!/usr/bin/env bash
# Stop hook 驗證閘門：agent 想結束時先跑該階段的閘門，沒過就擋下來（exit 2），
# 並把錯誤訊息經 stderr 交還給 agent 繼續修。
#
# 由誰告訴它要檢查什麼：orchestrator／scripts/loop.sh 啟動 session 時設定環境變數
#   LP_PROJECT_DIR  專案資料夾（必要）
#   LP_STAGE        閘門名稱，如 ingest、compose（必要）
#   LP_TARGET       閘門對象，如 s01（選用）
#   LP_STOP_MAX_BLOCKS  同一個 session、同一輪最多攔截幾次，預設 3
#   LP_ROUND        外層迴圈第幾輪（選用）。每一輪重新計算攔截次數
#   LP_PYTHON       執行閘門用的 Python（選用，預設 python3）
# 沒設定 LP_PROJECT_DIR／LP_STAGE（例如平常互動式開發）→ 直接放行。
#
# 退出條件（缺一不可，否則會無限迴圈）：
#   1. 閘門通過 → 放行
#   2. 這一輪已攔截 LP_STOP_MAX_BLOCKS 次 → 放行，交給外層迴圈處理
#   3. 檢查無法進行（找不到 python、缺套件而當掉、沒有網路、專案資料夾不存在）→ 放行並留下紀錄，不讓 agent 卡死
#      判斷方式：只有「結束碼 1 而且訊息裡有『閘門未通過』」才算內容沒過；
#      Python 自己當掉時結束碼也是 1，但不會有這行字。
#   另外 Claude Code 本身也會在連續攔截 8 次後強制放行。
set -u

INPUT="$(cat)"

[ -n "${LP_PROJECT_DIR:-}" ] && [ -n "${LP_STAGE:-}" ] || exit 0
[ -d "$LP_PROJECT_DIR" ] || exit 0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${LP_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || exit 0

MAX="${LP_STOP_MAX_BLOCKS:-3}"
SID="$(printf '%s' "$INPUT" | "$PY" -c 'import sys,json
try: print(json.load(sys.stdin).get("session_id","") or "nosession")
except Exception: print("nosession")' 2>/dev/null)"
SID="${SID//[^A-Za-z0-9_-]/}"

STATE_DIR="$LP_PROJECT_DIR/.state"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
ROUND="${LP_ROUND:-1}"; ROUND="${ROUND//[^0-9]/}"
COUNT_FILE="$STATE_DIR/stop-${LP_STAGE}${LP_TARGET:+-$LP_TARGET}-${SID:-nosession}-r${ROUND:-1}.count"
N="$(cat "$COUNT_FILE" 2>/dev/null || echo 0)"
case "$N" in ''|*[!0-9]*) N=0 ;; esac

ERR="$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m learnloop.gates "$LP_PROJECT_DIR" "$LP_STAGE" \
        ${LP_TARGET:+--target "$LP_TARGET"} 2>&1 >/dev/null)"
RC=$?

if [ "$RC" -eq 0 ]; then
  rm -f "$COUNT_FILE"
  exit 0
fi

case "$ERR" in *"閘門未通過"*) CONTENT_FAIL=1 ;; *) CONTENT_FAIL=0 ;; esac
if [ "$RC" -ne 1 ] || [ "$CONTENT_FAIL" -ne 1 ]; then
  # 閘門用法錯誤、當掉或無法進行：不是內容問題，放行並記錄
  printf '%s stop-gate: 閘門無法執行（rc=%s）：%s\n' "$(date '+%F %T')" "$RC" "$ERR" >> "$STATE_DIR/hook.log"
  exit 0
fi

if [ "$N" -ge "$MAX" ]; then
  printf '%s stop-gate: %s 已攔截 %s 次仍未通過，放行交給外層迴圈\n' "$(date '+%F %T')" "$LP_STAGE" "$N" >> "$STATE_DIR/hook.log"
  exit 0
fi

echo $((N + 1)) > "$COUNT_FILE"
{
  echo "驗證閘門未通過，還不能結束（第 $((N + 1))/$MAX 次提醒）。請依下列訊息修正後再結束："
  echo "$ERR"
  echo "修正時不要刪除內容或填入假資料來閃避檢查。"
} >&2
exit 2
