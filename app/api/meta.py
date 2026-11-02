from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_comments, get_hub, get_tts
from app.config import TIKTOK_USERNAME
from app.comments.buffer import CommentBuffer
from app.live.hub import LiveStreamHub
from app.tts.base import TTSEngine

router = APIRouter(tags=["meta"])


@router.get("/health")
async def health(
    comments: CommentBuffer = Depends(get_comments),
    hub: LiveStreamHub = Depends(get_hub),
    tts: TTSEngine | None = Depends(get_tts),
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
        },
        "sessions": hub.list_sessions(),
        "auto_watch": TIKTOK_USERNAME or None,
        "tts": await tts.status() if tts is not None else None,
    }
