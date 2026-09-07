"""Pure-paramiko SSH helpers — no Qt dependency.

These functions are shared by SSHWorker (GUI path) and the CLI session
layer so that connection/auth logic has a single source of truth.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
from pathlib import Path
from typing import Callable, Optional

import paramiko

from src.models.connection import Connection

# Asked to approve a host key we have never seen before.
# Receives (hostname, keytype, fingerprint); returns True to trust it.
HostKeyConfirm = Callable[[str, str, str], bool]

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


class UnknownHostKeyError(paramiko.SSHException):
    """Host key was not in known_hosts and the user did not approve it."""


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint, matching `ssh-keygen -lf`."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _missing_trailing_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            if fh.seek(0, os.SEEK_END) == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def _append_known_host(hostname: str, key: paramiko.PKey) -> None:
    """Append one entry to ~/.ssh/known_hosts.

    Deliberately append-only rather than paramiko's save_host_keys(), which
    rewrites the whole file and drops comments, @cert-authority/@revoked
    markers and any key type it cannot parse — that would quietly corrupt a
    real known_hosts shared with OpenSSH.
    """
    KNOWN_HOSTS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prefix = "\n" if _missing_trailing_newline(KNOWN_HOSTS) else ""
    fd = os.open(KNOWN_HOSTS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with open(fd, "a", encoding="utf-8") as fh:
        fh.write(f"{prefix}{hostname} {key.get_name()} {key.get_base64()}\n")


class _ConfirmPolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use policy for hosts absent from known_hosts.

    Replaces paramiko's AutoAddPolicy, which trusts whatever key a server
    presents and so gives no protection against a man-in-the-middle. Note
    that a key which *conflicts* with a known_hosts entry never reaches this
    policy — paramiko raises BadHostKeyException for that case.
    """

    def __init__(self, confirm: Optional[HostKeyConfirm]) -> None:
        self._confirm = confirm

    def missing_host_key(self, client, hostname, key) -> None:  # noqa: ANN001
        fp = fingerprint(key)
        if self._confirm is None or not self._confirm(hostname, key.get_name(), fp):
            raise UnknownHostKeyError(
                f"Host key for {hostname} is not trusted "
                f"({key.get_name()} {fp}) — connection aborted."
            )
        client.get_host_keys().add(hostname, key.get_name(), key)
        _append_known_host(hostname, key)


def connect_sock(host: str, port: int, timeout: float = 15) -> socket.socket:
    """Resolve host:port and return a connected socket, preferring IPv4.

    Sorting AF_INET first avoids failures with IPv6 link-local addresses
    (fe80::...) that mDNS/.local hostnames often resolve to on macOS.
    """
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    infos.sort(key=lambda x: 0 if x[0] == socket.AF_INET else 1)
    last_exc: Exception = OSError(f"Cannot connect to {host}:{port}")
    for af, socktype, proto, _canonname, sockaddr in infos:
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_exc = exc
            try:
                sock.close()
            except OSError:
                pass
    raise last_exc


def load_key(path: str, passphrase: str | None) -> paramiko.PKey:
    """Try all key types in order (Ed25519 → ECDSA → RSA → DSS)."""
    for cls in (
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
        paramiko.RSAKey,
        paramiko.DSSKey,
    ):
        try:
            return cls.from_private_key_file(path, password=passphrase)
        except paramiko.SSHException:
            continue
    raise paramiko.SSHException(f"Unsupported key format: {path}")


def open_jump_tunnel(
    conn: Connection,
    confirm_host_key: Optional[HostKeyConfirm] = None,
) -> socket.socket:
    """Open a TCP tunnel through a jump host using a second paramiko client.

    jump_host format: [user@]host[:port]

    The jump host's own key is verified exactly like the target's — it sees
    all traffic to the target, so trusting it blindly would defeat the point.
    """
    raw = conn.jump_host
    username = None
    if "@" in raw:
        username, raw = raw.split("@", 1)
    if ":" in raw:
        jump_host, jump_port_str = raw.rsplit(":", 1)
        jump_port = int(jump_port_str)
    else:
        jump_host, jump_port = raw, 22

    jump_client = build_client(confirm_host_key)
    jump_client.connect(
        hostname=jump_host,
        port=jump_port,
        username=username,
        timeout=10,
        allow_agent=True,
        look_for_keys=True,
    )
    transport = jump_client.get_transport()
    dest = (conn.host, conn.port)
    src = ("127.0.0.1", 0)
    channel = transport.open_channel("direct-tcpip", dest, src)
    return channel  # type: ignore[return-value]


def build_client(confirm_host_key: Optional[HostKeyConfirm] = None) -> paramiko.SSHClient:
    """Create a paramiko SSHClient that verifies host keys against known_hosts.

    Unknown hosts are referred to *confirm_host_key*; with no callback the
    connection is refused rather than trusted blindly, so any caller that
    cannot prompt fails closed.
    """
    client = paramiko.SSHClient()
    # Read-only: host key files are never rewritten, only appended to.
    for path in (None, "/etc/ssh/ssh_known_hosts", "/etc/ssh/known_hosts"):
        try:
            client.load_system_host_keys(path)
        except OSError:
            pass
    client.set_missing_host_key_policy(_ConfirmPolicy(confirm_host_key))
    return client


def connect_kwargs(
    conn: Connection,
    confirm_host_key: Optional[HostKeyConfirm] = None,
) -> dict:
    """Build the kwargs dict for paramiko SSHClient.connect()."""
    port = conn.effective_port()
    kwargs: dict = {
        "hostname": conn.host,
        "port": port,
        "username": conn.username or None,
        "timeout": 15,
        "allow_agent": True,
        "look_for_keys": True,
        "compress": conn.compression,
    }

    if conn.password:
        kwargs["password"] = conn.password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False

    if conn.private_key_file:
        key_path = os.path.expanduser(conn.private_key_file)
        try:
            pkey = load_key(key_path, conn.passphrase or None)
            kwargs["pkey"] = pkey
            kwargs["look_for_keys"] = False
        except Exception as exc:  # noqa: BLE001
            raise paramiko.SSHException(
                f"Cannot load key '{key_path}': {exc}"
            ) from exc

    if conn.jump_host:
        kwargs["sock"] = open_jump_tunnel(conn, confirm_host_key)
    else:
        kwargs["sock"] = connect_sock(conn.host, port, timeout=15)

    return kwargs


def establish(
    conn: Connection,
    confirm_host_key: Optional[HostKeyConfirm] = None,
) -> paramiko.SSHClient:
    """Build and return a connected paramiko SSHClient for *conn*."""
    client = build_client(confirm_host_key)
    client.connect(**connect_kwargs(conn, confirm_host_key))
    return client
