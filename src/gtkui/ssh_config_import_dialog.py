"""Import connections from ~/.ssh/config."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from src.gtkui.widgets import error_dialog
from src.models.ssh_config import host_to_connection, parse_ssh_config
from src.storage.database import Database


class _HostRow(GObject.Object):
    """One Host entry from the config file."""

    __gtype_name__ = "SshelfSshConfigHostRow"

    def __init__(self, host: dict, already_known: bool) -> None:
        super().__init__()
        self.host = host
        self.already_known = already_known
        self.selected = not already_known

    @property
    def alias(self) -> str:
        return self.host.get("alias", "")

    @property
    def hostname(self) -> str:
        return self.host.get("hostname", self.host.get("alias", ""))

    @property
    def user(self) -> str:
        return self.host.get("user", "")

    @property
    def port(self) -> str:
        return str(self.host.get("port", 22))


class SshConfigImportDialog(Gtk.Window):
    """Pick which Host entries from an SSH config to import."""

    def __init__(
        self,
        db: Database,
        parent: Optional[Gtk.Window] = None,
        on_imported: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(title="Import from SSH Config")
        self.db = db
        self._on_imported = on_imported
        self._rows = Gio.ListStore.new(_HostRow)

        self.set_modal(True)
        self.set_default_size(680, 460)
        if parent is not None:
            self.set_transient_for(parent)

        self._build_ui()
        self._load_default()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        self.set_titlebar(header)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._import_btn = Gtk.Button(label="Import Selected")
        self._import_btn.add_css_class("suggested-action")
        self._import_btn.set_sensitive(False)
        self._import_btn.connect("clicked", self._on_import)
        header.pack_end(self._import_btn)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._path_label = Gtk.Label(xalign=0.0)
        self._path_label.add_css_class("dim-label")
        self._path_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._path_label.set_hexpand(True)
        path_row.append(self._path_label)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse)
        path_row.append(browse)
        root.append(path_row)

        self._view = Gtk.ColumnView(model=Gtk.NoSelection(model=self._rows))
        self._view.append_column(self._check_column())
        self._view.append_column(self._text_column("Alias", lambda r: r.alias))
        self._view.append_column(self._text_column("Hostname", lambda r: r.hostname,
                                                   expand=True))
        self._view.append_column(self._text_column("User", lambda r: r.user))
        self._view.append_column(self._text_column("Port", lambda r: r.port))

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._view)
        root.append(scroller)

        sel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, value in (("Select All", True), ("Select None", False)):
            btn = Gtk.Button(label=label)
            btn.connect("clicked", self._on_select_all, value)
            sel_row.append(btn)
        self._summary = Gtk.Label(xalign=0.0)
        self._summary.add_css_class("dim-label")
        self._summary.set_hexpand(True)
        sel_row.append(self._summary)
        root.append(sel_row)

    def _check_column(self) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item: Gtk.ListItem) -> None:
            check = Gtk.CheckButton()
            check.connect("toggled", self._on_row_toggled, item)
            item.set_child(check)

        def bind(_f, item: Gtk.ListItem) -> None:
            row: _HostRow = item.get_item()
            check: Gtk.CheckButton = item.get_child()
            check.set_active(row.selected)
            check.set_sensitive(True)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return Gtk.ColumnViewColumn(title="", factory=factory)

    def _text_column(self, title: str, getter, expand: bool = False
                     ) -> Gtk.ColumnViewColumn:
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item: Gtk.ListItem) -> None:
            label = Gtk.Label(xalign=0.0)
            label.set_ellipsize(3)
            item.set_child(label)

        def bind(_f, item: Gtk.ListItem) -> None:
            row: _HostRow = item.get_item()
            label: Gtk.Label = item.get_child()
            label.set_text(getter(row))
            # Entries already present in the database are dimmed.
            if row.already_known:
                label.add_css_class("dim-label")
                label.set_tooltip_text("Already in your connections")
            else:
                label.remove_css_class("dim-label")
                label.set_tooltip_text(None)

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        return column

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_default(self) -> None:
        default = Path.home() / ".ssh" / "config"
        if default.exists():
            self._load_file(default)
        else:
            self._path_label.set_text("~/.ssh/config not found — use Browse…")
            self._update_summary()

    def _load_file(self, path: Path) -> None:
        try:
            hosts = parse_ssh_config(path)
        except Exception as exc:  # noqa: BLE001
            error_dialog(self, "Could not read that file", str(exc))
            return

        self._path_label.set_text(str(path))
        known = {c.host for c in self.db.all_connections()}
        self._rows.remove_all()
        for host in hosts:
            hostname = host.get("hostname", host.get("alias", ""))
            self._rows.append(_HostRow(host, hostname in known))
        self._update_summary()

    def _on_browse(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Select SSH config file")
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(ssh_dir)))

        def chosen(dlg, result) -> None:
            try:
                file = dlg.open_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is not None and file.get_path():
                self._load_file(Path(file.get_path()))

        dialog.open(self, None, chosen)

    # ------------------------------------------------------------------
    # Selection / import
    # ------------------------------------------------------------------

    def _on_row_toggled(self, check: Gtk.CheckButton, item: Gtk.ListItem) -> None:
        row: _HostRow = item.get_item()
        if row is not None:
            row.selected = check.get_active()
            self._update_summary()

    def _on_select_all(self, _button, value: bool) -> None:
        for i in range(self._rows.get_n_items()):
            self._rows.get_item(i).selected = value
        # Rebind every visible row so the checkboxes redraw.
        self._rows.items_changed(0, self._rows.get_n_items(),
                                 self._rows.get_n_items())
        self._update_summary()

    def _selected_rows(self) -> list[_HostRow]:
        return [
            self._rows.get_item(i)
            for i in range(self._rows.get_n_items())
            if self._rows.get_item(i).selected
        ]

    def _update_summary(self) -> None:
        total = self._rows.get_n_items()
        chosen = len(self._selected_rows())
        if total == 0:
            self._summary.set_text("No hosts found.")
        else:
            self._summary.set_text(f"{chosen} of {total} selected")
        self._import_btn.set_sensitive(chosen > 0)

    def _on_import(self, _button) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        for row in rows:
            self.db.save_connection(host_to_connection(row.host))
        if self._on_imported is not None:
            self._on_imported(len(rows))
        self.close()
