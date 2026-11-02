from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from app.agent.responder import LiveAgent
from app.agent.tts_text import TtsTextSanitizer
from app.api.tts import plan_tts_chunks
from app.config import AGENT_TTS_SINGLE_SHOT, TTS_FIRST_CHUNK_MAX_WORDS, TTS_FIRST_CHUNK_NUM_STEP, TTS_NUM_STEP
from app.tts.base import TTSEngine

_BOUNDARY_RE = re.compile(r"[,;.!?…\n]")


@dataclass
class SpeechMetrics:
    started_at: float
    first_token_at: float | None = None
    greeting_ready_at: float | None = None
    greeting: str = ""
    reply_parts: list[str] = field(default_factory=list)

    @property
    def reply(self) -> str:
        llm_reply = "".join(self.reply_parts).strip()
        return f"{self.greeting} {llm_reply}".strip() if llm_reply else ""


def _take_ready_chunk(buffer: str, first: bool) -> tuple[str, str] | None:
    """Cắt một chunk đã đủ để TTS; phần còn lại giữ nguyên (kể cả khoảng trắng cuối).

    Token LLM có thể dừng giữa từ ("s" + "úng"), nên chỉ cắt ở từ đã có khoảng trắng phía sau.
    """
    text = buffer.lstrip()
    if not text.strip():
        return None
    max_words = TTS_FIRST_CHUNK_MAX_WORDS if first else min(TTS_FIRST_CHUNK_MAX_WORDS * 2, 12)
    for boundary in _BOUNDARY_RE.finditer(text):
        mark = boundary.group()
        following = text[boundary.end():boundary.end() + 1]
        if mark in ".,":
            # "2.5", "1,5" hoặc "..." đang stream dở → chờ ký tự kế tiếp là khoảng trắng
            if not following or not following.isspace():
                continue
        elif mark in "!?" and following and following in "!?":
            continue
        candidate = text[:boundary.end()].strip()
        # chunk sau quá ngắn ("ơi.") nghe rời rạc → gộp sang dấu câu kế tiếp
        if first or len(candidate.split()) >= 3:
            return candidate, text[boundary.end():]
    words = list(re.finditer(r"\S+", text))
    if not text[-1].isspace():
        words = words[:-1]  # từ cuối có thể chưa stream xong
    if len(words) >= max_words:
        end = words[max_words - 1].end()
        return text[:end].strip(), text[end:]
    return None


async def _single_shot_audio(
    agent: LiveAgent,
    engine: TTSEngine,
    comment: Mapping[str, Any],
    voice_id: str | None,
    greeting: str,
    sanitizer: TtsTextSanitizer,
    timing: SpeechMetrics,
    clock: Callable[[], float],
) -> AsyncIterator[bytes]:
    """Chờ LLM trả xong, gộp greeting + reply thành một lần TTS để ngữ điệu liền mạch."""

    def finish_reply(reply: str) -> str:
        cleaned = sanitizer(reply)
        timing.reply_parts[:] = [cleaned] if cleaned else []
        return cleaned

    async for delta in agent.stream_reply(
        comment, greeting=greeting, reply_transform=finish_reply
    ):
        if timing.first_token_at is None and delta.strip():
            timing.first_token_at = clock()
    text = timing.reply
    if not text:
        return
    pcm = await engine.synthesize_pcm(text, voice_id, TTS_NUM_STEP)
    if greeting:
        timing.greeting_ready_at = clock()
    yield pcm


async def _agent_audio_stream(
    agent: LiveAgent,
    engine: TTSEngine,
    comment: Mapping[str, Any],
    voice_id: str | None,
    *,
    metrics: SpeechMetrics | None = None,
    clock: Callable[[], float] = time.perf_counter,
    single_shot: bool | None = None,
) -> AsyncIterator[bytes]:
    """Stream LLM -> tách câu -> prefetch TTS; dùng chung cho API và auto mode."""
    timing = metrics or SpeechMetrics(clock())
    greeting = agent.make_greeting(comment)
    timing.greeting = greeting
    sanitizer = TtsTextSanitizer(agent.tts_max_tags)
    if AGENT_TTS_SINGLE_SHOT if single_shot is None else single_shot:
        async for pcm in _single_shot_audio(
            agent, engine, comment, voice_id, greeting, sanitizer, timing, clock
        ):
            yield pcm
        return
    chunks: asyncio.Queue[tuple[str, int | None] | Exception | None] = asyncio.Queue()
    first_delta_or_done = asyncio.Event()
    if greeting:
        chunks.put_nowait((greeting, TTS_FIRST_CHUNK_NUM_STEP))

    async def produce_text() -> None:
        buffer = ""
        first = True
        final_chunks: list[tuple[str, int | None]] = []

        def finish_reply(_: str) -> str:
            nonlocal first
            if buffer.strip():
                max_words = TTS_FIRST_CHUNK_MAX_WORDS if first else 12
                for planned in plan_tts_chunks(buffer, max_words):
                    chunk = sanitizer(planned)
                    if not chunk:
                        continue
                    timing.reply_parts.append((" " if timing.reply_parts else "") + chunk)
                    final_chunks.append(
                        (chunk, TTS_FIRST_CHUNK_NUM_STEP if first else TTS_NUM_STEP)
                    )
                    first = False
            return "".join(timing.reply_parts)

        try:
            async for delta in agent.stream_reply(
                comment, greeting=greeting, reply_transform=finish_reply
            ):
                if timing.first_token_at is None and delta.strip():
                    timing.first_token_at = clock()
                    first_delta_or_done.set()
                buffer += delta
                while ready := _take_ready_chunk(buffer, first):
                    chunk, buffer = ready
                    chunk = sanitizer(chunk)
                    if not chunk:
                        continue
                    timing.reply_parts.append((" " if timing.reply_parts else "") + chunk)
                    await chunks.put((chunk, TTS_FIRST_CHUNK_NUM_STEP if first else TTS_NUM_STEP))
                    first = False
            for chunk in final_chunks:
                await chunks.put(chunk)
        except Exception as exc:
            await chunks.put(exc)
        finally:
            first_delta_or_done.set()
            await chunks.put(None)

    # Tổng hợp chunk kế tiếp trong khi client còn nhận/phát chunk trước.
    audio: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=2)

    async def produce_audio() -> None:
        index = 0
        try:
            while True:
                item = await chunks.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                text, num_step = item
                pcm = await engine.synthesize_pcm(text, voice_id, num_step)
                if greeting and index == 0:
                    timing.greeting_ready_at = clock()
                await audio.put(pcm)
                index += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await audio.put(exc)
        else:
            await audio.put(None)

    producers = [
        asyncio.create_task(produce_audio(), name="agent-tts-stream"),
        asyncio.create_task(produce_text(), name="agent-llm-stream"),
    ]
    try:
        while True:
            item = await audio.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if greeting and timing.first_token_at is None:
                # Greeting được tổng hợp sớm nhưng phải chờ delta LLM đầu tiên; nếu LLM
                # trả rỗng thì bỏ luôn PCM này để client/player không nghe reply giả.
                await first_delta_or_done.wait()
                if timing.first_token_at is None:
                    continue
            yield item
    finally:
        for task in producers:
            if not task.done():
                task.cancel()
        for task in producers:
            with suppress(asyncio.CancelledError):
                await task
