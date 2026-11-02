from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, field_validator
from starlette.websockets import WebSocketState

from TikTokLive.client.errors import UserNotFoundError, UserOfflineError

from app.api.deps import get_hub
from app.live.hub import LiveStreamHub

logger = logging.getLogger(__name__)
router = APIRouter(tags=["watch"])


class WatchRequest(BaseModel):
    unique_id: str

    @field_validator("unique_id")
    @classmethod
    def strip_at(cls, value: str) -> str:
        return value.strip().lstrip("@")


@router.post("/api/live/watch", status_code=201)
async def start_watch(req: WatchRequest, hub: LiveStreamHub = Depends(get_hub)) -> dict:
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
    return {"status": "watching", "unique_id": session.unique_id, "room_id": str(room_id) if room_id else None, "comments_endpoint": "/api/comments/latest"}


@router.delete("/api/live/watch/{unique_id}")
async def stop_watch(unique_id: str, hub: LiveStreamHub = Depends(get_hub)) -> dict:
    uid = unique_id.lstrip("@")
    if not await hub.stop_managed(uid):
        raise HTTPException(status_code=404, detail=f"Không có session nào cho @{uid}.")
    return {"status": "stopped", "unique_id": uid}


@router.get("/api/live/sessions")
async def list_sessions(hub: LiveStreamHub = Depends(get_hub)) -> dict:
    return {"sessions": hub.list_sessions()}


@router.websocket("/ws/live/{unique_id}")
async def live_comments_ws(websocket: WebSocket, unique_id: str) -> None:
    hub: LiveStreamHub = websocket.app.state.hub
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
