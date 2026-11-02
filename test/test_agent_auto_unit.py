from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock
from collections.abc import AsyncIterator
from typing import Any

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"
os.environ["LLM_API_KEY"] = ""

from app.agent.loop import auto_reply_loop
from app.agent.responder import LiveAgent
from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter
from app.tts.base import TTSEngine


class ScriptedLLM:
    def __init__(self, scripts: list[list[str | Exception]], delay: float = 0) -> None:
        self.scripts = scripts
        self.delay = delay
        self.stream_finished = False

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        script = self.scripts.pop(0)
        self.stream_finished = False
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item
            if self.delay:
                await asyncio.sleep(self.delay)
        self.stream_finished = True

    async def chat(self, messages: list[dict[str, str]]) -> str:
        return ""


class FakeTTS(TTSEngine):
    def __init__(self, llm: ScriptedLLM, audio_bytes: int = 480) -> None:
        self.llm = llm
        self.audio_bytes = audio_bytes
        self.first_before_llm_done = False

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b"wav"

    async def synthesize_pcm(
        self, text: str, voice_id: str | None = None, num_step: int | None = None
    ) -> bytes:
        if not self.first_before_llm_done:
            self.first_before_llm_done = not self.llm.stream_finished
        return b"\0" * self.audio_bytes

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class SequentialComments:
    def __init__(self, comments: list[dict], clock=None) -> None:
        self.comments = comments
        self.clock = clock
        self.taken_at: list[float] = []
        self.second_taken = asyncio.Event()

    async def next(self, timeout: float = 0) -> dict | None:
        if self.comments:
            if self.clock is not None:
                self.taken_at.append(self.clock())
            item = self.comments.pop(0)
            if len(self.taken_at) == 2:
                self.second_taken.set()
            return item
        await asyncio.sleep(10)
        return None


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def cancel(task: asyncio.Task) -> None:
    task.cancel()
    with unittest.TestCase().assertRaises(asyncio.CancelledError):
        await task


# Các test này kiểm tra luồng stream theo chunk
@mock.patch("app.agent.speech.AGENT_TTS_SINGLE_SHOT", False)
class AutoSpeakLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_order_va_audio_den_truoc_khi_llm_stream_xong(self) -> None:
        llm = ScriptedLLM([["Xin chào bạn,", " hôm nay vui nhé."]], delay=0.03)
        agent = LiveAgent(llm, "system", 1, greeting_templates=("{name} ơi,",))
        queue: asyncio.Queue[dict[str, Any] | bytes] = asyncio.Queue()
        agent.subscribers.add(queue)
        tts = FakeTTS(llm)
        comments = SequentialComments([{"comment": "Chào", "user": {"nickname": "Lan"}}])
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, tts, auto_speak=True, reply_gap_sec=0,
            skip_when_no_listener=False,
        ))
        try:
            frames = [await asyncio.wait_for(queue.get(), 0.5)]
            frames.append(await asyncio.wait_for(queue.get(), 0.5))
            self.assertEqual(frames[0]["type"], "reply_start")
            self.assertIsInstance(frames[1], bytes)
            self.assertFalse(llm.stream_finished)
            frames.extend([await asyncio.wait_for(queue.get(), 0.5) for _ in range(3)])
            self.assertIsInstance(frames[2], bytes)
            self.assertIsInstance(frames[3], bytes)
            self.assertEqual(frames[4]["type"], "reply_end")
            self.assertEqual(frames[4]["reply"], "Lan ơi, Xin chào bạn, hôm nay vui nhé.")
            self.assertIn("greeting_ready", frames[4]["latency_ms"])
            self.assertTrue(tts.first_before_llm_done)
        finally:
            await cancel(task)

    async def test_pacing_cho_audio_phat_xong_moi_lay_comment_ke(self) -> None:
        clock = FakeClock()
        llm = ScriptedLLM([["Câu một."], ["Câu hai."]])
        agent = LiveAgent(llm, "system", 0)
        agent.subscribers.add(asyncio.Queue())
        comments = SequentialComments([
            {"comment": "Một", "user": {}},
            {"comment": "Hai", "user": {}},
        ], clock)
        tts = FakeTTS(llm, audio_bytes=4800)  # 0,1 giây PCM.
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, tts, auto_speak=True, reply_gap_sec=0,
            skip_when_no_listener=False, sleep=clock.sleep, clock=clock,
        ))
        try:
            await asyncio.wait_for(comments.second_taken.wait(), 0.5)
            self.assertGreaterEqual(comments.taken_at[1] - comments.taken_at[0], 0.1)
            self.assertAlmostEqual(clock.sleeps[0], 0.2)
        finally:
            await cancel(task)

    async def test_reply_rong_bo_qua_loi_llm_phat_error_va_tiep_tuc(self) -> None:
        llm = ScriptedLLM([[], [RuntimeError("LLM hỏng")], ["Ổn rồi."]])
        agent = LiveAgent(llm, "system", 0)
        queue: asyncio.Queue[dict[str, Any] | bytes] = asyncio.Queue()
        agent.subscribers.add(queue)
        comments = SequentialComments([
            {"comment": "Rỗng", "user": {}},
            {"comment": "Lỗi", "user": {}},
            {"comment": "Tiếp", "user": {}},
        ])
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(llm), auto_speak=True, reply_gap_sec=0,
            skip_when_no_listener=False,
        ))
        try:
            frames = []
            while not any(isinstance(f, dict) and f.get("type") == "reply_end" for f in frames):
                frames.append(await asyncio.wait_for(queue.get(), 0.5))
            types = [f["type"] for f in frames if isinstance(f, dict)]
            self.assertEqual(types, ["reply_error", "reply_start", "reply_end"])
            self.assertIn("LLM hỏng", frames[0]["error"])
        finally:
            await cancel(task)

    async def test_khong_listener_thi_khong_consume(self) -> None:
        comments = CommentBuffer(
            comment_filter=CommentFilter(blocklist=(), dedup_window_sec=0),
            user_cooldown_sec=0,
        )
        await comments.add({"comment": "Còn trong buffer", "user": {"id": "1"}})
        agent = LiveAgent(ScriptedLLM([["Không được gọi."]]), "system", 0)
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(agent.llm), auto_speak=True,
            skip_when_no_listener=True,
        ))
        try:
            await asyncio.sleep(0.08)
            self.assertEqual(await comments.length(), 1)
            self.assertEqual(agent.stats()["replied"], 0)
        finally:
            await cancel(task)


if __name__ == "__main__":
    unittest.main()
