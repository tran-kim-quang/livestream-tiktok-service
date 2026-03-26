import httpx

BASE = "http://127.0.0.1:8000"
POP_TIMEOUT = 5  # giây BRPOP chờ Redis

# Tách read_timeout riêng = POP_TIMEOUT + buffer
# httpx.Timeout(connect, read, write, pool)
_TIMEOUT = httpx.Timeout(connect=5.0, read=POP_TIMEOUT + 8, write=5.0, pool=5.0)

# Dùng Client để tái sử dụng kết nối (nhanh hơn, không tạo mới mỗi lần)
_client = httpx.Client(timeout=_TIMEOUT)


def get_next_comment() -> dict | None:
    """Lấy comment tiếp theo từ hàng đợi Redis (blocking).
    Trả None nếu queue rỗng sau POP_TIMEOUT giây.
    """
    try:
        r = _client.get(f"{BASE}/api/queue/pop", params={"timeout": POP_TIMEOUT})
        r.raise_for_status()
        data = r.json()
        if data["empty"]:
            return None
        return data["comment"]  # {"comment": "...", "user": {"nickname": "...", ...}, ...}
    except httpx.ReadTimeout:
        return None  # server chưa kịp trả — thử lại
    except httpx.HTTPStatusError as e:
        print(f"[lỗi HTTP] {e.response.status_code}: {e.response.text[:120]}")
        return None
    except httpx.RequestError as e:
        print(f"[lỗi mạng] {e}")
        return None


# ── Vòng lặp agent ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[agent] Đang lắng nghe queue (POP_TIMEOUT={POP_TIMEOUT}s)... Ctrl+C để dừng\n")
    count = 0
    try:
        while True:
            item = get_next_comment()
            if item is None:
                continue  # queue rỗng, thử lại ngay (blocking nên không tốn CPU)

            count += 1
            user     = item.get("user") or {}
            username = user.get("nickname") or user.get("unique_id") or "unknown"
            query    = item.get("comment") or ""

            print(f"[#{count}] {username}: {query}")

            # → Gửi vào LLM/agent của bạn:
            # reply = my_agent.respond(query=query, sender=username)
            # print(f"  [reply] {reply}")

    except KeyboardInterrupt:
        print("\n[thoát]")
    finally:
        _client.close()
