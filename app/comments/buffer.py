from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from app.comments.audience import AudienceTracker, user_key
from app.comments.filters import CommentFilter
from app.comments.intents import is_pure_greeting, matches_greeting_pattern
from app.comments.relevance import RelevanceScorer, core_from_similarity
from app.comments.scoring import is_question, score
from app.config import (
    AGENT_CONTEXT_SIM_THRESHOLD, COMMENT_QUESTIONS_ONLY,
    COMMENT_BUFFER_SIZE, COMMENT_CORE_WEIGHT, COMMENT_FRESH_WINDOW_SEC,
    COMMENT_CHAT_MAX_AGE_SEC, COMMENT_CHAT_MAX_PER_USER,
    COMMENT_CHAT_USER_COOLDOWN_SEC, COMMENT_CHAT_USER_WINDOW_SEC,
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
        questions_only: bool = COMMENT_QUESTIONS_ONLY,
        question_threshold: float = AGENT_CONTEXT_SIM_THRESHOLD,
        chat_max_age_sec: float | None = None,
        chat_user_cooldown_sec: float | None = None,
        chat_max_per_user: int = COMMENT_CHAT_MAX_PER_USER,
        chat_user_window_sec: float = COMMENT_CHAT_USER_WINDOW_SEC,
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
        self.questions_only = questions_only
        self.question_threshold = question_threshold
        implicit_chat_age = (
            COMMENT_CHAT_MAX_AGE_SEC
            if max_age_sec == COMMENT_MAX_AGE_SEC and fresh_window_sec == COMMENT_FRESH_WINDOW_SEC
            else self.fresh_window_sec
        )
        self.chat_max_age_sec = max(0.0, implicit_chat_age if chat_max_age_sec is None else chat_max_age_sec)
        implicit_chat_cooldown = (
            COMMENT_CHAT_USER_COOLDOWN_SEC
            if user_cooldown_sec == COMMENT_USER_COOLDOWN_SEC else 0.0
        )
        self.chat_user_cooldown_sec = max(
            0.0, implicit_chat_cooldown if chat_user_cooldown_sec is None else chat_user_cooldown_sec
        )
        self.chat_max_per_user = max(0, chat_max_per_user)
        self.chat_user_window_sec = max(0.0, chat_user_window_sec)
        self._items: list[tuple[float, int, dict[str, Any]]] = []
        self._sequence = 0
        self._condition = asyncio.Condition()
        self._received = self._accepted = self._served = self._expired = 0
        self._dropped: Counter[str] = Counter()
        self._last_served_at: float | None = None
        self._served_at: dict[str, float] = {}
        self._merged = self._skipped_user_cooldown = self._bypassed_cooldown = 0
        self._accepted_lanes: Counter[str] = Counter()
        self._served_lanes: Counter[str] = Counter()
        self._chat_served_at: dict[str, deque[float]] = {}
        self._skipped_chat_user_cooldown = self._skipped_chat_user_window = 0

    async def _calculate_core(
        self, payload: Mapping[str, Any], text: str
    ) -> tuple[float, str, float | None, tuple[float, ...] | None]:
        similarity: float | None = None
        vector: tuple[float, ...] | None = None
        if self.relevance is not None:
            if hasattr(self.relevance, "analyze"):
                similarity, vector = await self.relevance.analyze(text)
            else:
                similarity = await self.relevance.similarity(text)
        if similarity is not None:
            return core_from_similarity(
                similarity, self.relevance.sim_floor, self.relevance.sim_ceil
            ), "similarity", similarity, vector
        if self.relevance is not None:
            self.relevance.count_heuristic()
        return min(max(self._scorer(payload), 0.0) / 11.5, 1.0), "heuristic", None, None

    def _lane(self, text: str, similarity: float | None, vector: tuple[float, ...] | None) -> str:
        if matches_greeting_pattern(text):
            return "greeting"
        question = similarity >= self.question_threshold if similarity is not None else is_question(text)
        if question:
            return "question"
        anchors = getattr(self.relevance, "greeting_vectors", ()) if self.relevance is not None else ()
        return "greeting" if is_pure_greeting(text, vector, anchors) else "chat"

    async def add(self, payload: Mapping[str, Any]) -> bool:
        """Lọc dưới lock; embedding chạy ngoài lock để queue vẫn được lấy."""
        async with self._condition:
            self._received += 1
            reason = self.filter.drop_reason(payload)
            if reason is not None:
                self._dropped[reason] += 1
                return False
        text = str(payload.get("comment") or "").strip()
        core, source, similarity, vector = await self._calculate_core(payload, text)
        lane = self._lane(text, similarity, vector)
        if self.questions_only and lane != "question":
            async with self._condition:
                self._dropped["not_question"] += 1
            return False

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
                    "embedding_checked": True,
                    "lane": self._lane(previous_text + separator + text, max(sims) if sims else None, vector),
                    "identity": dict(payload.get("identity") or {}),
                })
                self._sequence += 1
                self._items.append((now, self._sequence, record))
                self._accepted += 1
                self._accepted_lanes[record["lane"]] += 1
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
                "lane": lane, "embedding_checked": True,
            }
            self._sequence += 1
            self._items.append((now, self._sequence, record))
            self._accepted += 1
            self._accepted_lanes[lane] += 1
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

    def _fresh(self, item: tuple[float, int, dict[str, Any]], now: float) -> bool:
        age = now - item[0]
        return age <= (self.fresh_window_sec if item[2].get("lane") == "question" else self.chat_max_age_sec)

    def _chat_limit_wait(self, record: Mapping[str, Any], now: float) -> tuple[float, str | None]:
        if record.get("lane") not in ("chat", "greeting"):
            return 0.0, None
        key = user_key(record)
        if not key or self.audience.bypass_cooldown(key, now):
            return 0.0, None
        history = self._chat_served_at.get(key, deque())
        while history and now - history[0] >= self.chat_user_window_sec:
            history.popleft()
        if history:
            wait = self.chat_user_cooldown_sec - (now - history[-1])
            if wait > 0:
                return wait, "cooldown"
        if self.chat_max_per_user and len(history) >= self.chat_max_per_user:
            return max(0.0, self.chat_user_window_sec - (now - history[0])), "window"
        return 0.0, None

    def _mark_served(self, item: tuple[float, int, dict[str, Any]], now: float) -> dict[str, Any]:
        self._items.remove(item)
        self._served += 1
        lane = str(item[2].get("lane") or "chat")
        self._served_lanes[lane] += 1
        self._last_served_at = now
        key = user_key(item[2])
        if key and self.user_cooldown_sec > 0:
            self._served_at[key] = now
        if key and lane in ("chat", "greeting"):
            history = self._chat_served_at.setdefault(key, deque())
            history.append(now)
        return self._scored(item[2], now)

    async def next(
        self, timeout: float = 0, lanes: Collection[str] | None = None
    ) -> dict[str, Any] | None:
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
                candidates = [
                    item for item in self._items
                    if self._fresh(item, now) and (lanes is None or item[2].get("lane") in lanes)
                ]
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
                        chat_wait, reason = self._chat_limit_wait(item[2], now)
                        if chat_wait > 0:
                            cooldown_waits.append(chat_wait)
                            if item[1] not in cooldown_counted:
                                if reason == "cooldown":
                                    self._skipped_chat_user_cooldown += 1
                                else:
                                    self._skipped_chat_user_window += 1
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
                    return self._mark_served(best, now)
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

    async def available_lanes(self, now: float | None = None) -> set[str]:
        async with self._condition:
            now = self._clock() if now is None else now
            self._discard_expired(now)
            lanes = set()
            for item in self._items:
                if not self._fresh(item, now):
                    continue
                key = user_key(item[2])
                served = self._served_at.get(key) if key else None
                if served is not None and now - served < self.user_cooldown_sec and not self.audience.bypass_cooldown(key, now):
                    continue
                if self._chat_limit_wait(item[2], now)[0] <= 0:
                    lanes.add(str(item[2].get("lane") or "chat"))
            return lanes

    peek_lanes = available_lanes

    async def greeting_batch_ready(self, max_items: int, batch_sec: float) -> bool:
        async with self._condition:
            now = self._clock()
            items = []
            for item in self._items:
                if item[2].get("lane") != "greeting" or not self._fresh(item, now):
                    continue
                key = user_key(item[2])
                served = self._served_at.get(key) if key else None
                bypass = self.audience.bypass_cooldown(key, now)
                if served is not None and now - served < self.user_cooldown_sec and not bypass:
                    continue
                if self._chat_limit_wait(item[2], now)[0] <= 0:
                    items.append(item)
            distinct = {user_key(item[2]) for item in items}
            return bool(items and (now - min(item[0] for item in items) >= batch_sec or len(distinct) >= max_items))

    async def next_greeting_batch(self, max_items: int) -> list[dict[str, Any]]:
        async with self._condition:
            now = self._clock()
            selected = []
            users = set()
            for item in sorted(self._items, key=lambda value: (value[0], value[1])):
                key = user_key(item[2])
                if item[2].get("lane") != "greeting" or not self._fresh(item, now) or key in users:
                    continue
                served = self._served_at.get(key) if key else None
                if served is not None and now - served < self.user_cooldown_sec and not self.audience.bypass_cooldown(key, now):
                    continue
                if self._chat_limit_wait(item[2], now)[0] > 0:
                    continue
                selected.append(item)
                users.add(key)
                if len(selected) >= max(1, max_items):
                    break
            return [self._mark_served(item, now) for item in selected]

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
            return sum(self._fresh(item, now) for item in self._items)

    async def notify_priority_changed(self) -> None:
        """Đánh thức consumer đang chờ khi gift/follow đổi eligibility hoặc thứ tự."""
        async with self._condition:
            self._condition.notify_all()

    async def stats(self) -> dict[str, Any]:
        async with self._condition:
            now = self._clock()
            self._discard_expired(now)
            current = sum(self._fresh(item, now) for item in self._items)
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
                "accepted_by_lane": {lane: self._accepted_lanes[lane] for lane in ("question", "chat", "greeting")},
                "served_by_lane": {lane: self._served_lanes[lane] for lane in ("question", "chat", "greeting")},
                "skipped_chat_user_cooldown": self._skipped_chat_user_cooldown,
                "skipped_chat_user_window": self._skipped_chat_user_window,
                "current": current, "filter_config": self.filter.config,
                "buffer_config": {
                    "buffer_size": self.buffer_size, "max_age_sec": self.max_age_sec,
                    "fresh_window_sec": self.fresh_window_sec,
                    "max_serve_per_sec": self.max_serve_per_sec,
                    "user_cooldown_sec": self.user_cooldown_sec,
                    "merge_window_sec": self.merge_window_sec,
                    "merge_max_parts": self.merge_max_parts,
                    "chat_max_age_sec": self.chat_max_age_sec,
                    "chat_user_cooldown_sec": self.chat_user_cooldown_sec,
                    "chat_max_per_user": self.chat_max_per_user,
                    "chat_user_window_sec": self.chat_user_window_sec,
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
        kept = [item for item in self._items if now - item[0] <= (
            self.max_age_sec if item[2].get("lane") == "question"
            else max(self.max_age_sec, self.chat_max_age_sec)
        )]
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
