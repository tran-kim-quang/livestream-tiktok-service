from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from fastapi import WebSocket
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent
from TikTokLive.events.custom_events import DisconnectEvent, LiveEndEvent

from app.redis_queue import enqueue_comment_for_ai

logger = logging.getLogger(__name__)

OnEmpty = Callable[[str], Awaitable[None]]


def _comment_to_dict(client: TikTokLiveClient, event: CommentEvent) -> Dict[str, Any]:
    u = event.user
    return {
        "type": "comment",
        "comment": event.comment,
        "user": {
            "id": int(u.id) if getattr(u, "id", None) else None,
            "nickname": u.nickname,
            "unique_id": u.unique_id,
        },
        "room_id": client.room_id,
        "streamer_unique_id": client.unique_id,
    }


class LiveStreamSession:
    def __init__(self, unique_id: str, on_empty: OnEmpty) -> None:
        self.unique_id = unique_id
        self.client = TikTokLiveClient(unique_id=unique_id)
        self.subscribers: Set[WebSocket] = set()
        self._start_lock = asyncio.Lock()
        self._started = False
        self._listeners_attached = False
        self._on_empty = on_empty
        # Khi managed=True session tồn tại độc lập, không tự dừng khi hết WS subscriber
        self.managed: bool = False

    @property
    def room_id(self) -> Optional[int]:
        return self.client.room_id

    def info(self) -> Dict[str, Any]:
        return {
            "unique_id": self.unique_id,
            "room_id": str(self.room_id) if self.room_id else None,
            "managed": self.managed,
            "ws_subscribers": len(self.subscribers),
            "started": self._started,
        }

    def _attach_listeners(self) -> None:
        if self._listeners_attached:
            return
        self._listeners_attached = True

        @self.client.on(ConnectEvent)
        async def on_connect(_event: ConnectEvent) -> None:
            await self._broadcast(
                {
                    "type": "connected",
                    "room_id": self.client.room_id,
                    "streamer_unique_id": self.client.unique_id,
                }
            )

        @self.client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            data = _comment_to_dict(self.client, event)
            await enqueue_comment_for_ai(data)
            await self._broadcast(data)

        @self.client.on(LiveEndEvent)
        async def on_live_end(_event: LiveEndEvent) -> None:
            await self._broadcast({"type": "live_end", "streamer_unique_id": self.client.unique_id})

        @self.client.on(DisconnectEvent)
        async def on_disconnect(_event: DisconnectEvent) -> None:
            await self._broadcast({"type": "disconnect", "streamer_unique_id": self.client.unique_id})

    async def _broadcast(self, payload: Dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self.subscribers:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)
        # Chỉ tự dừng khi KHÔNG phải managed và hết WS subscriber
        if not self.subscribers and self._started and not self.managed:
            await self._stop_and_notify_empty()

    async def _stop_and_notify_empty(self) -> None:
        await self.shutdown()
        await self._on_empty(self.unique_id)

    async def ensure_started(self) -> None:
        async with self._start_lock:
            if self._started:
                return
            self._attach_listeners()
            await self.client.start(
                fetch_room_info=False,
                fetch_gift_info=False,
                fetch_live_check=True,
            )
            self._started = True

    async def discard_after_failed_start(self) -> None:
        try:
            await self.client.close()
        except Exception:
            logger.debug("close after failed start", exc_info=True)
        self._started = False

    async def add_subscriber(self, ws: WebSocket) -> None:
        self.subscribers.add(ws)

    async def remove_subscriber(self, ws: WebSocket) -> None:
        self.subscribers.discard(ws)
        # Nếu managed: không tự dừng dù hết WS subscriber
        if not self.subscribers and not self.managed:
            if self._started:
                await self._stop_and_notify_empty()
            else:
                await self._on_empty(self.unique_id)

    async def shutdown(self) -> None:
        if not self._started:
            return
        try:
            await self.client.disconnect(close_client=True)
        except Exception:
            logger.debug("client.disconnect", exc_info=True)
        self._started = False


class LiveStreamHub:
    def __init__(self) -> None:
        self._sessions: Dict[str, LiveStreamSession] = {}
        self._global_lock = asyncio.Lock()

    async def _release_empty(self, key: str) -> None:
        async with self._global_lock:
            self._sessions.pop(key, None)

    async def get_session(self, unique_id: str) -> LiveStreamSession:
        key = TikTokLiveClient.parse_unique_id(unique_id)
        async with self._global_lock:
            if key not in self._sessions:
                self._sessions[key] = LiveStreamSession(key, self._release_empty)
            return self._sessions[key]

    # ------------------------------------------------------------------ #
    # Managed API (dùng cho REST endpoint thay vì WebSocket)              #
    # ------------------------------------------------------------------ #

    async def start_managed(self, unique_id: str) -> LiveStreamSession:
        """Lấy hoặc tạo session, đánh dấu managed để không tự dừng."""
        session = await self.get_session(unique_id)
        session.managed = True
        return session

    async def stop_managed(self, unique_id: str) -> bool:
        """Dừng và xoá session theo unique_id. Trả False nếu không tồn tại."""
        key = TikTokLiveClient.parse_unique_id(unique_id)
        async with self._global_lock:
            session = self._sessions.pop(key, None)
        if session is None:
            return False
        await session.shutdown()
        return True

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [s.info() for s in self._sessions.values()]

    # ------------------------------------------------------------------ #

    async def shutdown_all(self) -> None:
        async with self._global_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            await s.shutdown()


hub = LiveStreamHub()
