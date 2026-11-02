from __future__ import annotations

import unittest

from app.agent.speech import _take_ready_chunk


def stream_chunks(deltas: list[str]) -> tuple[list[str], str]:
    buffer = ""
    first = True
    chunks: list[str] = []
    for delta in deltas:
        buffer += delta
        while ready := _take_ready_chunk(buffer, first):
            chunk, buffer = ready
            chunks.append(chunk)
            first = False
    return chunks, buffer.strip()


class TakeReadyChunkTest(unittest.TestCase):
    def test_khong_cat_giua_tu_khi_token_dung_giua_tu(self) -> None:
        # "súng" được LLM stream thành "s" + "úng"
        chunks, rest = stream_chunks(
            ["saberg_6", " ơi", " mình", " có", " chơi", " s", "úng", " tỉa", " nhé"]
        )
        self.assertEqual(chunks, ["saberg_6 ơi mình có chơi súng"])
        self.assertEqual(rest, "tỉa nhé")

    def test_so_thap_phan_khong_bi_cat(self) -> None:
        chunks, _ = stream_chunks(["Giá", " 1", ",", "5", " triệu", " nhé", "."," Ok", " bạn"])
        self.assertEqual(chunks[0], "Giá 1,5 triệu nhé.")

    def test_chunk_sau_qua_ngan_duoc_gop(self) -> None:
        chunks, _ = stream_chunks(
            ["Giá", " là", " 2", ".", "5", " triệu", " nhé", " bạn", " ơi", ".", " Ship", " toàn",
             " quốc", "!"]
        )
        self.assertEqual(chunks, ["Giá là 2.5 triệu nhé bạn", "ơi. Ship toàn quốc!"])

    def test_dau_cham_cuoi_buffer_cho_ky_tu_ke_tiep(self) -> None:
        self.assertIsNone(_take_ready_chunk("Xin chào.", first=True))
        self.assertEqual(_take_ready_chunk("Xin chào. ", first=True), ("Xin chào.", " "))

    def test_tag_truoc_dau_hoi_o_cung_chunk_voi_cau(self) -> None:
        self.assertEqual(
            _take_ready_chunk("Thật á [question-ah]? Để", first=True),
            ("Thật á [question-ah]?", " Để"),
        )


if __name__ == "__main__":
    unittest.main()
