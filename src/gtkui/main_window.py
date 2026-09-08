"""GTK main window — connection tree beside a notebook of session tabs."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

from src.gtkui.connection_tree import ConnectionTree
from src.gtkui.terminal_view import TerminalView
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
        self.show_all()
        self._status.set_text("Ready.")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(self.get_title())
        self.set_titlebar(header)

        new_btn = Gtk.Button.new_from_icon_name("list-add", Gtk.IconSize.BUTTON)
        new_btn.set_tooltip_text("New connection")
        new_btn.connect("clicked", self._on_new_connection)
        header.pack_start(new_btn)

        self._search = Gtk.SearchEntry()
        self._search.set_placeholder_text("Search connections…")
        self._search.set_width_chars(24)
        self._search.connect("search-changed",
                             lambda e: self._tree.filter(e.get_text()))
        header.pack_start(self._search)

        self._quick = Gtk.Entry()
        self._quick.set_placeholder_text("user@host:port")
        self._quick.set_width_chars(22)
        self._quick.connect("activate", self._on_quick_connect)
        header.pack_end(self._quick)

    def _build_body(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.set_position(_TREE_WIDTH)
        root.pack_start(self._paned, True, True, 0)

        self._tree = ConnectionTree(self.db)
        self._tree.on_activated = self._open_session
        self._tree.on_selected = self._on_connection_selected
        self._tree.on_cleared = lambda: self._status.set_text("Ready.")
        self._paned.pack1(self._tree, False, False)

        self._notebook = Gtk.Notebook()
        self._notebook.set_scrollable(True)
        self._notebook.connect("switch-page", self._on_switch_page)
        self._paned.pack2(self._notebook, True, False)

        self._add_home_tab()

        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.set_border_width(4)
        self._status = Gtk.Label(label="")
        self._status.set_xalign(0.0)
        self._status.set_ellipsize(3)  # Pango.EllipsizeMode.END
        status_bar.pack_start(self._status, True, True, 0)
        root.pack_start(status_bar, False, False, 0)

    def _add_home_tab(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(24)
        box.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>sshelf</span>")
        box.pack_start(title, False, False, 0)

        hint = Gtk.Label(label="Pick a connection on the left, or press Enter "
                               "in the quick-connect box to start a session.")
        hint.set_line_wrap(True)
        hint.set_justify(Gtk.Justification.CENTER)
        box.pack_start(hint, False, False, 0)

        box.show_all()
        self._notebook.append_page(box, Gtk.Label(label="Home"))

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def set_status(self, msg: str) -> None:
        self._status.set_text(msg)

    def _open_session(self, conn: Connection) -> None:
        """Open a tab for *conn*, or focus the existing one."""
        if conn.protocol not in ("ssh", "", None):
            self._error_dialog(
                f"{(conn.protocol or '').upper()} is not available yet",
                "The GTK port currently supports SSH sessions. RDP and VNC "
                "are still on the Qt build.",
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

    def _tab_label(self, conn: Connection, view: TerminalView) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.pack_start(Gtk.Label(label=conn.display_name()), True, True, 0)

        close = Gtk.Button.new_from_icon_name("window-close", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_: self._close_session_tab(view))
        box.pack_start(close, False, False, 0)

        box.show_all()
        return box

    def _close_session_tab(self, view: TerminalView) -> None:
        idx = self._notebook.page_num(view)
        if idx < 0:
            return
        view.shutdown()
        self._notebook.remove_page(idx)
        view.destroy()
        self.set_status("Session closed.")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_connection_selected(self, conn: Connection) -> None:
        self.set_status(f"{conn.display_name()} — {conn.connection_string()}")

    def _on_switch_page(self, _nb, page, _num) -> None:
        if isinstance(page, TerminalView):
            self.set_status(page.connection.connection_string())

    def _on_new_connection(self, _btn) -> None:
        self._error_dialog(
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

    def _error_dialog(self, text: str, secondary: str) -> None:
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK, text=text,
        )
        dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def do_delete_event(self, _event) -> bool:
        """Close every live session before the window goes away."""
        for i in range(self._notebook.get_n_pages()):
            page = self._notebook.get_nth_page(i)
            if isinstance(page, TerminalView):
                page.shutdown()
        return False
