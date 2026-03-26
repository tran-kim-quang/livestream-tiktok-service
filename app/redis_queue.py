from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import redis.asyncio as redis

from app.config import (
    REDIS_CONNECT_RETRIES,
    REDIS_CONNECT_RETRY_SEC,
    REDIS_ENABLED,
    REDIS_QUEUE_KEY,
    REDIS_ROOM_COMMENTS_PREFIX,
    REDIS_ROOM_MAX_COMMENTS,
    REDIS_URL,
)

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


async def init_redis() -> bool:
    """Kết nối Redis; trả False nếu tắt hoặc lỗi (service vẫn chạy, chỉ không ghi queue)."""
    global _client
    if not REDIS_ENABLED:
        logger.info("Redis tắt (REDIS_ENABLED=0).")
        return False

    last_err: Optional[BaseException] = None
    for attempt in range(1, REDIS_CONNECT_RETRIES + 1):
        r: Optional[redis.Redis] = None
        try:
            r = redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
            )
            await r.ping()
            _client = r
            if attempt > 1:
                logger.info("Redis kết nối được sau %s lần thử.", attempt)
            logger.info(
                "Redis đã kết nối: %s, queue=%s, room_prefix=%s",
                REDIS_URL,
                REDIS_QUEUE_KEY,
                REDIS_ROOM_COMMENTS_PREFIX,
            )
            return True
        except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
            last_err = e
            if r is not None:
                try:
                    await r.aclose()
                except Exception:
                    pass
            if attempt < REDIS_CONNECT_RETRIES:
                logger.debug(
                    "Redis lần %s/%s chưa kết nối được (%s), chờ %.2fs…",
                    attempt,
                    REDIS_CONNECT_RETRIES,
                    e,
                    REDIS_CONNECT_RETRY_SEC,
                )
                await asyncio.sleep(REDIS_CONNECT_RETRY_SEC)
            continue
        except Exception:
            if r is not None:
                try:
                    await r.aclose()
                except Exception:
                    pass
            logger.exception("Lỗi không mong đợi khi kết nối Redis.")
            _client = None
            return False

    logger.warning(
        "Không kết nối Redis tại %s sau %s lần (%s). "
        "App vẫn chạy nhưng không ghi/đọc queue. "
        "Kiểm tra *cùng môi trường* với uvicorn: `redis-cli -u %s ping` hoặc "
        "`nc -zv 127.0.0.1 6379`. Nếu Redis trong Docker: có `-p 6379:6379`? "
        "Nếu Redis trên máy khác / Windows: chỉnh REDIS_URL (vd host IP, không phải 127.0.0.1).",
        REDIS_URL,
        REDIS_CONNECT_RETRIES,
        last_err,
        REDIS_URL,
    )
    _client = None
    return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            logger.debug("redis close", exc_info=True)
        _client = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def room_comments_redis_key(room_id: int) -> str:
    return REDIS_ROOM_COMMENTS_PREFIX.format(room_id=room_id)


async def enqueue_comment_for_ai(payload: Dict[str, Any]) -> None:
    """
    Đẩy comment vào:
    - List toàn cục (hàng đợi worker AI, FIFO với BRPOP)
    - List theo room_id (để API đọc comment mới nhất theo phiên live)
    """
    if _client is None:
        return
    record = {
        "enqueued_at": _now_iso(),
        "streamer_unique_id": payload.get("streamer_unique_id"),
        "room_id": payload.get("room_id"),
        "comment": payload.get("comment"),
        "user": payload.get("user"),
    }
    try:
        data = json.dumps(record, ensure_ascii=False)
        pipe = _client.pipeline()
        pipe.lpush(REDIS_QUEUE_KEY, data)
        rid = payload.get("room_id")
        if rid is not None:
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                rid_int = None
            if rid_int is not None:
                rkey = room_comments_redis_key(rid_int)
                pipe.lpush(rkey, data)
                pipe.ltrim(rkey, 0, max(0, REDIS_ROOM_MAX_COMMENTS - 1))
        await pipe.execute()
    except Exception:
        logger.exception("Ghi Redis queue thất bại; bỏ qua lần này.")


async def fetch_latest_comments_for_room(room_id: int, limit: int) -> list[Dict[str, Any]]:
    """
    Đọc comment mới nhất của một phiên live (LPUSH → phần tử 0 là mới nhất).
    """
    if _client is None:
        return []
    key = room_comments_redis_key(room_id)
    try:
        raw_list = await _client.lrange(key, 0, max(0, limit - 1))
    except Exception:
        logger.exception("Đọc Redis room comments thất bại.")
        return []
    out: list[Dict[str, Any]] = []
    for raw in raw_list:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


async def pop_comment(timeout: float = 0) -> Optional[Dict[str, Any]]:
    """
    Lấy comment **cũ nhất** ra khỏi hàng đợi (FIFO) và **xoá** khỏi Redis.

    timeout=0  → RPOP  (non-blocking, trả None nếu queue rỗng)
    timeout>0  → BRPOP (blocking tối đa `timeout` giây, trả None nếu hết giờ)

    Agent gọi hàm này (hoặc endpoint /api/queue/pop) để nhận comment tiếp theo.
    """
    if _client is None:
        return None
    try:
        if timeout > 0:
            result = await _client.brpop(REDIS_QUEUE_KEY, timeout=int(timeout))
            if result is None:
                return None
            _, raw = result
        else:
            raw = await _client.rpop(REDIS_QUEUE_KEY)
            if raw is None:
                return None
        return json.loads(raw)
    except Exception:
        logger.exception("Pop comment từ Redis queue thất bại.")
        return None


async def queue_length() -> int:
    """Số comment đang chờ trong hàng đợi."""
    if _client is None:
        return 0
    try:
        return await _client.llen(REDIS_QUEUE_KEY)
    except Exception:
        return 0


async def ping() -> bool:
    if _client is None:
        return False
    try:
        return bool(await _client.ping())
    except Exception:
        return False
