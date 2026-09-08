"""Add / Edit connection dialog — supports SSH, RDP, and VNC.

GTK4 notes: Gtk.Dialog, ComboBoxText and EntryCompletion are all deprecated
as of 4.10, so this is a plain Gtk.Window with a header bar, Gtk.DropDown for
fixed choices, and an entry + popover for the editable group field. There is
no Qt-style modal exec(): the caller passes *on_saved* and the dialog closes
itself.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from src.models.connection import Connection
from src.storage.database import Database

_DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "vnc": 5900}
_DEPTHS = [8, 16, 24, 32]


class _Form(Gtk.Grid):
    """Two-column label/widget grid standing in for Qt's QFormLayout."""

    def __init__(self) -> None:
        super().__init__(row_spacing=10, column_spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self._row = 0

    def add(self, label_text: str, widget: Gtk.Widget) -> Gtk.Label:
        """Add a labelled row; returns the label so callers can hide the pair."""
        label = Gtk.Label(label=label_text, xalign=1.0)
        label.add_css_class("dim-label")
        label.set_valign(Gtk.Align.CENTER)
        self.attach(label, 0, self._row, 1, 1)
        widget.set_hexpand(True)
        self.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return label

    def add_wide(self, widget: Gtk.Widget) -> None:
        widget.set_hexpand(True)
        self.attach(widget, 0, self._row, 2, 1)
        self._row += 1

    def add_separator(self) -> None:
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        self.add_wide(sep)


def _hint(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0.0)
    label.set_wrap(True)
    label.add_css_class("dim-label")
    return label


def _entry(placeholder: str = "") -> Gtk.Entry:
    entry = Gtk.Entry()
    if placeholder:
        entry.set_placeholder_text(placeholder)
    return entry


def _spin(lo: int, hi: int, value: int) -> Gtk.SpinButton:
    spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
    spin.set_value(value)
    spin.set_hexpand(False)
    spin.set_halign(Gtk.Align.START)
    return spin


class ConnectionDialog(Gtk.Window):
    """Create or edit a connection.

    Pages
    -----
    Basic       — protocol, name, group, host, port, username, colour
    Auth        — password; SSH key + passphrase (SSH only)
    Advanced    — jump host, startup cmd, keep-alive, SSH options (SSH only)
    RDP Options — domain, resolution, colour depth                (RDP only)
    VNC Options — view-only                                       (VNC only)
    Notes       — free-text notes
    """

    def __init__(
        self,
        db: Database,
        connection: Optional[Connection] = None,
        parent: Optional[Gtk.Window] = None,
        on_saved: Optional[Callable[[Connection], None]] = None,
    ) -> None:
        super().__init__(
            title="Edit Connection" if (connection and connection.id) else "New Connection"
        )
        self.db = db
        self._conn = connection or Connection()
        self._on_saved = on_saved
        self._colour_value = ""

        self.set_modal(True)
        self.set_default_size(560, 520)
        if parent is not None:
            self.set_transient_for(parent)

        self._protocols = ["SSH"]
        if self.db.get_pref("enable_rdp", "0") == "1":
            self._protocols.append("RDP")
        if self.db.get_pref("enable_vnc", "0") == "1":
            self._protocols.append("VNC")

        self._build_ui()
        self._load_values()
        self._on_protocol_changed()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
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

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        switcher = Gtk.StackSwitcher(stack=self._stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(8)
        switcher.set_margin_bottom(4)
        root.append(switcher)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._stack)
        root.append(scroller)

        self._error = Gtk.Label(xalign=0.0)
        self._error.add_css_class("error")
        self._error.set_margin_start(16)
        self._error.set_margin_end(16)
        self._error.set_margin_bottom(8)
        self._error.set_visible(False)
        root.append(self._error)

        self._build_basic_page()
        self._build_auth_page()
        self._build_advanced_page()
        self._build_rdp_page()
        self._build_vnc_page()
        self._build_notes_page()

    def _build_basic_page(self) -> None:
        form = _Form()

        self._protocol = Gtk.DropDown.new_from_strings(self._protocols)
        self._protocol.set_hexpand(False)
        self._protocol.set_halign(Gtk.Align.START)
        self._protocol.connect("notify::selected",
                               lambda *_: self._on_protocol_changed())
        form.add("Protocol:", self._protocol)

        form.add_separator()

        self._name = _entry("My Server  (leave empty to use hostname)")
        form.add("Name:", self._name)

        # Editable combo: GTK4 deprecates ComboBoxText/EntryCompletion, so it's
        # a plain entry with a popover listing the groups already in use.
        group_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._group = _entry("Default")
        group_row.append(self._group)
        self._group_btn = Gtk.MenuButton()
        self._group_btn.set_icon_name("pan-down-symbolic")
        self._group_btn.set_tooltip_text("Pick an existing group")
        self._group_btn.set_popover(self._build_group_popover())
        group_row.append(self._group_btn)
        form.add("Group:", group_row)

        form.add_separator()

        self._host = _entry("192.168.1.1  or  dev.example.com")
        form.add("Host:", self._host)

        self._port = _spin(1, 65535, 22)
        form.add("Port:", self._port)

        self._username = _entry("your-user")
        form.add("Username:", self._username)

        form.add_separator()

        colour_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._colour_btn = Gtk.ColorDialogButton.new(Gtk.ColorDialog())
        self._colour_btn.set_tooltip_text("Pick a colour label for this connection")
        self._colour_btn.connect("notify::rgba", self._on_colour_picked)
        colour_row.append(self._colour_btn)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", self._on_colour_cleared)
        colour_row.append(clear)
        self._colour_hint = Gtk.Label(label="No colour", xalign=0.0)
        self._colour_hint.add_css_class("dim-label")
        colour_row.append(self._colour_hint)
        form.add("Colour:", colour_row)

        self._stack.add_titled(form, "basic", "Basic")

    def _build_group_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        groups = self.db.groups() or ["Default"]
        for name in groups:
            btn = Gtk.Button(label=name)
            btn.set_has_frame(False)
            btn.connect("clicked", self._on_group_chosen, name)
            box.append(btn)
        popover.set_child(box)
        return popover

    def _build_auth_page(self) -> None:
        form = _Form()

        self._password = Gtk.PasswordEntry()
        self._password.set_show_peek_icon(True)
        form.add("Password:", self._password)

        self._auth_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        form.add_wide(self._auth_sep)

        key_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._key_file = _entry("~/.ssh/id_rsa")
        key_row.append(self._key_file)
        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._on_browse_key)
        key_row.append(browse)
        self._key_row = key_row
        self._key_label = form.add("Private Key:", key_row)

        self._passphrase = Gtk.PasswordEntry()
        self._passphrase.set_show_peek_icon(True)
        self._pass_label = form.add("Passphrase:", self._passphrase)

        self._ssh_auth_note = _hint(
            "Tip: if all fields are empty, paramiko will try your SSH agent "
            "and then ~/.ssh/id_rsa / id_ed25519 automatically."
        )
        form.add_wide(self._ssh_auth_note)

        self._stack.add_titled(form, "auth", "Auth")

    def _build_advanced_page(self) -> None:
        form = _Form()

        self._jump_host = _entry("user@bastion.example.com:22")
        form.add("Jump Host:", self._jump_host)

        self._startup_cmd = _entry("tmux attach  or  bash --login")
        form.add("Startup Command:", self._startup_cmd)

        form.add_separator()

        self._keep_alive = _spin(0, 3600, 60)
        self._keep_alive.set_tooltip_text("Set 0 to disable keep-alive packets")
        form.add("Keep-Alive (s):", self._keep_alive)

        form.add_separator()

        self._agent_forward = Gtk.CheckButton(label="Forward SSH agent")
        form.add("Agent:", self._agent_forward)

        self._x11 = Gtk.CheckButton(label="Enable X11 forwarding")
        form.add("X11:", self._x11)

        self._compress = Gtk.CheckButton(label="Enable compression")
        form.add("Compression:", self._compress)

        self._advanced_page = form
        self._stack.add_titled(form, "advanced", "Advanced")

    def _build_rdp_page(self) -> None:
        form = _Form()

        self._rdp_domain = _entry("CORP  (leave empty for local account)")
        form.add("Domain:", self._rdp_domain)

        form.add_separator()

        res_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._rdp_width = _spin(640, 7680, 1920)
        res_row.append(self._rdp_width)
        res_row.append(Gtk.Label(label="×"))
        self._rdp_height = _spin(480, 4320, 1080)
        res_row.append(self._rdp_height)
        form.add("Resolution:", res_row)

        self._rdp_depth = Gtk.DropDown.new_from_strings(
            [f"{d}-bit" for d in _DEPTHS]
        )
        self._rdp_depth.set_selected(3)
        self._rdp_depth.set_halign(Gtk.Align.START)
        form.add("Colour depth:", self._rdp_depth)

        form.add_separator()
        form.add_wide(_hint(
            "Requires xfreerdp on macOS/Linux (brew install freerdp) "
            "or uses mstsc.exe on Windows."
        ))

        self._rdp_page = form
        self._stack.add_titled(form, "rdp", "RDP Options")

    def _build_vnc_page(self) -> None:
        form = _Form()

        self._vnc_view_only = Gtk.CheckButton(
            label="View only (no keyboard or mouse input)"
        )
        form.add("Mode:", self._vnc_view_only)

        form.add_separator()
        form.add_wide(_hint(
            "Password (set in the Auth tab) is the VNC password used for "
            "authentication. Leave blank for unauthenticated servers."
        ))

        self._vnc_page = form
        self._stack.add_titled(form, "vnc", "VNC Options")

    def _build_notes_page(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.append(Gtk.Label(label="Free-text notes for this connection:",
                             xalign=0.0))

        self._notes = Gtk.TextView()
        self._notes.set_wrap_mode(Gtk.WrapMode.WORD)
        frame = Gtk.Frame()
        frame.set_vexpand(True)
        frame.set_child(self._notes)
        box.append(frame)

        self._stack.add_titled(box, "notes", "Notes")

    # ------------------------------------------------------------------
    # Protocol switching
    # ------------------------------------------------------------------

    def _current_protocol(self) -> str:
        return self._protocols[self._protocol.get_selected()].lower()

    def _on_protocol_changed(self) -> None:
        """Show/hide pages and adjust defaults when the protocol changes."""
        protocol = self._current_protocol()
        is_ssh = protocol == "ssh"

        # The StackSwitcher builds its buttons from GtkStackPage:visible, which
        # is independent of the child widget's own visibility — hiding the
        # child alone leaves the button behind.
        self._stack.get_page(self._advanced_page).set_visible(is_ssh)
        self._stack.get_page(self._rdp_page).set_visible(protocol == "rdp")
        self._stack.get_page(self._vnc_page).set_visible(protocol == "vnc")

        current = self._stack.get_visible_child()
        if current is not None and not self._stack.get_page(current).get_visible():
            self._stack.set_visible_child_name("basic")

        for widget in (self._key_label, self._key_row,
                       self._pass_label, self._passphrase,
                       self._ssh_auth_note, self._auth_sep):
            widget.set_visible(is_ssh)

        if protocol == "vnc":
            placeholder = "VNC password (leave blank for none)"
        elif protocol == "rdp":
            placeholder = "RDP password"
        else:
            placeholder = "Leave blank to use key or agent"
        # Gtk.PasswordEntry exposes placeholder-text as a property only.
        self._password.set_property("placeholder-text", placeholder)

        # Move the port to the new protocol's default, but only if it is still
        # sitting on some protocol's default (i.e. the user hasn't set one).
        current = int(self._port.get_value())
        if current in _DEFAULT_PORTS.values():
            self._port.set_value(_DEFAULT_PORTS[protocol])

    # ------------------------------------------------------------------
    # Load / save values
    # ------------------------------------------------------------------

    def _load_values(self) -> None:
        c = self._conn

        wanted = (c.protocol or "ssh").upper()
        if wanted in self._protocols:
            self._protocol.set_selected(self._protocols.index(wanted))

        self._name.set_text(c.name or "")
        self._group.set_text(c.group or "")
        self._host.set_text(c.host or "")
        self._port.set_value(c.port or _DEFAULT_PORTS.get(c.protocol, 22))
        self._username.set_text(c.username or "")

        self._colour_value = c.color or ""
        self._apply_colour()

        self._password.set_text(c.password or "")
        self._key_file.set_text(c.private_key_file or "")
        self._passphrase.set_text(c.passphrase or "")

        self._jump_host.set_text(c.jump_host or "")
        self._startup_cmd.set_text(c.startup_command or "")
        self._keep_alive.set_value(c.keep_alive_interval)
        self._agent_forward.set_active(c.forward_agent)
        self._x11.set_active(c.x11_forward)
        self._compress.set_active(c.compression)

        self._rdp_domain.set_text(c.rdp_domain or "")
        self._rdp_width.set_value(c.rdp_width)
        self._rdp_height.set_value(c.rdp_height)
        if c.rdp_color_depth in _DEPTHS:
            self._rdp_depth.set_selected(_DEPTHS.index(c.rdp_color_depth))

        self._vnc_view_only.set_active(c.vnc_view_only)
        self._notes.get_buffer().set_text(c.notes or "")

    def _save_values(self) -> None:
        c = self._conn
        c.protocol = self._current_protocol()
        c.name = self._name.get_text().strip()
        c.group = self._group.get_text().strip() or "Default"
        c.host = self._host.get_text().strip()
        c.port = int(self._port.get_value())
        c.username = self._username.get_text().strip()
        c.color = self._colour_value

        c.password = self._password.get_text()

        c.private_key_file = self._key_file.get_text().strip()
        c.passphrase = self._passphrase.get_text()
        c.jump_host = self._jump_host.get_text().strip()
        c.startup_command = self._startup_cmd.get_text().strip()
        c.keep_alive_interval = int(self._keep_alive.get_value())
        c.forward_agent = self._agent_forward.get_active()
        c.x11_forward = self._x11.get_active()
        c.compression = self._compress.get_active()

        c.rdp_domain = self._rdp_domain.get_text().strip()
        c.rdp_width = int(self._rdp_width.get_value())
        c.rdp_height = int(self._rdp_height.get_value())
        c.rdp_color_depth = _DEPTHS[self._rdp_depth.get_selected()]

        c.vnc_view_only = self._vnc_view_only.get_active()

        buf = self._notes.get_buffer()
        c.notes = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_save(self, _button) -> None:
        if not self._host.get_text().strip():
            self._stack.set_visible_child_name("basic")
            self._host.add_css_class("error")
            self._host.grab_focus()
            self._error.set_text("A host is required.")
            self._error.set_visible(True)
            return

        self._host.remove_css_class("error")
        self._error.set_visible(False)
        self._save_values()
        saved = self.db.save_connection(self._conn)
        if self._on_saved is not None:
            self._on_saved(saved)
        self.close()

    def _on_group_chosen(self, _button, name: str) -> None:
        self._group.set_text(name)
        self._group_btn.get_popover().popdown()

    def _on_browse_key(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Private Key File")
        ssh_dir = os.path.expanduser("~/.ssh")
        if os.path.isdir(ssh_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))

        def chosen(dlg, result) -> None:
            try:
                file = dlg.open_finish(result)
            except Exception:  # noqa: BLE001 — dismissed
                return
            if file is not None and file.get_path():
                self._key_file.set_text(file.get_path())

        dialog.open(self, None, chosen)

    def _on_colour_picked(self, *_args) -> None:
        rgba = self._colour_btn.get_rgba()
        self._colour_value = "#%02x%02x%02x" % (
            round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
        )
        self._colour_hint.set_text(self._colour_value)

    def _on_colour_cleared(self, _button) -> None:
        self._colour_value = ""
        self._colour_hint.set_text("No colour")

    def _apply_colour(self) -> None:
        rgba = Gdk.RGBA()
        if self._colour_value and rgba.parse(self._colour_value):
            # set_rgba fires notify::rgba, which would re-derive the same value
            self._colour_btn.set_rgba(rgba)
            self._colour_hint.set_text(self._colour_value)
        else:
            self._colour_hint.set_text("No colour")
