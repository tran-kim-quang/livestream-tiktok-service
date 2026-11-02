from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from app.gpu import GpuNode
from app.ssh_tunnel import SshTunnel
from app.tts.base import TTSEngine, TTSError, VoiceNotFoundError


class VoiceStudioTTS(TTSEngine):
    """Client cho VoiceStudio API v0.5.x (OpenAI-compatible speech endpoint)."""

    name = "voicestudio"

    def __init__(
        self,
        node: GpuNode,
        port: int = 3900,
        default_voice_id: str = "",
        timeout: float = 120.0,
        max_concurrency: int = 1,
        api_key: str = "",
        num_step: int | None = None,
        speed: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.node = node
        self.default_voice_id = default_voice_id
        self.num_step = num_step
        self.speed = speed
        self._timeout = httpx.Timeout(timeout, connect=5.0)
        self._transport = transport
        self._tunnel = SshTunnel(node, remote_port=port) if node.use_ssh else None
        self.base_url = node.http_url(port)
        self._headers = {}
        if api_key and not node.is_local and not node.use_ssh:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = self._new_client(self.base_url)
        self._gpu_slots = asyncio.Semaphore(max_concurrency)
        self._voices_cache: List[Dict[str, Any]] | None = None
        self._voices_cache_at = 0.0

    def _new_client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url, headers=self._headers, timeout=self._timeout,
            transport=self._transport,
        )

    async def start(self) -> None:
        if self._tunnel is not None:
            await self._prepare_client()

    async def _prepare_client(self) -> None:
        if self._tunnel is None:
            return
        try:
            await self._tunnel.ensure()
        except Exception as exc:
            raise TTSError(f"Không mở được SSH tunnel tới GPU {self.node.host}: {exc}") from exc
        new_url = self._tunnel.base_url
        if new_url != self.base_url:
            old_client = self._client
            self.base_url = new_url
            self._client = self._new_client(new_url)
            await old_client.aclose()

    async def list_voices(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if self._voices_cache is not None and now - self._voices_cache_at < 30.0:
            return self._voices_cache
        await self._prepare_client()
        try:
            response = await self._client.get("/profiles")
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TTSError(f"Không lấy được danh sách giọng từ {self.base_url}: {exc}") from exc
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise TTSError(f"Danh sách giọng từ {self.base_url} không đúng định dạng.")
        voices = [dict(item) for item in raw if isinstance(item.get("id"), str)]
        self._voices_cache = voices
        self._voices_cache_at = now
        return voices

    async def _resolve_voice(self, voice_id: Optional[str]) -> str:
        voices = await self.list_voices()
        selected = voice_id if voice_id is not None else self.default_voice_id or None
        if selected is not None:
            if any(voice.get("id") == selected for voice in voices):
                return selected
            raise VoiceNotFoundError(f"Không tìm thấy giọng '{selected}'.")
        for voice in voices:
            if voice.get("is_default") and isinstance(voice.get("id"), str):
                return voice["id"]
        if voices:
            return voices[0]["id"]
        raise TTSError("VoiceStudio chưa có giọng đọc nào.")

    async def _speech(
        self,
        text: str,
        voice_id: Optional[str],
        response_format: str,
        num_step: int | None = None,
    ) -> bytes:
        text = text.strip()
        if not text:
            raise TTSError("Text rỗng.")
        voice = await self._resolve_voice(voice_id)
        payload: Dict[str, Any] = {
            "model": "omnivoice", "input": text, "voice": voice,
            "response_format": response_format, "speed": self.speed,
            "stream_format": "audio",
        }
        effective_num_step = num_step if num_step is not None else self.num_step
        if effective_num_step is not None:
            payload["num_step"] = effective_num_step
        await self._prepare_client()
        async with self._gpu_slots:
            try:
                response = await self._client.post("/v1/audio/speech", json=payload)
            except httpx.HTTPError as exc:
                raise TTSError(f"Gọi VoiceStudio tại {self.base_url} thất bại: {exc}") from exc
        if response.status_code == 404:
            raise VoiceNotFoundError(f"Không tìm thấy giọng '{voice}'.")
        if response.status_code != 200:
            raise TTSError(f"VoiceStudio trả lỗi HTTP {response.status_code}: {response.text[:200]}")
        return response.content

    async def synthesize(self, text: str, voice_id: Optional[str] = None) -> bytes:
        return await self._speech(text, voice_id, "wav")

    async def synthesize_pcm(
        self, text: str, voice_id: Optional[str] = None, num_step: int | None = None
    ) -> bytes:
        return await self._speech(text, voice_id, "pcm", num_step)

    async def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "engine": self.name, "url": self.base_url, "gpu": self.node.info(),
            "via": "ssh" if self._tunnel is not None else "direct",
        }
        try:
            await self._prepare_client()
            out["url"] = self.base_url
            response = await self._client.get("/model/status", timeout=5.0)
            response.raise_for_status()
            state = response.json()
            out.update(state if isinstance(state, dict) else {})
            out["ok"] = bool(isinstance(state, dict) and state.get("loaded") and state.get("status") == "ready")
        except Exception as exc:
            out["ok"] = False
            out["error"] = str(exc)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._tunnel is not None:
            await self._tunnel.close()
