from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import asyncssh

from app.config import GPU_SSH_KNOWN_HOSTS
from app.gpu import GpuNode

logger = logging.getLogger(__name__)


class SshTunnel:
    """SSH local-forward tới một API chỉ lắng nghe trên loopback của máy GPU."""

    def __init__(self, node: GpuNode, remote_host: str = "127.0.0.1", remote_port: int = 3900) -> None:
        self.node = node
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port: int | None = None
        self._connection: Any = None
        self._listener: Any = None
        self._lock = asyncio.Lock()
        self._warned_known_hosts = False
        self._warned_missing_key = False

    @property
    def base_url(self) -> str:
        if self.local_port is None:
            raise RuntimeError("SSH tunnel chưa được khởi động.")
        return f"http://127.0.0.1:{self.local_port}"

    async def start(self) -> None:
        await self.ensure()

    async def ensure(self) -> None:
        if self._alive():
            return
        async with self._lock:
            if self._alive():
                return
            old_port = self.local_port or 0
            await self._close_unlocked()
            known_hosts: str | None = GPU_SSH_KNOWN_HOSTS or None
            if known_hosts is None and not self._warned_known_hosts:
                logger.warning("GPU_SSH_KNOWN_HOSTS chưa đặt — SSH không kiểm tra host key.")
                self._warned_known_hosts = True
            options: dict[str, Any] = {
                "host": self.node.host,
                "port": self.node.ssh_port,
                "username": self.node.ssh_user,
                "known_hosts": known_hosts,
                "keepalive_interval": 15,
            }
            if self.node.ssh_key_path and os.path.isfile(self.node.ssh_key_path):
                options["client_keys"] = [self.node.ssh_key_path]
            elif self.node.ssh_key_path and not self._warned_missing_key:
                # Docker tạo thư mục rỗng khi file key trên host không tồn tại → dùng password.
                logger.warning(
                    "GPU_SSH_KEY %s không phải file key; bỏ qua và dùng GPU_SSH_PASSWORD (nếu có).",
                    self.node.ssh_key_path,
                )
                self._warned_missing_key = True
            if self.node.ssh_password:
                options["password"] = self.node.ssh_password
            try:
                self._connection = await asyncssh.connect(**options)
                try:
                    self._listener = await self._connection.forward_local_port(
                        "127.0.0.1", old_port, self.remote_host, self.remote_port
                    )
                except OSError:
                    if not old_port:
                        raise
                    logger.warning("Không giữ được local port %s; SSH tunnel sẽ dùng port mới.", old_port)
                    self._listener = await self._connection.forward_local_port(
                        "127.0.0.1", 0, self.remote_host, self.remote_port
                    )
                self.local_port = self._listener.get_port()
                logger.info("Đã mở SSH tunnel 127.0.0.1:%s tới %s:%s.", self.local_port, self.remote_host, self.remote_port)
            except Exception:
                await self._close_unlocked()
                raise

    def _alive(self) -> bool:
        return self._connection is not None and not self._connection.is_closed() and self._listener is not None

    async def close(self) -> None:
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        if self._listener is not None:
            self._listener.close()
            await self._listener.wait_closed()
            self._listener = None
        if self._connection is not None:
            self._connection.close()
            await self._connection.wait_closed()
            self._connection = None
