#!/usr/bin/env bash
# 開發這套系統本身時用的迴圈：在獨立的 git worktree 裡讓 Claude 改程式，直到測試通過。
# （內容管線不需要 worktree——各 agent 只寫自己的輸出檔；這支腳本是給「改程式碼」的任務用的。）
#
# 用法：scripts/dev-loop.sh <任務名稱> "<要做的事>"
# 例：  scripts/dev-loop.sh add-epub "讓 preprocess 支援 .epub，並補上測試"
#
# 流程：claude -p -w <任務名稱> 建立 .claude/worktrees/<任務名稱>（分支 worktree-<任務名稱>）
#       → 每輪結束跑測試 → 沒過就帶著失敗訊息 resume → 最多 MAX_ROUNDS 輪。
# -p 模式不會自動清理 worktree：通過後這裡會印出合併與清理指令；加 CLEANUP=1 則失敗時自動移除。
# worktree 建在 repo 根目錄的 .claude/worktrees/（本專案在子資料夾時也是）；實測它以目前分支在遠端的版本為基準，
# 還沒 push 的 commit 不會出現在 worktree 裡。
set -uo pipefail

[ "$#" -eq 2 ] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
NAME="$1"; TASK="$2"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_ROUNDS="${MAX_ROUNDS:-5}"; BUDGET_USD="${BUDGET_USD:-2.00}"
TEST_CMD="${TEST_CMD:-python3 -m unittest discover -s tests -q}"
cd "$ROOT"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "這裡不是 git repo，無法使用 worktree" >&2; exit 2; }
git rev-parse HEAD >/dev/null 2>&1 || { echo "repo 還沒有任何 commit，請先 commit 一次" >&2; exit 2; }
# 本專案可能放在 repo 的子資料夾（例如 learnloop/）：-w 會在 repo 根目錄建立 worktree，
# 測試與後續回合要在 worktree 裡對應本專案的那個資料夾執行
TOP="$(git rev-parse --show-toplevel)"
PREFIX="$(git rev-parse --show-prefix)"
WT="$TOP/.claude/worktrees/$NAME"
WD="$WT/$PREFIX"

SID=""; LOG="$(mktemp)"; trap 'rm -f "$LOG"' EXIT
for i in $(seq 1 "$MAX_ROUNDS"); do
  if [ -z "$SID" ]; then
    # 第一輪的 session 從 worktree 根目錄開始；本專案在子資料夾時要講清楚，路徑才不會寫錯地方（實測過）
    LOC=""; [ -n "$PREFIX" ] && LOC=$'\n\n'"本專案位於這個 repo 的 ${PREFIX%/}/ 資料夾：任務裡的路徑都相對於它，測試也在那裡執行。"
    P="$TASK$LOC"$'\n\n'"只做上述範圍。完成後執行測試：$TEST_CMD"
    EXTRA=(-w "$NAME")
  else
    P="測試仍未通過，請修正。失敗訊息："$'\n'"$(tail -n 60 "$LOG")"
    EXTRA=(--resume "$SID")
  fi
  echo "── 第 $i/$MAX_ROUNDS 輪" >&2
  # 權限：只開放編輯與「python3 -m unittest」「git diff／status」；其餘需要詢問的動作一律拒絕
  # 在 Claude Code 裡執行時清空繼承來的 session ID 變數，否則會沿用上層的 session、resume 到別人的對話
  OUT="$(cd "$([ -d "$WD" ] && echo "$WD" || echo "$ROOT")" && \
    CLAUDE_CODE_SESSION_ID= CLAUDE_CODE_REMOTE_SESSION_ID= claude -p "$P" "${EXTRA[@]}" \
      --permission-mode dontAsk --max-turns 30 --max-budget-usd "$BUDGET_USD" --output-format json ${MODEL:+--model "$MODEL"} \
      --allowedTools "Read" "Grep" "Glob" "Edit" "Write" "Bash(python3 -m unittest *)" "Bash(git diff *)" "Bash(git status *)" \
      < /dev/null)" || true
  NEW_SID="$(jq -r '.session_id // empty' <<<"$OUT" 2>/dev/null || true)"; [ -n "$NEW_SID" ] && SID="$NEW_SID"
  [ -d "$WT" ] || { echo "worktree 沒有建立成功：" >&2; printf '%s\n' "$OUT" | head -c 500 >&2; exit 1; }
  # 沒有任何變更卻「測試通過」，只代表原本的測試本來就會過，不算完成任務
  if [ -z "$(git -C "$WT" status --porcelain)" ] && [ "$(git -C "$WT" rev-parse HEAD)" = "$(git rev-parse HEAD)" ]; then
    echo "這一輪沒有產生任何變更。" > "$LOG"
    echo "   這一輪沒有產生任何變更（$(jq -r '.subtype // "claude 沒有回傳結果"' <<<"$OUT" 2>/dev/null)）" >&2
    continue
  fi
  if (cd "$WD" && $TEST_CMD) >"$LOG" 2>&1; then
    echo "PASS：第 $i 輪測試通過。變更在 $WT（分支 worktree-$NAME）"
    echo "  檢視：git -C \"$WT\" status && git -C \"$WT\" diff"
    echo "  採用：在 worktree 內 commit 後，回主目錄 git merge worktree-$NAME"
    echo "  清理：git worktree unlock \"$WT\"; git worktree remove \"$WT\" && git branch -d worktree-$NAME"
    echo "       （-p 模式建立的 worktree 會被鎖定且不會自動清理；有未 commit 的變更時 remove 要加 --force）"
    exit 0
  fi
  tail -n 15 "$LOG" | sed 's/^/   /' >&2
done

echo "FAIL：$MAX_ROUNDS 輪後測試仍未通過。" >&2
if [ "${CLEANUP:-0}" = "1" ]; then
  git worktree unlock "$WT" 2>/dev/null
  git worktree remove --force "$WT" && git branch -D "worktree-$NAME" >/dev/null 2>&1
  echo "已移除 worktree。" >&2
else
  echo "worktree 保留在 $WT；不要了請執行：git worktree unlock \"$WT\"; git worktree remove --force \"$WT\" && git branch -D worktree-$NAME" >&2
fi
exit 1
