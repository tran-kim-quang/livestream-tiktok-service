#!/usr/bin/env python3
"""
Mô phỏng agent lấy comment đã chọn lọc trong RAM qua livestream-service.

Cách dùng:
  python test/read_comments_terminal.py

Biến môi trường:
  BASE_URL   — mặc định http://127.0.0.1:8000
  POP_TIMEOUT — giây chờ tối đa mỗi lần pop (mặc định 5)

Cách agent gọi service:
  GET /api/queue/pop?timeout=5
    → {"empty": false, "comment": {"comment": "...", "user": {...}, ...}}
    → {"empty": true,  "comment": null}   ← queue rỗng, chờ live mới gửi

Mỗi comment được pop và XOÁ khỏi buffer; comment điểm cao nhất được ưu tiên,
bằng điểm thì comment mới hơn được chọn trước.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import httpx
except ImportError:
    print("Thiếu httpx — chạy: pip install httpx", file=sys.stderr)
    sys.exit(1)

BASE_URL: str = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
POP_TIMEOUT: int = int(os.environ.get("POP_TIMEOUT", "5"))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _check_service(client: httpx.Client) -> bool:
    """Kiểm tra service sẵn sàng và in thống kê comment."""
    try:
        r = client.get(f"{BASE_URL}/health", timeout=5)
        h = r.json()
    except Exception as e:
        print(f"[lỗi] Không kết nối được service tại {BASE_URL}: {e}", flush=True)
        return False

    sessions = h.get("sessions") or []
    comment_stats = h.get("comments") or {}
    queue_len = comment_stats.get("current", "?")

    print("=" * 60, flush=True)
    print(f"  Service  : {BASE_URL}", flush=True)
    print(f"  Queue    : {queue_len} comment đang chờ", flush=True)
    if sessions:
        s = sessions[0]
        print(f"  Live     : @{s.get('unique_id')} — room {s.get('room_id')}", flush=True)
        print(f"  Status   : {'đang kết nối' if s.get('started') else 'chưa kết nối'}", flush=True)
    else:
        print("  Live     : chưa có session nào", flush=True)
    print("=" * 60, flush=True)

    return True


def _pop_next(client: httpx.Client) -> Optional[Dict[str, Any]]:
    """
    Gọi GET /api/queue/pop?timeout=POP_TIMEOUT.
    Trả về dict comment nếu có, None nếu queue rỗng hoặc lỗi.
    """
    try:
        r = client.get(
            f"{BASE_URL}/api/queue/pop",
            params={"timeout": POP_TIMEOUT},
        )
    except httpx.ReadTimeout:
        return None  # hết POP_TIMEOUT mà queue rỗng — thử lại
    except httpx.RequestError as e:
        print(f"[lỗi mạng] {e}", flush=True)
        return None

    if r.status_code != 200:
        print(f"[HTTP {r.status_code}] {r.text[:120]}", flush=True)
        return None

    data = r.json()
    if data.get("empty"):
        return None
    return data.get("comment")


def _format_comment(c: Dict[str, Any]) -> str:
    """Format một comment để hiển thị trong terminal."""
    u    = c.get("user") or {}
    nick = u.get("nickname") or "?"
    uid  = u.get("unique_id") or "?"
    text = c.get("comment") or ""
    ts   = (c.get("enqueued_at") or "")[:19].replace("T", " ")
    return f"[{ts}]  {nick} (@{uid})\n  → {text}"


# ─────────────────────────────────────────────────────────────────────────────
# Main loop — mô phỏng đúng cách agent gọi service
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\nAgent consumer — timeout={POP_TIMEOUT}s/lần — Ctrl+C để dừng\n", flush=True)

    _timeout = httpx.Timeout(connect=5.0, read=POP_TIMEOUT + 8, write=5.0, pool=5.0)
    with httpx.Client(timeout=_timeout) as client:
        if not _check_service(client):
            sys.exit(1)

        print(f"\nBắt đầu lấy comment (blocking {POP_TIMEOUT}s/lần)...\n", flush=True)

        total_popped = 0
        empty_streak = 0

        while True:
            comment = _pop_next(client)

            if comment is None:
                empty_streak += 1
                # Thông báo mỗi 6 lần trống (~30s) để biết đang chờ
                if empty_streak % 6 == 1:
                    print(f"[chờ] Queue rỗng — đang đợi comment mới từ live...", flush=True)
                continue

            empty_streak = 0
            total_popped += 1

            # ── Đây là nơi agent xử lý comment ──────────────────────────────
            print(flush=True)
            print(f"── Comment #{total_popped} ──────────────────────────────", flush=True)
            print(_format_comment(comment), flush=True)

            # Agent lấy các field cần thiết:
            user     = comment.get("user") or {}
            username = user.get("nickname") or user.get("unique_id") or "unknown"
            text     = comment.get("comment") or ""

            # → Gửi vào agent AI:
            # agent_reply = my_agent.respond(query=text, sender=username)
            print(f"\n  [agent input]  user   = {username!r}", flush=True)
            print(f"  [agent input]  query  = {text!r}", flush=True)
            print(f"  [→ ready for next comment]", flush=True)
            # ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[thoát]", flush=True)
