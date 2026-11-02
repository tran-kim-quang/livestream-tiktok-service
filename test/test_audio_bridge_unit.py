from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from typing import Any

from app.audio.bridge import bridge_session, run_bridge
from app.audio.player import LocalAudioPlayer


class FakePlayer(LocalAudioPlayer):
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def start(self) -> None:
        self.events.append("start")

    async def play(self, pcm: bytes) -> None:
        self.events.append(("play", pcm))

    async def drain(self) -> None:
        self.events.append("drain")

    async def flush(self) -> None:
        self.events.append("flush")

    async def aclose(self) -> None:
        self.events.append("close")

    @property
    def active(self) -> bool:
        return True

    @property
    def queued_sec(self) -> float:
        return 0

    def info(self) -> dict[str, Any]:
        return {"backend": "fake", "device": "loa test", "active": True}


class FakeWebSocket:
    def __init__(self, frames: list[str | bytes]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class AudioBridgeProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_hello_play_drain_ack_va_flush(self) -> None:
        frames: list[str | bytes] = [
            json.dumps({"type": "reply_start", "id": "r1"}),
            b"mot",
            b"hai",
            json.dumps({"type": "reply_end", "id": "r1", "reply": "Xin chào"}),
            json.dumps({"type": "flush"}),
        ]
        websocket = FakeWebSocket(frames)
        player = FakePlayer()
        clock_values = iter((10.0, 10.012))

        await bridge_session(
            websocket,
            player,
            name="windows-test",
            backend="fake",
            device="loa test",
            monotonic=lambda: next(clock_values),
            wall_clock=lambda: 1234.5,
        )

        hello = json.loads(websocket.sent[0])
        ack = json.loads(websocket.sent[1])
        self.assertEqual(hello, {
            "type": "hello", "role": "player", "name": "windows-test",
            "backend": "fake", "device": "loa test",
        })
        self.assertEqual(player.events[:3], [
            ("play", b"mot"), ("play", b"hai"), "drain",
        ])
        self.assertEqual(player.events[-1], "flush")
        self.assertEqual(ack["type"], "played")
        self.assertEqual(ack["id"], "r1")
        self.assertEqual(ack["played_at"], 1234.5)
        self.assertEqual(ack["first_audio_played_ms"], 12)

    async def test_disconnect_luon_flush_truoc_khi_reconnect(self) -> None:
        player = FakePlayer()
        websocket = FakeWebSocket([])

        class Connection:
            async def __aenter__(self):
                return websocket

            async def __aexit__(self, *args):
                return False

        async def stop_after_first(_: float) -> None:
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await run_bridge(
                "ws://test", player, name="test",
                connector=lambda *args, **kwargs: Connection(),
                sleep=stop_after_first,
            )
        self.assertIn("flush", player.events)


class AudioBridgeImportTest(unittest.TestCase):
    def test_import_khong_keo_fastapi_hoac_tiktoklive(self) -> None:
        code = (
            "import sys; import app.audio.bridge; "
            "print(any(x == 'fastapi' or x.startswith('fastapi.') or "
            "x == 'TikTokLive' or x.startswith('TikTokLive.') for x in sys.modules))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_cli_help_khong_loi_code_page_windows(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "app.audio.bridge", "--help"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"--backend", result.stdout)


if __name__ == "__main__":
    unittest.main()
