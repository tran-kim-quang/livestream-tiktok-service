from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from app.comments.audience import AudienceTracker, user_key
from app.comments.filters import CommentFilter
from app.comments.relevance import RelevanceScorer, core_from_similarity
from app.comments.scoring import score
from app.config import (
    COMMENT_BUFFER_SIZE, COMMENT_CORE_WEIGHT, COMMENT_FRESH_WINDOW_SEC,
    COMMENT_MAX_AGE_SEC, COMMENT_MAX_CHARS, COMMENT_MAX_SERVE_PER_SEC,
    COMMENT_MERGE_MAX_PARTS, COMMENT_MERGE_WINDOW_SEC, COMMENT_PRIORITY_KEYWORDS,
    COMMENT_SIM_CEIL, COMMENT_SIM_FLOOR, COMMENT_USER_COOLDOWN_SEC,
)


class CommentBuffer:
    """Buffer giới hạn trong RAM, chấm core và ưu tiên khán giả khi lấy."""

    def __init__(
        self, *, comment_filter: CommentFilter | None = None,
        relevance: RelevanceScorer | None = None, audience: AudienceTracker | None = None,
        core_weight: float = COMMENT_CORE_WEIGHT, buffer_size: int = COMMENT_BUFFER_SIZE,
        max_age_sec: float = COMMENT_MAX_AGE_SEC,
        fresh_window_sec: float = COMMENT_FRESH_WINDOW_SEC,
        max_serve_per_sec: float = COMMENT_MAX_SERVE_PER_SEC,
        user_cooldown_sec: float = COMMENT_USER_COOLDOWN_SEC,
        merge_window_sec: float = COMMENT_MERGE_WINDOW_SEC,
        merge_max_parts: int = COMMENT_MERGE_MAX_PARTS, max_chars: int = COMMENT_MAX_CHARS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        scorer: Callable[[Mapping[str, Any]], float] = score,
    ) -> None:
        self.filter = comment_filter or CommentFilter(clock=clock)
        self.relevance = relevance
        self.audience = audience or AudienceTracker(clock=clock)
        self.core_weight = core_weight
        self.buffer_size = max(1, buffer_size)
        self.max_age_sec = max(0.0, max_age_sec)
        self.fresh_window_sec = max(0.0, min(fresh_window_sec, self.max_age_sec))
        self.max_serve_per_sec = max(0.0, max_serve_per_sec)
        self.user_cooldown_sec = max(0.0, user_cooldown_sec)
        self.merge_window_sec = max(0.0, merge_window_sec)
        self.merge_max_parts = max(1, merge_max_parts)
        self.max_chars = max(1, max_chars)
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._scorer = scorer
        self._items: list[tuple[float, int, dict[str, Any]]] = []
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._received = self._accepted = self._served = self._expired = 0
        self._dropped: Counter[str] = Counter()
        self._last_served_at: float | None = None
        self._served_at: dict[str, float] = {}
        self._merged = self._skipped_user_cooldown = self._bypassed_cooldown = 0

    async def _calculate_core(
        self, payload: Mapping[str, Any], text: str
    ) -> tuple[float, str, float | None]:
        similarity = await self.relevance.similarity(text) if self.relevance is not None else None
        if similarity is not None:
            return core_from_similarity(
                similarity, self.relevance.sim_floor, self.relevance.sim_ceil
            ), "similarity", similarity
        if self.relevance is not None:
            self.relevance.count_heuristic()
        return min(max(self._scorer(payload), 0.0) / 11.5, 1.0), "heuristic", None

    async def add(self, payload: Mapping[str, Any]) -> bool:
        """Lọc dưới lock; embedding chạy ngoài lock để queue vẫn được lấy."""
        async with self._condition:
            self._received += 1
            reason = self.filter.drop_reason(payload)
            if reason is not None:
                self._dropped[reason] += 1
                return False
        text = str(payload.get("comment") or "").strip()
        core, source, similarity = await self._calculate_core(payload, text)

        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            merge_index = self._merge_target(user_key(payload), now, text)
            if merge_index is not None:
                _, _, record = self._items.pop(merge_index)
                previous_text = str(record["comment"])
                separator = " " if previous_text.rstrip().endswith((".", "!", "?", "…")) else ". "
                parts = list(record.get("parts") or [previous_text]) + [text]
                msg_ids = list(record.get("msg_ids") or [record.get("msg_id")]) + [payload.get("msg_id")]
                part_cores = list(record.get("part_cores") or [record["core"]]) + [core]
                part_sources = list(record.get("part_sources") or [record["core_source"]]) + [source]
                part_sims = list(record.get("part_similarities") or [record.get("similarity")]) + [similarity]
                sims = [value for value in part_sims if value is not None]
                record.update({
                    "comment": previous_text + separator + text, "parts": parts,
                    "msg_ids": msg_ids, "part_cores": part_cores,
                    "part_sources": part_sources, "part_similarities": part_sims,
                    "merged": len(parts), "enqueued_at": self._wall_clock().isoformat(),
                    "core": max(part_cores),
                    "core_source": "similarity" if "similarity" in part_sources else "heuristic",
                    "similarity": max(sims) if sims else None,
                    "identity": dict(payload.get("identity") or {}),
                })
                self._sequence += 1
                self._items.append((now, self._sequence, record))
                self._accepted += 1
                self._merged += 1
                self._condition.notify_all()
                return True

            record = {
                "comment": text, "user": payload.get("user"),
                "identity": dict(payload.get("identity") or {}),
                "streamer_unique_id": payload.get("streamer_unique_id"),
                "room_id": payload.get("room_id"), "msg_id": payload.get("msg_id"),
                "enqueued_at": self._wall_clock().isoformat(), "core": core,
                "core_source": source, "similarity": similarity, "part_cores": [core],
                "part_sources": [source], "part_similarities": [similarity],
            }
            self._sequence += 1
            self._items.append((now, self._sequence, record))
            self._accepted += 1
            if len(self._items) > self.buffer_size:
                self._items.pop(0)
                self._dropped["buffer_full"] += 1
            self._condition.notify_all()
            return True

    def _scored(self, record: Mapping[str, Any], now: float) -> dict[str, Any]:
        result = dict(record)
        bonus_total, bonus = self.audience.bonus(record, now)
        core = float(record["core"])
        weighted = core * self.core_weight
        result["score"] = weighted + bonus_total
        result["score_detail"] = {
            "core": core, "core_source": record["core_source"],
            "similarity": record.get("similarity"), "core_weighted": weighted,
            "bonus": bonus, "bonus_total": bonus_total,
        }
        return result

    async def next(self, timeout: float = 0) -> dict[str, Any] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout > 0 else None
        cooldown_counted: set[int] = set()
        bypass_counted: set[int] = set()
        async with self._condition:
            while True:
                now = self._clock()
                self._discard_expired(now)
                self._prune_served_at(now)
                rate_wait = self._rate_wait(now)
                candidates = [item for item in self._items if now - item[0] <= self.fresh_window_sec]
                eligible: list[tuple[float, int, dict[str, Any]]] = []
                cooldown_waits = []
                for item in candidates:
                    key = user_key(item[2])
                    served = self._served_at.get(key) if key else None
                    cooldown_wait = self.user_cooldown_sec - (now - served) if served is not None else 0.0
                    bypass = cooldown_wait > 0 and self.audience.bypass_cooldown(key, now)
                    if cooldown_wait > 0 and not bypass:
                        cooldown_waits.append(cooldown_wait)
                        if item[1] not in cooldown_counted:
                            self._skipped_user_cooldown += 1
                            cooldown_counted.add(item[1])
                    else:
                        eligible.append(item)
                        if bypass and item[1] not in bypass_counted:
                            self._bypassed_cooldown += 1
                            bypass_counted.add(item[1])
                if eligible and rate_wait <= 0:
                    best = max(eligible, key=lambda item: (
                        self._scored(item[2], now)["score"], item[0], item[1]
                    ))
                    self._items.remove(best)
                    self._served += 1
                    self._last_served_at = now
                    key = user_key(best[2])
                    if key and self.user_cooldown_sec > 0:
                        self._served_at[key] = now
                        while len(self._served_at) > 100_000:
                            self._served_at.pop(next(iter(self._served_at)))
                    return self._scored(best[2], now)
                if timeout <= 0:
                    return None
                remaining = deadline - loop.time() if deadline is not None else 0
                if remaining <= 0:
                    return None
                wait_for = remaining
                if eligible and rate_wait > 0:
                    wait_for = min(wait_for, rate_wait)
                elif candidates and not eligible and cooldown_waits:
                    # Chỉ thức dậy khi cả cooldown user sớm nhất và rate guard đã hết.
                    wait_for = min(wait_for, max(min(cooldown_waits), rate_wait))
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    # Hết rate guard thì thử lại; hết deadline sẽ trả None ở vòng sau.
                    pass

    async def latest(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            newest = sorted(self._items, key=lambda item: (item[0], item[1]), reverse=True)
            return [self._scored(item[2], now) for item in newest[:limit]]

    async def length(self) -> int:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            return sum(now - item[0] <= self.fresh_window_sec for item in self._items)

    async def notify_priority_changed(self) -> None:
        """Đánh thức consumer đang chờ khi gift/follow đổi eligibility hoặc thứ tự."""
        async with self._condition:
            self._condition.notify_all()

    async def stats(self) -> dict[str, Any]:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            current = sum(now - item[0] <= self.fresh_window_sec for item in self._items)
            relevance = self.relevance.status() if self.relevance is not None else {
                "enabled": False, "ready": False, "model": None, "passages": 0,
                "context_file": None,
                "counts": {"similarity": 0, "heuristic": self._accepted, "timeout": 0, "error": 0},
                "avg_embed_ms": 0.0,
            }
            return {
                "received": self._received, "accepted": self._accepted,
                "dropped_by_reason": dict(sorted(self._dropped.items())), "served": self._served,
                "merged": self._merged, "skipped_user_cooldown": self._skipped_user_cooldown,
                "bypassed_cooldown": self._bypassed_cooldown, "expired": self._expired,
                "current": current, "filter_config": self.filter.config,
                "buffer_config": {
                    "buffer_size": self.buffer_size, "max_age_sec": self.max_age_sec,
                    "fresh_window_sec": self.fresh_window_sec,
                    "max_serve_per_sec": self.max_serve_per_sec,
                    "user_cooldown_sec": self.user_cooldown_sec,
                    "merge_window_sec": self.merge_window_sec,
                    "merge_max_parts": self.merge_max_parts,
                },
                "scoring_config": {
                    "core_weight": self.core_weight,
                    "similarity_floor": self.relevance.sim_floor if self.relevance else COMMENT_SIM_FLOOR,
                    "similarity_ceil": self.relevance.sim_ceil if self.relevance else COMMENT_SIM_CEIL,
                    "priority_keywords": list(COMMENT_PRIORITY_KEYWORDS),
                    "audience_weights": dict(self.audience.weights),
                    "gift_half_life_sec": self.audience.gift_half_life_sec,
                    "gift_diamond_cap": self.audience.gift_diamond_cap,
                    "priority_window_sec": self.audience.priority_window_sec,
                },
                "relevance": relevance, "audience": self.audience.status(),
            }

    def _discard_expired(self, now: float) -> None:
        kept = [item for item in self._items if now - item[0] <= self.max_age_sec]
        self._expired += len(self._items) - len(kept)
        self._items = kept

    def _rate_wait(self, now: float) -> float:
        if self.max_serve_per_sec <= 0 or self._last_served_at is None:
            return 0.0
        return max(0.0, 1.0 / self.max_serve_per_sec - (now - self._last_served_at))

    def _merge_target(self, key: str, now: float, text: str) -> int | None:
        if not key or self.merge_window_sec <= 0 or self.merge_max_parts <= 1:
            return None
        for index in range(len(self._items) - 1, -1, -1):
            arrived_at, _, record = self._items[index]
            if user_key(record) != key:
                continue
            parts = record.get("parts") or [record.get("comment")]
            if now - arrived_at > self.merge_window_sec or len(parts) >= self.merge_max_parts:
                return None
            previous = str(record.get("comment") or "")
            separator = " " if previous.rstrip().endswith((".", "!", "?", "…")) else ". "
            return index if len(previous + separator + text) <= self.max_chars else None
        return None

    def _prune_served_at(self, now: float) -> None:
        if self.user_cooldown_sec <= 0:
            self._served_at.clear()
            return
        cutoff = now - self.user_cooldown_sec
        self._served_at = {user: served for user, served in self._served_at.items() if served > cutoff}
