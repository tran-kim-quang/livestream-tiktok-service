"""Kiểm tra loa/headset local qua service hoặc phát tone trực tiếp."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import struct
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.audio.player import create_player


async def play_tone() -> None:
    player = create_player()
    if player is None:
        raise RuntimeError("Không có local audio backend khả dụng.")
    await player.start()
    try:
        pcm = b"".join(
            struct.pack("<h", round(8000 * math.sin(2 * math.pi * 440 * i / 24000)))
            for i in range(24000)
        )
        await player.play(pcm)
        await player.drain()
        print(f"Đã phát tone 440 Hz qua {player.info()}.")
    finally:
        await player.aclose()


def play_sentence(base_url: str, sentence: str) -> None:
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/agent/speak",
        json={"comment": sentence, "nickname": "Kiểm tra loa", "play_local": True},
        timeout=130,
    )
    response.raise_for_status()
    print(response.json())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tone", action="store_true", help="Phát tone 440 Hz trong 1 giây")
    parser.add_argument(
        "--url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8002")
    )
    parser.add_argument(
        "--sentence", default="Xin chào, đây là âm thanh kiểm tra trên máy local."
    )
    args = parser.parse_args()
    if args.tone:
        asyncio.run(play_tone())
    else:
        play_sentence(args.url, args.sentence)


if __name__ == "__main__":
    main()
