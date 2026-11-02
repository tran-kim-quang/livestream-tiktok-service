from __future__ import annotations

import asyncio
import unittest
from collections.abc import AsyncIterator

from app.agent.names import speakable_name
from app.agent.responder import LiveAgent
from app.agent.speech import SpeechMetrics, _agent_audio_stream
from app.config import TTS_FIRST_CHUNK_NUM_STEP, TTS_NUM_STEP
from app.tts.base import TTSEngine


class SpeakableNameTest(unittest.TestCase):
    def test_emoji_only_fallback_sang_unique_id(self) -> None:
        self.assertEqual(
            speakable_name({"nickname": "🔥✨", "unique_id": "saberg_6"}),
            "saberg 6",
        )

    def test_bo_emoji_va_chuan_hoa_unicode_trang_tri(self) -> None:
        self.assertEqual(speakable_name({"nickname": "Saber 🔥🔥"}), "Saber")
        self.assertEqual(speakable_name({"nickname": "𝓢𝓪𝓫𝓮𝓻"}), "Saber")
        self.assertEqual(speakable_name({"nickname": "S̷a̷b̷e̷r̷"}), "Saber")
        self.assertEqual(speakable_name({"nickname": "ᴄᴏɴᴀɴ ⚡"}), "conan")

    def test_giu_dau_tieng_viet_va_gioi_han_ten_dai(self) -> None:
        self.assertEqual(speakable_name({"nickname": "Nguyễn Ánh"}), "Nguyễn Ánh")
        self.assertEqual(
            speakable_name({"nickname": "Nguyễn Văn Anh Siêu Dài"}),
            "Nguyễn Văn Anh",
        )
        self.assertLessEqual(
            len(speakable_name({"nickname": "Một Cái Tên Rất Rất Dài"})), 24
        )


class FirstChoiceRng:
    @staticmethod
    def choice(items):
        return items[0]


class TextGreetingTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_day_du_history_khong_co_greeting_va_template_luan_phien(self) -> None:
        class LLM:
            def __init__(self) -> None:
                self.messages = []

            async def chat(self, messages):
                self.messages.append(messages)
                return "Nội dung trả lời."

        llm = LLM()
        agent = LiveAgent(
            llm,
            "Prompt riêng.",
            2,
            greeting_templates=("{name} ơi,", "Chào {name},", "À {name},"),
            rng=FirstChoiceRng(),
        )
        comment = {"comment": "Hỏi gì đó", "user": {"nickname": "Saber 🔥"}}
        records = [await agent.reply(comment) for _ in range(3)]

        self.assertEqual(
            [record["reply"].split(" Nội dung")[0] for record in records],
            ["Saber ơi,", "Chào Saber,", "Saber ơi,"],
        )
        self.assertTrue(all(record["display_name"] == "Saber" for record in records))
        self.assertEqual(llm.messages[-1][-2]["content"], "Nội dung trả lời.")
        self.assertIn("KHÔNG chào", llm.messages[-1][0]["content"])
        self.assertNotIn("Saber ơi,", llm.messages[-1][-2]["content"])

    async def test_prompt_tat_greeting_khong_co_lenh_cam_chao(self) -> None:
        class LLM:
            async def chat(self, messages):
                return "Trả lời."

        agent = LiveAgent(LLM(), "Prompt riêng.", 0, greeting_enabled=False)
        record = await agent.reply({"comment": "A", "user": {"nickname": "Lan"}})
        self.assertEqual(record["reply"], "Trả lời.")
        self.assertNotIn("KHÔNG chào", agent.system_prompt)


class GatedLLM:
    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.allow_first_delta = asyncio.Event()

    async def stream_chat(self, messages) -> AsyncIterator[str]:
        await self.allow_first_delta.wait()
        for delta in self.deltas:
            yield delta


class EmptyLLM:
    async def stream_chat(self, messages) -> AsyncIterator[str]:
        yield ""
        yield "   "


class RecordingTTS(TTSEngine):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.greeting_ready = asyncio.Event()

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b"wav"

    async def synthesize_pcm(
        self, text: str, voice_id: str | None = None, num_step: int | None = None
    ) -> bytes:
        self.calls.append((text, num_step))
        if len(self.calls) == 1:
            self.greeting_ready.set()
        return text.encode("utf-8")

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class GreetingSpeechPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_tong_hop_truoc_token_nhung_chi_release_sau_delta(self) -> None:
        llm = GatedLLM([
            "Một hai ba bốn năm sáu ",
            "bảy tám chín mười mười một mười hai ",
        ])
        tts = RecordingTTS()
        agent = LiveAgent(
            llm, "system", 1, greeting_templates=("{name} ơi,",)
        )
        metrics = SpeechMetrics(asyncio.get_running_loop().time())
        stream = _agent_audio_stream(
            agent, tts,
            {"comment": "A", "user": {"nickname": "Lan"}},
            None, metrics=metrics,
        )
        first_audio = asyncio.create_task(anext(stream))
        await asyncio.wait_for(tts.greeting_ready.wait(), 0.2)
        self.assertFalse(llm.allow_first_delta.is_set())
        self.assertFalse(first_audio.done())

        llm.allow_first_delta.set()
        chunks = [await asyncio.wait_for(first_audio, 0.2)]
        chunks.extend([chunk async for chunk in stream])
        self.assertEqual(chunks[0], "Lan ơi,".encode())
        self.assertEqual(
            [num_step for _, num_step in tts.calls],
            [TTS_FIRST_CHUNK_NUM_STEP, TTS_FIRST_CHUNK_NUM_STEP, TTS_NUM_STEP],
        )
        self.assertIsNotNone(metrics.greeting_ready_at)
        self.assertEqual(
            metrics.reply,
            "Lan ơi, Một hai ba bốn năm sáu bảy tám chín mười mười một mười hai",
        )

    async def test_llm_rong_khong_emit_greeting_audio(self) -> None:
        tts = RecordingTTS()
        agent = LiveAgent(
            EmptyLLM(), "system", 0, greeting_templates=("{name} ơi,",)
        )
        stream = _agent_audio_stream(
            agent, tts, {"comment": "A", "user": {"nickname": "Lan"}}, None
        )
        self.assertEqual([chunk async for chunk in stream], [])
        self.assertEqual(tts.calls, [("Lan ơi,", TTS_FIRST_CHUNK_NUM_STEP)])
        self.assertEqual(agent.recent_replies()[0]["reply"], "")


if __name__ == "__main__":
    unittest.main()
