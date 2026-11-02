from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import comments as comments_api
from app.api import meta, queue
from app.api.deps import get_comments, get_hub, get_tts
from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter, normalize_text
from app.comments.scoring import score


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def payload(text: str, user: str = "u1", streamer: str = "host") -> dict[str, Any]:
    return {
        "comment": text,
        "user": {"id": user, "nickname": user, "unique_id": user},
        "streamer_unique_id": streamer,
        "room_id": 123,
    }


class CommentFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def make_filter(self, **kwargs: Any) -> CommentFilter:
        return CommentFilter(clock=self.clock, blocklist=(), **kwargs)

    def test_empty_va_emoji_dau_cau(self) -> None:
        comment_filter = self.make_filter()
        self.assertEqual(comment_filter.drop_reason(payload("   ")), "empty")
        self.assertEqual(comment_filter.drop_reason(payload("😂!!!")), "emoji_or_punctuation_only")
        self.assertEqual(comment_filter.drop_reason(payload("❤️‍🔥")), "emoji_or_punctuation_only")

    def test_too_short(self) -> None:
        self.assertEqual(self.make_filter(min_chars=2).drop_reason(payload("a!")), "too_short")

    def test_too_long(self) -> None:
        self.assertEqual(self.make_filter(max_chars=5).drop_reason(payload("abcdef")), "too_long")

    def test_mention_kept(self) -> None:
        # Người xem hay chèn @ để comment không bị TikTok ẩn → không lọc tag
        self.assertIsNone(self.make_filter().drop_reason(payload("@host @ban")))
        self.assertIsNone(self.make_filter().drop_reason(payload("@host giá bao nhiêu")))

    def test_link_spam(self) -> None:
        self.assertEqual(self.make_filter().drop_reason(payload("xem https://spam.vn/a")), "link_spam")

    def test_blocklist_khong_phan_biet_dau_va_hoa_thuong(self) -> None:
        comment_filter = CommentFilter(clock=self.clock, blocklist=("Đồ ngu",))
        self.assertEqual(comment_filter.drop_reason(payload("BAN DO NGU!")), "blocklist")

    def test_duplicate_chuan_hoa_dau_va_ky_tu_lap(self) -> None:
        comment_filter = self.make_filter(dedup_window_sec=60, user_cooldown_sec=0)
        self.assertIsNone(comment_filter.drop_reason(payload("Hayyyyy quá", "u1")))
        self.assertEqual(comment_filter.drop_reason(payload("HÂY QUÁ", "u2")), "duplicate")
        self.clock.advance(60)
        self.assertIsNone(comment_filter.drop_reason(payload("hay quá", "u3")))

    def test_cooldown_theo_user(self) -> None:
        comment_filter = self.make_filter(dedup_window_sec=0, user_cooldown_sec=10)
        self.assertIsNone(comment_filter.drop_reason(payload("câu đầu", "u1")))
        self.assertEqual(comment_filter.drop_reason(payload("câu khác", "u1")), "user_cooldown")
        self.assertIsNone(comment_filter.drop_reason(payload("câu khác", "u2")))
        self.clock.advance(10)
        self.assertIsNone(comment_filter.drop_reason(payload("đã chờ đủ", "u1")))

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  HAYYYYY   ĐẤY  "), "hay day")


class CommentScoringTest(unittest.TestCase):
    def test_cau_hoi_cao_hon_loi_chao(self) -> None:
        self.assertGreater(score(payload("Bạn có khỏe không?")), score(payload("Xin chào bạn")))

    def test_keyword_va_nhac_streamer_duoc_cong_diem(self) -> None:
        base = score(payload("một nội dung bình thường"), priority_keywords=())
        boosted = score(payload("host ơi, tư vấn sản phẩm"), priority_keywords=("sản phẩm",))
        self.assertGreater(boosted, base)


class CommentBufferTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def make_buffer(self, **kwargs: Any) -> CommentBuffer:
        comment_filter = CommentFilter(
            clock=self.clock,
            blocklist=(),
            dedup_window_sec=0,
            user_cooldown_sec=0,
        )
        return CommentBuffer(comment_filter=comment_filter, clock=self.clock, **kwargs)

    async def test_best_pick_va_moi_comment_chi_served_mot_lan(self) -> None:
        buffer = self.make_buffer()
        await buffer.add(payload("xin chào", "u1"))
        self.clock.advance(0.1)
        await buffer.add(payload("bạn đang ở đâu?", "u2"))
        self.assertEqual((await buffer.next())["comment"], "bạn đang ở đâu?")
        self.assertEqual((await buffer.next())["comment"], "xin chào")
        self.assertIsNone(await buffer.next())
        self.assertEqual((await buffer.stats())["served"], 2)

    async def test_bang_diem_thi_comment_moi_hon(self) -> None:
        buffer = self.make_buffer(scorer=lambda _: 1.0)
        await buffer.add(payload("câu cũ", "u1"))
        self.clock.advance(1)
        await buffer.add(payload("câu mới", "u2"))
        self.assertEqual((await buffer.next())["comment"], "câu mới")

    async def test_latest_read_only_va_moi_nhat_truoc(self) -> None:
        buffer = self.make_buffer()
        await buffer.add(payload("câu cũ", "u1"))
        self.clock.advance(1)
        await buffer.add(payload("câu mới", "u2"))
        self.assertEqual([item["comment"] for item in await buffer.latest(2)], ["câu mới", "câu cũ"])
        self.assertEqual(await buffer.length(), 2)

    async def test_freshness_va_expiry(self) -> None:
        buffer = self.make_buffer(fresh_window_sec=5, max_age_sec=10)
        await buffer.add(payload("còn trong lịch sử"))
        self.clock.advance(6)
        self.assertIsNone(await buffer.next())
        self.assertEqual(await buffer.length(), 0)
        self.assertEqual(len(await buffer.latest()), 1)
        self.clock.advance(5)
        self.assertEqual(await buffer.latest(), [])
        self.assertEqual((await buffer.stats())["expired"], 1)

    async def test_timeout(self) -> None:
        buffer = self.make_buffer()
        started = time.perf_counter()
        self.assertIsNone(await buffer.next(timeout=0.02))
        self.assertGreaterEqual(time.perf_counter() - started, 0.015)

    async def test_rate_guard(self) -> None:
        buffer = self.make_buffer(max_serve_per_sec=1, scorer=lambda _: 1.0)
        await buffer.add(payload("một", "u1"))
        await buffer.add(payload("hai", "u2"))
        self.assertIsNotNone(await buffer.next())
        self.assertIsNone(await buffer.next())
        self.clock.advance(1)
        self.assertIsNotNone(await buffer.next())

    async def test_stats_filter_drop(self) -> None:
        buffer = self.make_buffer()
        await buffer.add(payload("😂"))
        stats = await buffer.stats()
        self.assertEqual(stats["received"], 1)
        self.assertEqual(stats["accepted"], 0)
        self.assertEqual(stats["dropped_by_reason"], {"emoji_or_punctuation_only": 1})


class FakeHub:
    def list_sessions(self) -> list[dict]:
        return [{"unique_id": "fake", "started": True}]


class CommentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = CommentBuffer(
            comment_filter=CommentFilter(blocklist=(), dedup_window_sec=0, user_cooldown_sec=0)
        )
        self.app = FastAPI()
        self.app.include_router(meta.router)
        self.app.include_router(queue.router)
        self.app.include_router(comments_api.router)
        self.app.dependency_overrides[get_comments] = lambda: self.buffer
        self.app.dependency_overrides[get_hub] = lambda: FakeHub()
        self.app.dependency_overrides[get_tts] = lambda: None

    def test_pop_va_stats(self) -> None:
        asyncio.run(self.buffer.add(payload("bạn thấy thế nào?")))
        with TestClient(self.app) as client:
            popped = client.get("/api/queue/pop").json()
            stats = client.get("/api/comments/stats").json()
            health = client.get("/health").json()
        self.assertFalse(popped["empty"])
        self.assertEqual(popped["comment"]["comment"], "bạn thấy thế nào?")
        self.assertIn("score", popped["comment"])
        self.assertEqual(stats["served"], 1)
        self.assertIn("filter_config", stats)
        self.assertEqual(health["comments"]["served"], 1)
        self.assertEqual(health["sessions"][0]["unique_id"], "fake")

    def test_pop_rong_khong_503(self) -> None:
        with TestClient(self.app) as client:
            response = client.get("/api/queue/pop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"empty": True, "comment": None})


if __name__ == "__main__":
    unittest.main()
