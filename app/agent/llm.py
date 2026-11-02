from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.config import LLM_MAX_CONCURRENCY, LLM_MAX_RPS


class LLMError(Exception):
    """Lỗi từ provider LLM, không chứa API key."""

    def __init__(self, message: str, status: int | None = None, body: str = "") -> None:
        self.status = status
        self.body = body[:500]
        detail = f"LLM HTTP {status}" if status is not None else f"Lỗi LLM: {message}"
        if self.body:
            detail += f": {self.body}"
        super().__init__(detail)


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_tokens: int,
        temperature: float,
        *,
        max_rps: float = LLM_MAX_RPS,
        max_concurrency: int = LLM_MAX_CONCURRENCY,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._max_rps = max(0.0, max_rps)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._rate_lock = asyncio.Lock()
        self._request_times: deque[float] = deque()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def _payload(self, messages: Sequence[Mapping[str, str]], stream: bool) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": stream,
        }

    async def _rate_limit(self) -> None:
        if self._max_rps <= 0:
            return
        while True:
            async with self._rate_lock:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] >= 1.0:
                    self._request_times.popleft()
                limit = max(1, int(self._max_rps))
                if len(self._request_times) < limit:
                    self._request_times.append(now)
                    return
                wait = 1.0 - (now - self._request_times[0])
            await asyncio.sleep(max(0.001, wait))

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            return min(2.0, max(0.0, float(response.headers.get("Retry-After", "0"))))
        except ValueError:
            return 0.0

    @asynccontextmanager
    async def _response(
        self, messages: Sequence[Mapping[str, str]], stream: bool
    ) -> AsyncIterator[httpx.Response]:
        async with self._semaphore:
            response: httpx.Response | None = None
            try:
                for attempt in range(2):
                    await self._rate_limit()
                    request = self._client.build_request(
                        "POST",
                        f"{self.base_url}/chat/completions",
                        json=self._payload(messages, stream),
                    )
                    try:
                        response = await self._client.send(request, stream=stream)
                    except httpx.HTTPError as exc:
                        raise LLMError(str(exc)) from exc
                    if response.status_code != 429 or attempt == 1:
                        break
                    retry_after = self._retry_after(response)
                    await response.aclose()
                    response = None
                    if retry_after:
                        await asyncio.sleep(retry_after)
                assert response is not None
                if response.is_error:
                    body = (await response.aread()).decode(errors="replace")[:500]
                    raise LLMError("provider error", response.status_code, body)
                yield response
            finally:
                if response is not None:
                    await response.aclose()

    async def chat(self, messages: Sequence[Mapping[str, str]]) -> str:
        async with self._response(messages, False) as response:
            try:
                data = response.json()
                return str(data["choices"][0]["message"]["content"] or "").strip()
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("response JSON không hợp lệ") from exc

    async def stream_chat(
        self, messages: Sequence[Mapping[str, str]]
    ) -> AsyncIterator[str]:
        async with self._response(messages, True) as response:
            try:
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    event = json.loads(data)
                    delta = event.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield str(delta)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("SSE không hợp lệ") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
