from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from app.config import (
    COMMENT_BLOCKLIST,
    COMMENT_DEDUP_WINDOW_SEC,
    COMMENT_MAX_CHARS,
    COMMENT_MIN_CHARS,
    COMMENT_USER_COOLDOWN_SEC,
)

URL_RE = re.compile(
    r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)+[a-z]{2,24}(?:/\S*)?",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")
REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}", re.UNICODE)


def strip_diacritics(text: str) -> str:
    """Bỏ dấu để so sánh tiếng Việt, bao gồm cả đ/Đ."""
    text = text.replace("đ", "d").replace("Đ", "D")
    return "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def normalize_text(text: str) -> str:
    normalized = strip_diacritics(text).lower()
    normalized = REPEATED_CHAR_RE.sub(r"\1", normalized)
    return WHITESPACE_RE.sub(" ", normalized).strip()


def _meaningful_text(text: str) -> str:
    """Giữ chữ/số và khoảng trắng; bỏ emoji, ký hiệu và dấu câu."""
    chars = [char for char in text if char.isalnum() or char.isspace()]
    return WHITESPACE_RE.sub(" ", "".join(chars)).strip()


class CommentFilter:
    """Bộ lọc có trạng thái cho dedup và cooldown theo người gửi."""

    def __init__(
        self,
        *,
        min_chars: int = COMMENT_MIN_CHARS,
        max_chars: int = COMMENT_MAX_CHARS,
        dedup_window_sec: float = COMMENT_DEDUP_WINDOW_SEC,
        user_cooldown_sec: float = COMMENT_USER_COOLDOWN_SEC,
        blocklist: tuple[str, ...] | list[str] = COMMENT_BLOCKLIST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_chars = max(0, min_chars)
        self.max_chars = max(1, max_chars)
        self.dedup_window_sec = max(0.0, dedup_window_sec)
        self.user_cooldown_sec = max(0.0, user_cooldown_sec)
        self.blocklist = tuple(
            normalized for word in blocklist if (normalized := normalize_text(word))
        )
        self._clock = clock
        self._seen_text: dict[str, float] = {}
        self._seen_user: dict[str, float] = {}

    @property
    def config(self) -> dict[str, Any]:
        return {
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "dedup_window_sec": self.dedup_window_sec,
            "user_cooldown_sec": self.user_cooldown_sec,
            "blocklist": list(self.blocklist),
        }

    def drop_reason(self, comment: Mapping[str, Any]) -> str | None:
        text = str(comment.get("comment") or "").strip()
        if not text:
            return "empty"

        meaningful = _meaningful_text(text)
        if not meaningful:
            return "emoji_or_punctuation_only"
        if URL_RE.search(text):
            return "link_spam"
        if len(meaningful.replace(" ", "")) < self.min_chars:
            return "too_short"
        if len(text) > self.max_chars:
            return "too_long"

        normalized = normalize_text(text)
        if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized) for word in self.blocklist):
            return "blocklist"

        now = self._clock()
        self._prune(now)
        previous = self._seen_text.get(normalized)
        if previous is not None and now - previous < self.dedup_window_sec:
            return "duplicate"

        user_key = self._user_key(comment)
        previous_user = self._seen_user.get(user_key) if user_key else None
        if previous_user is not None and now - previous_user < self.user_cooldown_sec:
            return "user_cooldown"

        self._seen_text[normalized] = now
        if user_key:
            self._seen_user[user_key] = now
        return None

    def _prune(self, now: float) -> None:
        if self.dedup_window_sec <= 0:
            self._seen_text.clear()
        else:
            cutoff = now - self.dedup_window_sec
            self._seen_text = {text: seen for text, seen in self._seen_text.items() if seen > cutoff}
        if self.user_cooldown_sec <= 0:
            self._seen_user.clear()
        else:
            cutoff = now - self.user_cooldown_sec
            self._seen_user = {user: seen for user, seen in self._seen_user.items() if seen > cutoff}

    @staticmethod
    def _user_key(comment: Mapping[str, Any]) -> str:
        user = comment.get("user") or {}
        if not isinstance(user, Mapping):
            return ""
        value = user.get("id") or user.get("unique_id")
        return str(value).lower() if value is not None else ""
