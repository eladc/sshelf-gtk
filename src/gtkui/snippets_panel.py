"""Command snippets panel — quick-send saved commands to the terminal.

Snippets live in the database. conn_id=None means global (shown in every
session); conn_id=<id> ties the snippet to one connection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GObject, Gtk  # noqa: E402

from src.gtkui.widgets import error_dialog, toolbar_button


class _Snippet(GObject.Object):
    __gtype_name__ = "SshelfSnippet"

    def __init__(self, sid: int, title: str, command: str,
                 conn_id: Optional[int]) -> None:
        super().__init__()
        self.sid = sid
        self.title = title
        self.command = command
        self.conn_id = conn_id


class SnippetsPanel(Gtk.Box):
    """Saved command launcher.

    Activating a row (double-click / Enter) or pressing Send calls
    on_send(command) — the terminal forwards it to the SSH channel.
    """

    def __init__(self, db, conn_id: Optional[int] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._db = db
        self._conn_id = conn_id
        self._store = Gio.ListStore.new(_Snippet)

        self.on_send: Callable[[str], None] = lambda command: None

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
        title = Gtk.Label(label="Commands", xalign=0.0)
        title.add_css_class("heading")
        title.set_hexpand(True)
        header.append(title)

        add = toolbar_button("list-add-symbolic", "Add command")
        add.connect("clicked", self._on_add)
        header.append(add)

        self._remove = toolbar_button("list-remove-symbolic", "Delete selected")
        self._remove.set_sensitive(False)
        self._remove.connect("clicked", self._on_delete)
        header.append(self._remove)

        export = toolbar_button("document-save-symbolic", "Export to JSON")
        export.connect("clicked", self._on_export)
        header.append(export)

        imp = toolbar_button("document-open-symbolic", "Import from JSON")
        imp.connect("clicked", self._on_import)
        header.append(imp)
        self.append(header)

        self._selection = Gtk.SingleSelection(model=self._store)
        self._selection.set_autoselect(False)
        self._selection.set_can_unselect(True)
        self._selection.connect("notify::selected-item",
                                lambda *_: self._sync_buttons())

        factory = Gtk.SignalListItemFactory()

        def setup(_f, item: Gtk.ListItem) -> None:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            name = Gtk.Label(xalign=0.0)
            name.set_ellipsize(3)
            box.append(name)
            cmd = Gtk.Label(xalign=0.0)
            cmd.set_ellipsize(3)
            cmd.add_css_class("dim-label")
            box.append(cmd)
            item.set_child(box)

        def bind(_f, item: Gtk.ListItem) -> None:
            snippet: _Snippet = item.get_item()
            box = item.get_child()
            name = box.get_first_child()
            cmd = name.get_next_sibling()
            name.set_text(snippet.title)
            cmd.set_text(snippet.command.replace("\n", " ⏎ "))
            scope = "global" if snippet.conn_id is None else "this connection"
            box.set_tooltip_text(f"{snippet.command}\n\nScope: {scope}")

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

        self._send = Gtk.Button(label="Send")
        self._send.add_css_class("suggested-action")
        self._send.set_sensitive(False)
        self._send.connect("clicked", self._on_send_selected)
        self.append(self._send)

        self._empty = Gtk.Label(xalign=0.0)
        self._empty.add_css_class("dim-label")
        self._empty.set_wrap(True)
        self.append(self._empty)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def reload(self) -> None:
        self._store.remove_all()
        if self._db is not None:
            for row in self._db.all_snippets(self._conn_id):
                self._store.append(
                    _Snippet(row["id"], row["title"], row["command"],
                             row.get("conn_id"))
                )
        self._empty.set_text(
            "" if self._store.get_n_items()
            else "No commands yet — press + to add one."
        )
        self._sync_buttons()

    def _selected(self) -> Optional[_Snippet]:
        return self._selection.get_selected_item()

    def _sync_buttons(self) -> None:
        has = self._selected() is not None
        self._remove.set_sensitive(has and self._db is not None)
        self._send.set_sensitive(has)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_activate(self, _view, position: int) -> None:
        snippet = self._store.get_item(position)
        if snippet is not None:
            self.on_send(snippet.command + "\n")

    def _on_send_selected(self, _button) -> None:
        snippet = self._selected()
        if snippet is not None:
            self.on_send(snippet.command + "\n")

    def _on_add(self, _button) -> None:
        if self._db is None:
            return
        SnippetEditor(
            parent=self.get_root(),
            allow_connection_scope=self._conn_id is not None,
            on_saved=self._save_new,
        ).present()

    def _save_new(self, title: str, command: str, connection_scope: bool) -> None:
        self._db.save_snippet(
            title, command, self._conn_id if connection_scope else None
        )
        self.reload()

    def _on_delete(self, _button) -> None:
        snippet = self._selected()
        if snippet is None or self._db is None:
            return
        self._db.delete_snippet(snippet.sid)
        self.reload()

    # ── import / export ───────────────────────────────────────────────────

    def _on_export(self, _button) -> None:
        if self._db is None:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Export Commands")
        dialog.set_initial_name("sshelf-commands.json")

        def chosen(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is None or not file.get_path():
                return
            snippets = self._db.all_snippets(self._conn_id)
            payload = {
                "version": "1.0",
                "app": "sshelf",
                "snippets": [
                    {
                        "title": s["title"],
                        "command": s["command"],
                        "scope": "connection" if s.get("conn_id") else "global",
                    }
                    for s in snippets
                ],
            }
            try:
                Path(file.get_path()).write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                error_dialog(self.get_root(), "Export failed", str(exc))

        dialog.save(self.get_root(), None, chosen)

    def _on_import(self, _button) -> None:
        if self._db is None:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Import Commands")

        def chosen(dlg, result) -> None:
            try:
                file = dlg.open_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is None or not file.get_path():
                return
            self._import_file(Path(file.get_path()))

        dialog.open(self.get_root(), None, chosen)

    def _import_file(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            error_dialog(self.get_root(), "Import failed",
                         f"Could not read file:\n{exc}")
            return

        snippets = data.get("snippets") if isinstance(data, dict) else None
        if not isinstance(snippets, list):
            error_dialog(self.get_root(), "Import failed",
                         "That file is not a sshelf command export.")
            return

        existing = {
            (s["title"], s["command"])
            for s in self._db.all_snippets(self._conn_id)
        }
        fresh = [
            s for s in snippets
            if isinstance(s, dict)
            and s.get("title") and s.get("command")
            and (s.get("title", ""), s.get("command", "")) not in existing
        ]
        skipped = len(snippets) - len(fresh)

        if not fresh:
            error_dialog(self.get_root(), "Nothing to import",
                         f"All {len(snippets)} command(s) are already saved.")
            return

        detail = f"Import {len(fresh)} new command(s)?"
        if skipped:
            detail += f"\n{skipped} already exist and will be skipped."

        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message("Import commands")
        dialog.set_detail(detail)
        dialog.set_buttons(["Cancel", "Import"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)

        def answered(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != 1:
                    return
            except Exception:  # noqa: BLE001 — dismissed
                return
            for s in fresh:
                self._db.save_snippet(
                    s["title"].strip(),
                    s["command"].strip(),
                    self._conn_id if s.get("scope") == "connection" else None,
                )
            self.reload()

        dialog.choose(self.get_root(), None, answered)


class SnippetEditor(Gtk.Window):
    """Small window for entering a snippet's label, command and scope."""

    def __init__(
        self,
        parent: Optional[Gtk.Window] = None,
        allow_connection_scope: bool = False,
        on_saved: Optional[Callable[[str, str, bool], None]] = None,
    ) -> None:
        super().__init__(title="Add Command")
        self._on_saved = on_saved
        self.set_modal(True)
        self.set_default_size(460, 320)
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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        self.set_child(box)

        box.append(Gtk.Label(label="Label", xalign=0.0))
        self._title = Gtk.Entry()
        self._title.set_placeholder_text("Restart Nginx")
        box.append(self._title)

        box.append(Gtk.Label(label="Command", xalign=0.0))
        self._command = Gtk.TextView()
        self._command.set_monospace(True)
        self._command.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        frame = Gtk.Frame()
        frame.set_vexpand(True)
        frame.set_child(self._command)
        box.append(frame)

        self._connection_scope = Gtk.CheckButton(
            label="Only show for this connection"
        )
        self._connection_scope.set_visible(allow_connection_scope)
        box.append(self._connection_scope)

        self._error = Gtk.Label(xalign=0.0)
        self._error.add_css_class("error")
        self._error.set_visible(False)
        box.append(self._error)

    def _on_save(self, _button) -> None:
        title = self._title.get_text().strip()
        buf = self._command.get_buffer()
        command = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                               False).strip()
        if not title or not command:
            self._error.set_text("Both a label and a command are required.")
            self._error.set_visible(True)
            return
        if self._on_saved is not None:
            self._on_saved(title, command, self._connection_scope.get_active())
        self.close()
