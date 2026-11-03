from __future__ import annotations

import random
import unittest

from app.agent.templates import TemplatePicker, ThanksBatcher, join_names


class Clock:
    def __init__(self) -> None: self.now = 0.0
    def __call__(self) -> float: return self.now


class TemplateTest(unittest.TestCase):
    def test_join_names(self) -> None:
        self.assertEqual(join_names(["Lan"]), "Lan")
        self.assertEqual(join_names(["Lan", "Minh"]), "Lan với Minh")
        self.assertEqual(join_names(["Lan", "Minh", "Hùng"]), "Lan, Minh với Hùng")
        self.assertEqual(join_names(["A", "B", "C", "D", "E"], 4), "A, B, C và cả nhà")

    def test_khong_lap_nam_mau_gan_nhat(self) -> None:
        picker = TemplatePicker(tuple(str(i) for i in range(7)), rng=random.Random(1))
        values = [picker.pick() for _ in range(20)]
        for index, value in enumerate(values):
            self.assertNotIn(value, values[max(0, index - 5):index])

    def test_thanks_gift_truoc_follow_va_min_gap(self) -> None:
        clock = Clock()
        batcher = ThanksBatcher(batch_sec=0, min_gap_sec=20, clock=clock)
        batcher.handle_follow({"user": {"id": "f", "nickname": "Mai"}})
        batcher.handle_gift({"user": {"id": "g", "nickname": "Lan"}, "diamonds": 1})
        self.assertEqual(batcher.pop_ready()["kind"], "gift")
        self.assertIsNone(batcher.pop_ready())
        clock.now = 20
        self.assertEqual(batcher.pop_ready()["kind"], "follow")

    def test_backlog_bi_bo(self) -> None:
        batcher = ThanksBatcher(batch_sec=0)
        batcher.handle_follow({"user": {"id": "f"}, "create_time": 1, "session_started_at": 10_000})
        self.assertFalse(batcher.ready())


if __name__ == "__main__": unittest.main()
