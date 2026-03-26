import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, field_validator
from starlette.websockets import WebSocketState

from TikTokLive.client.errors import UserNotFoundError, UserOfflineError

from app.config import TIKTOK_LIVE_RETRY_SEC, TIKTOK_USERNAME
from app.hub import hub
from app.redis_queue import (
    close_redis,
    fetch_latest_comments_for_room,
    init_redis,
    ping,
    pop_comment,
    queue_length,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Auto-watch background task
# ──────────────────────────────────────────────────────────────────────────────

async def _auto_watch(unique_id: str) -> None:
    """
    Giữ kết nối với phiên live của `unique_id`.
    - Nếu chưa live → chờ TIKTOK_LIVE_RETRY_SEC giây rồi thử lại.
    - Nếu live kết thúc → tự động reconnect sau TIKTOK_LIVE_RETRY_SEC giây.
    """
    while True:
        session = None
        try:
            session = await hub.start_managed(unique_id)
            await session.ensure_started()
            logger.info("Auto-watch: đã kết nối @%s (room_id=%s)", unique_id, session.room_id)
            # Block cho đến khi WebSocket TikTok thực sự ngắt (live kết thúc)
            while session.client.connected:
                await asyncio.sleep(2)
            logger.info("Auto-watch: @%s live kết thúc, thử lại sau %ss.", unique_id, TIKTOK_LIVE_RETRY_SEC)
        except UserOfflineError:
            logger.info("Auto-watch: @%s chưa live, thử lại sau %ss.", unique_id, TIKTOK_LIVE_RETRY_SEC)
        except asyncio.CancelledError:
            logger.info("Auto-watch: task bị huỷ.")
            break
        except Exception as exc:
            logger.warning("Auto-watch: @%s lỗi (%s), thử lại sau %ss.", unique_id, exc, TIKTOK_LIVE_RETRY_SEC)
        finally:
            if session is not None:
                await hub.stop_managed(unique_id)
        try:
            await asyncio.sleep(TIKTOK_LIVE_RETRY_SEC)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_redis()

    watch_task: asyncio.Task | None = None
    if TIKTOK_USERNAME:
        watch_task = asyncio.create_task(_auto_watch(TIKTOK_USERNAME))
        logger.info("Auto-watch khởi động cho @%s (retry=%ss)", TIKTOK_USERNAME, TIKTOK_LIVE_RETRY_SEC)
    else:
        logger.warning(
            "TIKTOK_USERNAME chưa được đặt — service không tự động lấy comment. "
            "Dùng POST /api/live/watch hoặc đặt biến môi trường TIKTOK_USERNAME."
        )

    yield

    if watch_task and not watch_task.done():
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass

    await hub.shutdown_all()
    await close_redis()


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="TikTok Live Comment Service",
    description=(
        "Thu thập comment realtime từ TikTok Live, đẩy vào Redis queue. "
        "Agent đọc queue qua `GET /api/queue/pop` (FIFO, pop-and-remove)."
    ),
    lifespan=lifespan,
)


# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

class WatchRequest(BaseModel):
    unique_id: str

    @field_validator("unique_id")
    @classmethod
    def strip_at(cls, v: str) -> str:
        return v.strip().lstrip("@")


# ──────────────────────────────────────────────────────────────────────────────
# Health / meta
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health() -> dict:
    redis_ok = await ping()
    return {
        "status": "ok",
        "redis": "ok" if redis_ok else "down",
        "queue_length": await queue_length() if redis_ok else None,
        "sessions": hub.list_sessions(),
        "auto_watch": TIKTOK_USERNAME or None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Agent queue — FIFO pop
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/queue/pop", tags=["queue"])
async def queue_pop(
    timeout: float = Query(
        0, ge=0, le=30,
        description="Giây chờ tối đa nếu queue rỗng (0=non-blocking, >0=blocking BRPOP)",
    )
) -> dict:
    """
    **Lấy comment tiếp theo từ hàng đợi và xoá khỏi Redis (FIFO).**

    - `empty: true` → hàng đợi rỗng, không có comment mới.
    - Dùng `timeout > 0` để giảm số lần polling: server sẽ chờ tối đa N giây
      trước khi trả về (thay vì agent phải loop gọi liên tục).
    """
    if not await ping():
        raise HTTPException(status_code=503, detail="Redis không kết nối.")
    comment = await pop_comment(timeout=timeout)
    if comment is None:
        return {"empty": True, "comment": None}
    return {"empty": False, "comment": comment}


@app.get("/api/queue/length", tags=["queue"])
async def api_queue_length() -> dict:
    """Số comment đang chờ trong hàng đợi (chưa được agent lấy)."""
    if not await ping():
        raise HTTPException(status_code=503, detail="Redis không kết nối.")
    return {"queue_length": await queue_length()}


# ──────────────────────────────────────────────────────────────────────────────
# Session management (tuỳ chọn — dùng thêm khi cần nhiều nametag)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/api/live/watch", tags=["watch"], status_code=201)
async def start_watch(req: WatchRequest) -> dict:
    """Bắt đầu theo dõi thêm một live khác (ngoài TIKTOK_USERNAME đã config)."""
    session = await hub.start_managed(req.unique_id)
    try:
        await session.ensure_started()
    except UserOfflineError:
        session.managed = False
        await session.discard_after_failed_start()
        raise HTTPException(status_code=404, detail=f"@{req.unique_id} không đang live.")
    except UserNotFoundError:
        session.managed = False
        await session.discard_after_failed_start()
        raise HTTPException(status_code=404, detail=f"Không tìm thấy @{req.unique_id}.")
    except Exception as exc:
        session.managed = False
        await session.discard_after_failed_start()
        logger.exception("Kết nối live thất bại: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    room_id = session.room_id
    return {
        "status": "watching",
        "unique_id": session.unique_id,
        "room_id": str(room_id) if room_id else None,
        "comments_endpoint": f"/api/live/{room_id}/comments",
    }


@app.delete("/api/live/watch/{unique_id}", tags=["watch"])
async def stop_watch(unique_id: str) -> dict:
    """Dừng theo dõi và giải phóng kết nối."""
    uid = unique_id.lstrip("@")
    if not await hub.stop_managed(uid):
        raise HTTPException(status_code=404, detail=f"Không có session nào cho @{uid}.")
    return {"status": "stopped", "unique_id": uid}


@app.get("/api/live/sessions", tags=["watch"])
async def list_sessions() -> dict:
    """Liệt kê tất cả phiên live đang chạy."""
    return {"sessions": hub.list_sessions()}


# ──────────────────────────────────────────────────────────────────────────────
# Comment history (đọc không xoá — khác với /api/queue/pop)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/live/{room_id}/comments", tags=["comments"])
async def api_live_room_comments(
    room_id: str,
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    """
    Xem lịch sử comment theo `room_id` (**không xoá** khỏi Redis).
    `room_id` lấy từ `/api/live/sessions` hoặc response của `POST /api/live/watch`.
    """
    try:
        rid = int(room_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="room_id phải là số nguyên.")
    if not await ping():
        raise HTTPException(status_code=503, detail="Redis không kết nối.")
    comments = await fetch_latest_comments_for_room(rid, limit)
    return {"room_id": str(rid), "limit": limit, "count": len(comments), "comments": comments}


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket (tuỳ chọn — realtime push, không cần polling)
# ──────────────────────────────────────────────────────────────────────────────

@app.websocket("/ws/live/{unique_id}")
async def live_comments_ws(websocket: WebSocket, unique_id: str) -> None:
    await websocket.accept()
    session = await hub.get_session(unique_id)
    await session.add_subscriber(websocket)
    try:
        try:
            await session.ensure_started()
        except UserOfflineError:
            await websocket.send_json({"type": "error", "code": "user_offline"})
            await session.discard_after_failed_start()
            return
        except UserNotFoundError:
            await websocket.send_json({"type": "error", "code": "user_not_found"})
            await session.discard_after_failed_start()
            return
        except Exception as exc:
            await websocket.send_json({"type": "error", "code": "connect_failed", "message": str(exc)})
            await session.discard_after_failed_start()
            return
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await session.remove_subscriber(websocket)
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception:
                pass
