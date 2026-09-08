"""Two-level connection tree (group → connection) on Gtk.TreeView."""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango  # noqa: E402

from src.models.connection import Connection
from src.storage.database import Database

# Model columns
COL_LABEL, COL_HOST, COL_CONN, COL_WEIGHT, COL_COLOR, COL_ICON = range(6)

_PROTO_ICON = {"rdp": "computer", "vnc": "network-wired"}
_DEFAULT_ICON = "utilities-terminal"

_HEALTH_ICON = {
    "connected": "media-record",
    "error": "dialog-error",
}


class ConnectionTree(Gtk.Box):
    """Connection list grouped by Connection.group.

    Callbacks (assigned by the owner, kept plain rather than GObject signals
    so the UI layer stays easy to rewire):
        on_selected(Connection)   selection moved to a connection
        on_activated(Connection)  double-click / Enter
        on_cleared()              selection is not a connection
    """

    def __init__(self, db: Database) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.db = db
        self._filter_text = ""
        self._connections: list[Connection] = []
        self._health: dict[int, str] = {}

        self.on_selected = lambda conn: None
        self.on_activated = lambda conn: None
        self.on_cleared = lambda: None

        self._first_load = True
        # label, host, Connection, weight, colour, icon-name
        self._store = Gtk.TreeStore(str, str, object, int, str, str)

        self._view = Gtk.TreeView(model=self._store)
        self._view.set_headers_visible(True)
        self._view.set_enable_tree_lines(False)
        self._view.set_tooltip_column(-1)

        name_col = Gtk.TreeViewColumn("Name")
        icon_cell = Gtk.CellRendererPixbuf()
        name_col.pack_start(icon_cell, False)
        name_col.add_attribute(icon_cell, "icon-name", COL_ICON)
        # No ellipsize here: it would drive the column's natural width to
        # near-zero and collapse the Name column.
        text_cell = Gtk.CellRendererText()
        name_col.pack_start(text_cell, True)
        name_col.add_attribute(text_cell, "text", COL_LABEL)
        name_col.add_attribute(text_cell, "weight", COL_WEIGHT)
        name_col.add_attribute(text_cell, "foreground", COL_COLOR)
        name_col.set_expand(False)
        self._view.append_column(name_col)

        host_cell = Gtk.CellRendererText()
        host_cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        host_col = Gtk.TreeViewColumn("Host", host_cell, text=COL_HOST)
        host_col.set_resizable(True)
        host_col.set_expand(True)
        self._view.append_column(host_col)

        self._view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self._view.get_selection().connect("changed", self._on_selection_changed)
        self._view.connect("row-activated", self._on_row_activated)
        self._view.connect("button-press-event", self._on_button_press)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self._view)
        self.pack_start(scroller, True, True, 0)

        self.reload()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reload(self) -> None:
        self._connections = self.db.all_connections()
        self._repopulate()

    def filter(self, text: str) -> None:
        self._filter_text = text.lower().strip()
        self._repopulate()

    def selected_connection(self) -> Optional[Connection]:
        model, it = self._view.get_selection().get_selected()
        if it is None:
            return None
        return model.get_value(it, COL_CONN)

    def set_health(self, conn_id: int, status: str) -> None:
        """Update the live status icon for a connection, in place."""
        self._health[conn_id] = status

        def visit(store, _path, it):
            conn = store.get_value(it, COL_CONN)
            if conn is not None and conn.id == conn_id:
                store.set_value(it, COL_ICON, self._icon_for(conn, status))
            return False

        self._store.foreach(visit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _icon_for(self, conn: Connection, health: str | None = None) -> str:
        if health is None and conn.id is not None:
            health = self._health.get(conn.id)
        if health in _HEALTH_ICON:
            return _HEALTH_ICON[health]
        return _PROTO_ICON.get(conn.protocol or "ssh", _DEFAULT_ICON)

    def _expanded_groups(self) -> set[str]:
        expanded: set[str] = set()
        it = self._store.get_iter_first()
        while it is not None:
            if self._view.row_expanded(self._store.get_path(it)):
                expanded.add(self._store.get_value(it, COL_LABEL))
            it = self._store.iter_next(it)
        return expanded

    def _repopulate(self) -> None:
        expanded = self._expanded_groups()
        self._store.clear()

        conns = self._connections
        if self._filter_text:
            conns = [
                c for c in conns
                if self._filter_text in c.display_name().lower()
                or self._filter_text in c.host.lower()
            ]

        groups: dict[str, Gtk.TreeIter] = {}
        for conn in conns:
            group_name = conn.group or "Default"
            if group_name not in groups:
                groups[group_name] = self._store.append(
                    None,
                    [group_name, "", None, Pango.Weight.BOLD, None, None],
                )
            self._store.append(
                groups[group_name],
                [
                    conn.display_name(),
                    conn.connection_string(),
                    conn,
                    Pango.Weight.NORMAL,
                    # None means "theme default"; "" is not a valid colour.
                    conn.color or None,
                    self._icon_for(conn),
                ],
            )

        # Filtering reveals everything, as does the first load so the list
        # isn't just a row of collapsed group headers.
        if self._filter_text or len(groups) == 1 or self._first_load:
            self._view.expand_all()
            self._first_load = False
        else:
            it = self._store.get_iter_first()
            while it is not None:
                if self._store.get_value(it, COL_LABEL) in expanded:
                    self._view.expand_row(self._store.get_path(it), False)
                it = self._store.iter_next(it)

    # ── events ────────────────────────────────────────────────────────────

    def _on_selection_changed(self, _selection) -> None:
        conn = self.selected_connection()
        if conn is not None:
            self.on_selected(conn)
        else:
            self.on_cleared()

    def _on_row_activated(self, view, path, _column) -> None:
        conn = self._store.get_value(self._store.get_iter(path), COL_CONN)
        if conn is not None:
            self.on_activated(conn)
        elif view.row_expanded(path):
            view.collapse_row(path)
        else:
            view.expand_row(path, False)

    def _on_button_press(self, view, event) -> bool:
        if event.button != Gdk.BUTTON_SECONDARY:
            return False
        hit = view.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        path = hit[0]
        view.get_selection().select_path(path)
        conn = self._store.get_value(self._store.get_iter(path), COL_CONN)
        if conn is None:
            return False
        self._show_context_menu(conn, event)
        return True

    def _show_context_menu(self, conn: Connection, event) -> None:
        menu = Gtk.Menu()
        for label, handler in (
            ("Connect", lambda *_: self.on_activated(conn)),
            ("Duplicate", lambda *_: self._duplicate(conn)),
            ("Delete", lambda *_: self._delete(conn)),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", handler)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _duplicate(self, conn: Connection) -> None:
        clone = Connection.from_dict(conn.to_dict())
        clone.id = None
        clone.name = f"{conn.display_name()} (copy)"
        self.db.save_connection(clone)
        self.reload()

    def _delete(self, conn: Connection) -> None:
        if conn.id is None:
            return
        dlg = Gtk.MessageDialog(
            transient_for=self.get_toplevel(),
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=f"Delete connection «{conn.display_name()}»?",
        )
        dlg.format_secondary_text("This cannot be undone.")
        confirmed = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        if confirmed:
            self.db.delete_connection(conn.id)
            self.reload()
            self.on_cleared()
