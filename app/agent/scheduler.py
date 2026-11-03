from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.templates import (
    GREETING, THANKS_FOLLOW, THANKS_GIFT, TemplatePicker, ThanksBatcher, join_names,
)
from app.agent.names import speakable_name
from app.comments.buffer import CommentBuffer
from app.config import (
    AGENT_CHAT, AGENT_CHAT_AIRTIME_RATIO, AGENT_CHAT_AIRTIME_WINDOW_SEC,
    AGENT_CHAT_BURST, AGENT_CHAT_RATE_PER_MIN, AGENT_GREETING_BATCH_MAX,
    AGENT_GREETING_BATCH_SEC,
)


@dataclass
class ScheduledReply:
    kind: str
    comment: dict[str, Any]
    batch: list[dict[str, Any]]
    text: str | None = None
    llm: bool = True


class AirtimeTracker:
    def __init__(self, window_sec: float = AGENT_CHAT_AIRTIME_WINDOW_SEC, *, ratio: float = AGENT_CHAT_AIRTIME_RATIO, clock: Callable[[], float] = time.monotonic) -> None:
        self.window_sec = max(0.0, window_sec)
        self.ratio = max(0.0, min(1.0, ratio))
        self._clock = clock
        self._items: deque[tuple[float, float, str]] = deque()

    def record(self, audio_sec: float, lane: str, finished_at: float | None = None) -> None:
        self._items.append((self._clock() if finished_at is None else finished_at, max(0.0, audio_sec), lane))
        self._prune(self._clock())

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self.window_sec:
            self._items.popleft()

    def totals(self, now: float | None = None) -> dict[str, float]:
        now = self._clock() if now is None else now
        self._prune(now)
        result: Counter[str] = Counter()
        for _, seconds, lane in self._items:
            result[lane] += seconds
        return dict(result)

    def allows_chat(self, estimate: float = 4.0, now: float | None = None) -> bool:
        totals = self.totals(now)
        total = sum(totals.values())
        chat = totals.get("chat", 0.0) + totals.get("greeting", 0.0)
        return (chat + estimate) / (total + estimate) <= self.ratio


class TokenBucket:
    def __init__(self, rate_per_min: float, burst: float, *, clock: Callable[[], float]) -> None:
        self.rate = max(0.0, rate_per_min) / 60.0
        self.capacity = max(0.0, burst)
        self.tokens = self.capacity
        self._clock = clock
        self._updated = clock()

    def available(self) -> float:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self._updated) * self.rate)
        self._updated = now
        return self.tokens

    def consume(self) -> bool:
        if self.available() < 1.0:
            return False
        self.tokens -= 1.0
        return True


class ReplyScheduler:
    def __init__(
        self, comments: CommentBuffer, thanks: ThanksBatcher | None = None, *,
        chat_enabled: bool = AGENT_CHAT, clock: Callable[[], float] = time.monotonic,
        rng: Any = None, airtime_ratio: float = AGENT_CHAT_AIRTIME_RATIO,
        airtime_window_sec: float = AGENT_CHAT_AIRTIME_WINDOW_SEC,
        chat_rate_per_min: float = AGENT_CHAT_RATE_PER_MIN,
        chat_burst: float = AGENT_CHAT_BURST,
        greeting_batch_sec: float = AGENT_GREETING_BATCH_SEC,
        greeting_batch_max: int = AGENT_GREETING_BATCH_MAX,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.comments = comments
        self.thanks = thanks
        self.chat_enabled = chat_enabled
        self._clock = clock
        self.airtime = AirtimeTracker(airtime_window_sec, ratio=airtime_ratio, clock=clock)
        self.bucket = TokenBucket(chat_rate_per_min, chat_burst, clock=clock)
        self.greeting_batch_sec = max(0.0, greeting_batch_sec)
        self.greeting_batch_max = max(1, greeting_batch_max)
        self._sleep = sleep
        import random
        source = random if rng is None else rng
        self._greeting = TemplatePicker(GREETING, rng=source)
        self._gift = TemplatePicker(THANKS_GIFT, rng=source)
        self._follow = TemplatePicker(THANKS_FOLLOW, rng=source)
        self._picks: Counter[str] = Counter()

    async def pick(self) -> ScheduledReply | None:
        if self.thanks is not None:
            batch = self.thanks.pop_ready(self._clock())
            if batch:
                users = batch["users"]
                names = join_names([item["name"] for item in users], self.thanks.batch_max)
                picker = self._gift if batch["kind"] == "gift" else self._follow
                record = {"comment": "", "user": users[0]["user"], "lane": "thanks"}
                result = ScheduledReply("thanks", record, users, picker.pick(names=names), False)
                self._picks["thanks"] += 1
                return result

        lanes = await self.comments.available_lanes(self._clock())
        question_waiting = "question" in lanes
        greeting_ready = self.chat_enabled and "greeting" in lanes and await self.comments.greeting_batch_ready(
            self.greeting_batch_max, self.greeting_batch_sec
        )
        chat_ready = self.chat_enabled and "chat" in lanes and self.bucket.available() >= 1.0
        chat_owed = (greeting_ready or chat_ready) and (
            not question_waiting or self.airtime.allows_chat(now=self._clock())
        )

        if question_waiting and not chat_owed:
            record = await self.comments.next(lanes={"question"})
            if record:
                self._picks["question"] += 1
                return ScheduledReply("question", record, [record])

        if greeting_ready and (not question_waiting or self.airtime.allows_chat(now=self._clock())):
            records = await self.comments.next_greeting_batch(self.greeting_batch_max)
            if records:
                names = join_names([
                    speakable_name(item.get("user") or {})
                    for item in records
                ], self.greeting_batch_max)
                self._picks["greeting"] += 1
                return ScheduledReply("greeting", records[0], records, self._greeting.pick(names=names), False)

        if chat_ready and (not question_waiting or self.airtime.allows_chat(now=self._clock())) and self.bucket.consume():
            record = await self.comments.next(lanes={"chat"})
            if record:
                self._picks["chat"] += 1
                return ScheduledReply("chat", record, [record])

        if question_waiting:
            record = await self.comments.next(lanes={"question"})
            if record:
                self._picks["question"] += 1
                return ScheduledReply("question", record, [record])
        return None

    async def next(self, timeout: float = 1.0) -> ScheduledReply | None:
        deadline = self._clock() + max(0.0, timeout)
        while True:
            result = await self.pick()
            if result is not None or timeout <= 0:
                return result
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            await self._sleep(min(0.25, remaining))

    def record_airtime(self, seconds: float, kind: str) -> None:
        self.airtime.record(seconds, kind, self._clock())

    async def status(self) -> dict[str, Any]:
        raw_totals = self.airtime.totals(self._clock())
        totals = {lane: raw_totals.get(lane, 0.0) for lane in ("question", "chat", "greeting", "thanks")}
        total = sum(totals.values())
        chat = totals.get("chat", 0.0) + totals.get("greeting", 0.0)
        latest = await self.comments.latest(500)
        return {
            "airtime": totals, "chat_ratio": chat / total if total else 0.0,
            "chat_cap": self.airtime.ratio,
            "chat_tokens": round(self.bucket.available(), 3),
            "picks": {lane: self._picks[lane] for lane in ("question", "chat", "greeting", "thanks")},
            "pending_greetings": sum(item.get("lane") == "greeting" for item in latest),
            "pending_thanks": self.thanks.status() if self.thanks is not None else {"gifts": 0, "follows": 0},
        }
