from __future__ import annotations

import re
from collections.abc import Sequence

from app.comments.filters import normalize_text
from app.comments.scoring import is_question
from app.config import AGENT_GREETING_MAX_WORDS, AGENT_GREETING_SIM_THRESHOLD


GREETING_ANCHORS = (
    "xin chào shop", "chào chị nha", "chào anh nha", "hello mọi người",
    "hi cả nhà", "alo shop ơi", "chào buổi sáng", "chào buổi trưa",
    "chào buổi tối", "hế lô streamer",
)

_GREETING = r"(?:xin chao|chao(?: buoi (?:sang|trua|toi))?|hello|helo|hi|hi|alo|he lo)"
# Không nhận "từ bất kỳ" làm người được chào: "hi gia" (hỏi giá) không được thành greeting.
# Lời chào kèm tên riêng ("chào Lan") đi đường embedding.
_ADDRESSEE = r"(?:shop|chi|anh|em|moi nguoi|mn|ca nha|ad|ban|idol|streamer|chu|co)"
_PUNCT_RE = re.compile(r"[^\w\s]")
_PARTICLE = r"(?:a|nha|nhe|oi|ne|a|hihi|haha)"
_PURE_GREETING_RE = re.compile(
    rf"^{_GREETING}(?:\s+{_ADDRESSEE})?(?:\s+{_PARTICLE})*$", re.IGNORECASE
)


def cosine_max(vector: Sequence[float], anchors: Sequence[Sequence[float]]) -> float:
    return max((sum(a * b for a, b in zip(vector, anchor)) for anchor in anchors), default=-1.0)


def matches_greeting_pattern(text: str, max_words: int = AGENT_GREETING_MAX_WORDS) -> bool:
    """Khớp trọn mẫu chào thì không thể chứa câu hỏi ("xin chào" không phải "xin giá")."""
    normalized = normalize_text(_PUNCT_RE.sub(" ", text))
    return 0 < len(normalized.split()) <= max_words and bool(_PURE_GREETING_RE.fullmatch(normalized))


def is_pure_greeting(
    text: str,
    vector: Sequence[float] | None = None,
    anchor_vectors: Sequence[Sequence[float]] = (),
    *,
    max_words: int = AGENT_GREETING_MAX_WORDS,
    threshold: float = AGENT_GREETING_SIM_THRESHOLD,
) -> bool:
    """Nhận diện lời chào thuần; cố ý bảo thủ để tránh nuốt nhầm câu hỏi."""
    if matches_greeting_pattern(text, max_words):
        return True
    words = normalize_text(text).split()
    if not words or len(words) > max_words or is_question(text):
        return False
    return bool(vector is not None and anchor_vectors and cosine_max(vector, anchor_vectors) >= threshold)
