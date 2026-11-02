from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress

from app.agent.responder import LiveAgent
from app.agent.speech import SpeechMetrics, _agent_audio_stream
from app.audio.player import LocalAudioPlayer
from app.comments.buffer import CommentBuffer
from app.config import (
    AGENT_PLAYED_ACK_GRACE_SEC,
    AGENT_REPLY_GAP_SEC,
    AGENT_SKIP_WHEN_NO_LISTENER,
)
from app.tts.base import TTSEngine

logger = logging.getLogger(__name__)
Sleep = Callable[[float], Awaitable[None]]


def _milliseconds(seconds: float | None) -> int | None:
    return None if seconds is None else round(seconds * 1000)


async def _next_comment(
    comments: CommentBuffer,
    agent: LiveAgent,
    skip_when_no_listener: bool,
    local_listener: bool = False,
) -> dict | None:
    if not skip_when_no_listener or local_listener:
        return await comments.next(timeout=1.0)

    comment_task = asyncio.create_task(comments.next(timeout=1.0), name="agent-next-comment")
    listener_task = asyncio.create_task(
        agent.wait_until_no_listeners(), name="agent-listener-guard"
    )
    try:
        done, _ = await asyncio.wait(
            (comment_task, listener_task), return_when=asyncio.FIRST_COMPLETED
        )
        # comment đã lấy khỏi buffer thì không được bỏ, kể cả khi listener vừa rời
        if comment_task not in done and agent.listener_count == 0:
            comment_task.cancel()
            with suppress(asyncio.CancelledError):
                await comment_task
            return None
        return await comment_task
    finally:
        for task in (comment_task, listener_task):
            if not task.done():
                task.cancel()
        for task in (comment_task, listener_task):
            with suppress(asyncio.CancelledError):
                await task


async def _speak_reply(
    agent: LiveAgent,
    engine: TTSEngine,
    comment: dict,
    *,
    reply_gap_sec: float,
    sleep: Sleep,
    clock: Callable[[], float],
    player: LocalAudioPlayer | None = None,
    played_ack_grace_sec: float = AGENT_PLAYED_ACK_GRACE_SEC,
) -> None:
    reply_id = str(uuid.uuid4())
    started_at = clock()
    metrics = SpeechMetrics(started_at)
    stream = _agent_audio_stream(agent, engine, comment, None, metrics=metrics, clock=clock)
    first_audio_sent_at: float | None = None
    first_audio_played_at: float | None = None
    played_future: asyncio.Future[dict] | None = None
    audio_bytes = 0
    try:
        first = await anext(stream)
        # Chỉ mở reply sau khi chắc chắn có text/audio; reply rỗng được bỏ qua hoàn toàn.
        agent.state = "speaking"
        await agent.broadcast_frame({
            "type": "reply_start",
            "id": reply_id,
            "comment": comment,
            "format": "pcm",
            "sample_rate": 24000,
            "channels": 1,
            "sample_format": "s16le",
        })
        first_audio_sent_at = clock()
        audio_bytes += len(first)
        if player is not None:
            await player.play(first)
            first_audio_played_at = clock()
        await agent.broadcast_frame(first)
        async for chunk in stream:
            audio_bytes += len(chunk)
            if player is not None:
                await player.play(chunk)
            await agent.broadcast_frame(chunk)

        finished_at = clock()
        first_token_sec = (
            metrics.first_token_at - started_at if metrics.first_token_at is not None else None
        )
        latencies = {
            "llm_first_token": _milliseconds(first_token_sec),
            "first_audio": _milliseconds(first_audio_sent_at - started_at),
            "greeting_ready": _milliseconds(
                metrics.greeting_ready_at - started_at
                if metrics.greeting_ready_at is not None else None
            ),
            "first_audio_played": _milliseconds(
                first_audio_played_at - started_at
                if first_audio_played_at is not None else None
            ),
            "total": _milliseconds(finished_at - started_at),
        }
        agent.last_latencies = latencies
        audio_sec = audio_bytes / (24000 * 2)
        # Đăng ký trước khi queue reply_end để ACK localhost nhanh không bị hụt.
        if player is None and agent.player_count:
            played_future = agent.expect_played(reply_id)
        await agent.broadcast_frame({
            "type": "reply_end",
            "id": reply_id,
            "reply": metrics.reply,
            "latency_ms": latencies,
            "audio_sec": round(audio_sec, 3),
        })

        agent.state = "cooldown"
        if player is not None:
            await player.drain()
            await sleep(reply_gap_sec)
        elif played_future is not None:
            try:
                await agent.wait_for_played(
                    reply_id, played_future, audio_sec + played_ack_grace_sec
                )
            except TimeoutError:
                logger.warning(
                    "Audio bridge không ACK reply %s sau %.3fs; tiếp tục comment kế.",
                    reply_id,
                    audio_sec + played_ack_grace_sec,
                )
            await sleep(reply_gap_sec)
        else:
            remaining_audio = audio_sec - (clock() - first_audio_sent_at)
            await sleep(max(0.0, remaining_audio + reply_gap_sec))
    except StopAsyncIteration:
        # LLM trả rỗng: LiveAgent vẫn ghi nhận stats nhưng không phát frame.
        logger.info("LLM trả reply rỗng; bỏ qua comment đã lấy khỏi buffer.")
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        await agent.broadcast_frame({"type": "reply_error", "id": reply_id, "error": error})
        logger.exception("Lỗi agent/TTS giữa reply; tiếp tục comment kế tiếp.")
    finally:
        agent.state = "idle"
        with suppress(Exception):
            await stream.aclose()


async def auto_reply_loop(
    comments: CommentBuffer,
    agent: LiveAgent,
    tts: TTSEngine | None = None,
    *,
    auto_speak: bool = False,
    skip_when_no_listener: bool = AGENT_SKIP_WHEN_NO_LISTENER,
    reply_gap_sec: float = AGENT_REPLY_GAP_SEC,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.perf_counter,
    player: LocalAudioPlayer | None = None,
    played_ack_grace_sec: float = AGENT_PLAYED_ACK_GRACE_SEC,
) -> None:
    """Consumer duy nhất của buffer khi AGENT_AUTO bật."""
    speak = auto_speak and tts is not None
    agent.auto_speak = speak
    while True:
        try:
            local_listener = player is not None and player.active
            if skip_when_no_listener and agent.listener_count == 0 and not local_listener:
                agent.state = "idle"
                await sleep(0.05)
                continue

            comment = await _next_comment(
                comments, agent, skip_when_no_listener, local_listener
            )
            if comment is None:
                continue
            agent.state = "thinking"
            if speak:
                await _speak_reply(
                    agent, tts, comment, reply_gap_sec=reply_gap_sec, sleep=sleep,
                    clock=clock, player=player,
                    played_ack_grace_sec=played_ack_grace_sec,
                )
                continue

            started_at = clock()
            record = await agent.reply(comment)
            if record["reply"]:
                await agent.broadcast(record)
            else:
                logger.info("LLM trả reply rỗng; bỏ qua comment đã lấy khỏi buffer.")
            agent.last_latencies = {
                "llm_first_token": None,
                "first_audio": None,
                "greeting_ready": None,
                "total": _milliseconds(clock() - started_at),
            }
            agent.state = "idle"
        except asyncio.CancelledError:
            raise
        except Exception:
            agent.state = "idle"
            logger.exception("Lỗi agent tự động; tiếp tục chờ comment.")
