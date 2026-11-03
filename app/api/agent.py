from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.llm import LLMError
from app.agent.names import speakable_name
from app.agent.responder import LiveAgent
from app.agent.speech import SpeechMetrics, _agent_audio_stream
from app.api.deps import get_agent, get_player, get_tts
from app.api.tts import streaming_wav_header
from app.audio.player import LocalAudioPlayer
from app.config import (
    AGENT_AUTO,
    LLM_BASE_URL,
    LLM_MODEL,
)
from app.tts.base import TTSEngine, TTSError, VoiceNotFoundError

router = APIRouter(prefix="/api/agent", tags=["agent"])
logger = logging.getLogger(__name__)


class ReplyRequest(BaseModel):
    comment: str = Field(min_length=1, max_length=500)
    nickname: str | None = Field(default=None, max_length=100)


class SpeakRequest(ReplyRequest):
    voice_id: str | None = None
    format: Literal["pcm", "wav"] = "pcm"
    play_local: bool = False


def _require_agent(agent: LiveAgent | None) -> LiveAgent:
    if agent is None:
        raise HTTPException(status_code=503, detail="agent disabled")
    return agent


def _comment_payload(body: ReplyRequest) -> dict[str, Any]:
    user = {"nickname": body.nickname}
    return {
        "comment": body.comment.strip(),
        "user": {"nickname": speakable_name(user)},
    }


@router.get("/status")
async def agent_status(
    agent: LiveAgent | None = Depends(get_agent),
    player: LocalAudioPlayer | None = Depends(get_player),
) -> dict[str, Any]:
    scheduler = getattr(agent, "scheduler", None) if agent is not None else None
    return {
        "enabled": agent is not None,
        "model": LLM_MODEL,
        "base_url": urlparse(LLM_BASE_URL).hostname,
        "auto": AGENT_AUTO,
        "auto_speak": agent.auto_speak if agent is not None else False,
        "listeners": agent.listener_count if agent is not None else 0,
        "players": agent.player_count if agent is not None else 0,
        "state": agent.state if agent is not None else "idle",
        "last_latencies": agent.last_latencies if agent is not None else None,
        "stats": agent.stats() if agent is not None else None,
        "player": player.info() if player is not None else None,
        "scheduler": await scheduler.status() if scheduler is not None else None,
    }


@router.post("/reply")
async def agent_reply(
    body: ReplyRequest, agent: LiveAgent | None = Depends(get_agent)
) -> dict[str, Any]:
    try:
        return await _require_agent(agent).reply(_comment_payload(body))
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/replies")
async def agent_replies(
    limit: int = 20, agent: LiveAgent | None = Depends(get_agent)
) -> dict[str, Any]:
    limit = min(100, max(1, limit))
    replies = _require_agent(agent).recent_replies(limit)
    return {"limit": limit, "count": len(replies), "replies": replies}


@router.post("/speak")
async def agent_speak(
    body: SpeakRequest,
    agent: LiveAgent | None = Depends(get_agent),
    tts: TTSEngine | None = Depends(get_tts),
    player: LocalAudioPlayer | None = Depends(get_player),
) -> Any:
    live_agent = _require_agent(agent)
    if tts is None:
        raise HTTPException(status_code=503, detail="TTS đang tắt.")
    if body.play_local and (player is None or not player.active):
        raise HTTPException(status_code=503, detail="Local audio player không khả dụng.")

    started_at = time.perf_counter()
    metrics = SpeechMetrics(started_at)
    stream = _agent_audio_stream(
        live_agent, tts, _comment_payload(body), body.voice_id, metrics=metrics
    )
    if body.play_local:
        first_audio_at: float | None = None
        audio_bytes = 0
        try:
            async for chunk in stream:
                audio_bytes += len(chunk)
                await player.play(chunk)
                if first_audio_at is None:
                    first_audio_at = time.perf_counter()
        except VoiceNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except (LLMError, TTSError, StopAsyncIteration) as exc:
            raise HTTPException(status_code=502, detail=str(exc) or "Không tạo được audio.")
        except Exception as exc:
            logger.exception("Lỗi agent/TTS khi phát local audio.")
            raise HTTPException(status_code=502, detail=str(exc) or "Không tạo được audio.")
        finally:
            await stream.aclose()
        if first_audio_at is None:
            raise HTTPException(status_code=502, detail="Không tạo được audio.")
        finished_at = time.perf_counter()
        return {
            "reply": metrics.reply,
            "latency_ms": {
                "llm_first_token": (
                    round((metrics.first_token_at - started_at) * 1000)
                    if metrics.first_token_at is not None else None
                ),
                "first_audio": round((first_audio_at - started_at) * 1000),
                "greeting_ready": (
                    round((metrics.greeting_ready_at - started_at) * 1000)
                    if metrics.greeting_ready_at is not None else None
                ),
                "total": round((finished_at - started_at) * 1000),
            },
            "audio_sec": round(audio_bytes / 48000, 3),
        }

    try:
        first = await anext(stream)
    except VoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (LLMError, TTSError, StopAsyncIteration) as exc:
        raise HTTPException(status_code=502, detail=str(exc) or "Không tạo được audio.")
    except Exception as exc:
        logger.exception("Lỗi agent/TTS trước audio đầu tiên.")
        raise HTTPException(status_code=502, detail=str(exc) or "Không tạo được audio.")

    async def audio_chunks() -> AsyncIterator[bytes]:
        try:
            if body.format == "wav":
                yield streaming_wav_header()
            yield first
            async for audio in stream:
                yield audio
        except Exception:
            logger.exception("Lỗi agent/TTS giữa luồng; kết thúc audio.")
        finally:
            await stream.aclose()

    headers = {"X-Sample-Rate": "24000", "X-Channels": "1", "X-Sample-Format": "s16le"}
    return StreamingResponse(
        audio_chunks(), media_type="audio/pcm" if body.format == "pcm" else "audio/wav",
        headers=headers,
    )


@router.post("/audio/stop")
async def stop_local_audio(
    agent: LiveAgent | None = Depends(get_agent),
    player: LocalAudioPlayer | None = Depends(get_player),
) -> dict[str, Any]:
    local_flushed = player is not None and player.active
    bridge_flushed = agent.player_count if agent is not None else 0
    if not local_flushed and not bridge_flushed:
        raise HTTPException(status_code=503, detail="Local audio player không khả dụng.")
    if local_flushed:
        await player.flush()
    if bridge_flushed and agent is not None:
        bridge_flushed = await agent.broadcast_players({"type": "flush"})
    return {
        "stopped": True,
        "local_flushed": local_flushed,
        "bridges_flushed": bridge_flushed,
    }


@router.websocket("/ws")
async def agent_ws(websocket: WebSocket) -> None:
    agent: LiveAgent | None = getattr(websocket.app.state, "agent", None)
    if agent is None:
        await websocket.close(code=1013, reason="agent disabled")
        return
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any] | bytes] = asyncio.Queue(maxsize=256)

    async def close_slow_client() -> None:
        await websocket.close(code=1013, reason="agent subscriber too slow")

    subscriber = agent.subscribe(queue, close_slow_client)

    async def send_frames() -> None:
        while True:
            frame = await queue.get()
            if isinstance(frame, bytes):
                await websocket.send_bytes(frame)
            else:
                await websocket.send_json(frame)

    async def watch_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                continue
            try:
                event = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Bỏ qua JSON không hợp lệ từ agent WebSocket.")
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "hello" and event.get("role") == "player":
                agent.mark_player(subscriber, event)
                logger.info(
                    "Audio bridge đã kết nối: name=%s, backend=%s, device=%s.",
                    event.get("name") or "unknown",
                    event.get("backend") or "unknown",
                    event.get("device") or "default",
                )
            elif event_type == "played" and subscriber.is_player:
                reply_id = event.get("id")
                if isinstance(reply_id, str) and reply_id:
                    agent.acknowledge_played(reply_id, event)

    tasks = [
        asyncio.create_task(send_frames(), name="agent-ws-send"),
        asyncio.create_task(watch_disconnect(), name="agent-ws-receive"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except Exception:
        pass
    finally:
        was_player = subscriber.is_player
        player_name = (subscriber.player_info or {}).get("name")
        agent.unsubscribe(subscriber)
        if was_player:
            logger.info("Audio bridge đã ngắt kết nối: name=%s.", player_name or "unknown")
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
