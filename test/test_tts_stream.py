"""Gọi sentence-streaming TTS, đo deadline phát và có thể phát ngay bằng ffplay."""

import argparse
import os
import shutil
import subprocess
import sys
import time

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8002").rstrip("/")
FORMAT = os.environ.get("FORMAT", "pcm").lower()
TEXT = os.environ.get(
    "TEXT",
    "Xin chào mọi người. Đây là bài kiểm tra phát giọng nói theo từng câu! Bạn nghe có rõ không?",
)


def _start_ffplay() -> subprocess.Popen[bytes]:
    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("Không tìm thấy ffplay trong PATH.")
    help_result = subprocess.run(
        [ffplay, "-h", "full"], capture_output=True, text=True, encoding="utf-8", errors="ignore"
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play", action="store_true", help="Phát PCM ngay khi từng chunk đến")
    args = parser.parse_args()

    audio_format = "pcm" if args.play else FORMAT
    output_path = os.environ.get("OUT", f"tts_stream.{audio_format}")
    player = _start_ffplay() if args.play else None
    started = time.perf_counter()
    first_audio_at: float | None = None
    queued_bytes = 0
    total_bytes = 0
    gaps: list[tuple[int, float]] = []

    try:
        with httpx.Client(timeout=httpx.Timeout(130.0, connect=5.0)) as client:
            with client.stream(
                "POST",
                f"{BASE_URL}/api/tts/stream",
                json={
                    "text": TEXT,
                    "voice_id": os.environ.get("TTS_VOICE_ID") or None,
                    "format": audio_format,
                },
            ) as response:
                response.raise_for_status()
                with open(output_path, "wb") as output:
                    for index, chunk in enumerate(response.iter_bytes(), 1):
                        arrived_at = time.perf_counter()
                        if player is not None and player.stdin is not None:
                            player.stdin.write(chunk)
                            player.stdin.flush()
                        elapsed = arrived_at - started
                        if first_audio_at is None:
                            first_audio_at = time.perf_counter() if player is not None else arrived_at
                            print(f"[tts] Time to first audio: {first_audio_at - started:.3f}s")
                        else:
                            deadline = first_audio_at + queued_bytes / 48000
                            gap = max(0.0, arrived_at - deadline)
                            if gap:
                                gaps.append((index, gap))
                        print(f"[tts] Chunk #{index}: +{elapsed:.3f}s, {len(chunk)} bytes")
                        output.write(chunk)
                        queued_bytes += len(chunk)
                        total_bytes += len(chunk)
    finally:
        if player is not None:
            if player.stdin is not None:
                player.stdin.close()
            player.wait()

    if gaps:
        print("[tts] Underrun: " + ", ".join(f"chunk #{index}: {gap * 1000:.0f} ms" for index, gap in gaps))
    else:
        print("[tts] Underrun: không có")
    print(f"[tts] Tổng: {time.perf_counter() - started:.3f}s, {total_bytes} bytes; đã lưu {output_path}")


if __name__ == "__main__":
    main()
