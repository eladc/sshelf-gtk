"""SFTP file browser panel.

Runs every SFTP call on a worker thread — paramiko's SFTP operations block,
and a stalled listing must never freeze the terminal. Results come back
through GLib.idle_add.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path, PurePosixPath
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from src.gtkui.widgets import toolbar_button

_DIR_MODE = 0o040000


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class _Entry(GObject.Object):
    __gtype_name__ = "SshelfSftpEntry"

    def __init__(self, name: str, is_dir: bool, size: int) -> None:
        super().__init__()
        self.name = name
        self.is_dir = is_dir
        self.size = size


class SFTPPanel(Gtk.Box):
    """Remote file browser over an existing SSH connection."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._sftp = None
        self._cwd = "."
        self._store = Gio.ListStore.new(_Entry)
        self._busy = False

        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label="Files", xalign=0.0)
        title.add_css_class("heading")
        title.set_hexpand(True)
        header.append(title)

        up = toolbar_button("go-up-symbolic", "Parent directory")
        up.connect("clicked", lambda *_: self._go_up())
        header.append(up)

        refresh = toolbar_button("view-refresh-symbolic", "Refresh")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.append(refresh)

        self._upload_btn = toolbar_button("go-up-symbolic", "Upload a file")
        self._upload_btn.set_icon_name("document-send-symbolic")
        self._upload_btn.set_sensitive(False)
        self._upload_btn.connect("clicked", self._on_upload)
        header.append(self._upload_btn)

        self._download_btn = toolbar_button("document-save-symbolic",
                                            "Download selected")
        self._download_btn.set_sensitive(False)
        self._download_btn.connect("clicked", self._on_download)
        header.append(self._download_btn)
        self.append(header)

        self._path_label = Gtk.Label(xalign=0.0)
        self._path_label.add_css_class("dim-label")
        self._path_label.set_ellipsize(3)
        self.append(self._path_label)

        self._selection = Gtk.SingleSelection(model=self._store)
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("notify::selected-item",
                                lambda *_: self._sync_buttons())

        factory = Gtk.SignalListItemFactory()

        def setup(_f, item: Gtk.ListItem) -> None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Image())
            name = Gtk.Label(xalign=0.0)
            name.set_ellipsize(3)
            name.set_hexpand(True)
            row.append(name)
            size = Gtk.Label(xalign=1.0)
            size.add_css_class("dim-label")
            row.append(size)
            item.set_child(row)

        def bind(_f, item: Gtk.ListItem) -> None:
            entry: _Entry = item.get_item()
            row = item.get_child()
            icon = row.get_first_child()
            name = icon.get_next_sibling()
            size = name.get_next_sibling()
            icon.set_from_icon_name(
                "folder-symbolic" if entry.is_dir else "text-x-generic-symbolic"
            )
            name.set_text(entry.name)
            size.set_text("" if entry.is_dir else _format_size(entry.size))

        factory.connect("setup", setup)
        factory.connect("bind", bind)

        self._list = Gtk.ListView(model=self._selection, factory=factory)
        self._list.set_single_click_activate(False)
        self._list.connect("activate", self._on_activate)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._list)
        self.append(scroller)

        self._status = Gtk.Label(xalign=0.0)
        self._status.add_css_class("dim-label")
        self._status.set_wrap(True)
        self._status.set_text("Not connected.")
        self.append(self._status)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attach(self, session) -> None:
        """Open SFTP over an established SSHSession (runs off the UI thread)."""
        if self._sftp is not None:
            return
        self._status.set_text("Opening SFTP…")

        def work() -> None:
            try:
                sftp = session.open_sftp()
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._failed, str(exc))
                return
            if sftp is None:
                GLib.idle_add(self._failed, "SFTP is not available on this server.")
            else:
                GLib.idle_add(self._attached, sftp)

        threading.Thread(target=work, daemon=True, name="sftp-open").start()

    def detach(self) -> None:
        sftp, self._sftp = self._sftp, None
        self._store.remove_all()
        self._status.set_text("Not connected.")
        self._sync_buttons()
        if sftp is not None:
            threading.Thread(
                target=lambda: _quietly(sftp.close), daemon=True
            ).start()

    def navigate_to(self, path: str) -> None:
        self._cwd = path
        self.refresh()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def _attached(self, sftp) -> bool:
        self._sftp = sftp
        try:
            self._cwd = sftp.normalize(".")
        except Exception:  # noqa: BLE001
            self._cwd = "."
        self._upload_btn.set_sensitive(True)
        self.refresh()
        return GLib.SOURCE_REMOVE

    def _failed(self, message: str) -> bool:
        self._status.set_text(f"Error: {message}")
        return GLib.SOURCE_REMOVE

    def refresh(self) -> None:
        if self._sftp is None or self._busy:
            return
        self._busy = True
        path = self._cwd
        self._path_label.set_text(path)
        self._status.set_text("Listing…")
        sftp = self._sftp

        def work() -> None:
            try:
                entries = []
                for attr in sftp.listdir_attr(path):
                    is_dir = bool(getattr(attr, "st_mode", 0) & _DIR_MODE)
                    entries.append((attr.filename, is_dir, attr.st_size or 0))
                entries.sort(key=lambda e: (not e[1], e[0].lower()))
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._listing_failed, str(exc))
                return
            GLib.idle_add(self._listing_done, entries)

        threading.Thread(target=work, daemon=True, name="sftp-list").start()

    def _listing_done(self, entries: list) -> bool:
        self._busy = False
        self._store.remove_all()
        for name, is_dir, size in entries:
            self._store.append(_Entry(name, is_dir, size))
        self._status.set_text(f"{len(entries)} item(s)")
        self._sync_buttons()
        return GLib.SOURCE_REMOVE

    def _listing_failed(self, message: str) -> bool:
        self._busy = False
        self._status.set_text(f"Error: {message}")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _selected(self) -> Optional[_Entry]:
        return self._selection.get_selected_item()

    def _sync_buttons(self) -> None:
        entry = self._selected()
        self._download_btn.set_sensitive(
            entry is not None and not entry.is_dir and self._sftp is not None
        )

    def _on_activate(self, _view, position: int) -> None:
        entry = self._store.get_item(position)
        if entry is None:
            return
        if entry.is_dir:
            self._cwd = str(PurePosixPath(self._cwd) / entry.name)
            self.refresh()
        else:
            self._download(entry)

    def _go_up(self) -> None:
        parent = str(PurePosixPath(self._cwd).parent)
        if parent != self._cwd:
            self._cwd = parent
            self.refresh()

    # ------------------------------------------------------------------
    # Transfers
    # ------------------------------------------------------------------

    def _on_download(self, _button) -> None:
        entry = self._selected()
        if entry is not None and not entry.is_dir:
            self._download(entry)

    def _download(self, entry: _Entry) -> None:
        if self._sftp is None:
            return
        remote = str(PurePosixPath(self._cwd) / entry.name)

        dialog = Gtk.FileDialog()
        dialog.set_title("Save File")
        dialog.set_initial_name(entry.name)

        def chosen(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is None or not file.get_path():
                return
            self._transfer(remote, file.get_path(), upload=False,
                           name=entry.name)

        dialog.save(self.get_root(), None, chosen)

    def _on_upload(self, _button) -> None:
        if self._sftp is None:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Upload File")

        def chosen(dlg, result) -> None:
            try:
                file = dlg.open_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is None or not file.get_path():
                return
            local = file.get_path()
            name = os.path.basename(local)
            remote = str(PurePosixPath(self._cwd) / name)
            self._transfer(remote, local, upload=True, name=name)

        dialog.open(self.get_root(), None, chosen)

    def _transfer(self, remote: str, local: str, upload: bool,
                  name: str) -> None:
        sftp = self._sftp
        if sftp is None:
            return
        verb = "Uploading" if upload else "Downloading"
        self._status.set_text(f"{verb} {name}…")

        def progress(done: int, total: int) -> None:
            if total:
                GLib.idle_add(
                    self._status.set_text,
                    f"{verb} {name}… {done * 100 // total}%",
                )

        def work() -> None:
            try:
                if upload:
                    sftp.put(local, remote, callback=progress)
                else:
                    sftp.get(remote, local, callback=progress)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._failed, str(exc))
                return
            GLib.idle_add(self._transfer_done, verb, name, upload)

        threading.Thread(target=work, daemon=True, name="sftp-transfer").start()

    def _transfer_done(self, verb: str, name: str, upload: bool) -> bool:
        self._status.set_text(
            f"{'Uploaded' if upload else 'Downloaded'} {name}"
        )
        if upload:
            self.refresh()
        return GLib.SOURCE_REMOVE


def _quietly(fn) -> None:
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass
