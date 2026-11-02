from __future__ import annotations

import asyncio
import json
import math
import unittest
from types import SimpleNamespace
from typing import Any, Sequence

import httpx

from app.comments.audience import AudienceTracker
from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter
from app.comments.relevance import OllamaEmbedder, RelevanceScorer
from app.gpu import GpuNode
from app.live.hub import _comment_to_dict, _follow_to_dict, _gift_to_dict


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def payload(text: str, user: str = "u1", **extra: Any) -> dict[str, Any]:
    return {
        "comment": text,
        "user": {"id": user, "unique_id": user, "nickname": user},
        "streamer_unique_id": "host",
        **extra,
    }


class FakeEmbedder:
    def __init__(self) -> None:
        self.fail = False
        self.slow = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, texts: list[str]) -> list[Sequence[float]]:
        if self.fail:
            raise RuntimeError("embed failed")
        if self.slow:
            self.entered.set()
            await self.release.wait()
        return [[1.0, 0.0] if "game" in text.lower() else [0.0, 1.0] for text in texts]


def make_filter(clock: FakeClock | None = None) -> CommentFilter:
    return CommentFilter(clock=clock or FakeClock(), blocklist=(), dedup_window_sec=0)


class RelevanceBufferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.embedder = FakeEmbedder()
        self.relevance = RelevanceScorer(
            self.embedder, passages=["game schedule"], timeout_sec=0.03,
            retry_sec=0.01, clock=self.clock,
        )
        await self.relevance.warmup()

    def buffer(self, **kwargs: Any) -> CommentBuffer:
        return CommentBuffer(
            questions_only=False,
            comment_filter=make_filter(self.clock), relevance=self.relevance,
            clock=self.clock, merge_window_sec=0, user_cooldown_sec=0, **kwargs,
        )

    async def test_similarity_outranks_long_question(self) -> None:
        buffer = self.buffer()
        await buffer.add(payload("game nay choi luc nao", "on"))
        await buffer.add(payload("mot cau hoi rat dai hoan toan lac de phai khong?", "off"))
        selected = await buffer.next()
        self.assertEqual(selected["user"]["id"], "on")
        self.assertEqual(selected["score_detail"]["core_source"], "similarity")

    async def test_fallback_not_ready_error_and_timeout(self) -> None:
        cold = RelevanceScorer(self.embedder, passages=["game schedule"], timeout_sec=0.01)
        cold_buffer = CommentBuffer(
            questions_only=False,
            comment_filter=make_filter(), relevance=cold, merge_window_sec=0,
            user_cooldown_sec=0, scorer=lambda _: 5.75,
        )
        await cold_buffer.add(payload("cold comment"))
        self.assertEqual((await cold_buffer.latest())[0]["score_detail"]["core"], 0.5)

        self.embedder.fail = True
        error_buffer = self.buffer(scorer=lambda _: 2.3)
        await error_buffer.add(payload("embed error"))
        self.assertEqual((await error_buffer.latest())[0]["score_detail"]["core_source"], "heuristic")
        self.embedder.fail = False

        self.embedder.slow = True
        timeout_buffer = self.buffer(scorer=lambda _: 1.15)
        await timeout_buffer.add(payload("slow comment"))
        detail = (await timeout_buffer.latest())[0]["score_detail"]
        self.assertAlmostEqual(detail["core"], 0.1)
        self.assertGreaterEqual(self.relevance.status()["counts"]["timeout"], 1)

    async def test_embedding_does_not_hold_buffer_lock(self) -> None:
        buffer = self.buffer()
        await buffer.add(payload("game ready", "ready"))
        self.embedder.slow = True
        task = asyncio.create_task(buffer.add(payload("slow off topic", "slow")))
        await asyncio.wait_for(self.embedder.entered.wait(), 0.1)
        selected = await asyncio.wait_for(buffer.next(), 0.05)
        stats = await asyncio.wait_for(buffer.stats(), 0.05)
        self.assertEqual(selected["user"]["id"], "ready")
        self.assertEqual(stats["served"], 1)
        self.embedder.release.set()
        await task

    async def test_merged_core_is_max_of_parts(self) -> None:
        buffer = CommentBuffer(
            questions_only=False,
            comment_filter=make_filter(self.clock), relevance=self.relevance,
            clock=self.clock, merge_window_sec=10, user_cooldown_sec=0,
        )
        await buffer.add(payload("off topic words", "u"))
        await buffer.add(payload("game question", "u"))
        merged = (await buffer.latest())[0]
        self.assertEqual(merged["core"], 1.0)
        self.assertEqual(merged["part_cores"], [0.0, 1.0])


class AudienceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tracker = AudienceTracker(clock=self.clock, gift_half_life_sec=10,
                                       gift_diamond_cap=100, priority_window_sec=20)

    def test_gift_log_cap_and_half_life(self) -> None:
        record = payload("hello", "u")
        self.tracker.record_gift(record["user"], 100)
        total, detail = self.tracker.bonus(record)
        self.assertAlmostEqual(detail["gift"], 4.0)
        self.clock.advance(10)
        _, decayed = self.tracker.bonus(record)
        expected = 4 * math.log1p(50) / math.log1p(100)
        self.assertAlmostEqual(decayed["gift"], expected)
        self.tracker.record_gift(record["user"], 1000)
        self.assertAlmostEqual(self.tracker.bonus(record)[1]["gift"], 4.0)
        self.assertGreater(total, 0)

    async def test_late_gift_changes_rank(self) -> None:
        buffer = CommentBuffer(
            questions_only=False,
            comment_filter=make_filter(self.clock), audience=self.tracker, clock=self.clock,
            merge_window_sec=0, user_cooldown_sec=0,
            scorer=lambda item: 1.1 if item["user"]["id"] == "plain" else 0.0,
        )
        await buffer.add(payload("plain comment", "plain"))
        await buffer.add(payload("gifter comment", "gifter"))
        self.tracker.record_gift({"id": "gifter"}, 100)
        self.assertEqual((await buffer.next())["user"]["id"], "gifter")

    async def test_bypass_window_forever_and_identity_not_bypass(self) -> None:
        buffer = CommentBuffer(
            questions_only=False,
            comment_filter=make_filter(self.clock), audience=self.tracker, clock=self.clock,
            merge_window_sec=0, user_cooldown_sec=100,
        )
        await buffer.add(payload("first answer", "u"))
        await buffer.next()
        await buffer.add(payload("second answer", "u"))
        self.tracker.record_follow({"id": "u"})
        self.assertIsNotNone(await buffer.next())
        self.clock.advance(21)
        await buffer.add(payload("third answer", "u"))
        self.assertIsNone(await buffer.next())

        forever = AudienceTracker(clock=self.clock, priority_window_sec=0)
        forever.record_gift({"id": "x"}, 1)
        self.clock.advance(10000)
        self.assertTrue(forever.bypass_cooldown("x"))

        identity = payload("subscriber words", "identity", identity={"subscriber": True})
        total, detail = self.tracker.bonus(identity)
        self.assertEqual(detail["subscriber"], 1.5)
        self.assertFalse(self.tracker.bypass_cooldown("identity"))
        self.assertGreater(total, 0)

    async def test_backlog_gift_and_follow_ignored(self) -> None:
        old = {"user": {"id": "u"}, "create_time": 1000,
               "session_started_at": 10_000, "diamonds": 50}
        await self.tracker.handle_gift(old)
        await self.tracker.handle_follow(old)
        self.assertEqual(self.tracker.status()["tracked_users"], 0)


class HubPayloadTest(unittest.TestCase):
    def event(self, **kwargs: Any) -> SimpleNamespace:
        defaults = {
            "user": SimpleNamespace(id="7", nickname="Lan", unique_id="lan"),
            "common": SimpleNamespace(msg_id=8, create_time=12_345),
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_identity_and_event_payloads(self) -> None:
        identity = SimpleNamespace(
            is_gift_giver_of_anchor=True, is_subscriber_of_anchor=True,
            is_follower_of_anchor=False, is_mutual_following_with_anchor=True,
            is_moderator_of_anchor=True,
        )
        event = self.event(comment="xin chao", user_identity=identity)
        comment = _comment_to_dict(SimpleNamespace(room_id=1, unique_id="host"), event)
        self.assertEqual(comment["identity"], {
            "gift_giver": True, "subscriber": True, "follower": False,
            "mutual": True, "moderator": True,
        })
        gift = self.event(
            gift=SimpleNamespace(diamond_count=5, name="Rose"), repeat_count=3,
            streaking=False,
        )
        self.assertEqual(_gift_to_dict(gift, 12_000)["diamonds"], 15)
        gift.streaking = True
        self.assertIsNone(_gift_to_dict(gift, 12_000))
        follow = _follow_to_dict(self.event(), 12_000)
        self.assertEqual(follow["type"], "follow")
        self.assertEqual(follow["session_started_at"], 12_000)


class OllamaAndCircuitTest(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_request_response_shape(self) -> None:
        captured: dict[str, Any] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})

        embedder = OllamaEmbedder(
            GpuNode.local(), model="model", keep_alive="30m", num_gpu=0,
            transport=httpx.MockTransport(handler),
        )
        vectors = await embedder.embed(["a", "b"])
        await embedder.aclose()
        self.assertEqual(captured["path"], "/api/embed")
        self.assertEqual(captured["body"], {
            "model": "model", "input": ["a", "b"], "keep_alive": "30m",
            "options": {"num_gpu": 0},
        })
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])

        gpu_auto = OllamaEmbedder(
            GpuNode.local(), url="http://gpu:11434/v1", model="m", num_gpu=None,
            transport=httpx.MockTransport(handler),
        )
        await gpu_auto.embed(["a", "b"])
        await gpu_auto.aclose()
        self.assertEqual(gpu_auto.base_url, "http://gpu:11434")
        self.assertNotIn("options", captured["body"])

    async def test_circuit_breaker_stops_calls_then_retries(self) -> None:
        clock = FakeClock()
        calls = 0
        failing = False

        async def embed(texts: list[str]) -> list[Sequence[float]]:
            nonlocal calls
            calls += 1
            if failing:
                raise RuntimeError("down")
            return [[1.0, 0.0] for _ in texts]

        scorer = RelevanceScorer(
            embed, passages=["context words"], failure_threshold=2,
            breaker_sec=30, clock=clock,
        )
        await scorer.warmup()
        failing = True
        self.assertIsNone(await scorer.similarity("one"))
        self.assertIsNone(await scorer.similarity("two"))
        after_failures = calls
        self.assertIsNone(await scorer.similarity("three"))
        self.assertEqual(calls, after_failures)
        clock.advance(31)
        self.assertIsNone(await scorer.similarity("retry"))
        self.assertEqual(calls, after_failures + 1)

    def test_context_file_skips_headings_and_short_lines(self) -> None:
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write("# Elen live\n\n- Elen chơi PUBG Mobile vị trí bắn tỉa.\nFAQ\n1. Lịch live từ 20h đến 23h.\n")
        try:
            scorer = RelevanceScorer(lambda texts: None, context_file=f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(
            scorer._passages,
            ["Elen chơi PUBG Mobile vị trí bắn tỉa.", "Lịch live từ 20h đến 23h."],
        )

    async def test_timeout_rewarms_cold_model_in_background(self) -> None:
        # Mô phỏng Ollama đã nhả model: request đầu chậm (đang nạp), sau đó nhanh.
        loaded = asyncio.Event()
        calls: list[list[str]] = []

        async def embed(texts: list[str]) -> list[Sequence[float]]:
            calls.append(texts)
            if not loaded.is_set():
                await asyncio.sleep(0.05)
                loaded.set()
            return [[1.0, 0.0] for _ in texts]

        scorer = RelevanceScorer(embed, passages=["context words"], timeout_sec=0.01)
        scorer._vectors, scorer._ready = [(1.0, 0.0)], True  # đã warmup trước đó
        self.assertIsNone(await scorer.similarity("first"))
        self.assertIsNotNone(scorer._rewarm_task)
        await asyncio.wait_for(scorer._rewarm_task, 1)
        self.assertEqual(calls[-1], ["ping"])
        self.assertAlmostEqual(await scorer.similarity("second"), 1.0)
        await scorer.aclose()


class QuestionsOnlyTest(unittest.IsolatedAsyncioTestCase):
    async def test_chi_nhan_comment_lien_quan_context(self) -> None:
        relevance = RelevanceScorer(FakeEmbedder(), passages=["game schedule"])
        await relevance.warmup()
        buffer = CommentBuffer(
            comment_filter=make_filter(), relevance=relevance,
            questions_only=True, question_threshold=0.4,
        )
        self.assertTrue(await buffer.add(payload("game gi vay", "a")))
        self.assertFalse(await buffer.add(payload("chao moi nguoi", "b")))
        self.assertEqual((await buffer.stats())["dropped_by_reason"]["not_question"], 1)

    async def test_khong_co_similarity_thi_dung_dang_cau_hoi(self) -> None:
        buffer = CommentBuffer(comment_filter=make_filter(), questions_only=True)
        self.assertTrue(await buffer.add(payload("mấy giờ live vậy?", "a")))
        self.assertTrue(await buffer.add(payload("xin link ảnh", "b")))
        self.assertFalse(await buffer.add(payload("mới dậy mà ae", "c")))


if __name__ == "__main__":
    unittest.main()
