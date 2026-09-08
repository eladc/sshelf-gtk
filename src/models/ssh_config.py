"""Parser for OpenSSH client config files.

Toolkit-free so both the Qt and GTK import dialogs can share it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from src.models.connection import Connection

_DIRECTIVE = re.compile(r"^(\w+)\s+(.+)$")


def parse_ssh_config(path: Path) -> list[dict]:
    """Parse an OpenSSH config into a list of host dicts.

    Wildcard patterns (including the catch-all ``Host *``) are skipped, since
    they describe defaults rather than a reachable host.
    """
    hosts: list[dict] = []
    current: Optional[dict] = None

    for raw in Path(path).read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        match = _DIRECTIVE.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2).strip()

        if key == "host":
            aliases = [a for a in value.split() if "*" not in a and "?" not in a]
            if not aliases:
                current = None
                continue
            current = {"name": aliases[0], "alias": aliases[0]}
            hosts.append(current)
        elif current is not None:
            if key == "hostname":
                current["hostname"] = value
            elif key == "user":
                current["user"] = value
            elif key == "port":
                try:
                    current["port"] = int(value)
                except ValueError:
                    pass
            elif key == "identityfile":
                current["identityfile"] = str(Path(value).expanduser())
            elif key == "proxyjump":
                current["proxyjump"] = value

    return hosts


def host_to_connection(host: dict) -> Connection:
    """Build a Connection from a parsed host entry."""
    conn = Connection()
    conn.name = host.get("name", "")
    conn.host = host.get("hostname", host.get("alias", ""))
    conn.username = host.get("user", "")
    conn.port = host.get("port", 22)
    conn.private_key_file = host.get("identityfile", "")
    conn.jump_host = host.get("proxyjump", "")
    conn.group = "Imported"
    return conn
