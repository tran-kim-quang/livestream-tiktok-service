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
    COMMENT_BACKLOG_SLACK_SEC,
    COMMENT_MSG_ID_TTL_SEC,
    COMMENT_SKIP_BACKLOG,
)
from app.comments.audience import user_key

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
    """Bộ lọc có trạng thái cho dedup nội dung và msg_id."""

    def __init__(
        self,
        *,
        min_chars: int = COMMENT_MIN_CHARS,
        max_chars: int = COMMENT_MAX_CHARS,
        dedup_window_sec: float = COMMENT_DEDUP_WINDOW_SEC,
        msg_id_ttl_sec: float = COMMENT_MSG_ID_TTL_SEC,
        skip_backlog: bool = COMMENT_SKIP_BACKLOG,
        backlog_slack_sec: float = COMMENT_BACKLOG_SLACK_SEC,
        blocklist: tuple[str, ...] | list[str] = COMMENT_BLOCKLIST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_chars = max(0, min_chars)
        self.max_chars = max(1, max_chars)
        self.dedup_window_sec = max(0.0, dedup_window_sec)
        self.msg_id_ttl_sec = max(0.0, msg_id_ttl_sec)
        self.skip_backlog = skip_backlog
        self.backlog_slack_sec = max(0.0, backlog_slack_sec)
        self.blocklist = tuple(
            normalized for word in blocklist if (normalized := normalize_text(word))
        )
        self._clock = clock
        self._seen_text: dict[str, float] = {}
        self._seen_msg_id: dict[str, float] = {}

    @property
    def config(self) -> dict[str, Any]:
        return {
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "dedup_window_sec": self.dedup_window_sec,
            "msg_id_ttl_sec": self.msg_id_ttl_sec,
            "skip_backlog": self.skip_backlog,
            "backlog_slack_sec": self.backlog_slack_sec,
            "blocklist": list(self.blocklist),
        }

    def drop_reason(self, comment: Mapping[str, Any]) -> str | None:
        text = str(comment.get("comment") or "").strip()
        if not text:
            return "empty"

        # Comment tạo trước lúc kết nối = backlog TikTok gửi lại → không đụng tới state khác
        if self.skip_backlog:
            created = comment.get("create_time")
            started = comment.get("session_started_at")
            if created and started and created < started - self.backlog_slack_sec * 1000:
                return "backlog"

        now = self._clock()
        self._prune_msg_ids(now)
        raw_msg_id = comment.get("msg_id")
        msg_id = str(raw_msg_id) if raw_msg_id not in (None, "") else ""
        if msg_id:
            previous_msg = self._seen_msg_id.get(msg_id)
            if previous_msg is not None and now - previous_msg < self.msg_id_ttl_sec:
                return "duplicate_id"
            if self.msg_id_ttl_sec > 0:
                self._seen_msg_id[msg_id] = now
                # TTL là giới hạn chính; chặn thêm để traffic bất thường
                # không thể làm dict tăng vô hạn trong một cửa sổ.
                while len(self._seen_msg_id) > 100_000:
                    self._seen_msg_id.pop(next(iter(self._seen_msg_id)))

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

        self._prune(now)
        previous = self._seen_text.get(normalized)
        if previous is not None and now - previous < self.dedup_window_sec:
            return "duplicate"

        self._seen_text[normalized] = now
        return None

    def _prune(self, now: float) -> None:
        if self.dedup_window_sec <= 0:
            self._seen_text.clear()
        else:
            cutoff = now - self.dedup_window_sec
            self._seen_text = {text: seen for text, seen in self._seen_text.items() if seen > cutoff}

    def _prune_msg_ids(self, now: float) -> None:
        if self.msg_id_ttl_sec <= 0:
            self._seen_msg_id.clear()
            return
        # dict giữ thứ tự chèn = thứ tự thời gian → chỉ cần bỏ từ đầu, không quét lại cả dict
        cutoff = now - self.msg_id_ttl_sec
        while self._seen_msg_id:
            oldest = next(iter(self._seen_msg_id))
            if self._seen_msg_id[oldest] > cutoff:
                break
            del self._seen_msg_id[oldest]

    @staticmethod
    def _user_key(comment: Mapping[str, Any]) -> str:
        return user_key(comment)
