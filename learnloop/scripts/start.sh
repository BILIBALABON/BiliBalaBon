#!/usr/bin/env bash
# 啟動監控介面。用法：scripts/start.sh [port]      模擬模式：LP_MOCK=1 scripts/start.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 -m learnloop doctor || echo "（有必要項目未就緒，請先處理上面標 ✗ 的項目）"
exec python3 -m learnloop serve --port "${1:-8765}"
