"""Mount data model for reverse directory mounts (local dir → remote host)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Mount:
    """A saved reverse-mount configuration: expose a local directory on a remote host."""

    id: Optional[int] = None
    conn_id: Optional[int] = None
    label: str = ""
    local_dir: str = ""
    remote_dir: str = ""
    enabled: bool = True

    def display(self) -> str:
        return f"{self.local_dir}  →  {self.remote_dir}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conn_id": self.conn_id,
            "label": self.label,
            "local_dir": self.local_dir,
            "remote_dir": self.remote_dir,
            "enabled": int(self.enabled),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mount":
        return cls(
            id=d.get("id"),
            conn_id=d.get("conn_id"),
            label=d.get("label", ""),
            local_dir=d.get("local_dir", ""),
            remote_dir=d.get("remote_dir", ""),
            enabled=bool(d.get("enabled", 1)),
        )
