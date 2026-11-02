from __future__ import annotations

import re
import unicodedata


_TAGS = (
    "laughter|sigh|confirmation-en|question-en|question-ah|question-oh|question-ei|"
    "question-yi|surprise-ah|surprise-oh|surprise-wa|surprise-yo|dissatisfaction-hnn"
)
_TAG_RE = re.compile(rf"\[({_TAGS})\]")
_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
_STAGE_RE = re.compile(r"\(([^()]*)\)")
_STAGE_DIRECTIONS = {"cười", "cười lớn", "thở dài", "haha"}
_LEADING_MARKER_RE = re.compile(r"(?m)^\s*(?:#+\s*|-\s+)")
_WHITESPACE_RE = re.compile(r"\s+")
_EXPRESSIVE_PUNCTUATION_RE = re.compile(r"[!?]+")
_KEEP_SYMBOLS = frozenset("°")
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x2300, 0x23FF),
)


def _is_pictograph(char: str) -> bool:
    # "°" cũng thuộc nhóm So nhưng là ký hiệu đo lường TTS đọc được ("30°C")
    if char in _KEEP_SYMBOLS:
        return False
    codepoint = ord(char)
    return (
        unicodedata.category(char) in {"So", "Sk"}
        or codepoint in {0x200D, 0xFE0E, 0xFE0F}
        or any(start <= codepoint <= end for start, end in _EMOJI_RANGES)
    )


def _sanitize(text: str, max_tags: int) -> tuple[str, int]:
    kept_tags = 0
    protected: list[str] = []

    def keep_tag(match: re.Match[str]) -> str:
        nonlocal kept_tags
        if kept_tags >= max(0, max_tags):
            return ""
        kept_tags += 1
        protected.append(match.group(0))
        return f"\ue000{len(protected) - 1}\ue001"

    value = _TAG_RE.sub(keep_tag, str(text or ""))
    while True:
        without_brackets = _BRACKET_RE.sub("", value)
        if without_brackets == value:
            break
        value = without_brackets
    for index, tag in enumerate(protected):
        value = value.replace(f"\ue000{index}\ue001", tag)

    def remove_stage_direction(match: re.Match[str]) -> str:
        content = _WHITESPACE_RE.sub(" ", match.group(1).strip()).casefold()
        return "" if content in _STAGE_DIRECTIONS else match.group(0)

    value = _STAGE_RE.sub(remove_stage_direction, value)
    value = "".join(char for char in value if not _is_pictograph(char))
    value = _LEADING_MARKER_RE.sub("", value)
    # "_" thành khoảng trắng để "Tiếng_Việt" không bị dính chữ
    value = value.replace("`", "").replace("*", "").replace("_", " ")
    value = _EXPRESSIVE_PUNCTUATION_RE.sub(lambda match: match.group(0)[-1], value)
    value = re.sub(r"\s+([,;.!?…])", r"\1", value)
    value = _WHITESPACE_RE.sub(" ", value).strip()

    speakable = _TAG_RE.sub("", value)
    if not any(unicodedata.category(char)[0] in {"L", "N"} for char in speakable):
        return "", kept_tags
    return value, kept_tags


def sanitize_for_tts(text: str, *, max_tags: int) -> str:
    """Làm sạch một reply trước khi gửi sang TTS."""
    return _sanitize(text, max_tags)[0]


class TtsTextSanitizer:
    """Giữ ngân sách tag chung cho toàn bộ các chunk của một reply."""

    def __init__(self, max_tags: int) -> None:
        self.remaining_tags = max(0, max_tags)

    def __call__(self, chunk: str) -> str:
        cleaned, used = _sanitize(chunk, self.remaining_tags)
        self.remaining_tags = max(0, self.remaining_tags - used)
        return cleaned
