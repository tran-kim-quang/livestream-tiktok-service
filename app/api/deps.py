from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.comments.buffer import CommentBuffer
from app.live.hub import LiveStreamHub
from app.tts.base import TTSEngine
from app.agent.responder import LiveAgent
from app.audio.player import LocalAudioPlayer


def get_comments(request: Request) -> CommentBuffer:
    return request.app.state.comments


def get_hub(request: Request) -> LiveStreamHub:
    return request.app.state.hub


def get_tts(request: Request) -> Optional[TTSEngine]:
    return request.app.state.tts


def get_agent(request: Request) -> Optional[LiveAgent]:
    return getattr(request.app.state, "agent", None)


def get_player(request: Request) -> Optional[LocalAudioPlayer]:
    return getattr(request.app.state, "audio_player", None)
