from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.config import COMMENT_PRIORITY_KEYWORDS
from app.comments.filters import normalize_text

# Các trọng số tập trung ở đây để dễ tinh chỉnh.
QUESTION_MARK_SCORE = 3.0
QUESTION_WORD_SCORE = 2.0
MEANINGFUL_CHAR_SCORE = 0.04
MEANINGFUL_CHAR_CAP = 100
STREAMER_MENTION_SCORE = 1.5
PRIORITY_KEYWORD_SCORE = 1.0

QUESTION_PATTERNS = (
    re.compile(r"\bkhong\b"),
    re.compile(r"\bsao\b"),
    re.compile(r"\bgi\b"),
    re.compile(r"\bbao nhieu\b"),
    re.compile(r"\bo dau\b"),
    re.compile(r"\bkhi nao\b"),
    re.compile(r"\blam sao\b"),
    re.compile(r"\bthe nao\b"),
    re.compile(r"\bco\b.*\bkhong\b"),
    re.compile(r"\bduoc khong\b"),
    re.compile(r"\bai\b"),
)
# chỉ dùng cho is_question, không cộng điểm
_EXTRA_QUESTION_PATTERNS = (
    re.compile(r"\bchua\b"),
    re.compile(r"\bnao\b"),
    re.compile(r"\bha\b"),
    re.compile(r"\bnhi\b"),
    re.compile(r"\bxin\b"),
)


def is_question(text: str) -> bool:
    """Comment có dạng câu hỏi/yêu cầu (fallback khi không có similarity)."""
    if "?" in text:
        return True
    normalized = normalize_text(text)
    return any(p.search(normalized) for p in QUESTION_PATTERNS + _EXTRA_QUESTION_PATTERNS)


def score(
    comment: Mapping[str, Any],
    priority_keywords: Sequence[str] | None = None,
) -> float:
    """Chấm điểm đơn giản, ổn định; điểm cao hơn được phục vụ trước."""
    text = str(comment.get("comment") or "")
    normalized = normalize_text(text)
    result = min(len(normalized), MEANINGFUL_CHAR_CAP) * MEANINGFUL_CHAR_SCORE
    if "?" in text:
        result += QUESTION_MARK_SCORE
    if any(pattern.search(normalized) for pattern in QUESTION_PATTERNS):
        result += QUESTION_WORD_SCORE

    streamer = normalize_text(str(comment.get("streamer_unique_id") or ""))
    if streamer and (f"@{streamer}" in normalized or re.search(rf"\b{re.escape(streamer)}\b", normalized)):
        result += STREAMER_MENTION_SCORE

    keywords = COMMENT_PRIORITY_KEYWORDS if priority_keywords is None else priority_keywords
    normalized_keywords = [normalize_text(keyword) for keyword in keywords if keyword.strip()]
    if any(keyword in normalized for keyword in normalized_keywords):
        result += PRIORITY_KEYWORD_SCORE
    return round(result, 4)
