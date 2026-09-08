"""SSH terminal tab backed by VTE.

VTE does the VT100 emulation, scrollback, alt-screen, selection and reflow
that the Qt build hand-rolled on top of pyte, so this layer only has to move
bytes between the widget and an SSHSession and marshal callbacks onto the
GTK main loop.

    remote ──► SSHSession.on_data ──GLib.idle_add──► Vte.Terminal.feed()
    Vte "commit" signal ─────────────────────────► SSHSession.send()
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gdk, Gtk, Pango, Vte  # noqa: E402

from src.models.connection import Connection
from src.protocols.ssh_session import SSHSession
from src.storage.database import Database

_SCROLLBACK = 10000


def _rgba(spec: str) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.parse(spec)
    return c


# xterm-compatible 16-colour palette (matches the Qt build's defaults)
_PALETTE = [
    "#2e3436", "#cc0000", "#4e9a06", "#c4a000",
    "#3465a4", "#75507b", "#06989a", "#d3d7cf",
    "#555753", "#ef2929", "#8ae234", "#fce94f",
    "#729fcf", "#ad7fa8", "#34e2e2", "#eeeeec",
]


class TerminalView(Gtk.Box):
    """One SSH session rendered in a VTE terminal.

    Mirrors the Qt SplitView/TerminalWidget contract that MainWindow relies
    on: matches_conn(), shutdown(), and the status/health/closed callbacks.
    """

    def __init__(self, conn: Connection, db: Database, window: Gtk.Window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._conn = conn
        self._db = db
        self._window = window
        self._session: Optional[SSHSession] = None
        self._connected = False
        self._closed = False
        self._sent_size = (0, 0)

        # Callbacks wired by MainWindow (kept plain to stay toolkit-agnostic)
        self.on_status = lambda msg: None
        self.on_health = lambda conn_id, status: None
        self.on_closed = lambda view: None

        self._build_ui()
        self._start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._term = Vte.Terminal()
        self._term.set_scrollback_lines(_SCROLLBACK)
        self._term.set_mouse_autohide(True)
        self._term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self._term.set_font(Pango.FontDescription("Monospace 11"))
        self._term.set_colors(_rgba("#d3d7cf"), _rgba("#1c1c1c"),
                              [_rgba(c) for c in _PALETTE])
        # No local PTY: bytes are fed in from the SSH channel instead.
        self._term.set_pty(None)
        self._term.connect("commit", self._on_commit)
        self._term.connect("size-allocate", self._on_size_allocate)
        self._term.connect("child-exited", lambda *_: None)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._term)
        self.pack_start(scroller, True, True, 0)

        # Reconnect bar, hidden until the session drops
        self._bar = Gtk.InfoBar()
        self._bar.set_message_type(Gtk.MessageType.WARNING)
        self._bar_label = Gtk.Label(label="")
        self._bar_label.set_line_wrap(True)
        self._bar_label.set_xalign(0.0)
        self._bar.get_content_area().add(self._bar_label)
        self._bar.add_button("Reconnect", Gtk.ResponseType.OK)
        self._bar.add_button("Close", Gtk.ResponseType.CLOSE)
        self._bar.connect("response", self._on_bar_response)
        self._bar.set_no_show_all(True)
        self.pack_start(self._bar, False, False, 0)

        self.show_all()
        self._bar.hide()

    # ------------------------------------------------------------------
    # Public API (contract used by MainWindow)
    # ------------------------------------------------------------------

    def matches_conn(self, conn: Connection) -> bool:
        return (
            self._conn.id is not None
            and conn.id is not None
            and self._conn.id == conn.id
        )

    @property
    def connection(self) -> Connection:
        return self._conn

    def focus_terminal(self) -> None:
        self._term.grab_focus()

    def shutdown(self) -> None:
        """Close the session without emitting on_closed (tab is going away)."""
        self._closed = True
        if self._session:
            self._session.disconnect()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _start(self) -> None:
        cols = max(self._term.get_column_count(), 80)
        rows = max(self._term.get_row_count(), 24)
        self._sent_size = (cols, rows)
        self._bar.hide()

        self._session = SSHSession(
            self._conn,
            on_connected=lambda: GLib.idle_add(self._ui_connected),
            on_data=lambda data: GLib.idle_add(self._ui_data, data),
            on_error=lambda msg: GLib.idle_add(self._ui_error, msg),
            on_finished=lambda: GLib.idle_add(self._ui_finished),
            on_host_key=lambda h, k, f: GLib.idle_add(self._ui_host_key, h, k, f),
            cols=cols,
            rows=rows,
        )
        self._session.start()
        self.on_status(f"Connecting to {self._conn.connection_string()}…")

    # ── callbacks, all already marshalled onto the GTK main loop ──────────

    def _ui_connected(self) -> bool:
        self._connected = True
        self.on_status(f"Connected to {self._conn.connection_string()}")
        if self._conn.id is not None:
            self.on_health(self._conn.id, "connected")
        self._term.grab_focus()
        return GLib.SOURCE_REMOVE

    def _ui_data(self, data: bytes) -> bool:
        self._term.feed(data)
        return GLib.SOURCE_REMOVE

    def _ui_error(self, msg: str) -> bool:
        self._term.feed(f"\r\n\x1b[31m{msg}\x1b[0m\r\n".encode())
        self._bar_label.set_text(msg)
        self._bar.show()
        self.on_status(msg.splitlines()[0] if msg else "Connection failed")
        if self._conn.id is not None:
            self.on_health(self._conn.id, "error")
        return GLib.SOURCE_REMOVE

    def _ui_finished(self) -> bool:
        was_connected, self._connected = self._connected, False
        if self._conn.id is not None:
            self.on_health(self._conn.id, "disconnected")
        if self._closed:
            return GLib.SOURCE_REMOVE
        if was_connected and not self._bar.get_visible():
            self._bar_label.set_text("Session closed.")
            self._bar.show()
        return GLib.SOURCE_REMOVE

    def _ui_host_key(self, hostname: str, keytype: str, fingerprint: str) -> bool:
        """Ask whether to trust a host key we have never seen before."""
        dlg = Gtk.MessageDialog(
            transient_for=self._window,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            text=f"The authenticity of host '{hostname}' can't be established.",
        )
        dlg.format_secondary_text(
            f"{keytype} key fingerprint:\n{fingerprint}\n\n"
            "This is expected the first time you connect to this host. If you "
            "did not expect it, someone may be impersonating the host."
        )
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Trust and connect", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        trusted = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if self._session:
            self._session.answer_host_key(trusted)
        return GLib.SOURCE_REMOVE

    # ── widget events ─────────────────────────────────────────────────────

    def _on_commit(self, _term, text: str, _size: int) -> None:
        """Keystrokes typed into the terminal go to the remote shell."""
        if self._session:
            self._session.send(text)

    def _on_size_allocate(self, _term, _alloc) -> None:
        """Keep the remote PTY in sync with the widget size."""
        cols = self._term.get_column_count()
        rows = self._term.get_row_count()
        if (cols, rows) != self._sent_size and cols > 0 and rows > 0:
            self._sent_size = (cols, rows)
            if self._session:
                self._session.resize(cols, rows)

    def _on_bar_response(self, _bar, response: int) -> None:
        if response == Gtk.ResponseType.OK:
            self._term.reset(True, True)
            self._start()
        else:
            self.shutdown()
            self.on_closed(self)
