"""GTK main window — connection tree beside a notebook of session tabs."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from src.gtkui.connection_tree import ConnectionTree
from src.models.connection import Connection
from src.storage.database import Database

_TREE_WIDTH = 320


class MainWindow(Gtk.ApplicationWindow):
    """Top-level window: header bar, connection tree, session notebook."""

    def __init__(self, app: Gtk.Application, db: Database,
                 name: str | None = None) -> None:
        title = f"sshelf — {name}" if name else "sshelf"
        super().__init__(application=app, title=title)
        self.db = db
        self.set_default_size(1200, 760)

        self._build_header()
        self._build_body()
        self.connect("close-request", self._on_close_request)
        self._status.set_text("Ready.")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)

        new_btn = Gtk.Button.new_from_icon_name("list-add")
        new_btn.set_tooltip_text("New connection")
        new_btn.connect("clicked", self._on_new_connection)
        header.pack_start(new_btn)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search connections…")
        self._search.set_max_width_chars(24)
        self._search.connect("search-changed",
                             lambda e: self._tree.filter(e.get_text()))
        header.pack_start(self._search)

        self._quick = Gtk.Entry()
        self._quick.set_placeholder_text("user@host:port")
        self._quick.set_max_width_chars(22)
        self._quick.connect("activate", self._on_quick_connect)
        header.pack_end(self._quick)

    def _build_body(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(_TREE_WIDTH)
        self._paned.set_vexpand(True)
        root.append(self._paned)

        self._tree = ConnectionTree(self.db)
        self._tree.on_activated = self._open_session
        self._tree.on_selected = self._on_connection_selected
        self._tree.on_cleared = lambda: self._status.set_text("Ready.")
        self._paned.set_start_child(self._tree)
        self._paned.set_resize_start_child(False)
        self._paned.set_shrink_start_child(False)

        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.set_hexpand(True)
        self._notebook.connect("switch-page", self._on_switch_page)
        self._paned.set_end_child(self._notebook)
        self._paned.set_resize_end_child(True)

        self._add_home_tab()

        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.set_margin_top(4)
        status_bar.set_margin_bottom(4)
        status_bar.set_margin_start(8)
        status_bar.set_margin_end(8)
        self._status = Gtk.Label(xalign=0.0)
        self._status.set_ellipsize(Pango.EllipsizeMode.END)
        self._status.set_hexpand(True)
        status_bar.append(self._status)
        root.append(status_bar)

    def _add_home_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>sshelf</span>")
        box.append(title)

        hint = Gtk.Label(label="Pick a connection on the left, or press Enter "
                               "in the quick-connect box to start a session.")
        hint.set_wrap(True)
        hint.set_justify(Gtk.Justification.CENTER)
        box.append(hint)

        self._notebook.append_page(box, Gtk.Label(label="Home"))

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def set_status(self, msg: str) -> None:
        self._status.set_text(msg)

    def _open_session(self, conn: Connection) -> None:
        """Open a tab for *conn*, or focus the existing one."""
        if conn.protocol not in ("ssh", "", None):
            self._info_dialog(
                f"{(conn.protocol or '').upper()} is not available yet",
                "The GTK port currently supports SSH sessions. RDP and VNC "
                "are still on the Qt build.",
            )
            return

        # Imported lazily so the rest of the app still runs when the GTK4
        # build of VTE (gir1.2-vte-3.91) is not installed.
        try:
            from src.gtkui.terminal_view import TerminalView
        except (ImportError, ValueError) as exc:
            self._info_dialog(
                "Terminal support unavailable",
                "The GTK4 build of VTE is required for terminal sessions.\n\n"
                "Install it with:\n    sudo apt install gir1.2-vte-3.91\n\n"
                f"({exc})",
            )
            return

        for i in range(1, self._notebook.get_n_pages()):
            page = self._notebook.get_nth_page(i)
            if isinstance(page, TerminalView) and page.matches_conn(conn):
                self._notebook.set_current_page(i)
                page.focus_terminal()
                return

        view = TerminalView(conn, self.db, self)
        view.on_status = self.set_status
        view.on_health = self._tree.set_health
        view.on_closed = self._close_session_tab

        idx = self._notebook.append_page(view, self._tab_label(conn, view))
        self._notebook.set_tab_reorderable(view, True)
        self._notebook.set_current_page(idx)
        view.focus_terminal()

    def _tab_label(self, conn: Connection, view) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(Gtk.Label(label=conn.display_name()))

        close = Gtk.Button.new_from_icon_name("window-close")
        close.set_has_frame(False)
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_: self._close_session_tab(view))
        box.append(close)

        return box

    def _close_session_tab(self, view) -> None:
        idx = self._notebook.page_num(view)
        if idx < 0:
            return
        view.shutdown()
        self._notebook.remove_page(idx)
        self.set_status("Session closed.")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_connection_selected(self, conn: Connection) -> None:
        self.set_status(f"{conn.display_name()} — {conn.connection_string()}")

    def _on_switch_page(self, _nb, page, _num) -> None:
        if hasattr(page, "connection"):
            self.set_status(page.connection.connection_string())

    def _on_new_connection(self, _btn) -> None:
        self._info_dialog(
            "Connection editor not ported yet",
            "Add connections with the CLI (sshelf add) or the Qt build for "
            "now — the GTK dialog lands in the next phase.",
        )

    def _on_quick_connect(self, entry) -> None:
        text = entry.get_text().strip()
        if not text:
            return
        self._open_session(self._parse_quick_connect(text))
        entry.set_text("")

    @staticmethod
    def _parse_quick_connect(text: str) -> Connection:
        """Parse 'user@host:port' (optionally scheme-prefixed) into a Connection."""
        conn = Connection()
        for scheme in ("ssh://", "rdp://", "vnc://"):
            if text.lower().startswith(scheme):
                conn.protocol = scheme[:3]
                text = text[len(scheme):]
                break
        if "@" in text:
            conn.username, text = text.split("@", 1)
        if ":" in text:
            host, port_str = text.rsplit(":", 1)
            try:
                conn.host, conn.port = host, int(port_str)
            except ValueError:
                conn.host = text
        else:
            conn.host = text
        conn.name = conn.host
        return conn

    def _info_dialog(self, message: str, detail: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(message)
        dialog.set_detail(detail)
        dialog.set_buttons(["OK"])
        dialog.show(self)

    def _on_close_request(self, _window) -> bool:
        """Close every live session before the window goes away."""
        for i in range(self._notebook.get_n_pages()):
            page = self._notebook.get_nth_page(i)
            if hasattr(page, "shutdown"):
                page.shutdown()
        return False
