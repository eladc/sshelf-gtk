"""Port-forwarding side panel.

Lists tunnel rules for one connection and starts/stops the forwarding
workers as the SSH session connects and disconnects.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from src.gtkui.widgets import Form, entry, spin, toolbar_button
from src.models.tunnel import Tunnel
from src.protocols.tunnel_core import build_worker

_TYPES = ["local", "remote"]


class _TunnelRow(GObject.Object):
    __gtype_name__ = "SshelfTunnelRow"

    def __init__(self, tunnel: Tunnel) -> None:
        super().__init__()
        self.tunnel = tunnel


class TunnelPanel(Gtk.Box):
    """Tunnels for one SSH connection.

    Call set_transport(transport) when the session connects and
    set_transport(None) when it closes.
    """

    def __init__(self, db, conn_id: Optional[int]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._db = db
        self._conn_id = conn_id
        self._transport = None
        self._active: dict[int, object] = {}   # tunnel id → running worker
        self._store = Gio.ListStore.new(_TunnelRow)

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label="Port Forwarding", xalign=0.0)
        title.add_css_class("heading")
        title.set_hexpand(True)
        header.append(title)

        add = toolbar_button("list-add-symbolic", "Add tunnel")
        add.connect("clicked", self._on_add)
        header.append(add)

        self._remove = toolbar_button("list-remove-symbolic", "Remove selected")
        self._remove.set_sensitive(False)
        self._remove.connect("clicked", self._on_remove)
        header.append(self._remove)
        self.append(header)

        self._status = Gtk.Label(xalign=0.0)
        self._status.add_css_class("dim-label")
        self._status.set_wrap(True)
        self.append(self._status)

        self._selection = Gtk.SingleSelection(model=self._store)
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("notify::selected-item",
                                lambda *_: self._sync_buttons())

        factory = Gtk.SignalListItemFactory()

        def setup(_f, item: Gtk.ListItem) -> None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dot = Gtk.Image()
            row.append(dot)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            label = Gtk.Label(xalign=0.0)
            label.set_ellipsize(3)
            text.append(label)
            route = Gtk.Label(xalign=0.0)
            route.set_ellipsize(3)
            route.add_css_class("dim-label")
            text.append(route)
            text.set_hexpand(True)
            row.append(text)
            switch = Gtk.Switch()
            switch.set_valign(Gtk.Align.CENTER)
            switch.connect("state-set", self._on_switch, item)
            row.append(switch)
            item.set_child(row)

        def bind(_f, item: Gtk.ListItem) -> None:
            tunnel = item.get_item().tunnel
            row = item.get_child()
            dot = row.get_first_child()
            text = dot.get_next_sibling()
            switch = text.get_next_sibling()
            label = text.get_first_child()
            route = label.get_next_sibling()

            running = tunnel.id in self._active
            dot.set_from_icon_name(
                "media-record-symbolic" if running else "media-playback-stop-symbolic"
            )
            dot.set_tooltip_text("Forwarding" if running else "Not forwarding")
            label.set_text(tunnel.label)
            route.set_text(
                f"{tunnel.type}  :{tunnel.local_port} → "
                f"{tunnel.remote_host}:{tunnel.remote_port}"
            )
            # Avoid re-entering _on_switch while syncing state from the model.
            switch.handler_block_by_func(self._on_switch)
            switch.set_active(tunnel.enabled)
            switch.handler_unblock_by_func(self._on_switch)

        factory.connect("setup", setup)
        factory.connect("bind", bind)

        self._list = Gtk.ListView(model=self._selection, factory=factory)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._list)
        self.append(scroller)

        self._empty = Gtk.Label(xalign=0.0)
        self._empty.add_css_class("dim-label")
        self._empty.set_wrap(True)
        self.append(self._empty)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_transport(self, transport) -> None:
        """Session connected (transport) or closed (None)."""
        self._transport = transport
        self._stop_all()
        if transport is not None:
            self._start_enabled()
        self.reload()

    def shutdown(self) -> None:
        self._stop_all()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def reload(self) -> None:
        self._store.remove_all()
        if self._db is not None and self._conn_id is not None:
            for row in self._db.all_tunnels(self._conn_id):
                self._store.append(_TunnelRow(Tunnel.from_dict(row)))

        count = self._store.get_n_items()
        if self._conn_id is None:
            self._empty.set_text("Save the connection to add tunnels.")
        elif count == 0:
            self._empty.set_text("No tunnels yet — press + to add one.")
        else:
            self._empty.set_text("")

        if self._transport is None:
            self._status.set_text("SSH not connected — tunnels start on connect.")
        else:
            self._status.set_text(f"Connected — {len(self._active)} tunnel(s) active.")
        self._sync_buttons()

    def _selected(self) -> Optional[Tunnel]:
        row = self._selection.get_selected_item()
        return row.tunnel if row is not None else None

    def _sync_buttons(self) -> None:
        self._remove.set_sensitive(self._selected() is not None)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_add(self, _button) -> None:
        if self._conn_id is None or self._db is None:
            return
        TunnelEditor(parent=self.get_root(), on_saved=self._save_new).present()

    def _save_new(self, tunnel: Tunnel) -> None:
        tunnel.conn_id = self._conn_id
        saved = self._db.save_tunnel(tunnel)
        # save_tunnel may return the stored row; fall back to what we sent.
        tunnel = saved if isinstance(saved, Tunnel) else tunnel
        if tunnel.enabled and self._transport is not None:
            self._start(tunnel)
        self.reload()

    def _on_remove(self, _button) -> None:
        tunnel = self._selected()
        if tunnel is None:
            return
        self._stop(tunnel)
        if tunnel.id is not None and self._db is not None:
            self._db.delete_tunnel(tunnel.id)
        self.reload()

    def _on_switch(self, switch: Gtk.Switch, active: bool,
                   item: Gtk.ListItem) -> bool:
        row = item.get_item()
        if row is None:
            return False
        tunnel = row.tunnel
        if tunnel.enabled == active:
            return False
        tunnel.enabled = active
        if self._db is not None and tunnel.id is not None:
            self._db.save_tunnel(tunnel)
        if active:
            if self._transport is not None:
                self._start(tunnel)
        else:
            self._stop(tunnel)
        # Refresh status text and the running dot without fighting the switch.
        GLib.idle_add(self._refresh_row, item.get_position())
        return False

    def _refresh_row(self, position: int) -> bool:
        if 0 <= position < self._store.get_n_items():
            self._store.items_changed(position, 1, 1)
        if self._transport is not None:
            self._status.set_text(f"Connected — {len(self._active)} tunnel(s) active.")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    def _start_enabled(self) -> None:
        for i in range(self._store.get_n_items()):
            tunnel = self._store.get_item(i).tunnel
            if tunnel.enabled:
                self._start(tunnel)

    def _start(self, tunnel: Tunnel) -> None:
        if self._transport is None or tunnel.id in self._active:
            return
        worker = build_worker(
            self._transport, tunnel,
            on_error=lambda msg, t=tunnel: GLib.idle_add(self._on_worker_error, t, msg),
        )
        worker.start()
        self._active[tunnel.id] = worker

    def _stop(self, tunnel: Tunnel) -> None:
        worker = self._active.pop(tunnel.id, None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass

    def _stop_all(self) -> None:
        for worker in list(self._active.values()):
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass
        self._active.clear()

    def _on_worker_error(self, tunnel: Tunnel, message: str) -> bool:
        self._active.pop(tunnel.id, None)
        self._status.set_text(f"{tunnel.label}: {message}")
        self.reload()
        return GLib.SOURCE_REMOVE


class TunnelEditor(Gtk.Window):
    """Create a tunnel rule."""

    def __init__(self, parent: Optional[Gtk.Window] = None,
                 tunnel: Optional[Tunnel] = None, on_saved=None) -> None:
        super().__init__(title="Add Tunnel" if tunnel is None else "Edit Tunnel")
        self._on_saved = on_saved
        self.set_modal(True)
        self.set_default_size(420, 300)
        if parent is not None:
            self.set_transient_for(parent)

        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        self.set_titlebar(header)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_end(save)

        form = Form()
        self.set_child(form)

        self._label = entry("e.g. PostgreSQL")
        if tunnel:
            self._label.set_text(tunnel.label)
        form.add("Label:", self._label)

        self._type = Gtk.DropDown.new_from_strings(_TYPES)
        self._type.set_halign(Gtk.Align.START)
        if tunnel and tunnel.type in _TYPES:
            self._type.set_selected(_TYPES.index(tunnel.type))
        form.add("Type:", self._type)

        self._local_port = spin(1, 65535, tunnel.local_port if tunnel else 5432)
        form.add("Local port:", self._local_port)

        self._remote_host = entry("127.0.0.1")
        if tunnel:
            self._remote_host.set_text(tunnel.remote_host)
        form.add("Remote host:", self._remote_host)

        self._remote_port = spin(1, 65535, tunnel.remote_port if tunnel else 5432)
        form.add("Remote port:", self._remote_port)

    def _on_save(self, _button) -> None:
        tunnel = Tunnel(
            id=None,
            conn_id=0,  # caller fills this in
            label=self._label.get_text().strip() or "Tunnel",
            type=_TYPES[self._type.get_selected()],
            local_port=int(self._local_port.get_value()),
            remote_host=self._remote_host.get_text().strip() or "127.0.0.1",
            remote_port=int(self._remote_port.get_value()),
            enabled=True,
        )
        if self._on_saved is not None:
            self._on_saved(tunnel)
        self.close()
