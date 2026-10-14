# LearnLoop：學習資料整理迴圈系統

把上課錄音、影片、演講、小組討論與講義，整理成**查證過、補充過、適合學習的簡報**。
架構是「外層迴圈 + 獨立 agent + 驗證閘門」，附本機監控介面。

```
前處理 → 資料擷取 → 規劃大綱 → 整理筆記 → 查證 ∥ 補充 → 整合修訂 → 編排簡報 → 審核 → 出版
（程式）  每檔一個 agent                        兩個 agent 平行                 ↑__退回修改__│  （程式）
```

每個階段做完都要通過檢查（閘門）才能往下走；沒過就自動重試，重試到上限就停下來問你。

## 開始使用

需要 Python 3.11 以上。音檔與影片另外需要 ffmpeg。

```bash
pip install -r requirements.txt
python3 -m learnloop doctor          # 檢查環境，標 ✗ 的要先處理
python3 -m learnloop serve           # 開啟 http://127.0.0.1:8765
```

Claude 的登入：在這台電腦執行過 `claude` 並完成 `/login`，或設定環境變數 `ANTHROPIC_API_KEY`。

**先試跑、不花錢**：`LP_MOCK=1 python3 -m learnloop serve`。模擬模式不呼叫模型，直接產生假內容，
用來熟悉介面與流程；畫面與成品都會標示「模擬」。

在介面上：新專案 → 拖進檔案（標明是教材還是參考資料）→ 填對象與學習目的 → 開始整理。
完成後在「成果」分頁開啟簡報。

## 三層把關

| 層 | 誰在檢查 | 沒過會怎樣 |
|---|---|---|
| 1. agent 自我檢查 | agent 交件前自己執行 `python3 -m learnloop.gates . <階段>`（Bash 只開放這一道指令） | 看到錯誤訊息，自己修 |
| 2. Stop hook | `.claude/hooks/stop-gate.sh`：agent 想結束時再跑一次閘門 | exit 2 擋下結束，錯誤訊息交還給 agent；同一 session 最多擋 3 次 |
| 3. 外層迴圈 | orchestrator 在 session 結束後跑閘門，以此為準 | 帶著錯誤訊息 resume 同一個 session，最多 3 輪；再不過就開處置單問你 |

閘門是確定性的程式（`learnloop/gates.py`），不是模型判斷。它檢查的內容見下表。
結束碼 1 代表內容沒過（交回給 agent 修）；3 代表檢查無法進行（沒有網路、程式出錯），這種情況不會再叫 agent 重試，直接問你。
只有最後的「審核」是由另一個唯讀的 agent 判斷內容品質，它的結論同樣要過閘門的門檻。

| 階段 | 產出 | 閘門檢查 |
|---|---|---|
| 資料擷取 | `work/sources/<sid>.md` | 固定四個段落、長度、時間點標註、沒有佔位文字 |
| 規劃大綱 | `work/outline.json` | 格式；每份教材都被引用 |
| 整理筆記 | `work/notes.md` | 每章都有、每章有來源標示 `[s01]` |
| 查證 | `work/factcheck.json` | 主張數量、每個判定都有可查的來源（網址，或你上傳的參考資料；教材不能證明自己）、錯的有更正說法、**連結連得上** |
| 補充 | `work/enrich.json` | 補充有出處、至少一支說明影片、**影片確實存在** |
| 整合修訂 | `work/notes.final.md` | 每個查證結果與補充都交代了處理方式；被推翻的內容不能原樣保留 |
| 編排簡報 | `work/deck.json` | 必備頁面、每張條列數與字數上限、講解備註、至少一張示意圖（字級夠大、只含繪圖元素）、網址都查證過 |
| 審核 | `work/review.json` | 結論通過、沒有重大問題、四項評分都 ≥ 4 |

## 模型選型

每個 agent 依自己的任務選模型與思考深度（effort），寫在 `.claude/agents/<名稱>.md` 的 frontmatter。
原則：以「每完成一件工作的成本」比較，預設用 Sonnet 5.5；逐檔平行、照格式忠實擷取的工作降到 Haiku 5.5；
產出小但影響全局、判斷本身就是工作、或品質取決於讀圖的工作升到 Opus 5.5；輸出量大的工作留在 Sonnet 5.5（Opus 的輸出單價是兩倍）。
理由、價格與實測的 token 用量見 [`docs/模型選型.md`](docs/模型選型.md)。

| agent | 模型 | effort | 為什麼 |
|---|---|---|---|
| ingest-document | Haiku 5.5（要看圖的來源改用 Opus 5.5／medium） | high | 每檔一個、平行；讀文字、照固定格式整理。掃描檔與板書照片要讀圖才升級 |
| ingest-audio | Sonnet 5.5 | medium | 要靠領域知識還原語音辨識的同音錯字 |
| ingest-video | Opus 5.5 | medium | 逐張讀關鍵畫面（投影片、板書、圖表），讀圖精準度決定品質 |
| planner | Opus 5.5 | high | 產出很小，但後面每個階段都依大綱工作 |
| organizer | Sonnet 5.5 | medium | 長篇寫作、輸出量大 |
| fact-checker | Sonnet 5.5 | high | 多輪搜尋；判定對錯是「查證過」的核心 |
| enricher | Sonnet 5.5 | medium | 搜尋與挑選 |
| integrator | Sonnet 5.5 | medium | 依規則修訂，機械部分由閘門把關 |
| slide-composer | Sonnet 5.5 | high | 產出者中輸出量最大；版面由閘門檢查、內容由 Opus 審核，high 用來計算 SVG 版面 |
| reviewer | Opus 5.5 | high | 最後一道判斷；不設備援模型，避免悄悄換成較弱的審核者 |

其餘 agent 在主要模型過載或無法使用時，自動改用 `fallbackModel`。`python3 -m learnloop.agents --list` 會印出目前的選型；
監控介面的階段詳情與即時紀錄會顯示每個 session 實際用的模型與 effort。

## 需要你決定的時候

系統遇到自己解決不了的事會暫停，並在監控介面跳出視窗，列出問題與建議的處置方式：

| 情況 | 可以選 |
|---|---|
| 某階段連續 3 輪沒過檢查 | 再試一次（可補充指示）／略過（部分階段）／停止 |
| 缺少工具（例如沒有語音辨識引擎） | 已處理好重試／略過這個檔案／停止 |
| 花費達到上限 | 追加預算／停止 |
| 審核多輪後仍未通過 | 再修改一輪／照目前版本出版（成品會標示）／停止 |
| 連不到外部網站、無法檢查連結 | 重新檢查／這次不檢查連結（成品會標示）／停止 |
| Claude 未登入或額度問題 | 處理後重試／停止 |

略過或強行出版的決定都會記在 `output/report.json`，並顯示在簡報封面。

按「停止」會保留進度，再按「開始」從中斷的階段續跑，已通過的階段不重做。
執行過後又新增、移除檔案或改了檔案用途，下次開始時會自動從頭整理；沒變的檔案不會重新轉檔，也不會重新擷取。
在流程圖點任一站可以「從這個階段重跑」，該階段與之後的產出會先清掉再重做。

## 花費

- 上限以新台幣設定（專案設定或 `config.toml`）。Claude 的介面只接受美元，系統依 `config.toml` 的匯率換算。
- 顯示的金額是 Claude Code 回報的估算值。用訂閱方案登入時，它代表用量而不是實際扣款。
- 上限是「超過就停」，不是「絕不超過」：一個 session 跑完一個回合才會比對，實測會多出幾美分。
- **花費主要來自「審核 → 退回修改」的迴圈，以及查證、補充的網路搜尋。** 實測（一頁講義，所有 agent 統一用 Haiku 5.5）：
  審核三輪仍未通過而停止，實際花費 0.66 美元，其中查證＋補充 0.43 美元大多是搜尋費用。
  依同一份 token 用量推估，預設選型約 5 美元，審核就佔一半；推估方法與各階段數字見 `docs/模型選型.md`。
  第一次使用建議先設低一點的上限跑一份短教材，看實際數字再調整。

## 設定

全部在 `config.toml`，每個值旁邊都有說明。常用的：

| 設定 | 預設 | 意義 |
|---|---|---|
| `budget.usd_twd_rate` | 32.0 | 匯率，請依現況調整 |
| `budget.default_project_budget_ntd` | 600 | 新專案的花費上限 |
| `limits.max_attempts` | 3 | 每個階段的外層迴圈輪數 |
| `limits.max_review_rounds` | 2 | 審核不過時自動退回修改的次數 |
| `limits.max_parallel` | 3 | 同時執行的 agent 數 |
| `limits.incident_timeout_s` | 0 | 等你回覆的秒數；0 = 一直等。無人值守時設成正數，逾時採用預設選項（一律是停止） |
| `models.override` | 空 | 填 `claude-haiku-5-5` 可讓所有 agent 改用最便宜的模型（測試用；同時停用備援模型與看圖路線）。環境變數 `LP_MODEL` 同義 |
| `transcribe.engine` | faster-whisper | 語音辨識引擎；`command` 可接自己的指令 |
| `linkcheck.mode` | strict | `strict` 失效連結會被擋下；`warn` 只警告；`off` 不檢查 |

agent 的角色、工具、模型與 effort 在 `.claude/agents/*.md`，共同規則在 `CLAUDE.md`。改這些檔案就能調整行為，不必動程式。

## 音檔與影片

Claude 不能直接聽或看影片，所以前處理會先轉成文字與圖片：

- 音檔 → 語音辨識 → 帶時間戳的逐字稿。
- 影片 → 抽音軌做語音辨識，並擷取關鍵畫面（場景變化時各一張，最多 24 張），讓 agent 讀投影片與板書。
- 已經有字幕？把 `.srt` 或 `.vtt` 取成和音檔／影片相同的檔名一起上傳，系統會直接採用，不做語音辨識。
- 語音辨識：`pip install faster-whisper`（第一次會下載模型；長錄音在沒有 GPU 的電腦上會很慢）。

「以書籍查證」：Claude 讀不到未公開的書。請把教科書章節或參考文件上傳並標為「參考資料」，查證時會拿來比對。

## 不透過介面使用

```bash
python3 -m learnloop new "熱傳導期中複習" 講義.pdf 課堂錄影.mp4     # 建立專案，印出專案代號
python3 -m learnloop run <專案代號>                                 # 執行（Ctrl+C 會保留進度）
python3 -m learnloop run <專案代號> --from compose                  # 從指定階段重跑
python3 -m learnloop gate <專案代號> compose                        # 手動跑某個閘門
python3 -m learnloop list
```

`scripts/loop.sh` 是同一套外層迴圈的 shell 版（`claude -p` + `--resume`），用來單獨重跑某一個階段：

```bash
scripts/loop.sh projects/<專案代號> compose slide-composer "請把 work/notes.final.md 編排成簡報，產出 work/deck.json。"
```

## 自動觸發

| 方式 | 用法 | 限制 |
|---|---|---|
| 本機收件匣（建議） | `python3 -m learnloop watch ~/學習收件匣`：裡面每個子資料夾會變成一個專案並自動執行。搭配 `incident_timeout_s` 或環境變數 `LP_INCIDENT_TIMEOUT` | 電腦要開著；遇到需要決定的事會停在那個階段等你 |
| 系統排程 | cron／工作排程器定時執行 `python3 -m learnloop watch <資料夾> --once` | 同上 |
| Claude Code `/loop` | 在開著的 session 輸入 `/loop 30m 執行 python3 -m learnloop watch inbox --once 並回報結果` | session 要開著；7 天後過期 |
| Routines（`/schedule`）、GitHub Actions | 在雲端執行，**碰不到這台電腦上的檔案與語音辨識**，不適合用來整理資料。repo 根目錄的 `.github/workflows/learnloop-*.yml` 附了兩個範例：CI 跑測試、每週檢查 CLI 相容性 | Routines 最短間隔 1 小時 |

## 安全

- agent 以 `dontAsk` 模式執行：沒有明確允許的動作一律拒絕。寫入只限專案的 `work/`；Bash 只能執行閘門指令；審核者沒有任何寫入工具。
- 有上網工具的 agent（查證、補充、審核）讀到的網頁可能夾帶指令。規則裡要求一律不照做，而且它們能寫的只有 `work/`，沒有其他執行能力。
- 監控介面只聽 `127.0.0.1`、沒有登入機制，**不要對外開放**。它會拒絕非本機 Host 與跨站來源的請求。
- 成品含有模型產生的內容，介面以沙箱方式提供；示意圖（SVG）只允許白名單內的元素與屬性，在閘門與出版時各處理一次。
- 連結檢查不會連到本機或內部網路的位址（模型給的網址可能是惡意的）。
- 上傳的教材會送到 Claude 處理。含機密或個資的資料請先確認是否適合。
- 金鑰用環境變數或 CI 的 secrets，不要寫進 repo；`.gitignore` 已排除 `.env` 與 `projects/`。

## 目錄

```
.claude/agents/*.md      10 個 agent 的角色定義與模型選型（唯一來源，orchestrator 與腳本共用）
.claude/hooks/stop-gate.sh   Stop hook 閘門
.claude/settings.json    在 repo 根目錄互動式使用 Claude Code 時載入的 hook 與拒絕規則
CLAUDE.md                所有 agent 的共同規則
config.toml              所有上限與開關
learnloop/
  pipeline.py            orchestrator：外層迴圈、平行、預算、處置單
  agents.py              讀 agent 定義；決定每個 session 用哪個模型與 effort
  runner.py              用 Agent SDK 啟動單一 session；mock.py 是不呼叫模型的模擬版
  gates.py  schemas.py   閘門與資料契約
  preprocess.py          書面擷取、語音辨識、關鍵畫面
  linkcheck.py           連結檢查
  svgsafe.py             示意圖的白名單檢查與清洗
  render.py              出版：HTML 簡報、PowerPoint、參考資料
  project.py             專案資料夾與狀態
  server.py  web/        監控介面
scripts/loop.sh          headless 外層迴圈（單一階段）
scripts/dev-loop.sh      開發這套系統本身用：在 git worktree 裡改程式直到測試通過
tests/                   自動測試（python3 -m unittest discover -s tests）
docs/                    優化後的 prompt、可行性審查、模型選型、介面截圖、一份實測產出的範例簡報
projects/<代號>/         每個專案的資料：uploads/ raw/ work/ output/ 與狀態檔
```

## 已驗證與未驗證

詳見 `docs/可行性審查.md` 第五節。重點：

- 已驗證：自動測試 69 項（GitHub Actions 的 CI 工作流程實際跑過並通過）；監控介面完整操作流程（模擬模式）；
  用最便宜的模型實際跑過完整流程；`scripts/loop.sh` 與 `scripts/dev-loop.sh` 實際執行；
  另請一個沒參與開發的 agent 做過兩輪找碴式審查（原始版本與模型選型），找到的問題都已修正並補上測試。
- 模型選型（`docs/模型選型.md`）：每個 model ID、effort、備援旗標與審核者的結構化輸出都以真實呼叫確認可用；
  選型的成品品質與實際花費沒有以真實流程量測（見該文件「實測與估算」）。
- **未驗證**：真正的語音辨識引擎、對真實網站的連結檢查與 YouTube 影片檢查（測試環境連不到外部網站）、
  預設選型的品質與實際花費、每週排程的相容性檢查工作流程、Windows、一小時以上的長教材。
