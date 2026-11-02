import os
import unittest

os.environ["TIKTOK_USERNAME"] = ""
os.environ["TTS_ENABLED"] = "0"

from fastapi.testclient import TestClient

from app.api.deps import get_tts
from app.main import app
from app.tts.base import TTSEngine, VoiceNotFoundError


class UnknownVoiceTTS(TTSEngine):
    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        raise VoiceNotFoundError(f"Không tìm thấy giọng '{voice_id}'.")

    async def list_voices(self) -> list[dict]:
        return []

    async def status(self) -> dict:
        return {"ok": True}


class ApiDisabledTest(unittest.TestCase):
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_api_khi_tts_tat(self) -> None:
        with TestClient(app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertIsNone(health.json()["tts"])

            self.assertEqual(client.get("/api/queue/length").json(), {"queue_length": 0})
            self.assertEqual(
                client.get("/api/tts/status").json(),
                {"ok": False, "enabled": False},
            )
            self.assertEqual(
                client.post("/api/tts/synthesize", json={"text": "Xin chao"}).status_code,
                503,
            )

    def test_giong_khong_ton_tai_tra_404(self) -> None:
        app.dependency_overrides[get_tts] = lambda: UnknownVoiceTTS()
        with TestClient(app) as client:
            response = client.post(
                "/api/tts/synthesize",
                json={"text": "Xin chào", "voice_id": "khong-co"},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Không tìm thấy giọng 'khong-co'."})


if __name__ == "__main__":
    unittest.main()
