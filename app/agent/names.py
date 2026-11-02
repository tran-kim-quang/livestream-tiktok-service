from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any


_WHITESPACE_RE = re.compile(r"\s+")
_MAX_WORDS = 3
_MAX_CHARS = 24
_VIETNAMESE_COMBINING_MARKS = {
    "\u0300",  # huyền
    "\u0301",  # sắc
    "\u0303",  # ngã
    "\u0309",  # hỏi
    "\u0323",  # nặng
    "\u0302",  # â/ê/ô
    "\u0306",  # ă
    "\u031b",  # ơ/ư
}


# Chữ "small caps" hay dùng trong tên TikTok — NFKC không đổi được nên map tay
_SMALL_CAPS = str.maketrans(
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ",
    "abcdefghijklmnopqrstuvwyz",
)


def _clean(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_SMALL_CAPS)
    text = text.replace("_", " ").replace(".", " ")
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category[0] in {"L", "N"} or char in _VIETNAMESE_COMBINING_MARKS:
            kept.append(char)
        elif char.isspace():
            kept.append(" ")
        elif char in {"'", "-"}:
            kept.append(char)

    words = [word.strip("'-") for word in _WHITESPACE_RE.split("".join(kept).strip())]
    words = [word for word in words if word][: _MAX_WORDS]
    selected: list[str] = []
    for word in words:
        candidate = " ".join((*selected, word))
        if len(candidate) <= _MAX_CHARS:
            selected.append(word)
            continue
        if not selected:
            selected.append(word[:_MAX_CHARS].rstrip("'-"))
        break
    return " ".join(selected)


def speakable_name(user: Mapping[str, Any]) -> str:
    """Lấy display name TikTok và loại ký tự không phù hợp để đọc bằng TTS."""
    return _clean(user.get("nickname")) or _clean(user.get("unique_id")) or "bạn"
