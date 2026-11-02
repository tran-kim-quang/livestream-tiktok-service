from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_comments
from app.comments.buffer import CommentBuffer

router = APIRouter(prefix="/api/comments", tags=["comments"])


@router.get("/latest")
async def latest_comments(
    limit: int = Query(20, ge=1, le=500),
    comments: CommentBuffer = Depends(get_comments),
) -> dict:
    items = await comments.latest(limit)
    return {"limit": limit, "count": len(items), "comments": items}


@router.get("/stats")
async def comment_stats(comments: CommentBuffer = Depends(get_comments)) -> dict:
    return await comments.stats()
