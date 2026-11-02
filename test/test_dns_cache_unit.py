from __future__ import annotations

import socket
import threading
import unittest
from unittest import mock

from app import dns_cache


class DnsCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple] = []
        self.refreshed = threading.Event()

        def fake(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
            self.calls.append((host, port))
            if len(self.calls) > 1:
                self.refreshed.set()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", port))]

        patcher = mock.patch.object(dns_cache, "_real_getaddrinfo", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(dns_cache.uninstall)
        self.now = 0.0
        clock = mock.patch.object(dns_cache.time, "monotonic", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        dns_cache.install(ttl=10)

    def test_lan_sau_dung_cache_va_bytes_chung_entry(self) -> None:
        socket.getaddrinfo("Ollama.com", 443, 0, socket.SOCK_STREAM)
        socket.getaddrinfo(b"ollama.com", 443, 0, socket.SOCK_STREAM)
        self.assertEqual(len(self.calls), 1)

    def test_het_han_tra_cache_cu_va_lam_moi_nen(self) -> None:
        socket.getaddrinfo("ollama.com", 443)
        self.now = 11.0
        result = socket.getaddrinfo("ollama.com", 443)
        self.assertEqual(result[0][4], ("1.2.3.4", 443))
        self.assertTrue(self.refreshed.wait(2))

    def test_ip_va_localhost_khong_cache(self) -> None:
        socket.getaddrinfo("127.0.0.1", 80)
        socket.getaddrinfo("127.0.0.1", 80)
        socket.getaddrinfo("localhost", 80)
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
