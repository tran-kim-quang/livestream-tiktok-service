"""Thử nhanh agent reply/speak trên service đang chạy, không chạy trong unit test."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8002").rstrip("/")
COMMENT = os.environ.get("COMMENT", "Hôm nay bạn muốn chia sẻ điều gì?")
NICKNAME = os.environ.get("NICKNAME", "Lan")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play", action="store_true", help="Phát PCM ngay bằng ffplay")
    args = parser.parse_args()
    player = None
    if args.play:
        ffplay = shutil.which("ffplay")
        if not ffplay:
            raise RuntimeError("Không tìm thấy ffplay trong PATH.")
        player = subprocess.Popen(
            [ffplay, "-f", "s16le", "-ar", "24000", "-ac", "1", "-nodisp",
             "-autoexit", "-loglevel", "quiet", "-"],
            stdin=subprocess.PIPE,
        )

    timeout = httpx.Timeout(130.0, connect=5.0)
    with httpx.Client(timeout=timeout) as client:
        started = time.perf_counter()
        reply = client.post(
            f"{BASE_URL}/api/agent/reply",
            json={"comment": COMMENT, "nickname": NICKNAME},
        )
        reply.raise_for_status()
        data = reply.json()
        print(f"[agent] LLM latency: {data['latency_ms']} ms")
        print(f"[agent] Reply: {data['reply']}")

        started = time.perf_counter()
        first_audio = None
        total = 0
        with client.stream(
            "POST", f"{BASE_URL}/api/agent/speak",
            json={"comment": COMMENT, "nickname": NICKNAME, "format": "pcm"},
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if first_audio is None:
                    first_audio = time.perf_counter()
                    print(f"[agent] Time to first audio: {first_audio - started:.3f}s")
                total += len(chunk)
                if player is not None and player.stdin is not None:
                    player.stdin.write(chunk)
                    player.stdin.flush()
        print(f"[agent] Audio: {total} bytes, total {time.perf_counter() - started:.3f}s")

    if player is not None:
        if player.stdin is not None:
            player.stdin.close()
        player.wait()


if __name__ == "__main__":
    main()
