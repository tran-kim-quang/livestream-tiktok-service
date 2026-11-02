from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.agent.responder import LiveAgent, default_system_prompt
from app.agent.speech import SpeechMetrics, _agent_audio_stream
from app.agent.tts_text import TtsTextSanitizer, sanitize_for_tts
from app.main import resolve_system_prompt
from app.tts.base import TTSEngine


class TtsTextSanitizerTest(unittest.TestCase):
    def test_giu_whitelist_va_ngan_sach_chung_giua_chunk(self) -> None:
        sanitizer = TtsTextSanitizer(1)
        self.assertEqual(sanitizer("Haha [laughter], vui quá!"), "Haha [laughter], vui quá!")
        self.assertEqual(sanitizer("Ừm [sigh], thử lại."), "Ừm, thử lại.")

    def test_max_tags_zero_va_bo_tag_khong_ho_tro(self) -> None:
        text = "[laughter] A [pause 500ms] [cười] [SIGH] nhé."
        self.assertEqual(sanitize_for_tts(text, max_tags=0), "A nhé.")

    def test_emoji_markdown_dau_lap_va_dau_tieng_viet(self) -> None:
        text = "# **Tiếng_Việt** 🎉!!!\n- `Ổn`?!?"
        self.assertEqual(sanitize_for_tts(text, max_tags=1), "Tiếng Việt! Ổn?")
        self.assertEqual(sanitize_for_tts("Trời 30°C 🔥", max_tags=1), "Trời 30°C")

    def test_chi_bo_stage_direction_ngan_da_biet(self) -> None:
        self.assertEqual(
            sanitize_for_tts("(cười lớn) Xin chào (nội dung bình thường).", max_tags=1),
            "Xin chào (nội dung bình thường).",
        )

    def test_idempotent_va_chi_tag_thi_rong(self) -> None:
        cleaned = sanitize_for_tts("Haha [laughter] 🎉!!!", max_tags=1)
        self.assertEqual(sanitize_for_tts(cleaned, max_tags=1), cleaned)
        self.assertEqual(sanitize_for_tts("[laughter]!?", max_tags=1), "")


class StreamingLLM:
    async def stream_chat(self, messages):
        for delta in ("Haha [laugh", "ter], vui quá 🎉!!! ", "Thật á [question-ah]? Để xem."):
            yield delta


class RecordingTTS(TTSEngine):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b""

    async def synthesize_pcm(self, text: str, voice_id=None, num_step=None) -> bytes:
        self.calls.append(text)
        return text.encode("utf-8")

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class TtsPipelineSanitizerTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_khong_stream_tra_ve_text_da_lam_sach(self) -> None:
        class ChatLLM:
            async def chat(self, messages):
                return "**Chào** 🎉!!! [pause 500ms]"

        agent = LiveAgent(ChatLLM(), "system", 1, greeting_enabled=False)
        record = await agent.reply({"comment": "A", "user": {}})
        self.assertEqual(record["reply"], "Chào!")
        self.assertEqual(agent._history[-1][1], "Chào!")

    async def test_stream_khong_cat_tag_va_bo_chunk_rong(self) -> None:
        tts = RecordingTTS()
        agent = LiveAgent(
            StreamingLLM(), "system", 1, greeting_enabled=False, tts_max_tags=1
        )
        metrics = SpeechMetrics(asyncio.get_running_loop().time())
        audio = [
            chunk async for chunk in _agent_audio_stream(
                agent, tts, {"comment": "A", "user": {}}, None, metrics=metrics, single_shot=False
            )
        ]
        self.assertTrue(audio)
        self.assertEqual(" ".join(tts.calls[:2]), "Haha [laughter], vui quá!")
        self.assertTrue(any("Thật á?" in chunk for chunk in tts.calls))
        self.assertFalse(any("[question-ah]" in chunk for chunk in tts.calls))
        self.assertEqual(metrics.reply, "Haha [laughter], vui quá! Thật á? Để xem.")
        self.assertEqual(agent.recent_replies()[0]["reply"], metrics.reply)

        tts = RecordingTTS()

        class OnlyTagLLM:
            async def stream_chat(self, messages):
                yield "[laughter]"

        empty_agent = LiveAgent(
            OnlyTagLLM(), "system", 0, greeting_enabled=False, tts_max_tags=1
        )
        self.assertEqual([
            chunk async for chunk in _agent_audio_stream(
                empty_agent, tts, {"comment": "A", "user": {}}, None, single_shot=False
            )
        ], [])
        self.assertEqual(tts.calls, [])


class SingleShotSpeechTest(unittest.IsolatedAsyncioTestCase):
    async def test_gop_greeting_va_reply_thanh_mot_lan_tts(self) -> None:
        tts = RecordingTTS()
        agent = LiveAgent(StreamingLLM(), "system", 1, greeting_enabled=True, tts_max_tags=1)
        metrics = SpeechMetrics(asyncio.get_running_loop().time())
        audio = [
            chunk async for chunk in _agent_audio_stream(
                agent, tts, {"comment": "A", "user": {"nickname": "Lan"}}, None,
                metrics=metrics, single_shot=True,
            )
        ]
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(tts.calls), 1)
        self.assertTrue(tts.calls[0].startswith(metrics.greeting))
        self.assertTrue(tts.calls[0].endswith("Haha [laughter], vui quá! Thật á? Để xem."))
        self.assertEqual(agent.recent_replies()[0]["reply"], tts.calls[0])

    async def test_llm_chi_tra_tag_thi_khong_doc_greeting(self) -> None:
        tts = RecordingTTS()

        class OnlyTagLLM:
            async def stream_chat(self, messages):
                yield "[laughter]"

        agent = LiveAgent(OnlyTagLLM(), "system", 0, greeting_enabled=True, tts_max_tags=1)
        audio = [
            chunk async for chunk in _agent_audio_stream(
                agent, tts, {"comment": "A", "user": {"nickname": "Lan"}}, None,
                single_shot=True,
            )
        ]
        self.assertEqual((audio, tts.calls), ([], []))


class LiveContextModeTest(unittest.IsolatedAsyncioTestCase):
    class CaptureLLM:
        def __init__(self) -> None:
            self.messages: list[dict[str, str]] = []

        async def chat(self, messages):
            self.messages = messages
            return "Ok nha."

    class FakeRelevance:
        def __init__(self, value: float | None) -> None:
            self.value = value
            self.calls: list[str] = []

        async def similarity(self, text: str) -> float | None:
            self.calls.append(text)
            return self.value

    def _agent(self, llm, relevance=None) -> LiveAgent:
        return LiveAgent(
            llm, "system", 0, greeting_enabled=False,
            live_context="- Rank vàng", context_threshold=0.4, relevance=relevance,
        )

    async def test_similarity_cao_gui_context_va_chi_dan_khong_biet(self) -> None:
        llm = self.CaptureLLM()
        record = await self._agent(llm).reply({"comment": "rank gì", "user": {}, "similarity": 0.55})
        last = llm.messages[-1]["content"]
        self.assertIn("- Rank vàng", last)
        self.assertIn("không biết", last)
        self.assertEqual((record["context_mode"], record["similarity"]), ("context", 0.55))

    async def test_similarity_thap_chat_binh_thuong_khong_kem_context(self) -> None:
        llm = self.CaptureLLM()
        record = await self._agent(llm).reply({"comment": "hello", "user": {}, "similarity": 0.39})
        self.assertNotIn("- Rank vàng", llm.messages[-1]["content"])
        self.assertIn("chat bình thường", llm.messages[-1]["content"])
        self.assertEqual(record["context_mode"], "chat")

    async def test_comment_api_embed_ngay_va_khong_gui_history(self) -> None:
        llm = self.CaptureLLM()
        relevance = self.FakeRelevance(0.7)
        agent = self._agent(llm, relevance)
        await agent.reply({"comment": "rank gì", "user": {}})
        await agent.reply({"comment": "còn gì nữa", "user": {}})
        self.assertEqual(relevance.calls, ["rank gì", "còn gì nữa"])
        self.assertEqual([m["role"] for m in llm.messages], ["system", "user"])

    async def test_embedding_loi_gui_context_voi_chi_dan_ket_hop(self) -> None:
        llm = self.CaptureLLM()
        record = await self._agent(llm, self.FakeRelevance(None)).reply({"comment": "hi", "user": {}})
        self.assertIn("- Rank vàng", llm.messages[-1]["content"])
        self.assertEqual(record["context_mode"], "unknown")

    async def test_khong_co_context_giu_nguyen_message(self) -> None:
        llm = self.CaptureLLM()
        agent = LiveAgent(llm, "system", 0, greeting_enabled=False)
        record = await agent.reply({"comment": "hi", "user": {"nickname": "A"}})
        self.assertEqual(llm.messages[-1]["content"], "Người xem: A\nComment: hi")
        self.assertEqual(record["context_mode"], "none")


class PromptGuideTest(unittest.TestCase):
    def test_guide_duoc_noi_vao_prompt_tuy_chinh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guide = Path(directory) / "guide.md"
            guide.write_text("Hướng dẫn TTS", encoding="utf-8")
            self.assertEqual(
                resolve_system_prompt("host", "Prompt riêng", "", str(guide)),
                "Prompt riêng\n\nHướng dẫn TTS",
            )

    def test_prompt_mac_dinh_co_ten_tro_ly(self) -> None:
        self.assertTrue(
            default_system_prompt("host").startswith(
                "Bạn là Bomne, trợ lý cho streamer host trong buổi live TikTok."
            )
        )

    def test_guide_rong_tat_tag_trong_prompt_mac_dinh(self) -> None:
        prompt = resolve_system_prompt("host", "", "", "")
        self.assertIn(
            "Không dùng emoji, markdown hay ký hiệu vì câu trả lời sẽ được đọc bằng TTS.",
            prompt,
        )
        self.assertNotIn("tag biểu cảm", prompt)
        self.assertIn("tag biểu cảm", default_system_prompt("host", tts_tags=True))


if __name__ == "__main__":
    unittest.main()
