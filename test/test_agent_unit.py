from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from unittest import mock

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"
os.environ["LLM_API_KEY"] = ""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.llm import LLMError, OpenAICompatLLM
from app.agent.loop import auto_reply_loop
from app.agent.responder import LiveAgent, default_system_prompt
from app.api import agent as agent_api
from app.api.deps import get_agent, get_tts
from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter
from app.tts.base import TTSEngine


class LLMClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_payload_auth_va_non_stream(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": " Xin chào "}}]})

        llm = OpenAICompatLLM(
            "https://llm.example/v1", "secret-key", "model-x", 5, 123, 0.2,
            transport=httpx.MockTransport(handler), max_rps=0,
        )
        try:
            self.assertEqual(await llm.chat([{"role": "user", "content": "Chào"}]), "Xin chào")
        finally:
            await llm.aclose()
        request = requests[0]
        self.assertEqual(request.headers["authorization"], "Bearer secret-key")
        self.assertEqual(request.url.path, "/v1/chat/completions")
        self.assertEqual(json.loads(request.content), {
            "model": "model-x", "messages": [{"role": "user", "content": "Chào"}],
            "max_tokens": 123, "temperature": 0.2, "stream": False,
        })

    async def test_sse_bo_qua_delta_rong_va_done(self) -> None:
        body = (
            b'data: {"choices":[{"delta":{"content":"Xin "}}]}\n\n'
            b'data: {"choices":[{"delta":{}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"chao"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        llm = OpenAICompatLLM(
            "https://llm.example/v1", "key", "m", 5, 10, 0.7,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)), max_rps=0,
        )
        try:
            self.assertEqual([part async for part in llm.stream_chat([])], ["Xin ", "chao"])
        finally:
            await llm.aclose()

    async def test_429_retry_va_error_mapping(self) -> None:
        calls = 0

        async def retry_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, text="rate")
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        llm = OpenAICompatLLM(
            "https://x/v1", "key", "m", 5, 10, 0.7,
            transport=httpx.MockTransport(retry_handler), max_rps=0,
        )
        self.assertEqual(await llm.chat([]), "ok")
        self.assertEqual(calls, 2)
        await llm.aclose()

        bad = OpenAICompatLLM(
            "https://x/v1", "key", "m", 5, 10, 0.7,
            transport=httpx.MockTransport(lambda _: httpx.Response(500, text="provider failed")),
            max_rps=0,
        )
        try:
            with self.assertRaises(LLMError) as caught:
                await bad.chat([])
            self.assertEqual(caught.exception.status, 500)
            self.assertIn("provider failed", str(caught.exception))
            self.assertNotIn("key", str(caught.exception))
        finally:
            await bad.aclose()


class FakeLLM:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = replies or ["Trả lời"]
        self.messages: list[list[dict[str, str]]] = []
        self.stream_finished = False

    async def chat(self, messages: list[dict[str, str]]) -> str:
        self.messages.append(messages)
        return self.replies.pop(0)

    async def stream_chat(self, messages: list[dict[str, str]]):
        self.messages.append(messages)
        yield "Xin chào bạn,"
        yield " hôm"
        await asyncio.sleep(0.03)
        yield " nay vui nhé."
        self.stream_finished = True


class LiveAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_mac_dinh_tra_loi_tho_tuc_lich_su_khong_rong(self) -> None:
        prompt = default_system_prompt("host")
        self.assertIn("thô tục", prompt)
        self.assertIn("vẫn phải được trả lời", prompt)
        self.assertIn("lịch sự", prompt)
        self.assertIn("Không bao giờ trả về rỗng", prompt)
        self.assertNotIn("lịch sự hoặc trả về rỗng", prompt)
        self.assertIn("KHÔNG chào", prompt)
        self.assertNotIn("Gọi tên người xem khi hợp lý", prompt)
        self.assertIn(
            "Không dùng emoji, markdown hay ký hiệu vì câu trả lời sẽ được đọc bằng TTS.",
            prompt,
        )
        self.assertIn("tag biểu cảm", default_system_prompt("host", tts_tags=True))
        prompt_without_greeting = default_system_prompt("host", greeting_enabled=False)
        self.assertNotIn("KHÔNG chào", prompt_without_greeting)
        self.assertIn("Gọi tên người xem khi hợp lý", prompt_without_greeting)

    async def test_prompt_nickname_va_cat_history(self) -> None:
        llm = FakeLLM(["một", "hai", "ba"])
        agent = LiveAgent(
            llm, "system", history_turns=1,
            greeting_templates=("{name} ơi,",),
        )
        await agent.reply({"comment": "A", "user": {"nickname": "Lan"}})
        await agent.reply({"comment": "B", "user": {"nickname": "Minh"}})
        await agent.reply({"comment": "C", "user": {"nickname": "An"}})
        last = llm.messages[-1]
        self.assertEqual([item["role"] for item in last], ["system", "user", "assistant", "user"])
        self.assertEqual(last[1]["content"], "Người xem: Minh\nComment: B")
        self.assertEqual(last[2]["content"], "hai")
        self.assertEqual(last[-1]["content"], "Người xem: An\nComment: C")


class FakeTTS(TTSEngine):
    def __init__(self, llm: FakeLLM) -> None:
        self.llm = llm
        self.calls: list[str] = []
        self.first_started_before_llm_done = False

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b"wav"

    async def synthesize_pcm(self, text: str, voice_id: str | None = None, num_step=None) -> bytes:
        if not self.calls:
            self.first_started_before_llm_done = not self.llm.stream_finished
        self.calls.append(text)
        await asyncio.sleep(0.01)
        return text.encode()

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


# Các test này kiểm tra luồng stream theo chunk
@mock.patch("app.agent.speech.AGENT_TTS_SINGLE_SHOT", False)
class AgentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(agent_api.router)
        self.llm = FakeLLM(["Chào Lan"])
        self.agent = LiveAgent(
            self.llm, "system", 2, greeting_templates=("{name} ơi,",)
        )
        self.tts = FakeTTS(self.llm)

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def test_disabled(self) -> None:
        self.app.dependency_overrides[get_agent] = lambda: None
        self.app.dependency_overrides[get_tts] = lambda: None
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/agent/reply", json={"comment": "Chào"}).status_code, 503)
            self.assertEqual(client.get("/api/agent/replies").status_code, 503)

    def test_reply_replies_va_speak_pipeline(self) -> None:
        self.app.dependency_overrides[get_agent] = lambda: self.agent
        self.app.dependency_overrides[get_tts] = lambda: self.tts
        with TestClient(self.app) as client:
            reply = client.post(
                "/api/agent/reply", json={"comment": "Chào", "nickname": "Lan🔥"}
            )
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["reply"], "Lan ơi, Chào Lan")
            self.assertEqual(reply.json()["display_name"], "Lan")
            listed = client.get("/api/agent/replies").json()["replies"]
            self.assertEqual(listed[0]["reply"], "Lan ơi, Chào Lan")
            spoken = client.post("/api/agent/speak", json={"comment": "Nói gì đi"})
        self.assertEqual(spoken.status_code, 200)
        self.assertTrue(spoken.content)
        self.assertTrue(self.tts.first_started_before_llm_done)
        self.assertGreaterEqual(len(self.tts.calls), 3)
        self.assertEqual(
            self.agent.recent_replies()[0]["reply"],
            "bạn ơi, Xin chào bạn, hôm nay vui nhé.",
        )


class AutoLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_consume_va_store_reply(self) -> None:
        comments = CommentBuffer(
            comment_filter=CommentFilter(blocklist=(), dedup_window_sec=0),
            user_cooldown_sec=0,
        )
        await comments.add({"comment": "Có ai không?", "user": {"id": "1", "nickname": "Lan"}})
        agent = LiveAgent(
            FakeLLM(["Có Lan đây"]), "system", 1,
            greeting_templates=("{name} ơi,",),
        )
        task = asyncio.create_task(auto_reply_loop(comments, agent, skip_when_no_listener=False))
        try:
            deadline = time.monotonic() + 0.5
            while not agent.recent_replies() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(agent.recent_replies()[0]["reply"], "Lan ơi, Có Lan đây")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
