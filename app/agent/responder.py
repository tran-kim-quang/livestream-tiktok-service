from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.agent.llm import OpenAICompatLLM
from app.agent.names import speakable_name
from app.agent.tts_text import sanitize_for_tts
from app.config import (
    AGENT_CONTEXT_SIM_THRESHOLD, AGENT_GREETING, AGENT_GREETING_TEMPLATES, AGENT_NAME,
    AGENT_TTS_MAX_TAGS,
)


AgentFrame = dict[str, Any] | bytes


@dataclass(eq=False)
class AgentSubscriber:
    queue: asyncio.Queue[AgentFrame]
    close: Callable[[], Awaitable[None] | None] | None = None
    is_player: bool = False
    player_info: dict[str, Any] | None = None


_NO_GREETING_INSTRUCTION = (
    "Lời chào và tên người xem đã được nói trước — KHÔNG chào, KHÔNG gọi lại tên "
    "người xem ở đầu câu, đi thẳng vào nội dung trả lời."
)


def default_system_prompt(
    username: str,
    greeting_enabled: bool = AGENT_GREETING,
    tts_tags: bool = False,
) -> str:
    streamer = username or "streamer"
    addressing = (
        f"{_NO_GREETING_INSTRUCTION} "
        if greeting_enabled
        else "Gọi tên người xem khi hợp lý. "
    )
    tts_format = (
        "Không dùng emoji, markdown hay ký hiệu ngoài các tag biểu cảm được hướng dẫn vì câu trả lời "
        "sẽ được đọc bằng TTS. "
        if tts_tags
        else "Không dùng emoji, markdown hay ký hiệu vì câu trả lời sẽ được đọc bằng TTS. "
    )
    return (
        f"Bạn là {AGENT_NAME}, trợ lý cho streamer {streamer} trong buổi live TikTok. "
        "Hãy trả lời comment người xem bằng tiếng Việt tự nhiên, ngắn gọn, "
        f"chỉ 1–2 câu và tối đa khoảng 40 từ. {addressing}"
        f"{tts_format}"
        "Không bịa thông tin. Comment thô tục, chửi bậy hoặc khiêu khích vẫn phải được trả lời: "
        "lịch sự, bình tĩnh, hài hước nhẹ nhàng, không lặp lại từ thô tục, không đáp trả "
        "gay gắt, không lên lớp dài dòng; có thể lái sang nội dung buổi live. Không bao giờ trả về rỗng."
    )


class LiveAgent:
    def __init__(
        self,
        llm: OpenAICompatLLM,
        system_prompt: str,
        history_turns: int,
        reply_buffer: int = 50,
        *,
        greeting_enabled: bool = AGENT_GREETING,
        greeting_templates: tuple[str, ...] = AGENT_GREETING_TEMPLATES,
        rng: Any = random,
        tts_max_tags: int = AGENT_TTS_MAX_TAGS,
        live_context: str = "",
        context_threshold: float = AGENT_CONTEXT_SIM_THRESHOLD,
        relevance: Any = None,
    ) -> None:
        self.llm = llm
        self.greeting_enabled = greeting_enabled
        if greeting_enabled and _NO_GREETING_INSTRUCTION not in system_prompt:
            system_prompt = f"{system_prompt.rstrip()} {_NO_GREETING_INSTRUCTION}"
        elif not greeting_enabled and _NO_GREETING_INSTRUCTION in system_prompt:
            system_prompt = system_prompt.replace(
                _NO_GREETING_INSTRUCTION, "Gọi tên người xem khi hợp lý."
            )
        self.system_prompt = system_prompt
        self.greeting_templates = greeting_templates or ("{name} ơi,",)
        self._rng = rng
        self.tts_max_tags = max(0, tts_max_tags)
        self.live_context = live_context.strip()
        self.context_threshold = context_threshold
        self._relevance = relevance
        self._last_greeting_template: str | None = None
        self.history_turns = max(0, history_turns)
        self._history: deque[tuple[str, str]] = deque(maxlen=self.history_turns or None)
        self._replies: deque[dict[str, Any]] = deque(maxlen=max(1, reply_buffer))
        self._replied = 0
        self._errors = 0
        self._latency_total_ms = 0
        self._lock = asyncio.Lock()
        self.subscribers: set[AgentSubscriber | asyncio.Queue[AgentFrame]] = set()
        self.auto_speak = False
        self.state = "idle"
        self.last_latencies: dict[str, int | None] | None = None
        self._listeners_changed = asyncio.Event()
        self._played_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def listener_count(self) -> int:
        return len(self.subscribers)

    @property
    def player_count(self) -> int:
        return sum(
            isinstance(subscriber, AgentSubscriber) and subscriber.is_player
            for subscriber in self.subscribers
        )

    def subscribe(
        self,
        queue: asyncio.Queue[AgentFrame],
        close: Callable[[], Awaitable[None] | None] | None = None,
    ) -> AgentSubscriber:
        subscriber = AgentSubscriber(queue, close)
        self.subscribers.add(subscriber)
        self._listeners_changed.set()
        return subscriber

    def unsubscribe(self, subscriber: AgentSubscriber) -> None:
        if subscriber in self.subscribers:
            self.subscribers.discard(subscriber)
            self._listeners_changed.set()

    def mark_player(self, subscriber: AgentSubscriber, info: Mapping[str, Any]) -> None:
        """Đánh dấu WebSocket là audio bridge; kết nối này vẫn là một listener."""
        if subscriber in self.subscribers:
            subscriber.is_player = True
            subscriber.player_info = dict(info)
            self._listeners_changed.set()

    def expect_played(self, reply_id: str) -> asyncio.Future[dict[str, Any]]:
        """Đăng ký trước khi gửi reply_end để không bỏ lỡ ACK rất nhanh."""
        previous = self._played_waiters.pop(reply_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._played_waiters[reply_id] = future
        return future

    def acknowledge_played(self, reply_id: str, payload: Mapping[str, Any]) -> bool:
        future = self._played_waiters.get(reply_id)
        if future is None or future.done():
            return False
        ack = dict(payload)
        first_audio_ms = ack.get("first_audio_played_ms")
        if isinstance(first_audio_ms, (int, float)) and self.last_latencies is not None:
            value = max(0, round(first_audio_ms))
            # Giữ key cũ trong latency_ms và thêm tên đầy đủ của protocol bridge.
            self.last_latencies["first_audio_played"] = value
            self.last_latencies["first_audio_played_ms"] = value
        future.set_result(ack)
        return True

    async def wait_for_played(
        self,
        reply_id: str,
        future: asyncio.Future[dict[str, Any]],
        timeout: float,
    ) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(future, timeout=max(0.0, timeout))
        finally:
            if self._played_waiters.get(reply_id) is future:
                self._played_waiters.pop(reply_id, None)

    async def wait_until_no_listeners(self) -> None:
        """Thức dậy ngay khi listener cuối rời đi; dùng để không pop nhầm comment."""
        while self.listener_count:
            self._listeners_changed.clear()
            if not self.listener_count:
                break
            await self._listeners_changed.wait()

    @staticmethod
    def _user_message(comment: Mapping[str, Any]) -> str:
        user = comment.get("user") or {}
        name = speakable_name(user if isinstance(user, Mapping) else {})
        return f"Người xem: {name}\nComment: {str(comment.get('comment') or '').strip()}"

    @staticmethod
    def display_name(comment: Mapping[str, Any]) -> str:
        user = comment.get("user") or {}
        return speakable_name(user if isinstance(user, Mapping) else {})

    def make_greeting(self, comment: Mapping[str, Any]) -> str:
        if not self.greeting_enabled:
            return ""
        choices = tuple(
            template
            for template in self.greeting_templates
            if template != self._last_greeting_template
        ) or self.greeting_templates
        template = self._rng.choice(choices)
        self._last_greeting_template = template
        try:
            return template.format(name=self.display_name(comment)).strip()
        except (KeyError, ValueError):
            return f"{self.display_name(comment)} ơi,"

    async def _similarity(self, comment: Mapping[str, Any]) -> float | None:
        """Similarity với live context: lấy từ buffer, hoặc embed ngay (comment gọi qua API)."""
        value = comment.get("similarity")
        if value is None and self._relevance is not None and self.live_context:
            text = str(comment.get("comment") or "").strip()
            if text:
                value = await self._relevance.similarity(text)
        return float(value) if value is not None else None

    def context_mode(self, similarity: float | None) -> str:
        if not self.live_context:
            return "none"
        if similarity is None:
            return "unknown"
        return "context" if similarity >= self.context_threshold else "chat"

    def _context_note(self, mode: str) -> str:
        context = f"Thông tin buổi live:\n{self.live_context}"
        if mode == "context":
            return (
                f"{context}\n\nComment này hỏi về buổi live: chỉ trả lời dựa trên thông tin trên; "
                "nếu thông tin trên không có thì nói thật là mình không biết."
            )
        if mode == "unknown":
            return (
                f"{context}\n\nNếu comment hỏi về buổi live hay về streamer thì chỉ trả lời dựa trên "
                "thông tin trên, không có thì nói là mình không biết; còn lại thì trò chuyện bình thường."
            )
        if mode == "chat":
            return "Đây là chat bình thường: trò chuyện tự nhiên, thân thiện với người xem."
        return ""

    def _messages(
        self, comment: Mapping[str, Any], similarity: float | None = None
    ) -> tuple[list[dict[str, str]], str]:
        current = self._user_message(comment)
        note = self._context_note(self.context_mode(similarity))
        messages = [{"role": "system", "content": self.system_prompt}]
        for user_message, reply in self._history:
            messages.extend((
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": reply},
            ))
        # history chỉ lưu comment gốc, không lưu context để prompt không phình
        messages.append({"role": "user", "content": f"{current}\n\n{note}" if note else current})
        return messages, current

    async def _finish(
        self,
        comment: Mapping[str, Any],
        user_message: str,
        llm_reply: str,
        greeting: str,
        started: float,
        similarity: float | None = None,
    ) -> dict[str, Any]:
        latency_ms = round((time.perf_counter() - started) * 1000)
        llm_reply = sanitize_for_tts(llm_reply, max_tags=self.tts_max_tags)
        visible_reply = f"{greeting} {llm_reply}".strip() if llm_reply else ""
        record = {
            "comment": dict(comment),
            "display_name": self.display_name(comment),
            "reply": visible_reply,
            "similarity": similarity,
            "context_mode": self.context_mode(similarity),
            "latency_ms": latency_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            if self.history_turns:
                self._history.append((user_message, llm_reply))
            self._replies.append(record)
            self._replied += 1
            self._latency_total_ms += latency_ms
        return record

    async def reply(self, comment_payload: Mapping[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        similarity = await self._similarity(comment_payload)
        messages, user_message = self._messages(comment_payload, similarity)
        greeting = self.make_greeting(comment_payload)
        try:
            reply = await self.llm.chat(messages)
            return await self._finish(
                comment_payload, user_message, reply, greeting, started, similarity
            )
        except Exception:
            self._errors += 1
            raise

    async def stream_reply(
        self,
        comment_payload: Mapping[str, Any],
        *,
        greeting: str | None = None,
        reply_transform: Callable[[str], str] | None = None,
    ) -> AsyncIterator[str]:
        started = time.perf_counter()
        similarity = await self._similarity(comment_payload)
        messages, user_message = self._messages(comment_payload, similarity)
        selected_greeting = self.make_greeting(comment_payload) if greeting is None else greeting
        parts: list[str] = []
        try:
            async for delta in self.llm.stream_chat(messages):
                parts.append(delta)
                yield delta
        except Exception:
            self._errors += 1
            raise
        else:
            reply = "".join(parts)
            if reply_transform is not None:
                reply = reply_transform(reply)
            await self._finish(
                comment_payload, user_message, reply, selected_greeting, started, similarity
            )

    def recent_replies(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(reversed(self._replies))[:limit]

    def stats(self) -> dict[str, int | float]:
        avg = self._latency_total_ms / self._replied if self._replied else 0.0
        return {"replied": self._replied, "errors": self._errors, "avg_latency_ms": round(avg, 1)}

    async def broadcast_frame(self, frame: AgentFrame) -> None:
        """Phát nguyên frame; client chậm bị đóng thay vì mất chunk giữa reply."""
        dropped: list[AgentSubscriber] = []
        for subscriber in tuple(self.subscribers):
            queue = subscriber.queue if isinstance(subscriber, AgentSubscriber) else subscriber
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                self.subscribers.discard(subscriber)
                self._listeners_changed.set()
                if isinstance(subscriber, AgentSubscriber):
                    dropped.append(subscriber)
        for subscriber in dropped:
            if subscriber.close is None:
                continue
            try:
                result = subscriber.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Socket có thể vừa tự đóng đúng lúc queue đầy.
                pass

    async def broadcast_players(self, frame: AgentFrame) -> int:
        """Gửi control frame chỉ tới bridge player, trả về số player đã nhận."""
        sent = 0
        dropped: list[AgentSubscriber] = []
        for subscriber in tuple(self.subscribers):
            if not isinstance(subscriber, AgentSubscriber) or not subscriber.is_player:
                continue
            try:
                subscriber.queue.put_nowait(frame)
                sent += 1
            except asyncio.QueueFull:
                self.subscribers.discard(subscriber)
                self._listeners_changed.set()
                dropped.append(subscriber)
        for subscriber in dropped:
            if subscriber.close is None:
                continue
            try:
                result = subscriber.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
        return sent

    async def broadcast(self, record: Mapping[str, Any]) -> None:
        await self.broadcast_frame({"type": "reply", **dict(record)})
