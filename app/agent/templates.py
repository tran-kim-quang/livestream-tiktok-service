from __future__ import annotations

import random
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from app.agent.names import speakable_name
from app.comments.audience import is_backlog_event, user_key
from app.config import (
    AGENT_THANKS, AGENT_THANKS_BATCH_MAX, AGENT_THANKS_BATCH_SEC,
    AGENT_THANKS_MIN_DIAMONDS, AGENT_THANKS_MIN_GAP_SEC,
)


GREETING = (
    "Chào {names} nha, cả nhà ngồi chơi với em xíu nha",
    "Hello {names}, vào ngồi chơi với cả nhà nha",
    "Chào mừng {names} ghé live nha",
    "Em chào {names}, mình cùng tám chuyện nha",
    "Chào {names} nha, hôm nay mình vui vẻ hết mình nhé",
    "Hế lô {names}, cảm ơn đã ghé chơi nha",
)
THANKS_GIFT = (
    "Cảm ơn {names} đã tặng quà nha, thương lắm luôn",
    "Cảm ơn quà của {names} nha, dễ thương quá trời",
    "Em nhận được quà của {names} rồi, cảm ơn nhiều nha",
    "Cảm ơn {names} tiếp sức cho live nha",
    "Quà xinh quá, cảm ơn {names} thật nhiều nha",
    "Cảm ơn {names} đã thương và tặng quà cho em nha",
)
THANKS_FOLLOW = (
    "Cảm ơn {names} đã follow kênh nha",
    "Cảm ơn {names} vừa theo dõi kênh, chào mừng nha",
    "Em cảm ơn {names} đã bấm follow nha",
    "Chào mừng {names} vào hội theo dõi kênh nha",
    "Cảm ơn lượt follow của {names} nhiều nha",
    "Cảm ơn {names} đã ở lại đồng hành cùng kênh nha",
)


def join_names(names: Sequence[str], max_names: int = 4) -> str:
    clean = [name.strip() for name in names if name and name.strip()]
    if not clean:
        return "cả nhà"
    limit = max(1, max_names)
    if len(clean) > limit:
        return ", ".join(clean[:max(1, limit - 1)]) + " và cả nhà"
    if len(clean) == 1:
        return clean[0]
    return ", ".join(clean[:-1]) + " với " + clean[-1]


class TemplatePicker:
    def __init__(self, templates: Sequence[str], *, rng: Any = random) -> None:
        self.templates = tuple(templates)
        if not self.templates:
            raise ValueError("Cần ít nhất một template.")
        self.rng = rng
        self._recent: deque[str] = deque(maxlen=min(5, max(0, len(self.templates) - 1)))

    def pick(self, **values: str) -> str:
        choices = [item for item in self.templates if item not in self._recent] or list(self.templates)
        selected = self.rng.choice(choices)
        self._recent.append(selected)
        return selected.format(**values)


class ThanksBatcher:
    def __init__(
        self, *, enabled: bool = AGENT_THANKS,
        batch_sec: float = AGENT_THANKS_BATCH_SEC,
        batch_max: int = AGENT_THANKS_BATCH_MAX,
        min_gap_sec: float = AGENT_THANKS_MIN_GAP_SEC,
        min_diamonds: float = AGENT_THANKS_MIN_DIAMONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.batch_sec = max(0.0, batch_sec)
        self.batch_max = max(1, batch_max)
        self.min_gap_sec = max(0.0, min_gap_sec)
        self.min_diamonds = max(0.0, min_diamonds)
        self._clock = clock
        self._gifts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._follows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._last_reply_at: float | None = None

    def _add(self, target: OrderedDict[str, dict[str, Any]], payload: Mapping[str, Any]) -> None:
        user = payload.get("user") or {}
        key = user_key(user) if isinstance(user, Mapping) else ""
        if not key:
            return
        if key not in target:
            target[key] = {
                "user_key": key, "name": speakable_name(user), "user": dict(user),
                "received_at": self._clock(),
            }

    def handle_gift(self, payload: Mapping[str, Any]) -> None:
        if self.enabled and not is_backlog_event(payload) and float(payload.get("diamonds") or 0) >= self.min_diamonds:
            self._add(self._gifts, payload)

    def handle_follow(self, payload: Mapping[str, Any]) -> None:
        if self.enabled and not is_backlog_event(payload):
            self._add(self._follows, payload)

    def _prune(self, now: float) -> None:
        for target in (self._gifts, self._follows):
            for key, item in list(target.items()):
                if now - item["received_at"] > 120:
                    del target[key]

    def ready(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        self._prune(now)
        if self._last_reply_at is not None and now - self._last_reply_at < self.min_gap_sec:
            return False
        return self._target_ready(self._gifts, now) or self._target_ready(self._follows, now)

    def _target_ready(self, target: OrderedDict[str, dict[str, Any]], now: float) -> bool:
        if not target:
            return False
        first = next(iter(target.values()))
        return now - first["received_at"] >= self.batch_sec or len(target) >= self.batch_max

    def pop_ready(self, now: float | None = None) -> dict[str, Any] | None:
        now = self._clock() if now is None else now
        if not self.ready(now):
            return None
        if self._target_ready(self._gifts, now):
            kind, target = "gift", self._gifts
        else:
            kind, target = "follow", self._follows
        users = []
        for key in list(target)[:self.batch_max]:
            users.append(target.pop(key))
        self._last_reply_at = now
        return {"kind": kind, "users": users}

    def status(self) -> dict[str, int | float | bool]:
        self._prune(self._clock())
        return {"enabled": self.enabled, "gifts": len(self._gifts), "follows": len(self._follows)}
