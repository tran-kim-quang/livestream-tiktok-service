"""Cache DNS cho cả process: trả kết quả đã có ngay, làm mới ở nền khi hết hạn.

Trên máy có adapter khai báo DNS server không phản hồi (vd. Tailscale với
``fec0:0:0:ffff::1``), mỗi lần cache của OS hết hạn ``getaddrinfo`` mất ~15 s,
khiến request tới LLM cloud chờ theo. asyncio/httpx/asyncssh đều gọi
``socket.getaddrinfo`` lúc chạy nên chỉ cần thay hàm này.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_real_getaddrinfo = socket.getaddrinfo
_cache: dict[tuple[Any, ...], tuple[float, list[Any]]] = {}
_refreshing: set[tuple[Any, ...]] = set()
_lock = threading.Lock()
_ttl = 300.0


def _cacheable(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str) or not host or host.lower() == "localhost":
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _key(host: Any, *rest: Any) -> tuple[Any, ...]:
    # anyio truyền host dạng bytes IDNA, asyncio dạng str → chung một entry
    name = host.decode("ascii", "ignore") if isinstance(host, bytes) else str(host)
    return (name.lower().rstrip("."), *rest)


def _refresh(key: tuple[Any, ...]) -> None:
    try:
        result = _real_getaddrinfo(*key)
    except OSError as exc:
        # giữ kết quả cũ, lần sau thử lại
        logger.debug("Làm mới DNS %s thất bại: %s", key[0], exc)
    else:
        with _lock:
            _cache[key] = (time.monotonic(), result)
    finally:
        with _lock:
            _refreshing.discard(key)


def _cached_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    if not _cacheable(host):
        return _real_getaddrinfo(host, port, family, type, proto, flags)
    key = _key(host, port, family, type, proto, flags)
    with _lock:
        entry = _cache.get(key)
        stale = entry is not None and time.monotonic() - entry[0] > _ttl
        if stale and key not in _refreshing:
            _refreshing.add(key)
            threading.Thread(target=_refresh, args=(key,), daemon=True).start()
    if entry is not None:
        return list(entry[1])
    result = _real_getaddrinfo(host, port, family, type, proto, flags)
    with _lock:
        _cache[key] = (time.monotonic(), result)
    return list(result)


def install(ttl: float = 300.0) -> None:
    """Bật cache (idempotent)."""
    global _ttl
    _ttl = max(1.0, ttl)
    socket.getaddrinfo = _cached_getaddrinfo


def uninstall() -> None:
    socket.getaddrinfo = _real_getaddrinfo
    with _lock:
        _cache.clear()
        _refreshing.clear()


def prefetch(host: str, port: int = 443) -> None:
    """Tra trước ở nền để comment đầu tiên không phải chờ DNS."""

    def run() -> None:
        # đúng tham số asyncio dùng khi mở kết nối TCP
        try:
            socket.getaddrinfo(host, port, family=0, type=socket.SOCK_STREAM, proto=0, flags=0)
        except OSError as exc:
            logger.warning("Không tra được DNS cho %s: %s", host, exc)

    if _cacheable(host):
        threading.Thread(target=run, name=f"dns-prefetch-{host}", daemon=True).start()
