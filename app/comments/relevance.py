from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx

from app.comments.filters import normalize_text
from app.config import (
    AGENT_PROMPT_FILE,
    COMMENT_CONTEXT_FILE,
    COMMENT_EMBED_KEEP_ALIVE,
    COMMENT_EMBED_MODEL,
    COMMENT_EMBED_NUM_GPU,
    COMMENT_EMBED_PORT,
    COMMENT_EMBED_TIMEOUT_SEC,
    COMMENT_EMBED_URL,
    COMMENT_RELEVANCE,
    COMMENT_SIM_CEIL,
    COMMENT_SIM_FLOOR,
)
from app.gpu import GpuNode
from app.ssh_tunnel import SshTunnel

logger = logging.getLogger(__name__)
EmbedCallable = Callable[[list[str]], Awaitable[list[Sequence[float]]]]
_PREFIX_RE = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)")
_MIN_PASSAGE_WORDS = 4


class OllamaEmbedder:
    def __init__(
        self,
        node: GpuNode,
        *,
        port: int = COMMENT_EMBED_PORT,
        url: str = COMMENT_EMBED_URL,
        model: str = COMMENT_EMBED_MODEL,
        keep_alive: str = COMMENT_EMBED_KEEP_ALIVE,
        num_gpu: int | None = COMMENT_EMBED_NUM_GPU,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.node = node
        self.model = model
        self.keep_alive = keep_alive
        self.num_gpu = num_gpu
        self._direct_url = url.strip().rstrip("/")
        self._tunnel = (
            SshTunnel(node, remote_port=port) if not self._direct_url and node.use_ssh else None
        )
        initial = self._direct_url or node.http_url(port)
        self.base_url = self._api_base(initial)
        self._transport = transport
        self._client = self._new_client(self.base_url)

    @staticmethod
    def _api_base(url: str) -> str:
        # Dùng API gốc /api/embed (không phải /v1/embeddings) vì chỉ API gốc nhận options.num_gpu
        url = url.rstrip("/")
        for suffix in ("/api/embed", "/api", "/v1"):
            if url.endswith(suffix):
                return url[: -len(suffix)]
        return url

    def _new_client(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=self._transport, timeout=None)

    async def _prepare_client(self) -> None:
        if self._tunnel is None:
            return
        await self._tunnel.ensure()
        new_url = self._api_base(self._tunnel.base_url)
        if new_url != self.base_url:
            old_client = self._client
            self.base_url = new_url
            self._client = self._new_client(new_url)
            await old_client.aclose()

    async def embed(self, texts: list[str]) -> list[Sequence[float]]:
        await self._prepare_client()
        payload: dict[str, Any] = {"model": self.model, "input": texts, "keep_alive": self.keep_alive}
        if self.num_gpu is not None:
            # num_gpu=0: chạy trên CPU máy GPU, nhường VRAM cho VoiceStudio (card 6 GB không đủ cho cả hai)
            payload["options"] = {"num_gpu": self.num_gpu}
        response = await self._client.post("/api/embed", json=payload)
        response.raise_for_status()
        raw = response.json()
        vectors = raw.get("embeddings") if isinstance(raw, dict) else None
        if not isinstance(vectors, list):
            raise ValueError("Ollama trả response embedding không hợp lệ.")
        if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
            raise ValueError("Ollama trả thiếu embedding.")
        return vectors

    async def __call__(self, texts: list[str]) -> list[Sequence[float]]:
        return await self.embed(texts)

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._tunnel is not None:
            await self._tunnel.close()


def _unit(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    length = math.sqrt(sum(value * value for value in values))
    if length <= 0:
        raise ValueError("Embedding có độ dài 0.")
    return tuple(value / length for value in values)


def core_from_similarity(
    similarity: float,
    floor: float = COMMENT_SIM_FLOOR,
    ceil: float = COMMENT_SIM_CEIL,
) -> float:
    if ceil <= floor:
        return 1.0 if similarity >= ceil else 0.0
    return max(0.0, min(1.0, (similarity - floor) / (ceil - floor)))


class RelevanceScorer:
    def __init__(
        self,
        embedder: EmbedCallable | Any | None,
        *,
        context_file: str | None = None,
        passages: Sequence[str] | None = None,
        enabled: bool = COMMENT_RELEVANCE,
        model: str = COMMENT_EMBED_MODEL,
        timeout_sec: float = COMMENT_EMBED_TIMEOUT_SEC,
        sim_floor: float = COMMENT_SIM_FLOOR,
        sim_ceil: float = COMMENT_SIM_CEIL,
        failure_threshold: int = 3,
        breaker_sec: float = 30.0,
        retry_sec: float = 30.0,
        cache_size: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.embedder = embedder
        self.model = model
        self.timeout_sec = max(0.01, timeout_sec)
        self.sim_floor = sim_floor
        self.sim_ceil = sim_ceil
        self.failure_threshold = max(1, failure_threshold)
        self.breaker_sec = max(0.0, breaker_sec)
        self.retry_sec = max(0.01, retry_sec)
        self.cache_size = max(1, cache_size)
        self._clock = clock
        self.context_file = (context_file if context_file is not None else COMMENT_CONTEXT_FILE) or AGENT_PROMPT_FILE
        self._passages = list(passages) if passages is not None else self._read_passages()
        self.enabled = bool(enabled and embedder is not None and self._passages)
        self._ready = False
        self._vectors: list[tuple[float, ...]] = []
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._failures = 0
        self._breaker_until = 0.0
        self._counts = {"similarity": 0, "heuristic": 0, "timeout": 0, "error": 0}
        self._embed_ms_total = 0.0
        self._embed_calls = 0
        self._rewarm_task: asyncio.Task[None] | None = None
        if not self.enabled:
            logger.info(
                "Comment relevance tắt: context file rỗng/thiếu, embedder tắt hoặc COMMENT_RELEVANCE=0."
            )

    @property
    def ready(self) -> bool:
        return self.enabled and self._ready

    @property
    def passages(self) -> int:
        return len(self._passages)

    def _read_passages(self) -> list[str]:
        if not self.context_file:
            return []
        try:
            lines = Path(self.context_file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            logger.warning("Không đọc được comment context: %s", self.context_file)
            return []
        passages = []
        for line in lines:
            line = line.strip()
            # Tiêu đề và dòng quá ngắn ("Elen live") quá chung chung, comment nào cũng giống → bỏ
            if line.startswith("#"):
                continue
            cleaned = _PREFIX_RE.sub("", line).strip()
            if len(cleaned.split()) >= _MIN_PASSAGE_WORDS:
                passages.append(cleaned)
        return passages

    async def _embed(self, texts: list[str]) -> list[Sequence[float]]:
        if hasattr(self.embedder, "embed"):
            return await self.embedder.embed(texts)
        return await self.embedder(texts)

    async def warmup(self) -> None:
        if not self.enabled:
            return
        while not self._ready:
            now = self._clock()
            if now < self._breaker_until:
                await asyncio.sleep(min(self.retry_sec, self._breaker_until - now))
                continue
            started = time.perf_counter()
            try:
                vectors = await asyncio.wait_for(
                    self._embed(self._passages), timeout=max(30.0, self.timeout_sec)
                )
                if len(vectors) != len(self._passages):
                    raise ValueError("Số embedding context không khớp số passage.")
                self._vectors = [_unit(vector) for vector in vectors]
                if len({len(vector) for vector in self._vectors}) != 1:
                    raise ValueError("Kích thước embedding giữa các passage không khớp.")
                self._ready = True
                self._success(started)
                logger.info("Comment relevance sẵn sàng: %s passages, model=%s.", len(vectors), self.model)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self._failure("timeout")
            except Exception:
                self._failure("error")
                logger.warning("Warmup comment embedding thất bại; sẽ thử lại.", exc_info=True)
            if not self._ready:
                await asyncio.sleep(self.retry_sec)

    async def similarity(self, text: str) -> float | None:
        if not self.ready or self._clock() < self._breaker_until:
            return None
        key = normalize_text(text)
        if key in self._cache:
            result = self._cache.pop(key)
            self._cache[key] = result
            self._counts["similarity"] += 1
            return result
        started = time.perf_counter()
        try:
            vectors = await asyncio.wait_for(self._embed([text]), timeout=self.timeout_sec)
            if len(vectors) != 1:
                raise ValueError("Ollama không trả đúng một embedding.")
            vector = _unit(vectors[0])
            if self._vectors and len(vector) != len(self._vectors[0]):
                raise ValueError("Kích thước embedding comment/context không khớp.")
            result = max(sum(a * b for a, b in zip(vector, passage)) for passage in self._vectors)
            self._success(started)
            self._counts["similarity"] += 1
            self._cache[key] = result
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._failure("timeout")
            self._schedule_rewarm()
        except Exception:
            self._failure("error")
            logger.debug("Embedding comment thất bại.", exc_info=True)
        return None

    def _schedule_rewarm(self) -> None:
        # Ollama nhả model sau keep_alive; lần nạp lại mất vài giây, vượt timeout của từng comment.
        # Gửi một request nền với timeout dài để nạp model, các comment sau lại nhanh.
        if self._rewarm_task is not None and not self._rewarm_task.done():
            return
        self._rewarm_task = asyncio.create_task(self._rewarm(), name="comment-embed-rewarm")

    async def _rewarm(self) -> None:
        try:
            await asyncio.wait_for(self._embed(["ping"]), timeout=max(30.0, self.timeout_sec))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Nạp lại model embedding thất bại.", exc_info=True)
            return
        self._failures = 0
        self._breaker_until = 0.0

    def count_heuristic(self) -> None:
        self._counts["heuristic"] += 1

    def _success(self, started: float) -> None:
        self._embed_ms_total += (time.perf_counter() - started) * 1000
        self._embed_calls += 1
        self._failures = 0

    def _failure(self, kind: str) -> None:
        self._counts[kind] += 1
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._breaker_until = self._clock() + self.breaker_sec
            self._failures = 0

    def status(self) -> dict[str, Any]:
        status = {
            "enabled": self.enabled,
            "ready": self.ready,
            "model": self.model,
            "passages": len(self._passages),
            "context_file": self.context_file or None,
            "counts": dict(self._counts),
            "avg_embed_ms": round(
                self._embed_ms_total / self._embed_calls, 2
            ) if self._embed_calls else 0.0,
        }
        if self.embedder is not None and hasattr(self.embedder, "base_url"):
            url = httpx.URL(self.embedder.base_url)
            status["url"] = str(url.copy_with(username=None, password=None, query=None))
        return status

    async def aclose(self) -> None:
        if self._rewarm_task is not None and not self._rewarm_task.done():
            self._rewarm_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._rewarm_task
        if self.embedder is not None and hasattr(self.embedder, "aclose"):
            await self.embedder.aclose()
