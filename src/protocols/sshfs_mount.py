"""Reverse-mount engine: expose a local directory on a remote host via sshfs slave mode.

How it works
------------
sshfs supports a ``-o slave`` mode where it speaks the SFTP protocol over its own
stdin/stdout instead of spawning an ssh child.  We exploit this to ride the *existing*
authenticated paramiko channel:

  local sftp-server (serves ~/local-dir)
       |
       |  two pump threads (bytes <-> bytes)
       |
  paramiko exec channel  ──────────────────────────────→  remote: sshfs -o slave
  (existing SSH session)                                           mounts /remote/dir

No reverse tunnel, no second SSH connection, no extra authentication needed.
The remote only needs sshfs + FUSE installed (``apt install sshfs``).
Windows-as-local is unsupported (no stock sftp-server binary).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


# Candidate paths for the local OpenSSH sftp-server subsystem binary.
# The binary serves the local directory over the SFTP protocol to the remote sshfs.
SFTP_SERVER_CANDIDATES = [
    "/usr/libexec/sftp-server",           # macOS
    "/usr/lib/openssh/sftp-server",       # Debian/Ubuntu
    "/usr/lib/ssh/sftp-server",           # Arch/Alpine
    "/usr/libexec/openssh/sftp-server",   # RHEL/CentOS/Fedora
    "/usr/sbin/sftp-server",              # some BSDs
]


def find_sftp_server() -> Optional[str]:
    """Return the path to the local sftp-server binary, or None if not found."""
    for path in SFTP_SERVER_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def remote_has_sshfs(transport) -> bool:
    """Check whether sshfs is available on the remote host.

    Opens a short-lived exec channel and runs ``command -v sshfs``.
    Returns True if the exit status is 0 (found).
    """
    try:
        chan = transport.open_session()
        chan.settimeout(10)
        chan.exec_command("command -v sshfs")
        chan.makefile("r").read()  # drain stdout
        status = chan.recv_exit_status()
        chan.close()
        return status == 0
    except Exception:  # noqa: BLE001
        return False


def _pump(src, dst_write) -> None:
    """Copy bytes from *src* (paramiko channel or file-like) to *dst_write* until EOF."""
    try:
        while True:
            if hasattr(src, "recv"):
                data = src.recv(4096)
            else:
                data = src.read(4096)
            if not data:
                break
            dst_write(data)
    except OSError:
        pass
    except Exception:  # noqa: BLE001
        pass


def _shell_remote_path(path: str) -> str:
    """Return a shell-safe string for *path* in a remote exec_command.

    shlex.quote wraps paths in single quotes which prevents tilde expansion
    (e.g. '~/dir' → literal '~' directory).  For tilde-relative paths we
    substitute $HOME and use double quotes instead, which the remote shell
    expands correctly.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        # Escape only the chars that are special inside double quotes
        rest = path[2:].replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
        return f'"$HOME/{rest}"'
    return shlex.quote(path)


class ReverseMountError(Exception):
    """Raised when a reverse mount cannot be started."""


class ReverseMount:
    """Mounts a local directory on a remote host via sshfs slave mode.

    Usage::

        m = ReverseMount(transport, "~/my-dir", "/mnt/remote-dir")
        m.start()   # blocks until mount is live or raises ReverseMountError
        ...
        m.stop()    # unmounts on the remote and cleans up local process

    Parameters
    ----------
    transport : paramiko.Transport
        An already-authenticated paramiko transport.
    local_dir : str
        Local directory to expose (~ is expanded).
    remote_dir : str
        Absolute mountpoint path on the remote host.
    """

    def __init__(self, transport, local_dir: str, remote_dir: str) -> None:
        self._transport  = transport
        self._local_dir  = str(Path(local_dir).expanduser().resolve())
        self._remote_dir = remote_dir
        self._proc: Optional[subprocess.Popen] = None
        self._chan        = None
        self._threads: list[threading.Thread] = []
        self._alive       = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the reverse mount.  Raises ReverseMountError on failure."""
        if sys.platform == "win32":
            raise ReverseMountError(
                "Reverse mount is not supported on Windows (no local sftp-server binary)."
            )

        sftp_server = find_sftp_server()
        if sftp_server is None:
            raise ReverseMountError(
                "Cannot find a local sftp-server binary. "
                "Ensure openssh-client is installed:\n"
                "  macOS:  ssh is pre-installed, sftp-server at /usr/libexec/sftp-server\n"
                "  Linux:  sudo apt install openssh-client"
            )

        if not os.path.isdir(self._local_dir):
            raise ReverseMountError(
                f"Local directory does not exist: {self._local_dir}"
            )

        # 1. Start local sftp-server (serves self._local_dir via SFTP protocol).
        #    -e: log to stderr (not syslog)
        #    -R: read-write (default, but explicit)
        #    -d: restrict to this directory (chroot-like, some implementations)
        # Note: macOS sftp-server does not support -d; we pass just -e.
        try:
            self._proc = subprocess.Popen(
                [sftp_server, "-e"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ReverseMountError(f"Failed to start local sftp-server: {exc}") from exc

        # 2. Open a dedicated exec channel and run sshfs in slave mode on the remote.
        #    The channel's stdin/stdout become sshfs's stdin/stdout.
        #    :<local_abs> is the sshfs "host:path" — empty host means use slave stdin/stdout.
        safe_remote = _shell_remote_path(self._remote_dir)
        safe_local  = shlex.quote(f":{self._local_dir}")
        remote_cmd  = (
            f"mkdir -p {safe_remote} && "
            f"exec sshfs {safe_local} {safe_remote} -o slave"
        )
        try:
            self._chan = self._transport.open_session()
            self._chan.exec_command(remote_cmd)
        except Exception as exc:  # noqa: BLE001
            self._proc.terminate()
            raise ReverseMountError(f"Failed to open exec channel: {exc}") from exc

        # 3. Pump bytes: channel-stdout → sftp-server-stdin and sftp-server-stdout → channel-stdin.
        t1 = threading.Thread(
            target=_pump,
            args=(self._chan, self._proc.stdin.write),
            daemon=True,
            name="mount-chan-to-sftp",
        )
        t2 = threading.Thread(
            target=_pump,
            args=(self._proc.stdout, self._chan.sendall),
            daemon=True,
            name="mount-sftp-to-chan",
        )
        # Drain stderr so it doesn't block the process.
        t3 = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
            name="mount-sftp-stderr",
        )
        self._threads = [t1, t2, t3]
        for t in self._threads:
            t.start()

        # 4. Brief settle — sshfs needs ~300 ms to negotiate with sftp-server.
        time.sleep(0.5)

        if self._proc.poll() is not None:
            stderr = b""
            try:
                stderr = self._proc.stderr.read(2048)
            except Exception:  # noqa: BLE001
                pass
            raise ReverseMountError(
                f"sftp-server exited immediately.\n"
                f"stderr: {stderr.decode(errors='replace')}"
            )

        self._alive = True

    def stop(self) -> None:
        """Unmount on the remote and terminate the local sftp-server."""
        self._alive = False

        # Best-effort remote unmount (opens a fresh short-lived exec channel).
        try:
            safe_remote = _shell_remote_path(self._remote_dir)
            chan = self._transport.open_session()
            chan.settimeout(10)
            chan.exec_command(
                f"fusermount -u {safe_remote} 2>/dev/null "
                f"|| umount {safe_remote} 2>/dev/null || true"
            )
            chan.recv_exit_status()
            chan.close()
        except Exception:  # noqa: BLE001
            pass

        # Close the exec channel (stops sshfs on the remote).
        try:
            if self._chan:
                self._chan.close()
        except Exception:  # noqa: BLE001
            pass

        # Terminate the local sftp-server subprocess.
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass

    @property
    def is_alive(self) -> bool:
        """True while the mount is active (pump threads running, process alive)."""
        if not self._alive:
            return False
        if self._proc and self._proc.poll() is not None:
            self._alive = False
        return self._alive

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drain_stderr(self) -> None:
        try:
            for _ in self._proc.stderr:
                pass
        except Exception:  # noqa: BLE001
            pass
