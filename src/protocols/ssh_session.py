"""Toolkit-neutral threaded SSH session.

Wraps ssh_core in a background thread and reports everything through plain
callbacks, so a UI layer only has to marshal those onto its own main loop
(GLib.idle_add for GTK). Nothing here imports a GUI toolkit.

Callbacks are invoked from the session thread, never from the caller's.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Optional

import paramiko

from src.models.connection import Connection
from src.protocols import ssh_core


class SSHSession:
    """A paramiko shell session running on its own thread.

    Parameters
    ----------
    on_connected()          shell channel is open
    on_data(bytes)          raw bytes from the remote shell
    on_error(str)           connection / auth failure message
    on_finished()           session fully closed
    on_host_key(host, keytype, fingerprint)
        An unseen host key needs approval. The session thread blocks until
        answer_host_key() is called, so the UI can prompt at its leisure.
        If omitted, unknown hosts are refused (see ssh_core.build_client).
    """

    def __init__(
        self,
        conn: Connection,
        *,
        on_connected: Callable[[], None],
        on_data: Callable[[bytes], None],
        on_error: Callable[[str], None],
        on_finished: Callable[[], None],
        on_host_key: Optional[Callable[[str, str, str], None]] = None,
        term: str = "xterm-256color",
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self._conn = conn
        self._on_connected = on_connected
        self._on_data = on_data
        self._on_error = on_error
        self._on_finished = on_finished
        self._on_host_key = on_host_key
        self._term = term
        self._cols = cols
        self._rows = rows

        self._client: Optional[paramiko.SSHClient] = None
        self._channel: Optional[paramiko.Channel] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._host_key_answered = threading.Event()
        self._host_key_trusted = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin connecting on a background thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"ssh-{self._conn.host}"
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            self._client = ssh_core.build_client(self._confirm_host_key)
            self._client.connect(
                **ssh_core.connect_kwargs(self._conn, self._confirm_host_key)
            )
            self._channel = self._client.invoke_shell(
                term=self._term, width=self._cols, height=self._rows
            )
            self._channel.setblocking(False)
            self._running = True
            self._on_connected()

            if self._conn.startup_command:
                self.send(self._conn.startup_command + "\n")

            self._read_loop()

        except paramiko.BadHostKeyException as exc:
            self._on_error(
                f"WARNING: the host key for {exc.hostname} has changed.\n"
                f"Expected {ssh_core.fingerprint(exc.expected_key)}, "
                f"got {ssh_core.fingerprint(exc.key)}.\n"
                "Someone may be intercepting this connection, or the host was "
                "rebuilt. If the change is legitimate, remove the old entry "
                f"with:  ssh-keygen -R '{exc.hostname}'"
            )
        except ssh_core.UnknownHostKeyError as exc:
            self._on_error(str(exc))
        except paramiko.AuthenticationException as exc:
            self._on_error(f"Authentication failed: {exc}")
        except paramiko.SSHException as exc:
            self._on_error(f"SSH error: {exc}")
        except socket.gaierror as exc:
            self._on_error(f"Cannot resolve host '{self._conn.host}': {exc}")
        except OSError as exc:
            self._on_error(f"Network error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._on_error(f"Unexpected error: {exc}")
        finally:
            self._cleanup()
            self._on_finished()

    def _read_loop(self) -> None:
        while self._running:
            if self._channel.closed or self._channel.eof_received:
                break
            try:
                chunk = self._channel.recv(4096)
                if chunk:
                    self._on_data(chunk)
                else:
                    time.sleep(0.02)
            except socket.timeout:
                time.sleep(0.02)
            except OSError:
                break

    def disconnect(self) -> None:
        """Stop the session. Safe to call from any thread, more than once."""
        self._running = False
        # Release a pending host key prompt so the thread cannot hang on it.
        self.answer_host_key(False)
        self._cleanup()

    def _cleanup(self) -> None:
        self._running = False
        for obj in (self._channel, self._client):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def send(self, data: str | bytes) -> None:
        """Send bytes (or a UTF-8 string) to the remote shell."""
        if not self._channel or not self._running:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        try:
            self._channel.sendall(data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Tell the remote PTY the window size changed."""
        self._cols, self._rows = cols, rows
        if self._channel and self._running:
            try:
                self._channel.resize_pty(width=cols, height=rows)
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._running

    def get_transport(self):
        """Underlying paramiko Transport, or None (used by tunnels/SFTP)."""
        return self._client.get_transport() if self._client else None

    def open_sftp(self):
        """Open an SFTP client on the existing connection, or None."""
        if not self._client:
            return None
        try:
            return self._client.open_sftp()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Host key confirmation (session thread asks, UI thread answers)
    # ------------------------------------------------------------------

    def _confirm_host_key(self, hostname: str, keytype: str, fingerprint: str) -> bool:
        """Block the session thread until the UI approves an unseen host key."""
        if self._on_host_key is None:
            return False
        self._host_key_trusted = False
        self._host_key_answered.clear()
        self._on_host_key(hostname, keytype, fingerprint)
        self._host_key_answered.wait()
        return self._host_key_trusted

    def answer_host_key(self, trusted: bool) -> None:
        """Deliver the user's answer. Safe to call from the UI thread."""
        self._host_key_trusted = trusted
        self._host_key_answered.set()
