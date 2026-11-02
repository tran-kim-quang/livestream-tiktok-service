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
from app.live.hub import _comment_to_dict
from types import SimpleNamespace


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def payload(
    text: str, user: str = "u1", streamer: str = "host", msg_id: str | None = None
) -> dict[str, Any]:
    return {
        "comment": text,
        "user": {"id": user, "nickname": user, "unique_id": user},
        "streamer_unique_id": streamer,
        "room_id": 123,
        "msg_id": msg_id,
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

    def test_backlog_before_connect_dropped(self) -> None:
        started = 1_000_000
        old = {**payload("comment cũ", user="u7"), "create_time": started - 600_000,
               "session_started_at": started}
        fresh = {**payload("comment mới", user="u7"), "create_time": started + 500,
                 "session_started_at": started}
        lag = {**payload("lệch đồng hồ", user="u8"), "create_time": started - 1_000,
               "session_started_at": started}
        comment_filter = self.make_filter()
        self.assertEqual(comment_filter.drop_reason(old), "backlog")
        # backlog không kích hoạt cooldown của user đó
        self.assertIsNone(comment_filter.drop_reason(fresh))
        self.assertIsNone(comment_filter.drop_reason(lag))
        self.assertIsNone(self.make_filter(skip_backlog=False).drop_reason(old))

    def test_mention_kept(self) -> None:
        # Người xem hay chèn @ để comment không bị TikTok ẩn → không lọc tag
        self.assertIsNone(self.make_filter().drop_reason(payload("@host @ban")))
        self.assertIsNone(self.make_filter().drop_reason(payload("@host giá bao nhiêu")))

    def test_link_spam(self) -> None:
        self.assertEqual(self.make_filter().drop_reason(payload("xem https://spam.vn/a")), "link_spam")

    def test_blocklist_khong_phan_biet_dau_va_hoa_thuong(self) -> None:
        comment_filter = CommentFilter(clock=self.clock, blocklist=("Đồ ngu",))
        self.assertEqual(comment_filter.drop_reason(payload("BAN DO NGU!")), "blocklist")

    def test_tho_tuc_khong_bi_loai_khi_blocklist_rong(self) -> None:
        self.assertIsNone(self.make_filter().drop_reason(payload("đồ ngu, nói tiếp đi")))

    def test_duplicate_chuan_hoa_dau_va_ky_tu_lap(self) -> None:
        comment_filter = self.make_filter(dedup_window_sec=60)
        self.assertIsNone(comment_filter.drop_reason(payload("Hayyyyy quá", "u1")))
        self.assertEqual(comment_filter.drop_reason(payload("HÂY QUÁ", "u2")), "duplicate")
        self.clock.advance(60)
        self.assertIsNone(comment_filter.drop_reason(payload("hay quá", "u3")))

    def test_khong_cooldown_theo_thoi_diem_nhan(self) -> None:
        comment_filter = self.make_filter(dedup_window_sec=0)
        self.assertIsNone(comment_filter.drop_reason(payload("câu đầu", "u1")))
        self.assertIsNone(comment_filter.drop_reason(payload("câu khác", "u1")))
        self.assertIsNone(comment_filter.drop_reason(payload("câu khác", "u2")))

    def test_trung_msg_id(self) -> None:
        comment_filter = self.make_filter(msg_id_ttl_sec=600, dedup_window_sec=0)
        self.assertIsNone(comment_filter.drop_reason(payload("câu đầu", "u1", msg_id="99")))
        self.clock.advance(9)
        self.assertEqual(
            comment_filter.drop_reason(payload("câu đầu", "u1", msg_id="99")),
            "duplicate_id",
        )
        self.clock.advance(1)
        self.assertIsNone(comment_filter.drop_reason(payload("câu mới", "u1", msg_id="100")))

    def test_replay_id_khong_anh_huong_user_khac(self) -> None:
        comment_filter = self.make_filter(msg_id_ttl_sec=600, dedup_window_sec=0)
        self.assertIsNone(comment_filter.drop_reason(payload("câu đầu", "u1", msg_id="99")))
        self.assertEqual(
            comment_filter.drop_reason(payload("bản replay", "u2", msg_id="99")),
            "duplicate_id",
        )
        self.assertIsNone(comment_filter.drop_reason(payload("câu riêng", "u2", msg_id="100")))

    def test_thieu_msg_id_dung_hanh_vi_cu(self) -> None:
        comment_filter = self.make_filter(dedup_window_sec=60)
        self.assertIsNone(comment_filter.drop_reason(payload("giống nhau", "u1")))
        self.assertEqual(comment_filter.drop_reason(payload("GIỐNG NHAU", "u2")), "duplicate")

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  HAYYYYY   ĐẤY  "), "hay day")


class CommentScoringTest(unittest.TestCase):
    def test_cau_hoi_cao_hon_loi_chao(self) -> None:
        self.assertGreater(score(payload("Bạn có khỏe không?")), score(payload("Xin chào bạn")))

    def test_keyword_va_nhac_streamer_duoc_cong_diem(self) -> None:
        base = score(payload("một nội dung bình thường"), priority_keywords=())
        boosted = score(payload("host ơi, tư vấn sản phẩm"), priority_keywords=("sản phẩm",))
        self.assertGreater(boosted, base)


class CommentPayloadTest(unittest.TestCase):
    def test_lay_msg_id_tu_common_proto(self) -> None:
        client = SimpleNamespace(room_id=123, unique_id="host")
        event = SimpleNamespace(
            comment="Xin chào", common=SimpleNamespace(msg_id=987654321),
            user=SimpleNamespace(id="1", nickname="Lan", unique_id="lan"),
        )
        self.assertEqual(_comment_to_dict(client, event)["msg_id"], "987654321")

    def test_common_thieu_thi_msg_id_none(self) -> None:
        client = SimpleNamespace(room_id=123, unique_id="host")
        event = SimpleNamespace(
            comment="Xin chào", common=None,
            user=SimpleNamespace(id="1", nickname="Lan", unique_id="lan"),
        )
        self.assertIsNone(_comment_to_dict(client, event)["msg_id"])


class CommentBufferTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def make_buffer(self, **kwargs: Any) -> CommentBuffer:
        comment_filter = CommentFilter(
            clock=self.clock,
            blocklist=(),
            dedup_window_sec=0,
        )
        kwargs.setdefault("user_cooldown_sec", 0)
        kwargs.setdefault("merge_window_sec", 0)
        return CommentBuffer(comment_filter=comment_filter, clock=self.clock, **{"questions_only": False, **kwargs})

    async def test_gop_hai_comment_cung_user_va_lam_moi_timestamp(self) -> None:
        buffer = self.make_buffer(
            merge_window_sec=10, merge_max_parts=3, fresh_window_sec=5, max_age_sec=20
        )
        await buffer.add(payload("sao troll rồi", "saberg_6", msg_id="1"))
        self.clock.advance(4)
        await buffer.add(payload("trả giá vì troll rồi", "saberg_6", msg_id="2"))
        self.clock.advance(4)
        merged = await buffer.next()
        self.assertEqual(merged["comment"], "sao troll rồi. trả giá vì troll rồi")
        self.assertEqual(merged["parts"], ["sao troll rồi", "trả giá vì troll rồi"])
        self.assertEqual(merged["msg_ids"], ["1", "2"])
        self.assertEqual(merged["msg_id"], "1")
        self.assertEqual(merged["merged"], 2)
        self.assertEqual((await buffer.stats())["merged"], 1)

    async def test_phan_thu_tu_sau_max_parts_tao_record_moi(self) -> None:
        buffer = self.make_buffer(merge_window_sec=10, merge_max_parts=3)
        for index, text in enumerate(("phần một", "phần hai", "phần ba", "phần bốn"), 1):
            await buffer.add(payload(text, "u1", msg_id=str(index)))
            self.clock.advance(1)
        latest = await buffer.latest()
        self.assertEqual([item["comment"] for item in latest], [
            "phần bốn", "phần một. phần hai. phần ba",
        ])

    async def test_ngoai_window_va_khac_user_khong_gop(self) -> None:
        buffer = self.make_buffer(merge_window_sec=10)
        await buffer.add(payload("câu đầu", "u1"))
        self.clock.advance(11)
        await buffer.add(payload("quá cửa sổ", "u1"))
        await buffer.add(payload("user khác", "u2"))
        self.assertEqual(len(await buffer.latest()), 3)
        self.assertTrue(all("merged" not in item for item in await buffer.latest()))

    async def test_record_da_served_khong_duoc_gop(self) -> None:
        buffer = self.make_buffer(merge_window_sec=10)
        await buffer.add(payload("câu đã trả lời", "u1"))
        self.assertEqual((await buffer.next())["comment"], "câu đã trả lời")
        await buffer.add(payload("câu gửi sau", "u1"))
        latest = await buffer.latest()
        self.assertEqual([item["comment"] for item in latest], ["câu gửi sau"])
        self.assertNotIn("merged", latest[0])

    async def test_cooldown_tinh_tu_luc_served_va_nhuong_user_khac(self) -> None:
        scores = {"A đầu": 10, "A tiếp": 9, "B thấp": 1}
        buffer = self.make_buffer(
            user_cooldown_sec=10, scorer=lambda item: scores[str(item["comment"])]
        )
        await buffer.add(payload("A đầu", "A"))
        self.assertEqual((await buffer.next())["comment"], "A đầu")
        await buffer.add(payload("A tiếp", "a"))
        await buffer.add(payload("B thấp", "B"))
        self.assertEqual((await buffer.next())["comment"], "B thấp")
        self.clock.advance(10)
        self.assertEqual((await buffer.next())["comment"], "A tiếp")
        self.assertEqual((await buffer.stats())["skipped_user_cooldown"], 1)

    async def test_all_in_cooldown_cho_den_khi_het(self) -> None:
        buffer = CommentBuffer(
            questions_only=False,
            comment_filter=CommentFilter(blocklist=(), dedup_window_sec=0),
            user_cooldown_sec=0.03,
            merge_window_sec=0,
            fresh_window_sec=1,
            max_age_sec=1,
        )
        await buffer.add(payload("câu đầu", "u1"))
        await buffer.next()
        await buffer.add(payload("câu tiếp", "u1"))
        started = time.perf_counter()
        self.assertEqual((await buffer.next(timeout=0.2))["comment"], "câu tiếp")
        self.assertGreaterEqual(time.perf_counter() - started, 0.02)

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
            questions_only=False,
            comment_filter=CommentFilter(blocklist=(), dedup_window_sec=0),
            user_cooldown_sec=0,
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
