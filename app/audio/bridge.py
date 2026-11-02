from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from app.audio.player import LocalAudioPlayer, create_player

DEFAULT_WS_URL = "ws://127.0.0.1:8002/api/agent/ws"


def _status(message: str) -> None:
    line = f"[audio-bridge] {message}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows PowerShell cũ có thể vẫn dùng cp1252 dù source là UTF-8.
        encoding = sys.stdout.encoding or "ascii"
        print(line.encode(encoding, errors="replace").decode(encoding), flush=True)


async def bridge_session(
    websocket: Any,
    player: LocalAudioPlayer,
    *,
    name: str,
    backend: str,
    device: str,
    monotonic: Callable[[], float] = time.perf_counter,
    wall_clock: Callable[[], float] = time.time,
) -> None:
    """Xử lý một kết nối; tách riêng để test protocol không cần network/audio."""
    await websocket.send(json.dumps({
        "type": "hello",
        "role": "player",
        "name": name,
        "backend": backend,
        "device": device,
    }, ensure_ascii=False))

    reply_id: str | None = None
    reply_started_at: float | None = None
    first_audio_played_ms: int | None = None
    async for frame in websocket:
        if isinstance(frame, bytes):
            await player.play(frame)
            if first_audio_played_ms is None and reply_started_at is not None:
                first_audio_played_ms = max(
                    0, round((monotonic() - reply_started_at) * 1000)
                )
            continue

        try:
            event = json.loads(frame)
        except (json.JSONDecodeError, TypeError):
            _status("Bỏ qua control frame không hợp lệ.")
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        if event_type == "reply_start":
            reply_id = str(event.get("id") or "") or None
            reply_started_at = monotonic()
            first_audio_played_ms = None
            comment_payload = event.get("comment")
            comment = (
                str(comment_payload.get("comment") or "").strip()
                if isinstance(comment_payload, dict) else ""
            )
            if len(comment) > 120:
                comment = f"{comment[:117]}..."
            suffix = f" — comment: {comment}" if comment else ""
            _status(f"Bắt đầu reply {reply_id or 'không-id'}{suffix}.")
        elif event_type == "reply_end":
            ended_id = str(event.get("id") or reply_id or "")
            await player.drain()
            await websocket.send(json.dumps({
                "type": "played",
                "id": ended_id,
                "played_at": wall_clock(),
                "first_audio_played_ms": first_audio_played_ms,
            }, ensure_ascii=False))
            reply = str(event.get("reply") or "").strip()
            latency = "?" if first_audio_played_ms is None else str(first_audio_played_ms)
            _status(f"Đã phát reply {ended_id}: {reply} (audio đầu {latency} ms).")
            reply_id = None
            reply_started_at = None
            first_audio_played_ms = None
        elif event_type == "reply_error":
            _status(
                f"Reply {event.get('id') or reply_id or 'không-id'} lỗi: "
                f"{event.get('error') or 'không rõ lỗi'}"
            )
        elif event_type == "flush":
            await player.flush()
            reply_id = None
            reply_started_at = None
            first_audio_played_ms = None
            _status("Đã dừng và xóa hàng đợi audio.")


async def run_bridge(
    url: str,
    player: LocalAudioPlayer,
    *,
    name: str,
    connector: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    if connector is None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "Thiếu package websockets; cài requirements-bridge.txt."
            ) from exc
        connector = websockets.connect

    info = player.info()
    backend = str(info.get("backend") or "unknown")
    device = str(info.get("device") or "system default")
    backoff = 1.0
    while True:
        connected_at: float | None = None
        disconnect_reason = "Server đã đóng kết nối"
        try:
            async with connector(url, max_size=None) as websocket:
                connected_at = time.monotonic()
                _status(f"Đã kết nối {url} ({backend}, {device}).")
                await bridge_session(
                    websocket, player, name=name, backend=backend, device=device
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            disconnect_reason = f"Mất kết nối: {exc}"
        finally:
            # Không để audio cũ tiếp tục sau khi server mất kết nối/restart.
            with suppress(Exception):
                await player.flush()
        # Sau một phiên ổn định, lần reconnect kế tiếp bắt đầu lại từ 1 giây.
        if connected_at is not None and time.monotonic() - connected_at >= 10:
            backoff = 1.0
        _status(f"{disconnect_reason}. Thử lại sau {backoff:g}s.")
        await sleep(backoff)
        backoff = min(10.0, backoff * 2)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nhận PCM từ service Docker và phát trên loa Windows."
    )
    parser.add_argument("--url", default=os.environ.get("BRIDGE_WS_URL", DEFAULT_WS_URL))
    parser.add_argument("--device", default=os.environ.get("AUDIO_OUTPUT_DEVICE", ""))
    parser.add_argument(
        "--backend",
        choices=("auto", "sounddevice", "ffplay"),
        default=os.environ.get("AUDIO_PLAYER", "auto").strip().lower(),
    )
    parser.add_argument("--name", default=socket.gethostname())
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> None:
    player = create_player(args.backend, args.device)
    if player is None:
        raise RuntimeError(
            f"Không có audio backend '{args.backend}' khả dụng; "
            "cài sounddevice hoặc ffplay."
        )
    await player.start()
    try:
        await run_bridge(args.url, player, name=args.name)
    finally:
        await player.aclose()


def main(argv: list[str] | None = None) -> None:
    # Argparse cũng in tiếng Việt; không để code page Windows cũ làm --help crash.
    with suppress(AttributeError, OSError):
        sys.stdout.reconfigure(errors="replace")
    args = _parse_args(argv)
    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        _status("Đã dừng.")
    except Exception as exc:
        raise SystemExit(f"[audio-bridge] {exc}") from exc


if __name__ == "__main__":
    main()
