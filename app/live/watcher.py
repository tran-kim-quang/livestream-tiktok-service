from __future__ import annotations

import asyncio
import logging

from TikTokLive.client.errors import UserOfflineError

from app.live.hub import LiveStreamHub

logger = logging.getLogger(__name__)


async def auto_watch(
    hub: LiveStreamHub, unique_id: str, retry_sec: float, reconnect_sec: float = 3.0
) -> None:
    """Giữ kết nối tới live và thử lại khi streamer chưa live hoặc vừa kết thúc."""
    while True:
        session = None
        wait_sec = retry_sec
        try:
            session = await hub.start_managed(unique_id)
            await session.ensure_started()
            logger.info("Auto-watch: đã kết nối @%s (room_id=%s)", unique_id, session.room_id)
            # client.start() trả về trước khi WS kết nối xong → chờ task vòng lặp WS kết thúc
            if session.task is not None:
                try:
                    await session.task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Auto-watch: @%s WebSocket lỗi (%s).", unique_id, exc)
            if session.live_ended:
                logger.info("Auto-watch: @%s live kết thúc, thử lại sau %ss.", unique_id, retry_sec)
            else:
                wait_sec = reconnect_sec
                logger.info("Auto-watch: @%s mất kết nối, kết nối lại sau %ss.", unique_id, wait_sec)
        except UserOfflineError:
            logger.info("Auto-watch: @%s chưa live, thử lại sau %ss.", unique_id, retry_sec)
        except asyncio.CancelledError:
            logger.info("Auto-watch: task bị huỷ.")
            break
        except Exception as exc:
            logger.warning("Auto-watch: @%s lỗi (%s), thử lại sau %ss.", unique_id, exc, retry_sec)
        finally:
            if session is not None:
                await hub.stop_managed(unique_id)
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            break
