from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from app.comments.filters import CommentFilter
from app.comments.scoring import score
from app.config import (
    COMMENT_BUFFER_SIZE,
    COMMENT_FRESH_WINDOW_SEC,
    COMMENT_MAX_AGE_SEC,
    COMMENT_MAX_SERVE_PER_SEC,
    COMMENT_PRIORITY_KEYWORDS,
)


class CommentBuffer:
    """Buffer giới hạn trong RAM, ưu tiên comment đáng trả lời và còn mới."""

    def __init__(
        self,
        *,
        comment_filter: CommentFilter | None = None,
        buffer_size: int = COMMENT_BUFFER_SIZE,
        max_age_sec: float = COMMENT_MAX_AGE_SEC,
        fresh_window_sec: float = COMMENT_FRESH_WINDOW_SEC,
        max_serve_per_sec: float = COMMENT_MAX_SERVE_PER_SEC,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        scorer: Callable[[Mapping[str, Any]], float] = score,
    ) -> None:
        self.filter = comment_filter or CommentFilter(clock=clock)
        self.buffer_size = max(1, buffer_size)
        self.max_age_sec = max(0.0, max_age_sec)
        self.fresh_window_sec = max(0.0, min(fresh_window_sec, self.max_age_sec))
        self.max_serve_per_sec = max(0.0, max_serve_per_sec)
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._scorer = scorer
        self._items: list[tuple[float, int, dict[str, Any]]] = []
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._received = 0
        self._accepted = 0
        self._served = 0
        self._expired = 0
        self._dropped: Counter[str] = Counter()
        self._last_served_at: float | None = None

    async def add(self, payload: Mapping[str, Any]) -> bool:
        """Lọc trên receipt; trả True khi comment được giữ lại."""
        async with self._condition:
            self._received += 1
            reason = self.filter.drop_reason(payload)
            if reason is not None:
                self._dropped[reason] += 1
                return False

            now = self._clock()
            record = {
                "comment": payload.get("comment"),
                "user": payload.get("user"),
                "streamer_unique_id": payload.get("streamer_unique_id"),
                "room_id": payload.get("room_id"),
                "enqueued_at": self._wall_clock().isoformat(),
                "score": self._scorer(payload),
            }
            self._sequence += 1
            self._items.append((now, self._sequence, record))
            self._accepted += 1
            if len(self._items) > self.buffer_size:
                self._items.pop(0)
                self._dropped["buffer_full"] += 1
            self._condition.notify_all()
            return True

    async def next(self, timeout: float = 0) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout > 0 else None
        async with self._condition:
            while True:
                now = self._clock()
                self._discard_expired(now)
                rate_wait = self._rate_wait(now)
                candidates = [item for item in self._items if now - item[0] <= self.fresh_window_sec]
                if candidates and rate_wait <= 0:
                    best = max(candidates, key=lambda item: (item[2]["score"], item[0], item[1]))
                    self._items.remove(best)
                    self._served += 1
                    self._last_served_at = now
                    return dict(best[2])

                if timeout <= 0:
                    return None
                remaining = deadline - loop.time() if deadline is not None else 0
                if remaining <= 0:
                    return None
                wait_for = remaining
                if candidates and rate_wait > 0:
                    wait_for = min(wait_for, rate_wait)
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    # Hết rate guard thì thử lại; hết deadline sẽ trả None ở vòng sau.
                    pass

    async def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._condition:
            self._discard_expired(self._clock())
            newest = sorted(self._items, key=lambda item: (item[0], item[1]), reverse=True)
            return [dict(item[2]) for item in newest[:limit]]

    async def length(self) -> int:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            return sum(now - item[0] <= self.fresh_window_sec for item in self._items)

    async def stats(self) -> dict[str, Any]:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            current = sum(now - item[0] <= self.fresh_window_sec for item in self._items)
            return {
                "received": self._received,
                "accepted": self._accepted,
                "dropped_by_reason": dict(sorted(self._dropped.items())),
                "served": self._served,
                "expired": self._expired,
                "current": current,
                "filter_config": self.filter.config,
                "buffer_config": {
                    "buffer_size": self.buffer_size,
                    "max_age_sec": self.max_age_sec,
                    "fresh_window_sec": self.fresh_window_sec,
                    "max_serve_per_sec": self.max_serve_per_sec,
                },
                "scoring_config": {
                    "priority_keywords": list(COMMENT_PRIORITY_KEYWORDS),
                },
            }

    def _discard_expired(self, now: float) -> None:
        kept = [item for item in self._items if now - item[0] <= self.max_age_sec]
        self._expired += len(self._items) - len(kept)
        self._items = kept

    def _rate_wait(self, now: float) -> float:
        if self.max_serve_per_sec <= 0 or self._last_served_at is None:
            return 0.0
        return max(0.0, 1.0 / self.max_serve_per_sec - (now - self._last_served_at))
