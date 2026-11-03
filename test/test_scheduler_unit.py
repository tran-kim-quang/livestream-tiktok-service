from __future__ import annotations

import asyncio
import random
import unittest

from app.agent.loop import _speak_template
from app.agent.responder import LiveAgent
from app.agent.scheduler import ReplyScheduler, ScheduledReply
from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter
from app.tts.base import TTSEngine


class Clock:
    def __init__(self) -> None: self.now = 100.0
    def __call__(self) -> float: return self.now
    def advance(self, value: float) -> None: self.now += value
    async def sleep(self, value: float) -> None: self.now += value


def payload(text: str, user: str) -> dict:
    return {"comment": text, "user": {"id": user, "nickname": user}}


class FakeLLM:
    def __init__(self) -> None: self.calls = 0
    async def chat(self, messages): self.calls += 1; return "llm"


class FakeTTS(TTSEngine):
    name = "fake"
    def __init__(self) -> None: self.calls = []
    async def synthesize(self, text, voice_id=None): return b""
    async def list_voices(self): return []
    async def status(self): return {"ok": True}
    async def synthesize_pcm(self, text, voice_id=None, num_step=None):
        self.calls.append(text); return b"x" * 480


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    def make(self, *, burst=2) -> tuple[Clock, CommentBuffer, ReplyScheduler]:
        clock = Clock()
        buffer = CommentBuffer(
            comment_filter=CommentFilter(clock=clock, blocklist=(), dedup_window_sec=0),
            questions_only=False, user_cooldown_sec=0, chat_user_cooldown_sec=0,
            merge_window_sec=0, clock=clock,
        )
        scheduler = ReplyScheduler(
            buffer, clock=clock, rng=random.Random(1), chat_burst=burst,
            chat_rate_per_min=0, greeting_batch_sec=0,
        )
        return clock, buffer, scheduler

    async def test_airtime_cap_va_khong_co_question(self) -> None:
        _, buffer, scheduler = self.make()
        await buffer.add(payload("vui quá", "c")); await buffer.add(payload("giá bao nhiêu?", "q"))
        self.assertEqual((await scheduler.pick()).kind, "question")
        scheduler.record_airtime(16, "question")
        self.assertEqual((await scheduler.pick()).kind, "chat")
        _, buffer2, scheduler2 = self.make()
        await buffer2.add(payload("vui ghê", "c"))
        self.assertEqual((await scheduler2.pick()).kind, "chat")

    async def test_token_bucket_chan_burst(self) -> None:
        _, buffer, scheduler = self.make(burst=1)
        await buffer.add(payload("vui ghê", "a")); await buffer.add(payload("hay quá", "b"))
        self.assertEqual((await scheduler.pick()).kind, "chat")
        self.assertIsNone(await scheduler.pick())

    async def test_greeting_batch_mot_tts_khong_llm(self) -> None:
        records = [payload("chào shop", value) for value in ("Lan", "Minh", "Hùng")]
        for record in records: record["lane"] = "greeting"
        scheduled = ScheduledReply(
            "greeting", records[0], records,
            "Chào Lan, Minh với Hùng nha", False,
        )
        llm = FakeLLM(); agent = LiveAgent(llm, "system", 0)
        tts = FakeTTS(); clock = Clock()
        await _speak_template(
            agent, tts, scheduled, reply_gap_sec=0, sleep=clock.sleep, clock=clock
        )
        self.assertEqual(tts.calls, ["Chào Lan, Minh với Hùng nha"])
        self.assertEqual(llm.calls, 0)
        self.assertFalse(agent.recent_replies()[0]["llm"])


if __name__ == "__main__": unittest.main()
