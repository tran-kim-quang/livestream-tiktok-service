import json
import unittest

import httpx

from app.gpu import GpuNode
from app.tts.voicestudio import VoiceStudioTTS


class GpuNodeTest(unittest.TestCase):
    def test_info_khong_lo_password(self) -> None:
        node = GpuNode(
            host="gpu.example", ssh_user="gpu", ssh_port=2222,
            ssh_key_path="/secret/key", ssh_password="mat-khau",
        )
        self.assertTrue(node.use_ssh)
        self.assertNotIn("password", node.info())
        self.assertNotIn("mat-khau", repr(node.info()))
        self.assertNotIn("mat-khau", repr(node))


class VoiceStudioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/profiles":
                return httpx.Response(200, json=[{"id": "voice-1", "name": "Giọng một", "extra": 7}])
            if request.url.path == "/model/status":
                return httpx.Response(200, json={"status": "ready", "loaded": True, "checkpoint": "k2-fsa/OmniVoice"})
            if request.url.path == "/v1/audio/speech":
                payload = json.loads(request.content)
                return httpx.Response(200, content=b"PCM" if payload["response_format"] == "pcm" else b"WAV")
            return httpx.Response(404, json={"detail": "not found"})

        self.engine = VoiceStudioTTS(
            GpuNode.local(), default_voice_id="voice-1", num_step=8,
            transport=httpx.MockTransport(handler),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.aclose()

    async def test_status_mapping(self) -> None:
        status = await self.engine.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["checkpoint"], "k2-fsa/OmniVoice")
        self.assertEqual(status["via"], "direct")

    async def test_profiles_mapping(self) -> None:
        voices = await self.engine.list_voices()
        self.assertEqual(voices, [{"id": "voice-1", "name": "Giọng một", "extra": 7}])

    async def test_speech_payload(self) -> None:
        self.assertEqual(await self.engine.synthesize_pcm("Xin chào"), b"PCM")
        request = next(item for item in self.requests if item.url.path == "/v1/audio/speech")
        payload = json.loads(request.content)
        self.assertEqual(payload, {
            "model": "omnivoice", "input": "Xin chào", "voice": "voice-1",
            "response_format": "pcm", "speed": 1.0, "stream_format": "audio", "num_step": 8,
        })

    async def test_num_step_theo_tung_lan_goi(self) -> None:
        await self.engine.synthesize_pcm("Chunk đầu", num_step=4)
        request = [item for item in self.requests if item.url.path == "/v1/audio/speech"][-1]
        self.assertEqual(json.loads(request.content)["num_step"], 4)


if __name__ == "__main__":
    unittest.main()
