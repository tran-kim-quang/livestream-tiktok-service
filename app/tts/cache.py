from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from typing import Any, Optional

from app.tts.base import TTSEngine


_SPACE_RE = re.compile(r"\s+")


class CachedTTSEngine(TTSEngine):
    """LRU PCM trong RAM, có single-flight cho request trùng nhau."""

    def __init__(self, engine: TTSEngine, max_bytes: int) -> None:
        self.engine = engine
        self.name = engine.name
        self.max_bytes = max(0, int(max_bytes))
        self._cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._bytes = 0
        self._hits = self._misses = 0
        self._inflight: dict[tuple[Any, ...], asyncio.Task[bytes]] = {}
        self._lock = asyncio.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.engine, name)

    def _key(self, text: str, voice_id: str | None, num_step: int | None) -> tuple[Any, ...]:
        normalized = _SPACE_RE.sub(" ", text).strip()
        voice = voice_id or getattr(self.engine, "default_voice_id", None)
        return normalized, voice, num_step, getattr(self.engine, "speed", None)

    async def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        return await self.engine.synthesize(text, voice_id)

    async def list_voices(self) -> list[dict[str, Any]]:
        return await self.engine.list_voices()

    async def status(self) -> dict[str, Any]:
        result = dict(await self.engine.status())
        result["cache"] = self.cache_status()
        return result

    async def start(self) -> None:
        await self.engine.start()

    async def aclose(self) -> None:
        await self.engine.aclose()

    async def synthesize_pcm(
        self, text: str, voice_id: Optional[str] = None, num_step: int | None = None
    ) -> bytes:
        key = self._key(text, voice_id, num_step)
        async with self._lock:
            cached = self._cache.pop(key, None)
            if cached is not None:
                self._cache[key] = cached
                self._hits += 1
                return cached
            task = self._inflight.get(key)
            if task is None:
                self._misses += 1
                task = asyncio.create_task(self.engine.synthesize_pcm(text, voice_id, num_step))
                self._inflight[key] = task
                # Gỡ inflight ngay khi task xong, kể cả khi mọi caller đã bị huỷ.
                task.add_done_callback(lambda done, key=key: self._finish_inflight(key, done))
        value = await asyncio.shield(task)
        async with self._lock:
            if key not in self._cache and len(value) <= self.max_bytes and self.max_bytes > 0:
                while self._cache and self._bytes + len(value) > self.max_bytes:
                    _, removed = self._cache.popitem(last=False)
                    self._bytes -= len(removed)
                self._cache[key] = value
                self._bytes += len(value)
        return value

    def _finish_inflight(self, key: tuple[Any, ...], task: asyncio.Task[bytes]) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key)
        if not task.cancelled():
            task.exception()  # đánh dấu đã đọc để không log "exception was never retrieved"

    def cache_status(self) -> dict[str, int]:
        return {
            "hits": self._hits, "misses": self._misses, "entries": len(self._cache),
            "bytes": self._bytes, "max_bytes": self.max_bytes,
        }
