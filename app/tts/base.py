from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import Any, AsyncIterator, Dict, List, Optional


class TTSError(Exception):
    """Lỗi khi gọi engine TTS (mạng, engine chưa sẵn sàng, giọng không tồn tại...)."""


class VoiceNotFoundError(TTSError):
    """Giọng đọc được yêu cầu không tồn tại."""


class TTSEngine(ABC):
    """Interface chung cho mọi engine TTS — API layer chỉ làm việc với lớp này."""

    name: str = "tts"

    @abstractmethod
    async def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        """Sinh giọng nói cho `text`, trả về bytes WAV."""

    @abstractmethod
    async def list_voices(self) -> List[Dict[str, Any]]:
        """Danh sách giọng đọc có sẵn."""

    @abstractmethod
    async def status(self) -> Dict[str, Any]:
        """Trạng thái engine; luôn có key `ok` (bool)."""

    async def start(self) -> None:
        return None

    async def synthesize_pcm(
        self, text: str, voice_id: Optional[str] = None, num_step: int | None = None
    ) -> bytes:
        raise TTSError(f"Engine {self.name} chưa hỗ trợ PCM.")

    async def stream_sentences(
        self,
        sentences: List[str],
        voice_id: Optional[str] = None,
        first_chunk_num_step: int | None = None,
    ) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(maxsize=2)

        async def produce() -> None:
            try:
                for index, sentence in enumerate(sentences):
                    num_step = first_chunk_num_step if index == 0 else None
                    await queue.put(await self.synthesize_pcm(sentence, voice_id, num_step))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(exc)
            else:
                await queue.put(None)

        producer = asyncio.create_task(produce(), name="tts-chunk-producer")
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer

    async def aclose(self) -> None:
        return None
