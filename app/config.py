import os


def _truthy(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── TikTok ────────────────────────────────────────────────────────────────────
# Username TikTok được cấu hình sẵn; service tự kết nối khi startup
TIKTOK_USERNAME: str = os.environ.get("TIKTOK_USERNAME", "")
# Giây chờ trước khi thử kết nối lại (khi chưa live hoặc live kết thúc)
TIKTOK_LIVE_RETRY_SEC: float = float(os.environ.get("TIKTOK_LIVE_RETRY_SEC", "30"))

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_CONNECT_RETRIES = max(1, int(os.environ.get("REDIS_CONNECT_RETRIES", "8")))
REDIS_CONNECT_RETRY_SEC = float(os.environ.get("REDIS_CONNECT_RETRY_SEC", "0.75"))
REDIS_ENABLED = _truthy("REDIS_ENABLED", True)
# Hàng đợi toàn cục — agent RPOP/BRPOP từ đây (FIFO: LPUSH mới, RPOP cũ)
REDIS_QUEUE_KEY = os.environ.get("REDIS_QUEUE_KEY", "livestream:comments")
# Lưu lịch sử theo room_id (LPUSH + LTRIM, chỉ đọc, không xoá)
REDIS_ROOM_COMMENTS_PREFIX = os.environ.get(
    "REDIS_ROOM_COMMENTS_PREFIX", "livestream:room:{room_id}:comments"
)
REDIS_ROOM_MAX_COMMENTS = int(os.environ.get("REDIS_ROOM_MAX_COMMENTS", "2000"))
