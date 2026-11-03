from __future__ import annotations

import unittest

from app.comments.buffer import CommentBuffer
from app.comments.filters import CommentFilter
from app.comments.relevance import RelevanceScorer


class Clock:
    def __init__(self) -> None: self.now = 100.0
    def __call__(self) -> float: return self.now
    def advance(self, value: float) -> None: self.now += value


def payload(text: str, user: str = "u1") -> dict:
    return {"comment": text, "user": {"id": user, "nickname": user}}


class ChatLaneTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.buffer = CommentBuffer(
            comment_filter=CommentFilter(clock=self.clock, blocklist=(), dedup_window_sec=0),
            questions_only=False, user_cooldown_sec=0, merge_window_sec=10,
            chat_user_cooldown_sec=0, clock=self.clock,
        )

    async def test_phan_lane_va_gop_tinh_lai(self) -> None:
        await self.buffer.add(payload("chào shop", "a"))
        await self.buffer.add(payload("hôm nay vui quá", "b"))
        await self.buffer.add(payload("áo này bao nhiêu?", "c"))
        lanes = {item["comment"]: item["lane"] for item in await self.buffer.latest()}
        self.assertEqual(lanes["chào shop"], "greeting")
        self.assertEqual(lanes["hôm nay vui quá"], "chat")
        self.assertEqual(lanes["áo này bao nhiêu?"], "question")
        await self.buffer.add(payload("áo này giá sao?", "a"))
        merged = next(item for item in await self.buffer.latest() if item["user"]["id"] == "a")
        self.assertEqual(merged["lane"], "question")

    async def test_greeting_co_cau_hoi_khong_bi_nhan_nham(self) -> None:
        await self.buffer.add(payload("chào shop, áo này bao nhiêu?"))
        self.assertEqual((await self.buffer.next())["lane"], "question")

    def test_mau_chao_bao_thu(self) -> None:
        from app.comments.intents import is_pure_greeting
        for text in ("chào shop!", "Hello mn ạ", "xin chào chị nha", "chàooo cả nhà"):
            self.assertTrue(is_pure_greeting(text), text)
        # "hi giá" là hỏi giá; tên riêng chỉ nhận qua embedding
        for text in ("hi giá", "hi Lan", "chào shop áo này bao nhiêu"):
            self.assertFalse(is_pure_greeting(text), text)

    async def test_chat_cooldown_va_bypass_gift(self) -> None:
        buffer = CommentBuffer(
            comment_filter=CommentFilter(clock=self.clock, blocklist=(), dedup_window_sec=0),
            questions_only=False, user_cooldown_sec=0, merge_window_sec=0,
            chat_user_cooldown_sec=60, chat_max_per_user=3, clock=self.clock,
        )
        await buffer.add(payload("nói vui ghê", "a")); self.assertIsNotNone(await buffer.next())
        await buffer.add(payload("hay quá trời", "a")); self.assertIsNone(await buffer.next())
        buffer.audience.record_gift({"id": "a"}, 1)
        self.assertIsNotNone(await buffer.next())

    async def test_moi_comment_chi_mot_request_embedding(self) -> None:
        class Embedder:
            def __init__(self): self.calls = []
            async def embed(self, texts):
                self.calls.append(list(texts)); return [[1.0, 0.0] for _ in texts]
        embedder = Embedder()
        relevance = RelevanceScorer(embedder, passages=["thông tin sản phẩm đang bán"], timeout_sec=1)
        await relevance.warmup()
        buffer = CommentBuffer(
            relevance=relevance,
            comment_filter=CommentFilter(clock=self.clock, blocklist=(), dedup_window_sec=0),
            questions_only=False, user_cooldown_sec=0, clock=self.clock,
        )
        await buffer.add(payload("sản phẩm này đẹp"))
        self.assertEqual(len(embedder.calls), 2)  # một warmup, một comment
        self.assertEqual(embedder.calls[-1], ["sản phẩm này đẹp"])


if __name__ == "__main__": unittest.main()
