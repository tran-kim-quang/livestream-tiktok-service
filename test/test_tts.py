"""Gọi API TTS của service và lưu WAV để nghe kiểm tra nhanh."""

import os
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8002").rstrip("/")
OUT = os.environ.get("OUT", "tts_test.wav")
TEXT = os.environ.get("TEXT", "Xin chào, đây là kiểm tra VoiceStudio.")


def main() -> None:
    with httpx.Client(timeout=httpx.Timeout(130.0, connect=5.0)) as client:
        status = client.get(f"{BASE_URL}/api/tts/status")
        status.raise_for_status()
        print("[tts status]", status.json())

        voices = client.get(f"{BASE_URL}/api/tts/voices")
        voices.raise_for_status()
        print("[tts voices]", voices.json()["voices"])

        response = client.post(
            f"{BASE_URL}/api/tts/synthesize",
            json={"text": TEXT, "voice_id": os.environ.get("TTS_VOICE_ID") or None},
        )
        response.raise_for_status()
        with open(OUT, "wb") as wav:
            wav.write(response.content)
    print(f"[tts] Đã lưu WAV: {OUT} ({len(response.content)} bytes)")


if __name__ == "__main__":
    main()
