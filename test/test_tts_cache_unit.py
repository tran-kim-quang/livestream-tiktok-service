from __future__ import annotations

import asyncio
import unittest

from app.tts.base import TTSEngine
from app.tts.cache import CachedTTSEngine


class FakeTTS(TTSEngine):
    name = "fake"
    def __init__(self) -> None: self.calls = 0; self.fail = False
    async def synthesize(self, text, voice_id=None): return b"wav"
    async def list_voices(self): return []
    async def status(self): return {"ok": True}
    async def synthesize_pcm(self, text, voice_id=None, num_step=None):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.fail: raise RuntimeError("fail")
        return text.encode()


class CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_hit_va_chuan_hoa_space(self) -> None:
        inner = FakeTTS(); cache = CachedTTSEngine(inner, 100)
        self.assertEqual(await cache.synthesize_pcm("xin  chào"), await cache.synthesize_pcm(" xin chào "))
        self.assertEqual(inner.calls, 1); self.assertEqual(cache.cache_status()["hits"], 1)

    async def test_lru_theo_bytes(self) -> None:
        inner = FakeTTS(); cache = CachedTTSEngine(inner, 5)
        await cache.synthesize_pcm("aaa"); await cache.synthesize_pcm("bbb")
        self.assertEqual(cache.cache_status()["entries"], 1)
        await cache.synthesize_pcm("aaa"); self.assertEqual(inner.calls, 3)

    async def test_single_flight_va_loi_khong_cache(self) -> None:
        inner = FakeTTS(); cache = CachedTTSEngine(inner, 100)
        await asyncio.gather(cache.synthesize_pcm("abc"), cache.synthesize_pcm("abc"))
        self.assertEqual(inner.calls, 1)
        inner.fail = True
        with self.assertRaises(RuntimeError): await cache.synthesize_pcm("bad")
        with self.assertRaises(RuntimeError): await cache.synthesize_pcm("bad")
        self.assertEqual(inner.calls, 3)


if __name__ == "__main__": unittest.main()
