from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from TikTokLive.client.web.web_settings import WebDefaults

from app.api import comments as comments_api
from app.api import live, meta, queue, tts
from app.comments import CommentBuffer
from app.config import SIGN_API_KEY, TIKTOK_LIVE_RETRY_SEC, TIKTOK_USERNAME
from app.gpu import GpuNode
from app.live.hub import LiveStreamHub
from app.live.watcher import auto_watch
from app.tts import create_tts_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    WebDefaults.tiktok_sign_api_key = SIGN_API_KEY or None
    logger.info("Euler sign API key: %s", "on" if SIGN_API_KEY else "off")
    comments = CommentBuffer()
    hub = LiveStreamHub(on_comment=comments.add)
    engine = None
    watch_task: asyncio.Task[None] | None = None
    try:
        node = GpuNode.from_env()
        engine = create_tts_engine(node)
        if engine is not None:
            await engine.start()

        app.state.comments = comments
        app.state.hub = hub
        app.state.tts = engine
        logger.info("Máy GPU đang dùng: %s", node.info())

        if TIKTOK_USERNAME:
            watch_task = asyncio.create_task(
                auto_watch(hub, TIKTOK_USERNAME, TIKTOK_LIVE_RETRY_SEC)
            )
            logger.info(
                "Auto-watch khởi động cho @%s (retry=%ss)",
                TIKTOK_USERNAME,
                TIKTOK_LIVE_RETRY_SEC,
            )
        else:
            logger.warning("TIKTOK_USERNAME chưa được đặt — service không tự động lấy comment.")

        yield
    finally:
        if watch_task is not None:
            if not watch_task.done():
                watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass
        try:
            await hub.shutdown_all()
        except Exception:
            logger.exception("Lỗi khi đóng các phiên TikTok Live.")
        try:
            if engine is not None:
                await engine.aclose()
        except Exception:
            logger.exception("Lỗi khi đóng TTS engine.")


app = FastAPI(
    title="TikTok Live Comment Service",
    description="Thu thập và chọn comment TikTok Live mới, đáng trả lời trong bộ nhớ.",
    lifespan=lifespan,
)
app.include_router(meta.router)
app.include_router(queue.router)
app.include_router(comments_api.router)
app.include_router(live.router)
app.include_router(tts.router)
