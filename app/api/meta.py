from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agent.responder import LiveAgent
from app.api.deps import get_agent, get_comments, get_hub, get_tts
from app.config import AGENT_AUTO, LLM_MODEL, TIKTOK_USERNAME
from app.comments.buffer import CommentBuffer
from app.live.hub import LiveStreamHub
from app.tts.base import TTSEngine

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health(
    comments: CommentBuffer = Depends(get_comments),
    hub: LiveStreamHub = Depends(get_hub),
    tts: TTSEngine | None = Depends(get_tts),
    agent: LiveAgent | None = Depends(get_agent),
) -> dict:
    comment_stats = await comments.stats()
    return {
        "status": "ok",
        "comments": {
            "current": comment_stats["current"],
            "received": comment_stats["received"],
            "accepted": comment_stats["accepted"],
            "served": comment_stats["served"],
            "expired": comment_stats["expired"],
            "relevance": comment_stats["relevance"],
        },
        "sessions": hub.list_sessions(),
        "auto_watch": TIKTOK_USERNAME or None,
        "tts": await tts.status() if tts is not None else None,
        "agent": {
            "enabled": agent is not None,
            "model": LLM_MODEL if agent is not None else None,
            "auto": AGENT_AUTO if agent is not None else False,
            "stats": agent.stats() if agent is not None else None,
        },
    }
