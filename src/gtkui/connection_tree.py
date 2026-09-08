"""Two-level connection tree (group → connection) on Gtk.ColumnView.

GTK4 retires Gtk.TreeView/TreeStore, so rows are plain GObjects held in nested
Gio.ListStores, wrapped by a Gtk.TreeListModel that supplies the expander rows
and rendered through per-column signal factories.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GObject, Gtk, Pango  # noqa: E402

from src.models.connection import Connection
from src.storage.database import Database

# Symbolic variants: GTK4 recolours these to follow the text colour, so they
# stay legible whatever the icon theme's own palette is.
_PROTO_ICON = {"rdp": "computer-symbolic", "vnc": "network-wired-symbolic"}
_DEFAULT_ICON = "utilities-terminal-symbolic"

_HEALTH_ICON = {
    "connected": "media-record-symbolic",
    "error": "dialog-error-symbolic",
}

_BOLD = Pango.AttrList()
_BOLD.insert(Pango.attr_weight_new(Pango.Weight.BOLD))


def _colour_attrs(colour: str) -> Optional[Pango.AttrList]:
    """Pango attributes tinting text with *colour*, or None if unusable."""
    rgba = Gdk.RGBA()
    if not colour or not rgba.parse(colour):
        return None
    attrs = Pango.AttrList()
    attrs.insert(
        Pango.attr_foreground_new(
            int(rgba.red * 65535), int(rgba.green * 65535), int(rgba.blue * 65535)
        )
    )
    return attrs


class _Row(GObject.Object):
    """One row: a group header (conn is None) or a connection."""

    __gtype_name__ = "SshelfConnectionRow"

    def __init__(
        self,
        label: str,
        host: str = "",
        conn: Optional[Connection] = None,
        children: Optional[Gio.ListStore] = None,
    ) -> None:
        super().__init__()
        self.label = label
        self.host = host
        self.conn = conn
        self.children = children

    @property
    def is_group(self) -> bool:
        return self.conn is None


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
        self._menu_target: Optional[Connection] = None

        self.on_selected = lambda conn: None
        self.on_activated = lambda conn: None
        self.on_cleared = lambda: None

        self._root = Gio.ListStore.new(_Row)
        self._tree_model = Gtk.TreeListModel.new(
            self._root, False, False, lambda row: row.children
        )
        self._selection = Gtk.SingleSelection(model=self._tree_model)
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("notify::selected-item", self._on_selection_changed)

        self._view = Gtk.ColumnView(model=self._selection)
        self._view.set_single_click_activate(False)
        self._view.connect("activate", self._on_activate)
        self._view.append_column(self._build_name_column())
        self._view.append_column(self._build_host_column())

        self._menu = Gtk.PopoverMenu()
        self._menu.set_has_arrow(False)
        self._menu.set_halign(Gtk.Align.START)
        self._install_actions()

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._view)
        self.append(scroller)

        self.reload()

    # ------------------------------------------------------------------
    # Columns
    # ------------------------------------------------------------------

    def _build_name_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item: Gtk.ListItem) -> None:
            expander = Gtk.TreeExpander()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.append(Gtk.Image())
            label = Gtk.Label(xalign=0.0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(label)
            expander.set_child(box)

            # Per-row gesture: ColumnView has no "which row was right-clicked"
            # API, and the bound ListItem always reports its current row.
            click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
            click.connect("pressed", self._on_row_right_click, item)
            expander.add_controller(click)

            item.set_child(expander)

        def bind(_factory, item: Gtk.ListItem) -> None:
            list_row: Gtk.TreeListRow = item.get_item()
            row: _Row = list_row.get_item()
            expander: Gtk.TreeExpander = item.get_child()
            expander.set_list_row(list_row)

            box = expander.get_child()
            image: Gtk.Image = box.get_first_child()
            label: Gtk.Label = image.get_next_sibling()
            label.set_text(row.label)

            if row.is_group:
                image.set_visible(False)
                label.set_attributes(_BOLD)
            else:
                image.set_visible(True)
                image.set_from_icon_name(self._icon_for(row.conn))
                label.set_attributes(_colour_attrs(row.conn.color))

        factory.connect("setup", setup)
        factory.connect("bind", bind)

        column = Gtk.ColumnViewColumn(title="Name", factory=factory)
        column.set_resizable(True)
        return column

    def _build_host_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, item: Gtk.ListItem) -> None:
            label = Gtk.Label(xalign=0.0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            item.set_child(label)

        def bind(_factory, item: Gtk.ListItem) -> None:
            row: _Row = item.get_item().get_item()
            item.get_child().set_text(row.host)

        factory.connect("setup", setup)
        factory.connect("bind", bind)

        column = Gtk.ColumnViewColumn(title="Host", factory=factory)
        column.set_expand(True)
        return column

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
        list_row = self._selection.get_selected_item()
        if list_row is None:
            return None
        return list_row.get_item().conn

    def set_health(self, conn_id: int, status: str) -> None:
        """Update the live status icon for a connection, in place."""
        self._health[conn_id] = status
        # Re-emit items-changed for the row so its factory rebinds.
        for g in range(self._root.get_n_items()):
            children = self._root.get_item(g).children
            if children is None:
                continue
            for c in range(children.get_n_items()):
                row = children.get_item(c)
                if row.conn is not None and row.conn.id == conn_id:
                    children.items_changed(c, 1, 1)
                    return

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _icon_for(self, conn: Connection) -> str:
        health = self._health.get(conn.id) if conn.id is not None else None
        if health in _HEALTH_ICON:
            return _HEALTH_ICON[health]
        return _PROTO_ICON.get(conn.protocol or "ssh", _DEFAULT_ICON)

    def _repopulate(self) -> None:
        self._root.remove_all()

        conns = self._connections
        if self._filter_text:
            conns = [
                c for c in conns
                if self._filter_text in c.display_name().lower()
                or self._filter_text in c.host.lower()
            ]

        groups: dict[str, Gio.ListStore] = {}
        order: list[str] = []
        for conn in conns:
            name = conn.group or "Default"
            if name not in groups:
                groups[name] = Gio.ListStore.new(_Row)
                order.append(name)
            groups[name].append(
                _Row(conn.display_name(), conn.connection_string(), conn)
            )

        for name in order:
            self._root.append(_Row(name, children=groups[name]))

        # Expand everything so the list isn't just a row of collapsed headers.
        # Expanding splices children in, so the row count grows as we go and
        # must be re-read each step rather than snapshotted by range().
        i = 0
        while i < self._tree_model.get_n_items():
            row = self._tree_model.get_row(i)
            if row is not None and row.is_expandable():
                row.set_expanded(True)
            i += 1

    # ── events ────────────────────────────────────────────────────────────

    def _on_selection_changed(self, *_args) -> None:
        conn = self.selected_connection()
        if conn is not None:
            self.on_selected(conn)
        else:
            self.on_cleared()

    def _on_activate(self, _view, position: int) -> None:
        list_row = self._tree_model.get_row(position)
        if list_row is None:
            return
        row: _Row = list_row.get_item()
        if row.conn is not None:
            self.on_activated(row.conn)
        else:
            list_row.set_expanded(not list_row.get_expanded())

    def _on_row_right_click(
        self, gesture, _n_press: int, _x: float, _y: float, item: Gtk.ListItem
    ) -> None:
        row: _Row = item.get_item().get_item()
        if row.conn is None:
            return
        self._menu_target = row.conn
        self._selection.set_selected(item.get_position())

        # Re-parent onto the clicked row so the popover lands on it without
        # any coordinate translation.
        if self._menu.get_parent() is not None:
            self._menu.unparent()
        self._menu.set_parent(gesture.get_widget())
        self._menu.popup()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        for name, handler in (
            ("connect", lambda *_: self._with_target(self.on_activated)),
            ("duplicate", lambda *_: self._with_target(self._duplicate)),
            ("delete", lambda *_: self._with_target(self._delete)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            group.add_action(action)
        self.insert_action_group("row", group)

        menu = Gio.Menu()
        menu.append("Connect", "row.connect")
        menu.append("Duplicate", "row.duplicate")
        menu.append("Delete", "row.delete")
        self._menu.set_menu_model(menu)

    def _with_target(self, fn) -> None:
        if self._menu_target is not None:
            fn(self._menu_target)

    def _duplicate(self, conn: Connection) -> None:
        clone = Connection.from_dict(conn.to_dict())
        clone.id = None
        clone.name = f"{conn.display_name()} (copy)"
        self.db.save_connection(clone)
        self.reload()

    def _delete(self, conn: Connection) -> None:
        if conn.id is None:
            return
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Delete connection «{conn.display_name()}»?")
        dialog.set_detail("This cannot be undone.")
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def done(dlg, result) -> None:
            try:
                confirmed = dlg.choose_finish(result) == 1
            except Exception:  # noqa: BLE001 — dismissed
                return
            if confirmed:
                self.db.delete_connection(conn.id)
                self.reload()
                self.on_cleared()

        dialog.choose(self.get_root(), None, done)
