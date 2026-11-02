from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_comments
from app.comments.buffer import CommentBuffer

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/pop")
async def queue_pop(
    timeout: float = Query(0, ge=0, le=30, description="Giây chờ tối đa nếu chưa có comment mới"),
    comments: CommentBuffer = Depends(get_comments),
) -> dict:
    """Lấy comment mới, đáng trả lời nhất và xoá khỏi buffer."""
    comment = await comments.next(timeout=timeout)
    return {"empty": comment is None, "comment": comment}


@router.get("/length")
async def api_queue_length(comments: CommentBuffer = Depends(get_comments)) -> dict:
    """Số comment mới, chưa được agent lấy."""
    return {"queue_length": await comments.length()}
