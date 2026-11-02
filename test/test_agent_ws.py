"""Nghe auto reply từ /api/agent/ws, in timing, lưu PCM và có thể phát bằng ffplay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import time
from typing import Any


def _start_ffplay() -> subprocess.Popen[bytes]:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("Không tìm thấy ffplay trong PATH.")
    help_result = subprocess.run(
        [ffplay, "-h", "full"], capture_output=True, text=True,
        encoding="utf-8", errors="ignore",
    )
    help_text = help_result.stdout + help_result.stderr
    channel_args = ["-ch_layout", "mono"] if "-ch_layout" in help_text else ["-ac", "1"]
    return subprocess.Popen(
        [
            ffplay, "-f", "s16le", "-ar", "24000", *channel_args,
            "-nodisp", "-autoexit", "-loglevel", "quiet", "-",
        ],
        stdin=subprocess.PIPE,
    )


async def listen(url: str, output_path: str, play: bool) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("Cài package 'websockets' để chạy script này.") from exc

    player = _start_ffplay() if play else None
    connected_at = time.perf_counter()
    reply_started_at: float | None = None
    total_bytes = 0
    try:
        async with websockets.connect(url, max_size=None) as websocket:
            print(f"[ws] Đã kết nối {url}")
            with open(output_path, "wb") as output:
                async for frame in websocket:
                    now = time.perf_counter()
                    if isinstance(frame, bytes):
                        if reply_started_at is not None and total_bytes == 0:
                            print(f"[ws] Audio đầu: +{now - reply_started_at:.3f}s")
                        output.write(frame)
                        total_bytes += len(frame)
                        if player is not None and player.stdin is not None:
                            player.stdin.write(frame)
                            player.stdin.flush()
                        print(f"[ws] binary {len(frame)} bytes (tổng {total_bytes})")
                        continue

                    event: dict[str, Any] = json.loads(frame)
                    print(f"[ws] +{now - connected_at:.3f}s {json.dumps(event, ensure_ascii=False)}")
                    if event.get("type") == "reply_start":
                        reply_started_at = now
                        total_bytes = 0
                    elif event.get("type") in {"reply_end", "reply_error"}:
                        print(f"[ws] Đã lưu audio reply gần nhất vào {output_path}")
    finally:
        if player is not None:
            if player.stdin is not None:
                player.stdin.close()
            player.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("WS_URL", "ws://127.0.0.1:8002/api/agent/ws"),
    )
    parser.add_argument("--play", action="store_true", help="Phát PCM ngay bằng ffplay")
    parser.add_argument("--out", default=os.environ.get("OUT", "agent_ws.pcm"))
    args = parser.parse_args()
    asyncio.run(listen(args.url, args.out, args.play))


if __name__ == "__main__":
    main()
