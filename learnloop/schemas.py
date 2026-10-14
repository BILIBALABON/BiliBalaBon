"""各階段產出的 JSON Schema —— 這是 agent 與閘門之間的資料契約。"""
from __future__ import annotations

_STR = {"type": "string", "minLength": 1}
_SEC = {"type": "string", "pattern": r"^sec\d+$"}

_SOURCE = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": _STR,
        "url": {"type": "string"},          # 網路來源
        "ref": {"type": "string"},          # 或使用者上傳的參考資料代號，如 s03
        "quote": {"type": "string"},
        "accessed": {"type": "string"},
    },
    "additionalProperties": False,
}

OUTLINE = {
    "type": "object",
    "required": ["title", "audience", "objectives", "sections"],
    "properties": {
        "title": _STR,
        "audience": _STR,
        "language": {"type": "string"},
        "objectives": {"type": "array", "minItems": 2, "items": _STR},
        "sections": {
            "type": "array", "minItems": 2,
            "items": {
                "type": "object",
                "required": ["id", "title", "key_points", "source_refs"],
                "properties": {
                    "id": _SEC, "title": _STR,
                    "key_points": {"type": "array", "minItems": 1, "items": _STR},
                    "source_refs": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": r"^s\d+$"}},
                },
                "additionalProperties": False,
            },
        },
        "glossary": {"type": "array", "items": {
            "type": "object", "required": ["term", "definition"],
            "properties": {"term": _STR, "definition": _STR}, "additionalProperties": False}},
        "open_questions": {"type": "array", "items": _STR},
    },
    "additionalProperties": False,
}

FACTCHECK = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "section_id", "claim", "verdict", "confidence", "explanation", "sources"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^c\d+$"},
                    "section_id": _SEC,
                    "claim": _STR,
                    "verdict": {"enum": ["supported", "partially_supported", "refuted", "unverifiable"]},
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "explanation": _STR,
                    "correction": {"type": "string"},
                    "sources": {"type": "array", "items": _SOURCE},
                },
                "additionalProperties": False,
            },
        },
        "summary": {"type": "object"},
    },
    "additionalProperties": False,
}

ENRICH = {
    "type": "object",
    "required": ["supplements", "media"],
    "properties": {
        "supplements": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "section_id", "type", "title", "content", "sources"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^e\d+$"},
                    "section_id": _SEC,
                    "type": {"enum": ["background", "definition", "example", "application", "misconception", "further_reading"]},
                    "title": _STR, "content": _STR,
                    "sources": {"type": "array", "minItems": 1, "items": _SOURCE},
                },
                "additionalProperties": False,
            },
        },
        "media": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "section_id", "kind", "title", "url", "why"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^m\d+$"},
                    "section_id": _SEC,
                    "kind": {"enum": ["video", "article", "interactive"]},
                    "title": _STR, "url": _STR, "why": _STR,
                    "start_seconds": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

INTEGRATION = {
    "type": "object",
    "required": ["claims", "supplements"],
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object", "required": ["id", "action"],
            "properties": {"id": {"type": "string"}, "action": {"enum": ["corrected", "removed", "flagged", "kept"]},
                           "note": {"type": "string"}},
            "additionalProperties": False}},
        "supplements": {"type": "array", "items": {
            "type": "object", "required": ["id", "action"],
            "properties": {"id": {"type": "string"}, "action": {"enum": ["inserted", "skipped"]},
                           "note": {"type": "string"}},
            "additionalProperties": False}},
    },
    "additionalProperties": False,
}

_BULLETS = {"type": "array", "items": _STR}
_VISUAL = {
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {"enum": ["svg", "image", "video", "link"]},
        "svg": {"type": "string"},      # type=svg：完整的 <svg>…</svg>
        "path": {"type": "string"},     # type=image：專案內相對路徑，如 raw/s02/frames/f_000123.jpg
        "url": {"type": "string"},      # type=video / link
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "start_seconds": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

DECK = {
    "type": "object",
    "required": ["meta", "slides"],
    "properties": {
        "meta": {
            "type": "object", "required": ["title"],
            "properties": {"title": _STR, "subtitle": {"type": "string"}, "language": {"type": "string"},
                           "audience": {"type": "string"}},
            "additionalProperties": False,
        },
        "slides": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "layout", "title"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^p\d+$"},
                    "layout": {"enum": ["title", "agenda", "content", "diagram", "media", "comparison",
                                        "quiz", "summary", "references"]},
                    "section_id": _SEC,
                    "title": _STR,
                    "subtitle": {"type": "string"},
                    "bullets": _BULLETS,
                    "notes": {"type": "string"},
                    "visual": _VISUAL,
                    "columns": {"type": "array", "minItems": 2, "maxItems": 3, "items": {
                        "type": "object", "required": ["heading", "bullets"],
                        "properties": {"heading": _STR, "bullets": _BULLETS}, "additionalProperties": False}},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": _STR},
                    "answer_index": {"type": "integer", "minimum": 0},
                    "explanation": {"type": "string"},
                    "references": {"type": "array", "items": {
                        "type": "object", "required": ["title"],
                        "properties": {"title": _STR, "url": {"type": "string"}}, "additionalProperties": False}},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

# 審核結果：由審核 agent 以「結構化輸出」回傳，orchestrator 代為寫成 work/review.json。
# 結構化輸出不支援 minimum/maximum 等數值限制，分數範圍改由閘門檢查。
REVIEW = {
    "type": "object",
    "required": ["verdict", "scores", "issues", "summary"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "revise"]},
        "scores": {
            "type": "object",
            "required": ["accuracy", "completeness", "readability", "pedagogy"],
            "properties": {k: {"type": "integer"} for k in ("accuracy", "completeness", "readability", "pedagogy")},
            "additionalProperties": False,
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "scope", "ref", "problem", "fix"],
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "scope": {"type": "string", "enum": ["deck", "notes"]},
                    "ref": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}
