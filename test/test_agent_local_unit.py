from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"
os.environ["LLM_API_KEY"] = ""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.loop import auto_reply_loop
from app.agent.responder import LiveAgent
from app.api import agent as agent_api
from app.api.deps import get_agent, get_player, get_tts
from app.audio.player import LocalAudioPlayer
from app.tts.base import TTSEngine


class FakePlayer(LocalAudioPlayer):
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.events: list[str] = []
        self.flushed = False
        self._active = True

    async def start(self) -> None:
        self._active = True

    async def play(self, pcm: bytes) -> None:
        self.chunks.append(pcm)
        self.events.append("play")

    async def drain(self) -> None:
        self.events.append("drain")

    async def flush(self) -> None:
        self.flushed = True
        self.chunks.clear()

    async def aclose(self) -> None:
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def queued_sec(self) -> float:
        return sum(map(len, self.chunks)) / 48000

    def info(self) -> dict[str, Any]:
        return {
            "backend": "fake", "device": "test", "active": self.active,
            "queued_sec": self.queued_sec,
        }


class FakeLLM:
    async def stream_chat(self, messages):
        yield "Xin chào,"
        yield " bạn nhé."

    async def chat(self, messages):
        return "Xin chào"


class FakeTTS(TTSEngine):
    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b"wav"

    async def synthesize_pcm(self, text, voice_id=None, num_step=None) -> bytes:
        return text.encode("utf-8") * 100

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class OneComment:
    def __init__(self) -> None:
        self.taken = asyncio.Event()
        self.done = False

    async def next(self, timeout=0):
        if not self.done:
            self.done = True
            self.taken.set()
            return {"comment": "Chào", "user": {"nickname": "Lan"}}
        await asyncio.sleep(10)


class LocalAutoLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_la_listener_phat_dung_thu_tu_va_drain_de_pacing(self) -> None:
        player = FakePlayer()
        agent = LiveAgent(
            FakeLLM(), "system", 0, greeting_templates=("{name} ơi,",)
        )
        frames: asyncio.Queue[dict | bytes] = asyncio.Queue()
        agent.subscribers.add(frames)
        comments = OneComment()
        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            await asyncio.sleep(0)

        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(), auto_speak=True,
            skip_when_no_listener=True, reply_gap_sec=0.25,
            sleep=fake_sleep, player=player,
        ))
        try:
            while "drain" not in player.events:
                await asyncio.wait_for(asyncio.sleep(0.01), 0.2)
            self.assertEqual(len(player.chunks), 3)
            received = [await frames.get() for _ in range(5)]
            self.assertEqual(
                [type(item) for item in received], [dict, bytes, bytes, bytes, dict]
            )
            self.assertEqual(player.events, ["play", "play", "play", "drain"])
            self.assertIn("first_audio_played", received[-1]["latency_ms"])
            self.assertIn(0.25, sleep_calls)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_local_khong_ws_van_consume_comment(self) -> None:
        player = FakePlayer()
        agent = LiveAgent(FakeLLM(), "system", 0)
        comments = OneComment()
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(), auto_speak=True,
            skip_when_no_listener=True, reply_gap_sec=0, player=player,
        ))
        try:
            await asyncio.wait_for(comments.taken.wait(), 0.3)
            self.assertTrue(comments.done)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class LocalAgentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(agent_api.router)
        self.agent = LiveAgent(
            FakeLLM(), "system", 0, greeting_templates=("{name} ơi,",)
        )
        self.tts = FakeTTS()
        self.player = FakePlayer()
        self.app.dependency_overrides[get_agent] = lambda: self.agent
        self.app.dependency_overrides[get_tts] = lambda: self.tts
        self.app.dependency_overrides[get_player] = lambda: self.player

    def test_speak_local_json_status_va_flush(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                "/api/agent/speak", json={"comment": "Thử loa", "play_local": True}
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["reply"], "bạn ơi, Xin chào, bạn nhé.")
            self.assertEqual(len(self.player.chunks), 3)
            self.assertEqual(
                set(data["latency_ms"]),
                {"llm_first_token", "first_audio", "greeting_ready", "total"},
            )
            self.assertGreater(data["audio_sec"], 0)
            self.assertEqual(client.get("/api/agent/status").json()["player"]["backend"], "fake")
            stopped = client.post("/api/agent/audio/stop")
            self.assertEqual(stopped.status_code, 200)
            self.assertTrue(self.player.flushed)

    def test_speak_local_503_khi_khong_co_player(self) -> None:
        self.app.dependency_overrides[get_player] = lambda: None
        with TestClient(self.app) as client:
            response = client.post(
                "/api/agent/speak", json={"comment": "Thử loa", "play_local": True}
            )
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
