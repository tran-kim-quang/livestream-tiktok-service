from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from app.config import (
    COMMENT_BACKLOG_SLACK_SEC,
    COMMENT_BONUS_FOLLOW,
    COMMENT_BONUS_FOLLOWER,
    COMMENT_BONUS_GIFT,
    COMMENT_BONUS_GIFT_GIVER,
    COMMENT_BONUS_MODERATOR,
    COMMENT_BONUS_SUBSCRIBER,
    COMMENT_GIFT_DIAMOND_CAP,
    COMMENT_GIFT_HALF_LIFE_SEC,
    COMMENT_PRIORITY_WINDOW_SEC,
    COMMENT_SKIP_BACKLOG,
)


def user_key(user: Mapping[str, Any]) -> str:
    """Khoá user chung cho filter, buffer và event gift/follow."""
    nested = user.get("user")
    if isinstance(nested, Mapping):
        user = nested
    value = user.get("id") or user.get("unique_id")
    return str(value).lower() if value is not None else ""


def is_backlog_event(
    payload: Mapping[str, Any],
    *,
    skip: bool = COMMENT_SKIP_BACKLOG,
    slack_sec: float = COMMENT_BACKLOG_SLACK_SEC,
) -> bool:
    if not skip:
        return False
    created = payload.get("create_time")
    started = payload.get("session_started_at")
    return bool(created and started and created < started - slack_sec * 1000)


class AudienceTracker:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        gift_half_life_sec: float = COMMENT_GIFT_HALF_LIFE_SEC,
        gift_diamond_cap: float = COMMENT_GIFT_DIAMOND_CAP,
        priority_window_sec: float = COMMENT_PRIORITY_WINDOW_SEC,
        bonus_gift: float = COMMENT_BONUS_GIFT,
        bonus_follow: float = COMMENT_BONUS_FOLLOW,
        bonus_subscriber: float = COMMENT_BONUS_SUBSCRIBER,
        bonus_gift_giver: float = COMMENT_BONUS_GIFT_GIVER,
        bonus_follower: float = COMMENT_BONUS_FOLLOWER,
        bonus_moderator: float = COMMENT_BONUS_MODERATOR,
        max_users: int = 100_000,
    ) -> None:
        self._clock = clock
        self.gift_half_life_sec = max(0.01, gift_half_life_sec)
        self.gift_diamond_cap = max(0.01, gift_diamond_cap)
        self.priority_window_sec = max(0.0, priority_window_sec)
        self.weights = {
            "gift": bonus_gift,
            "follow": bonus_follow,
            "subscriber": bonus_subscriber,
            "gift_giver": bonus_gift_giver,
            "follower": bonus_follower,
            "moderator": bonus_moderator,
        }
        self.max_users = max(1, max_users)
        self._users: dict[str, dict[str, float | None]] = {}
        self._gifts_recorded = 0
        self._diamonds_recorded = 0.0
        self._follows_recorded = 0

    def _decay(self, value: float, elapsed: float) -> float:
        return value * math.pow(0.5, max(0.0, elapsed) / self.gift_half_life_sec)

    def _entry(self, key: str, now: float) -> dict[str, float | None]:
        entry = self._users.get(key)
        if entry is None:
            entry = {"diamonds": 0.0, "updated_at": now, "last_gift_at": None,
                     "last_follow_at": None}
            self._users[key] = entry
        else:
            self._users.pop(key)
            self._users[key] = entry
        return entry

    def record_gift(self, user: Mapping[str, Any], diamonds: float) -> None:
        key = user_key(user)
        if not key:
            return
        now = self._clock()
        entry = self._entry(key, now)
        previous = float(entry["diamonds"] or 0.0)
        updated_at = float(entry["updated_at"] if entry["updated_at"] is not None else now)
        entry["diamonds"] = self._decay(previous, now - updated_at) + max(0.0, diamonds)
        entry["updated_at"] = now
        entry["last_gift_at"] = now
        self._gifts_recorded += 1
        self._diamonds_recorded += max(0.0, diamonds)
        self._prune(now)

    def record_follow(self, user: Mapping[str, Any]) -> None:
        key = user_key(user)
        if not key:
            return
        now = self._clock()
        self._entry(key, now)["last_follow_at"] = now
        self._follows_recorded += 1
        self._prune(now)

    async def handle_gift(self, payload: Mapping[str, Any]) -> None:
        if not is_backlog_event(payload):
            self.record_gift(payload.get("user") or {}, float(payload.get("diamonds") or 0))

    async def handle_follow(self, payload: Mapping[str, Any]) -> None:
        if not is_backlog_event(payload):
            self.record_follow(payload.get("user") or {})

    def bonus(self, record: Mapping[str, Any], now: float | None = None) -> tuple[float, dict[str, float]]:
        # Không prune ở đây: bonus() chạy cho từng candidate mỗi lần next(); prune khi ghi event là đủ.
        now = self._clock() if now is None else now
        key = user_key(record)
        entry = self._users.get(key)
        diamonds = 0.0
        followed = False
        if entry is not None:
            diamonds = self._decay(
                float(entry["diamonds"] or 0.0),
                now - float(entry["updated_at"] if entry["updated_at"] is not None else now),
            )
            last_follow = entry["last_follow_at"]
            followed = last_follow is not None and self._within(float(last_follow), now)
        identity = record.get("identity") or {}
        if not isinstance(identity, Mapping):
            identity = {}
        detail = {
            "gift": self.weights["gift"] * min(
                1.0, math.log1p(diamonds) / math.log1p(self.gift_diamond_cap)
            ),
            "follow": self.weights["follow"] if followed else 0.0,
            "subscriber": self.weights["subscriber"] if identity.get("subscriber") else 0.0,
            "gift_giver": self.weights["gift_giver"] if identity.get("gift_giver") else 0.0,
            "follower": self.weights["follower"] if identity.get("follower") else 0.0,
            "moderator": self.weights["moderator"] if identity.get("moderator") else 0.0,
        }
        return sum(detail.values()), detail

    def bypass_cooldown(self, key: str, now: float | None = None) -> bool:
        if not key or key not in self._users:
            return False
        now = self._clock() if now is None else now
        entry = self._users[key]
        return any(
            timestamp is not None and self._within(float(timestamp), now)
            for timestamp in (entry["last_gift_at"], entry["last_follow_at"])
        )

    def _within(self, timestamp: float, now: float) -> bool:
        return self.priority_window_sec == 0 or now - timestamp <= self.priority_window_sec

    def _prune(self, now: float) -> None:
        if self.priority_window_sec > 0:
            for key, entry in list(self._users.items()):
                last_event = max(
                    float(entry["last_gift_at"] or 0.0),
                    float(entry["last_follow_at"] or 0.0),
                )
                diamonds = self._decay(
                    float(entry["diamonds"] or 0.0),
                    now - float(entry["updated_at"] if entry["updated_at"] is not None else now),
                )
                if now - last_event > self.priority_window_sec and diamonds < 1.0:
                    del self._users[key]
        while len(self._users) > self.max_users:
            self._users.pop(next(iter(self._users)))

    def status(self) -> dict[str, int | float]:
        self._prune(self._clock())
        return {
            "tracked_users": len(self._users),
            "gifts_recorded": self._gifts_recorded,
            "diamonds_recorded": self._diamonds_recorded,
            "follows_recorded": self._follows_recorded,
        }
