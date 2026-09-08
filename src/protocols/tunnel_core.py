"""Port-forwarding workers — toolkit-neutral.

Same forwarding logic as the Qt tunnel_worker, with the two Qt signals
replaced by an optional on_error callback so the GTK UI (and anything else)
can drive them. Callbacks fire on the worker thread.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable, Optional

from src.models.tunnel import Tunnel


def _pipe(src, dst) -> None:
    """Copy data from src to dst until either side closes."""
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass


class _BaseTunnel:
    def __init__(
        self,
        transport,
        tunnel: Tunnel,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._transport = transport
        self._tunnel = tunnel
        self._on_error = on_error
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def tunnel(self) -> Tunnel:
        return self._tunnel

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"tunnel-{self._tunnel.label}"
        )
        self._thread.start()

    def _fail(self, message: str) -> None:
        self._running = False
        if self._on_error is not None:
            self._on_error(message)

    def _run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class LocalTunnelWorker(_BaseTunnel):
    """Listen on localhost:<local_port>, forward to <remote_host>:<remote_port>
    through the SSH transport via a direct-tcpip channel."""

    def __init__(self, transport, tunnel: Tunnel,
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(transport, tunnel, on_error)
        self._server: Optional[socket.socket] = None

    def stop(self) -> None:
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def _run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.settimeout(1.0)
            srv.bind(("127.0.0.1", self._tunnel.local_port))
            srv.listen(10)
            self._server = srv
        except OSError as exc:
            self._fail(f"Cannot bind port {self._tunnel.local_port}: {exc}")
            return

        while self._running:
            try:
                client, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(client,), daemon=True
            ).start()

        self._running = False

    def _handle_client(self, client: socket.socket) -> None:
        try:
            peer = client.getpeername()
            chan = self._transport.open_channel(
                "direct-tcpip",
                (self._tunnel.remote_host, self._tunnel.remote_port),
                peer,
            )
        except Exception:  # noqa: BLE001
            client.close()
            return

        if chan is None:
            client.close()
            return

        t1 = threading.Thread(target=_pipe, args=(client, chan), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(chan, client), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        for closeable in (chan, client):
            try:
                closeable.close()
            except Exception:  # noqa: BLE001
                pass


class RemoteTunnelWorker(_BaseTunnel):
    """Ask the SSH server to listen on <remote_port> and forward incoming
    connections back to <remote_host>:<local_port> on this machine."""

    def stop(self) -> None:
        self._running = False
        try:
            self._transport.cancel_port_forward("", self._tunnel.remote_port)
        except Exception:  # noqa: BLE001
            pass

    def _run(self) -> None:
        try:
            self._transport.request_port_forward("", self._tunnel.remote_port)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Remote forward failed: {exc}")
            return

        while self._running:
            chan = self._transport.accept(timeout=1.0)
            if chan is None:
                continue
            threading.Thread(
                target=self._handle_channel, args=(chan,), daemon=True
            ).start()

        self._running = False

    def _handle_channel(self, chan) -> None:
        try:
            sock = socket.create_connection(
                (self._tunnel.remote_host, self._tunnel.local_port)
            )
        except OSError:
            chan.close()
            return
        t1 = threading.Thread(target=_pipe, args=(chan, sock), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(sock, chan), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            sock.close()
        except OSError:
            pass


def build_worker(
    transport,
    tunnel: Tunnel,
    on_error: Optional[Callable[[str], None]] = None,
) -> _BaseTunnel:
    """Return the right worker for *tunnel*'s direction."""
    cls = LocalTunnelWorker if tunnel.type == "local" else RemoteTunnelWorker
    return cls(transport, tunnel, on_error)
