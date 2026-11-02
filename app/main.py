from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from TikTokLive.client.web.web_settings import WebDefaults

from app.api import agent as agent_api
from app.api import comments as comments_api
from app.api import live, meta, queue, tts
from app.agent import OpenAICompatLLM, LiveAgent, default_system_prompt
from app.agent.loop import auto_reply_loop
from app.audio.player import LocalAudioPlayer, create_player
from app.comments import CommentBuffer
from app.config import (
    AGENT_AUTO, AGENT_AUTO_SPEAK, AGENT_ENABLED, AGENT_HISTORY_TURNS,
    AGENT_LOCAL_PLAYBACK,
    AGENT_PLAYED_ACK_GRACE_SEC, AGENT_PROMPT_FILE, AGENT_REPLY_BUFFER,
    AGENT_REPLY_GAP_SEC,
    AGENT_SKIP_WHEN_NO_LISTENER, AGENT_SYSTEM_PROMPT, LLM_API_KEY,
    LLM_BASE_URL, LLM_MAX_TOKENS, LLM_MODEL, LLM_TEMPERATURE,
    LLM_TIMEOUT_SEC, SIGN_API_KEY, TIKTOK_LIVE_RETRY_SEC, TIKTOK_USERNAME,
    AUDIO_PLAYER, AUDIO_PLAYER_EXPLICIT,
)
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
    agent_task: asyncio.Task[None] | None = None
    llm: OpenAICompatLLM | None = None
    audio_player: LocalAudioPlayer | None = None
    try:
        node = GpuNode.from_env()
        engine = create_tts_engine(node)
        if engine is not None:
            await engine.start()

        app.state.comments = comments
        app.state.hub = hub
        app.state.tts = engine
        app.state.agent = None
        app.state.audio_player = None

        need_player = (
            AGENT_LOCAL_PLAYBACK and AGENT_AUTO and AGENT_AUTO_SPEAK
            and AGENT_ENABLED and bool(LLM_API_KEY) and engine is not None
        ) or (AUDIO_PLAYER_EXPLICIT and AUDIO_PLAYER != "none")
        if need_player and AUDIO_PLAYER != "none":
            candidate: LocalAudioPlayer | None = None
            try:
                candidate = create_player()
                if candidate is not None:
                    await candidate.start()
                    audio_player = candidate
                    app.state.audio_player = candidate
                    info = candidate.info()
                    logger.info(
                        "Local audio: backend=%s, device=%s, sample_rate=%s Hz.",
                        info["backend"], info["device"], info.get("sample_rate", 24000),
                    )
                else:
                    logger.warning("Local audio không có backend khả dụng.")
            except Exception:
                audio_player = None
                logger.exception("Không khởi động được local audio; service vẫn tiếp tục.")
                if candidate is not None:
                    try:
                        await candidate.aclose()
                    except Exception:
                        logger.exception("Lỗi khi dọn local audio khởi động thất bại.")

        if AGENT_ENABLED and LLM_API_KEY:
            prompt = AGENT_SYSTEM_PROMPT
            if not prompt and AGENT_PROMPT_FILE:
                with open(AGENT_PROMPT_FILE, encoding="utf-8") as prompt_file:
                    prompt = prompt_file.read().strip()
            llm = OpenAICompatLLM(
                LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_TIMEOUT_SEC,
                LLM_MAX_TOKENS, LLM_TEMPERATURE,
            )
            agent = LiveAgent(
                llm, prompt or default_system_prompt(TIKTOK_USERNAME),
                AGENT_HISTORY_TURNS, AGENT_REPLY_BUFFER,
            )
            app.state.agent = agent
            if AGENT_AUTO:
                auto_speak = AGENT_AUTO_SPEAK and engine is not None
                if AGENT_AUTO_SPEAK and engine is None:
                    logger.warning(
                        "AGENT_AUTO_SPEAK đã bật nhưng TTS đang tắt; dùng auto mode chỉ text."
                    )
                agent.auto_speak = auto_speak
                agent_task = asyncio.create_task(auto_reply_loop(
                    comments,
                    agent,
                    engine,
                    auto_speak=auto_speak,
                    skip_when_no_listener=AGENT_SKIP_WHEN_NO_LISTENER,
                    reply_gap_sec=AGENT_REPLY_GAP_SEC,
                    played_ack_grace_sec=AGENT_PLAYED_ACK_GRACE_SEC,
                    player=(
                        audio_player
                        if AGENT_LOCAL_PLAYBACK and auto_speak else None
                    ),
                ))
            logger.info(
                "Agent LLM đã bật (model=%s, auto=%s, auto_speak=%s).",
                LLM_MODEL, AGENT_AUTO, agent.auto_speak,
            )
        else:
            logger.info("Agent LLM đang tắt.")
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
        if agent_task is not None:
            agent_task.cancel()
            try:
                await agent_task
            except asyncio.CancelledError:
                pass
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
        try:
            if audio_player is not None:
                await audio_player.aclose()
        except Exception:
            logger.exception("Lỗi khi đóng local audio player.")
        try:
            if llm is not None:
                await llm.aclose()
        except Exception:
            logger.exception("Lỗi khi đóng LLM client.")


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
app.include_router(agent_api.router)
