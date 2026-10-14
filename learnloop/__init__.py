"""LearnLoop：外層迴圈 + 獨立 agent + 驗證閘門 的學習資料整理系統。"""

__version__ = "1.0.0"

# 管線階段（順序即執行順序）。verify = 查證 + 補充，兩個 agent 平行。
STAGES = ["preprocess", "ingest", "plan", "organize", "verify", "integrate", "compose", "review", "publish"]

STAGE_LABELS = {
    "preprocess": "前處理",
    "ingest": "資料擷取",
    "plan": "規劃大綱",
    "organize": "整理筆記",
    "verify": "查證與補充",
    "integrate": "整合修訂",
    "compose": "編排簡報",
    "review": "審核",
    "publish": "出版",
}
