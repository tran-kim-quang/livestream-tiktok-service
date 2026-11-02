from __future__ import annotations

import asyncio
import os
import unittest

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"
os.environ["LLM_API_KEY"] = ""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.loop import auto_reply_loop
from app.agent.responder import LiveAgent
from app.api import agent as agent_api
from app.api.deps import get_agent, get_player
from app.tts.base import TTSEngine


class FakeLLM:
    async def stream_chat(self, messages):
        yield "Một câu."

    async def chat(self, messages):
        return "Một câu."


class FakeTTS(TTSEngine):
    async def synthesize(self, text, voice_id=None):
        return b"wav"

    async def synthesize_pcm(self, text, voice_id=None, num_step=None):
        return b"\0" * 48

    async def list_voices(self):
        return []

    async def status(self):
        return {"ok": True}


class TwoComments:
    def __init__(self) -> None:
        self.items = [
            {"comment": "Một", "user": {}},
            {"comment": "Hai", "user": {}},
        ]
        self.second_taken = asyncio.Event()

    async def next(self, timeout=0):
        if self.items:
            result = self.items.pop(0)
            if len(self.items) == 0:
                self.second_taken.set()
            return result
        await asyncio.sleep(10)


class AgentBridgePacingTest(unittest.IsolatedAsyncioTestCase):
    def make_player(self, agent: LiveAgent):
        queue: asyncio.Queue = asyncio.Queue()
        subscriber = agent.subscribe(queue)
        agent.mark_player(subscriber, {
            "type": "hello", "role": "player", "name": "test",
        })
        return subscriber, queue

    async def test_cho_ack_truoc_comment_ke_va_ghi_latency(self) -> None:
        agent = LiveAgent(FakeLLM(), "system", 0)
        subscriber, queue = self.make_player(agent)
        comments = TwoComments()
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(), auto_speak=True,
            skip_when_no_listener=True, reply_gap_sec=0,
            played_ack_grace_sec=1,
        ))
        try:
            reply_end = None
            while reply_end is None:
                frame = await asyncio.wait_for(queue.get(), 0.5)
                if isinstance(frame, dict) and frame.get("type") == "reply_end":
                    reply_end = frame
            await asyncio.sleep(0.02)
            self.assertFalse(comments.second_taken.is_set())
            self.assertTrue(agent.acknowledge_played(reply_end["id"], {
                "type": "played", "id": reply_end["id"],
                "first_audio_played_ms": 17,
            }))
            await asyncio.wait_for(comments.second_taken.wait(), 0.5)
            self.assertEqual(agent.last_latencies["first_audio_played"], 17)
            self.assertEqual(agent.last_latencies["first_audio_played_ms"], 17)
        finally:
            agent.unsubscribe(subscriber)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_timeout_ack_van_tiep_tuc(self) -> None:
        agent = LiveAgent(FakeLLM(), "system", 0)
        subscriber, _ = self.make_player(agent)
        comments = TwoComments()
        task = asyncio.create_task(auto_reply_loop(
            comments, agent, FakeTTS(), auto_speak=True,
            skip_when_no_listener=True, reply_gap_sec=0,
            played_ack_grace_sec=0.01,
        ))
        try:
            await asyncio.wait_for(comments.second_taken.wait(), 0.5)
        finally:
            agent.unsubscribe(subscriber)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class AgentBridgeApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(agent_api.router)
        self.agent = LiveAgent(FakeLLM(), "system", 0)
        self.app.state.agent = self.agent
        self.app.state.audio_player = None
        self.app.dependency_overrides[get_agent] = lambda: self.agent
        self.app.dependency_overrides[get_player] = lambda: None

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def test_hello_danh_dau_player_status_va_stop_gui_flush(self) -> None:
        with TestClient(self.app) as client:
            with client.websocket_connect("/api/agent/ws") as websocket:
                websocket.send_json({
                    "type": "hello", "role": "player", "name": "win",
                    "backend": "sounddevice", "device": "Speakers",
                })
                for _ in range(50):
                    status = client.get("/api/agent/status").json()
                    if status["players"] == 1:
                        break
                self.assertEqual(status["players"], 1)
                self.assertEqual(status["listeners"], 1)
                stopped = client.post("/api/agent/audio/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertEqual(stopped.json()["bridges_flushed"], 1)
                self.assertEqual(websocket.receive_json(), {"type": "flush"})


if __name__ == "__main__":
    unittest.main()
