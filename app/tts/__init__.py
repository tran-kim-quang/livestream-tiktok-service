from __future__ import annotations

from typing import Optional

from app.config import (
    TTS_ENGINE,
    TTS_CACHE,
    TTS_CACHE_MAX_MB,
    TTS_ENABLED,
    TTS_MAX_CONCURRENCY,
    TTS_NUM_STEP,
    TTS_SPEED,
    TTS_TIMEOUT_SEC,
    TTS_VOICE_ID,
    VOICESTUDIO_API_KEY,
    VOICESTUDIO_API_PORT,
    VOICESTUDIO_PORT,
)
from app.gpu import GpuNode
from app.tts.base import TTSEngine, TTSError, VoiceNotFoundError
from app.tts.cache import CachedTTSEngine
from app.tts.pyramid import PyramidVideoTTS
from app.tts.voicestudio import VoiceStudioTTS

__all__ = ["TTSEngine", "TTSError", "VoiceNotFoundError", "CachedTTSEngine", "PyramidVideoTTS", "VoiceStudioTTS", "create_tts_engine"]


def create_tts_engine(node: Optional[GpuNode] = None) -> Optional[TTSEngine]:
    """Tạo engine TTS theo config; trả None nếu TTS_ENABLED=0."""
    if not TTS_ENABLED:
        return None
    gpu = node or GpuNode.from_env()
    common = {
        "default_voice_id": TTS_VOICE_ID,
        "timeout": TTS_TIMEOUT_SEC,
        "max_concurrency": TTS_MAX_CONCURRENCY,
    }
    engine: TTSEngine
    if TTS_ENGINE == "pyramid":
        engine = PyramidVideoTTS(gpu, port=VOICESTUDIO_PORT, **common)
    elif TTS_ENGINE == "voicestudio":
        engine = VoiceStudioTTS(
            gpu, port=VOICESTUDIO_API_PORT, api_key=VOICESTUDIO_API_KEY,
            num_step=TTS_NUM_STEP, speed=TTS_SPEED, **common,
        )
    else:
        raise ValueError(f"TTS_ENGINE không hợp lệ: {TTS_ENGINE!r}")
    return CachedTTSEngine(engine, int(TTS_CACHE_MAX_MB * 1024 * 1024)) if TTS_CACHE else engine
