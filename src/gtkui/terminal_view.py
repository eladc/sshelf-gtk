"""SSH terminal tab backed by VTE.

VTE does the VT100 emulation, scrollback, alt-screen, selection and reflow
that the Qt build hand-rolled on top of pyte, so this layer only has to move
bytes between the widget and an SSHSession and marshal callbacks onto the
GTK main loop.

    remote ──► SSHSession.on_data ──GLib.idle_add──► Vte.Terminal.feed()
    Vte "commit" signal ─────────────────────────► SSHSession.send()

Needs the GTK4 build of VTE (Vte-3.91, Debian: gir1.2-vte-3.91); the GTK3
build (Vte-2.91) cannot be embedded in a GTK4 window.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")
from gi.repository import GLib, Gdk, Gtk, Pango, Vte  # noqa: E402

from src.models.connection import Connection
from src.protocols.ssh_session import SSHSession
from src.storage.database import Database
from src.ui.themes import get_theme  # pure data, no Qt import

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

    Mirrors the contract MainWindow relies on: matches_conn(), shutdown(),
    and the status/health/closed callbacks.
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
        self._term.set_hexpand(True)
        self._term.set_vexpand(True)
        # No local PTY: bytes are fed in from the SSH channel instead.
        self._term.set_pty(None)
        self._term.connect("commit", self._on_commit)
        # GTK4 drops "size-allocate" for widgets; VTE reports grid changes
        # through these instead.
        self._term.connect("notify::column-count", self._on_grid_changed)
        self._term.connect("notify::row-count", self._on_grid_changed)
        self.apply_appearance()

        self._toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._toolbar.set_margin_top(4)
        self._toolbar.set_margin_start(6)
        self._toolbar.set_margin_end(6)
        self.append(self._toolbar)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._term)

        # Terminal on the left, side panels on the right.
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_vexpand(True)
        self._paned.set_start_child(scroller)
        self._paned.set_resize_start_child(True)

        self._panel_stack = Gtk.Stack()
        self._panel_stack.set_size_request(320, -1)
        self._panel_stack.set_visible(False)
        self._paned.set_end_child(self._panel_stack)
        self._paned.set_resize_end_child(False)
        self.append(self._paned)

        self._panels: dict[str, Gtk.Widget] = {}
        self._panel_buttons: dict[str, Gtk.ToggleButton] = {}
        self._build_panel_buttons()

        # Reconnect bar, hidden until the session drops
        self._bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._bar.set_margin_top(6)
        self._bar.set_margin_bottom(6)
        self._bar.set_margin_start(8)
        self._bar.set_margin_end(8)
        self._bar_label = Gtk.Label(xalign=0.0)
        self._bar_label.set_wrap(True)
        self._bar_label.set_hexpand(True)
        self._bar.append(self._bar_label)

        reconnect = Gtk.Button(label="Reconnect")
        reconnect.connect("clicked", self._on_reconnect)
        self._bar.append(reconnect)

        close = Gtk.Button(label="Close")
        close.connect("clicked", self._on_close_clicked)
        self._bar.append(close)

        self._bar.set_visible(False)
        self.append(self._bar)

    # ------------------------------------------------------------------
    # Appearance / side panels
    # ------------------------------------------------------------------

    def apply_appearance(self) -> None:
        """Apply the terminal theme and font size from preferences."""
        size = 11
        theme_name = ""
        if self._db is not None:
            try:
                size = int(self._db.get_pref("terminal_font_size", "13"))
                theme_name = self._db.get_pref("terminal_theme", "")
            except (TypeError, ValueError):
                pass

        self._term.set_font(Pango.FontDescription(f"Monospace {size}"))

        if theme_name:
            theme = get_theme(theme_name)
            palette = [
                theme.black, theme.red, theme.green, theme.yellow,
                theme.blue, theme.magenta, theme.cyan, theme.white,
                theme.bright_black, theme.bright_red, theme.bright_green,
                theme.bright_yellow, theme.bright_blue, theme.bright_magenta,
                theme.bright_cyan, theme.bright_white,
            ]
            self._term.set_colors(_rgba(theme.fg), _rgba(theme.bg),
                                  [_rgba(c) for c in palette])
            self._term.set_color_cursor(_rgba(theme.cursor))
        else:
            self._term.set_colors(_rgba("#d3d7cf"), _rgba("#1c1c1c"),
                                  [_rgba(c) for c in _PALETTE])

    def _enabled(self, key: str, default: str) -> bool:
        if self._db is None:
            return default == "1"
        return self._db.get_pref(key, default) == "1"

    def _build_panel_buttons(self) -> None:
        """One toggle per enabled side panel."""
        specs = [
            ("files", "folder-symbolic", "Files (SFTP)", "feature_sftp", "1"),
            ("commands", "media-playback-start-symbolic", "Commands",
             "feature_snippets", "1"),
            ("tunnels", "network-transmit-receive-symbolic", "Port forwarding",
             "feature_tunnels", "0"),
        ]
        for name, icon, tooltip, pref, default in specs:
            if not self._enabled(pref, default):
                continue
            button = Gtk.ToggleButton()
            button.set_icon_name(icon)
            button.set_tooltip_text(tooltip)
            button.set_has_frame(False)
            button.connect("toggled", self._on_panel_toggled, name)
            self._toolbar.append(button)
            self._panel_buttons[name] = button

    def _on_panel_toggled(self, button: Gtk.ToggleButton, name: str) -> None:
        if not button.get_active():
            if self._panel_stack.get_visible_child_name() == name:
                self._panel_stack.set_visible(False)
            return

        # Radio-like behaviour: only one panel open at a time.
        for other, other_button in self._panel_buttons.items():
            if other != name and other_button.get_active():
                other_button.set_active(False)

        self._ensure_panel(name)
        self._panel_stack.set_visible_child_name(name)
        self._panel_stack.set_visible(True)

    def _ensure_panel(self, name: str) -> None:
        if name in self._panels:
            return

        conn_id = self._conn.id
        if name == "files":
            from src.gtkui.sftp_panel import SFTPPanel
            panel = SFTPPanel()
            if self._connected and self._session is not None:
                panel.attach(self._session)
        elif name == "commands":
            from src.gtkui.snippets_panel import SnippetsPanel
            panel = SnippetsPanel(self._db, conn_id)
            panel.on_send = self._send_text
        else:
            from src.gtkui.tunnel_panel import TunnelPanel
            panel = TunnelPanel(self._db, conn_id)
            if self._connected and self._session is not None:
                panel.set_transport(self._session.get_transport())

        self._panels[name] = panel
        self._panel_stack.add_named(panel, name)

    def _send_text(self, text: str) -> None:
        if self._session is not None:
            self._session.send(text)

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
        panel = self._panels.get("tunnels")
        if panel is not None:
            panel.shutdown()
        panel = self._panels.get("files")
        if panel is not None:
            panel.detach()
        if self._session:
            self._session.disconnect()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def _start(self) -> None:
        cols = max(self._term.get_column_count(), 80)
        rows = max(self._term.get_row_count(), 24)
        self._sent_size = (cols, rows)
        self._bar.set_visible(False)

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

        panel = self._panels.get("files")
        if panel is not None:
            panel.attach(self._session)
        panel = self._panels.get("tunnels")
        if panel is not None:
            panel.set_transport(self._session.get_transport())
        return GLib.SOURCE_REMOVE

    def _ui_data(self, data: bytes) -> bool:
        self._term.feed(data)
        return GLib.SOURCE_REMOVE

    def _ui_error(self, msg: str) -> bool:
        self._term.feed(f"\r\n\x1b[31m{msg}\x1b[0m\r\n".encode())
        self._bar_label.set_text(msg)
        self._bar.set_visible(True)
        self.on_status(msg.splitlines()[0] if msg else "Connection failed")
        if self._conn.id is not None:
            self.on_health(self._conn.id, "error")
        return GLib.SOURCE_REMOVE

    def _ui_finished(self) -> bool:
        was_connected, self._connected = self._connected, False
        if self._conn.id is not None:
            self.on_health(self._conn.id, "disconnected")

        panel = self._panels.get("files")
        if panel is not None:
            panel.detach()
        panel = self._panels.get("tunnels")
        if panel is not None:
            panel.set_transport(None)
        if self._closed:
            return GLib.SOURCE_REMOVE
        if was_connected and not self._bar.get_visible():
            self._bar_label.set_text("Session closed.")
            self._bar.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _ui_host_key(self, hostname: str, keytype: str, fingerprint: str) -> bool:
        """Ask whether to trust a host key we have never seen before.

        GTK4 has no blocking dialog.run(); the session thread is already
        parked on its own event, so answering from the async callback is
        enough — and it keeps the UI responsive while the prompt is up.
        """
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(
            f"The authenticity of host '{hostname}' can't be established."
        )
        dialog.set_detail(
            f"{keytype} key fingerprint:\n{fingerprint}\n\n"
            "This is expected the first time you connect to this host. If you "
            "did not expect it, someone may be impersonating the host."
        )
        dialog.set_buttons(["Cancel", "Trust and connect"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(dlg, result) -> None:
            try:
                trusted = dlg.choose_finish(result) == 1
            except Exception:  # noqa: BLE001 — dialog dismissed
                trusted = False
            if self._session:
                self._session.answer_host_key(trusted)

        dialog.choose(self._window, None, answered)
        return GLib.SOURCE_REMOVE

    # ── widget events ─────────────────────────────────────────────────────

    def _on_commit(self, _term, text: str, _size: int) -> None:
        """Keystrokes typed into the terminal go to the remote shell."""
        if self._session:
            self._session.send(text)

    def _on_grid_changed(self, *_args) -> None:
        """Keep the remote PTY in sync with the terminal's grid size."""
        cols = self._term.get_column_count()
        rows = self._term.get_row_count()
        if (cols, rows) != self._sent_size and cols > 0 and rows > 0:
            self._sent_size = (cols, rows)
            if self._session:
                self._session.resize(cols, rows)

    def _on_reconnect(self, _button) -> None:
        self._term.reset(True, True)
        self._start()

    def _on_close_clicked(self, _button) -> None:
        self.shutdown()
        self.on_closed(self)
