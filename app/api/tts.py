from __future__ import annotations

import logging
import re
import struct
from typing import AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import get_tts
from app.config import TTS_FIRST_CHUNK_MAX_WORDS, TTS_FIRST_CHUNK_NUM_STEP
from app.tts.base import TTSEngine, TTSError, VoiceNotFoundError

router = APIRouter(prefix="/api/tts", tags=["tts"])
logger = logging.getLogger(__name__)


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    voice_id: str | None = None


class StreamRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice_id: str | None = None
    format: Literal["pcm", "wav"] = "pcm"
    first_chunk_num_step: int | None = Field(default=None, ge=1, le=64)
    first_chunk_max_words: int | None = Field(default=None, ge=2, le=30)


def _hard_split(text: str, max_chars: int = 200) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max(window.rfind(","), window.rfind(" "))
        if cut < max_chars // 2:
            cut = max_chars
        elif window[cut] == ",":
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_sentences(text: str) -> list[str]:
    """Tách theo dấu câu/newline, ghép mẩu quá ngắn và chặn câu quá dài."""
    normalized = re.sub(r"\r\n?", "\n", text).strip()
    raw = [part.strip() for part in re.findall(r"[^.!?…;\n]+[.!?…;]*|[.!?…;]+", normalized)]
    raw = [part for part in raw if part]
    merged: list[str] = []
    pending = ""
    for part in raw:
        if len(part) < 12:
            pending = f"{pending} {part}".strip()
            continue
        if pending:
            part = f"{pending} {part}"
            pending = ""
        merged.append(part)
    if pending:
        if merged:
            merged[-1] = f"{merged[-1]} {pending}"
        else:
            merged.append(pending)
    return [chunk for sentence in merged for chunk in _hard_split(sentence)]


def _word_spans(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"\S+", text))


def _cut_words_without_short_tail(text: str, max_words: int) -> tuple[str, str]:
    """Cắt theo từ; giữ thêm đuôi 1–2 từ của câu để lời đọc không bị cụt."""
    words = _word_spans(text)
    if len(words) <= max_words:
        return text.strip(), ""

    cut = words[max_words - 1].end()
    sentence_end = re.search(r"[.!?…;\n]", text[cut:])
    local_end = cut + sentence_end.end() if sentence_end else len(text)
    tail_words = _word_spans(text[cut:local_end])
    if len(tail_words) <= 2:
        cut = local_end
    return text[:cut].strip(), text[cut:].strip()


def plan_tts_chunks(text: str, first_max_words: int = 6) -> list[str]:
    """Lập chunk đầu ngắn, chunk thứ hai lớn dần, rồi dùng tách câu thường."""
    remaining = re.sub(r"[ \t\f\v]+", " ", re.sub(r"\r\n?", "\n", text)).strip()
    if not remaining:
        return []

    first_boundary = re.search(r"[,\.!?…;\n]", remaining)
    if first_boundary:
        clause = remaining[:first_boundary.end()]
        if len(_word_spans(clause)) <= first_max_words:
            cut = first_boundary.end()
            # Nếu dấu phẩy tạo đuôi câu chỉ 1–2 từ thì đọc liền cho tự nhiên.
            if first_boundary.group() == ",":
                sentence_end = re.search(r"[.!?…;\n]", remaining[cut:])
                local_end = cut + sentence_end.end() if sentence_end else len(remaining)
                if len(_word_spans(remaining[cut:local_end])) <= 2:
                    cut = local_end
            first = remaining[:cut].strip()
            remaining = remaining[cut:].strip()
        else:
            first, remaining = _cut_words_without_short_tail(remaining, first_max_words)
    else:
        first, remaining = _cut_words_without_short_tail(remaining, first_max_words)

    chunks = [first]
    following = split_sentences(remaining) if remaining else []
    if not following:
        return chunks

    second, rest = _cut_words_without_short_tail(following[0], min(first_max_words * 2, 12))
    chunks.append(second)
    if rest:
        chunks.extend(split_sentences(rest))
    chunks.extend(following[1:])
    return [chunk for chunk in chunks if chunk]


def streaming_wav_header() -> bytes:
    """WAV PCM 24 kHz mono; kích thước mở để player đọc tới EOF."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ", 16, 1, 1,
        24000, 24000 * 2, 2, 16, b"data", 0xFFFFFFFF,
    )


def _require_tts(tts: TTSEngine | None) -> TTSEngine:
    if tts is None:
        raise HTTPException(status_code=503, detail="TTS đang tắt.")
    return tts


@router.get("/status")
async def tts_status(tts: TTSEngine | None = Depends(get_tts)) -> dict:
    if tts is None:
        return {"ok": False, "enabled": False}
    try:
        return await tts.status()
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/voices")
async def tts_voices(tts: TTSEngine | None = Depends(get_tts)) -> dict:
    try:
        return {"voices": await _require_tts(tts).list_voices()}
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/synthesize")
async def tts_synthesize(body: SynthesizeRequest, tts: TTSEngine | None = Depends(get_tts)) -> Response:
    try:
        audio = await _require_tts(tts).synthesize(body.text, body.voice_id)
    except VoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TTSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return Response(content=audio, media_type="audio/wav")


@router.post("/stream")
async def tts_stream(body: StreamRequest, tts: TTSEngine | None = Depends(get_tts)) -> StreamingResponse:
    engine = _require_tts(tts)
    max_words = body.first_chunk_max_words or TTS_FIRST_CHUNK_MAX_WORDS
    chunks = plan_tts_chunks(body.text, max_words)
    first_num_step = (
        body.first_chunk_num_step
        if body.first_chunk_num_step is not None
        else TTS_FIRST_CHUNK_NUM_STEP
    )
    stream = engine.stream_sentences(chunks, body.voice_id, first_num_step)
    try:
        first = await anext(stream)
    except VoiceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (TTSError, StopAsyncIteration) as exc:
        detail = str(exc) if str(exc) else "TTS không tạo được audio."
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:
        logger.exception("Lỗi TTS trước chunk đầu tiên.")
        raise HTTPException(status_code=502, detail=str(exc) or "TTS không tạo được audio.")

    async def audio_chunks() -> AsyncIterator[bytes]:
        try:
            if body.format == "wav":
                yield streaming_wav_header()
            yield first
            async for chunk in stream:
                yield chunk
        except Exception:
            logger.exception("Lỗi TTS giữa luồng; kết thúc audio tại câu hiện tại.")
        finally:
            await stream.aclose()

    headers = {
        "X-Sample-Rate": "24000",
        "X-Channels": "1",
        "X-Sample-Format": "s16le",
    }
    media_type = "audio/pcm" if body.format == "pcm" else "audio/wav"
    return StreamingResponse(audio_chunks(), media_type=media_type, headers=headers)
