from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.config import (
    GPU_HOST, GPU_LOCAL_HOST, GPU_NAME, GPU_SSH_KEY, GPU_SSH_PASSWORD,
    GPU_SSH_PORT, GPU_SSH_USER,
)

_LOCAL_ALIASES = {"", "local", "localhost", "127.0.0.1"}


@dataclass(frozen=True)
class GpuNode:
    """
    Máy có GPU mà service gửi việc tới (TTS, ...).

    - GpuNode.local()                       → card trên chính máy này (mặc định)
    - GpuNode.from_address("100.98.141.59") → máy khác trong LAN / Tailscale
    - GpuNode.from_env()                    → đọc GPU_HOST / GPU_NAME
    """

    host: str
    name: str = ""
    is_local: bool = False
    ssh_user: str = ""
    ssh_port: int = 22
    ssh_key_path: str = ""
    ssh_password: str = field(default="", repr=False)

    @classmethod
    def local(cls) -> GpuNode:
        return cls(host=GPU_LOCAL_HOST, name="local", is_local=True)

    @classmethod
    def from_address(cls, address: Optional[str], name: str = "") -> GpuNode:
        addr = (address or "").strip()
        if addr.lower() in _LOCAL_ALIASES:
            return cls(host=GPU_LOCAL_HOST, name=name or "local", is_local=True)
        return cls(host=addr, name=name or addr)

    @classmethod
    def from_env(cls) -> GpuNode:
        node = cls.from_address(GPU_HOST, GPU_NAME)
        if node.is_local:
            return node
        return cls(
            host=node.host, name=node.name, ssh_user=GPU_SSH_USER,
            ssh_port=GPU_SSH_PORT, ssh_key_path=GPU_SSH_KEY,
            ssh_password=GPU_SSH_PASSWORD,
        )

    @property
    def use_ssh(self) -> bool:
        return not self.is_local and bool(self.ssh_user)

    def http_url(self, port: int, path: str = "") -> str:
        normalized_path = f"/{path.lstrip('/')}" if path else ""
        return f"http://{self.host}:{port}{normalized_path}"

    def info(self) -> Dict[str, Any]:
        info = {"name": self.name, "host": self.host, "local": self.is_local, "ssh": self.use_ssh}
        if self.use_ssh:
            info.update({"ssh_user": self.ssh_user, "ssh_port": self.ssh_port})
        return info
