from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from typing import Any, Dict, List, Optional

import httpx

from app.gpu import GpuNode
from app.tts.base import TTSEngine, TTSError, VoiceNotFoundError

logger = logging.getLogger(__name__)


class PyramidVideoTTS(TTSEngine):
    """
    Gọi model VoiceStudio đang chạy trên máy GPU qua web API (Pyramid Video Web).

    Dùng các endpoint:
      GET  /api/status                       — VoiceStudio đã sẵn sàng chưa
      GET  /api/voices                       — danh sách giọng (clone)
      POST /api/voices/{id}/preview {text}   — sinh audio/wav
    """

    name = "pyramid"

    def __init__(
        self,
        node: GpuNode,
        port: int = 8080,
        default_voice_id: str = "",
        timeout: float = 120.0,
        max_concurrency: int = 1,
    ) -> None:
        self.node = node
        self.base_url = node.http_url(port)
        self.default_voice_id = default_voice_id
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )
        # GPU dùng chung với job render video — giới hạn số request đồng thời
        self._gpu_slots = asyncio.Semaphore(max_concurrency)
        self._voices_cache: List[Dict[str, Any]] | None = None
        self._voices_cache_at = 0.0

    async def list_voices(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if self._voices_cache is not None and now - self._voices_cache_at < 30.0:
            return self._voices_cache
        try:
            r = await self._client.get("/api/voices")
            r.raise_for_status()
            voices = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TTSError(f"Không lấy được danh sách giọng từ {self.base_url}: {e}") from e
        if not isinstance(voices, list) or not all(isinstance(voice, dict) for voice in voices):
            raise TTSError(f"Danh sách giọng từ {self.base_url} không đúng định dạng.")
        self._voices_cache = voices
        self._voices_cache_at = now
        return voices

    async def _resolve_voice(self, voice_id: Optional[str]) -> str:
        voices = await self.list_voices()
        selected_voice_id = voice_id if voice_id is not None else self.default_voice_id or None
        if selected_voice_id is not None:
            if any(v.get("id") == selected_voice_id for v in voices):
                return selected_voice_id
            raise VoiceNotFoundError(f"Không tìm thấy giọng '{selected_voice_id}'.")
        for v in voices:
            if v.get("is_default"):
                voice_id = v.get("id")
                if isinstance(voice_id, str) and voice_id:
                    return voice_id
        if voices:
            voice_id = voices[0].get("id")
            if isinstance(voice_id, str) and voice_id:
                return voice_id
            raise TTSError("VoiceStudio trả về giọng đọc không có ID hợp lệ.")
        raise TTSError("VoiceStudio chưa có giọng đọc nào.")

    async def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        text = text.strip()
        if not text:
            raise TTSError("Text rỗng.")
        vid = await self._resolve_voice(voice_id)
        async with self._gpu_slots:
            try:
                r = await self._client.post(f"/api/voices/{vid}/preview", json={"text": text})
            except httpx.HTTPError as e:
                raise TTSError(f"Gọi VoiceStudio tại {self.base_url} thất bại: {e}") from e
        if r.status_code == 404:
            raise VoiceNotFoundError(f"Không tìm thấy giọng '{vid}'.")
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("audio/"):
            raise TTSError(f"VoiceStudio trả lỗi HTTP {r.status_code}: {r.text[:200]}")
        return r.content

    async def synthesize_pcm(
        self, text: str, voice_id: Optional[str] = None, num_step: int | None = None
    ) -> bytes:
        wav = await self.synthesize(text, voice_id)
        try:
            with wave.open(io.BytesIO(wav), "rb") as audio:
                return audio.readframes(audio.getnframes())
        except (wave.Error, EOFError) as exc:
            raise TTSError(f"VoiceStudio trả WAV không hợp lệ: {exc}") from exc

    async def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"engine": self.name, "url": self.base_url, "gpu": self.node.info()}
        try:
            r = await self._client.get("/api/status", timeout=5.0)
            r.raise_for_status()
            s = r.json()
            out["ok"] = bool(s.get("voicestudio") and s.get("ready"))
            out["ready"] = s.get("ready")
        except Exception as e:
            out["ok"] = False
            out["error"] = str(e)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
