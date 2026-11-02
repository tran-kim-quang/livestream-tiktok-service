import asyncio
import os
import unittest

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"

from fastapi.testclient import TestClient

from app.api.deps import get_tts
from app.api.tts import plan_tts_chunks, split_sentences
from app.main import app
from app.tts.base import TTSEngine, VoiceNotFoundError


class FakeTTS(TTSEngine):
    def __init__(self) -> None:
        self.pcm_calls: list[tuple[str, int | None]] = []

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        return b"wav"

    async def synthesize_pcm(
        self, text: str, voice_id: str | None = None, num_step: int | None = None
    ) -> bytes:
        self.pcm_calls.append((text, num_step))
        if voice_id == "missing":
            raise VoiceNotFoundError("Không tìm thấy giọng 'missing'.")
        return (f"<{text}>".encode("utf-8") + b"\x00\x00")

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class SentenceSplitterTest(unittest.TestCase):
    def test_dau_cau_va_xuong_dong(self) -> None:
        self.assertEqual(
            split_sentences("Xin chào mọi người. Hôm nay khỏe không?\nTuyệt vời!"),
            ["Xin chào mọi người.", "Hôm nay khỏe không? Tuyệt vời!"],
        )

    def test_ghep_mau_ngan(self) -> None:
        self.assertEqual(split_sentences("Ừ. Đây là một câu đủ dài để đọc."), ["Ừ. Đây là một câu đủ dài để đọc."])

    def test_cat_cau_qua_dai(self) -> None:
        parts = split_sentences(("một cụm từ khá dài, " * 20).strip())
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 200 for part in parts))


class ChunkPlannerTest(unittest.TestCase):
    def test_cat_o_dau_phay(self) -> None:
        self.assertEqual(
            plan_tts_chunks("Xin chào mọi người, hôm nay chúng ta bắt đầu nhé.", 6)[0],
            "Xin chào mọi người,",
        )

    def test_cat_dung_ranh_gioi_tu(self) -> None:
        chunks = plan_tts_chunks("Một hai ba bốn năm sáu bảy tám chín mười mười một.", 6)
        self.assertEqual(chunks[0], "Một hai ba bốn năm sáu")

    def test_khong_de_duoi_mot_hai_tu(self) -> None:
        chunks = plan_tts_chunks("Một hai ba bốn năm sáu bảy tám.", 6)
        self.assertEqual(chunks, ["Một hai ba bốn năm sáu bảy tám."])

    def test_dau_phay_khong_de_duoi_hai_tu(self) -> None:
        self.assertEqual(plan_tts_chunks("Xin chào mọi người, bạn nhé.", 6), ["Xin chào mọi người, bạn nhé."])

    def test_text_ngan_chi_co_mot_chunk(self) -> None:
        self.assertEqual(plan_tts_chunks("Xin chào bạn.", 6), ["Xin chào bạn."])

    def test_xuong_dong_la_ket_thuc_chunk_dau(self) -> None:
        self.assertEqual(plan_tts_chunks("Xin chào bạn\nHôm nay trời đẹp.", 6)[0], "Xin chào bạn")

    def test_chunk_thu_hai_lon_dan_roi_tach_cau_binh_thuong(self) -> None:
        text = " ".join(f"từ{i}" for i in range(1, 25)) + ". Câu cuối đủ dài để giữ riêng."
        chunks = plan_tts_chunks(text, 6)
        self.assertEqual(len(chunks[0].split()), 6)
        self.assertLessEqual(len(chunks[1].split()), 12)
        self.assertGreaterEqual(len(chunks), 3)


class PipelineTTS(FakeTTS):
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.started_second = asyncio.Event()
        self.release_second = asyncio.Event()
        self.cancelled_second = asyncio.Event()

    async def synthesize_pcm(
        self, text: str, voice_id: str | None = None, num_step: int | None = None
    ) -> bytes:
        self.calls.append((text, num_step))
        if text == "hai":
            self.started_second.set()
            try:
                await self.release_second.wait()
            except asyncio.CancelledError:
                self.cancelled_second.set()
                raise
        return text.encode()


class StreamPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_prefetch_truoc_khi_consumer_doc_chunk_dau(self) -> None:
        engine = PipelineTTS()
        stream = engine.stream_sentences(["một", "hai"], first_chunk_num_step=8)
        first_task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(engine.started_second.wait(), 0.2)
        first = await first_task
        self.assertEqual(first, "một".encode())
        engine.release_second.set()
        self.assertEqual(await anext(stream), b"hai")
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)
        self.assertEqual(engine.calls, [("một", 8), ("hai", None)])

    async def test_dong_stream_huy_producer(self) -> None:
        engine = PipelineTTS()
        stream = engine.stream_sentences(["một", "hai"])
        self.assertEqual(await anext(stream), "một".encode())
        await asyncio.wait_for(engine.started_second.wait(), 0.2)
        await stream.aclose()
        await asyncio.wait_for(engine.cancelled_second.wait(), 0.2)


class StreamApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tts = FakeTTS()
        app.dependency_overrides[get_tts] = lambda: self.tts

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_pcm(self) -> None:
        with TestClient(app) as client:
            response = client.post("/api/tts/stream", json={"text": "Câu đầu tiên. Câu thứ hai đủ dài!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/pcm")
        self.assertEqual(response.headers["x-sample-rate"], "24000")
        self.assertIn("Câu đầu tiên.", response.content.decode("utf-8", errors="ignore"))

    def test_wav_co_header_streaming(self) -> None:
        with TestClient(app) as client:
            response = client.post("/api/tts/stream", json={"text": "Đây là một câu đủ dài.", "format": "wav"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content[:4], b"RIFF")
        self.assertEqual(response.content[8:12], b"WAVE")
        self.assertEqual(response.content[36:40], b"data")
        self.assertEqual(int.from_bytes(response.content[40:44], "little"), 0xFFFFFFFF)

    def test_404_truoc_chunk_dau(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/tts/stream",
                json={"text": "Đây là câu đủ dài.", "voice_id": "missing"},
            )
        self.assertEqual(response.status_code, 404)

    def test_tuy_chon_chunk_dau_va_num_step(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/tts/stream",
                json={
                    "text": "một hai ba bốn năm sáu bảy tám chín mười. Câu sau đủ dài để đọc.",
                    "first_chunk_max_words": 4,
                    "first_chunk_num_step": 4,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.tts.pcm_calls[0], ("một hai ba bốn", 4))
        self.assertTrue(all(num_step is None for _, num_step in self.tts.pcm_calls[1:]))

    def test_tuy_chon_chunk_dau_duoc_validate(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/api/tts/stream",
                json={"text": "Xin chào.", "first_chunk_num_step": 65, "first_chunk_max_words": 1},
            )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
