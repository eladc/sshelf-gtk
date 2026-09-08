"""SSH key-pair generation dialog.

Key generation runs on a worker thread (RSA-4096 takes seconds) and reports
back through GLib.idle_add, so the UI stays responsive.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from src.gtkui.widgets import Form, entry, hint

# label → (paramiko key type, bits, suggested filename)
_KEY_TYPES: list[tuple[str, str, int, str]] = [
    ("ed25519 (recommended)", "ed25519", 0, "id_ed25519"),
    ("ECDSA-256", "ecdsa", 256, "id_ecdsa"),
    ("ECDSA-384", "ecdsa", 384, "id_ecdsa"),
    ("ECDSA-521", "ecdsa", 521, "id_ecdsa"),
    ("RSA-2048", "rsa", 2048, "id_rsa"),
    ("RSA-4096", "rsa", 4096, "id_rsa"),
]

_FILENAME_RE = re.compile(r"^[\w.\-]+$")


def _generate_ed25519(path: Path, passphrase: Optional[str]) -> tuple[str, str]:
    """Generate an Ed25519 key.

    paramiko has no Ed25519Key.generate() — it can only load such keys — so
    this goes through `cryptography` and writes OpenSSH format directly.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=encryption,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "wb") as fh:
        fh.write(private_bytes)

    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return f"{public} sshelf-generated"


def _generate_key(key_type: str, bits: int, path: Path,
                  passphrase: Optional[str]) -> tuple[str, str]:
    """Generate a key pair on disk; returns (private path, public key text)."""
    path.parent.mkdir(parents=True, exist_ok=True)

    if key_type == "ed25519":
        pub_text = _generate_ed25519(path, passphrase)
    else:
        import paramiko

        if key_type == "ecdsa":
            key = paramiko.ECDSAKey.generate(bits=bits)
        else:
            key = paramiko.RSAKey.generate(bits)
        key.write_private_key_file(str(path), password=passphrase)
        pub_text = f"{key.get_name()} {key.get_base64()} sshelf-generated"

    # Re-assert 0600: O_CREAT leaves the mode alone when overwriting.
    path.chmod(0o600)
    Path(str(path) + ".pub").write_text(pub_text + "\n")
    return str(path), pub_text


class KeyGenerationDialog(Gtk.Window):
    """Generate an SSH key pair into ~/.ssh/."""

    def __init__(self, parent: Optional[Gtk.Window] = None) -> None:
        super().__init__(title="Generate SSH Key Pair")
        self._pub_key_text = ""
        self._busy = False

        self.set_modal(True)
        self.set_default_size(560, 420)
        if parent is not None:
            self.set_transient_for(parent)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(False)
        self.set_titlebar(header)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.close())
        header.pack_start(close)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_bottom(16)
        self.set_child(root)

        form = Form()
        self._key_type = Gtk.DropDown.new_from_strings([t[0] for t in _KEY_TYPES])
        self._key_type.set_halign(Gtk.Align.START)
        self._key_type.connect("notify::selected", self._on_type_changed)
        form.add("Key type:", self._key_type)

        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._filename = entry()
        self._filename.set_text("id_ed25519")
        name_row.append(self._filename)
        suffix = Gtk.Label(label="saved to ~/.ssh/")
        suffix.add_css_class("dim-label")
        name_row.append(suffix)
        form.add("Filename:", name_row)

        self._passphrase = Gtk.PasswordEntry()
        self._passphrase.set_show_peek_icon(True)
        self._passphrase.set_property("placeholder-text",
                                      "Leave blank for no passphrase")
        form.add("Passphrase:", self._passphrase)

        self._passphrase2 = Gtk.PasswordEntry()
        self._passphrase2.set_show_peek_icon(True)
        self._passphrase2.set_property("placeholder-text", "Confirm passphrase")
        form.add("Confirm:", self._passphrase2)
        root.append(form)

        self._generate = Gtk.Button(label="Generate Key Pair")
        self._generate.add_css_class("suggested-action")
        self._generate.set_margin_start(16)
        self._generate.set_margin_end(16)
        self._generate.connect("clicked", self._on_generate)
        root.append(self._generate)

        self._status = Gtk.Label(label="", xalign=0.0)
        self._status.set_wrap(True)
        self._status.add_css_class("dim-label")
        self._status.set_margin_start(16)
        self._status.set_margin_end(16)
        root.append(self._status)

        self._pubkey = Gtk.TextView()
        self._pubkey.set_editable(False)
        self._pubkey.set_wrap_mode(Gtk.WrapMode.CHAR)
        self._pubkey.set_monospace(True)
        self._pubkey.set_size_request(-1, 70)
        frame = Gtk.Frame()
        frame.set_margin_start(16)
        frame.set_margin_end(16)
        frame.set_child(self._pubkey)
        root.append(frame)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_start(16)
        btn_row.set_margin_end(16)
        self._copy_key = Gtk.Button(label="Copy Public Key")
        self._copy_key.set_sensitive(False)
        self._copy_key.connect("clicked", self._on_copy_key)
        btn_row.append(self._copy_key)

        self._copy_cmd = Gtk.Button(label="Copy ssh-copy-id Command")
        self._copy_cmd.set_sensitive(False)
        self._copy_cmd.connect("clicked", self._on_copy_cmd)
        btn_row.append(self._copy_cmd)
        root.append(btn_row)

        root.append(hint(
            "  The private key stays in ~/.ssh/. Share only the .pub file — "
            "ssh-copy-id installs it on a server for you."
        ))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_type_changed(self, *_args) -> None:
        self._filename.set_text(_KEY_TYPES[self._key_type.get_selected()][3])

    def _on_generate(self, _button) -> None:
        if self._busy:
            return

        passphrase = self._passphrase.get_text()
        if passphrase != self._passphrase2.get_text():
            self._fail("Passphrases do not match.")
            return

        name = self._filename.get_text().strip()
        if not name or not _FILENAME_RE.match(name):
            self._fail(
                "Filename may only contain letters, numbers, dots, hyphens "
                "and underscores."
            )
            return

        path = Path("~/.ssh").expanduser() / name
        if path.exists():
            self._confirm_overwrite(path, passphrase)
        else:
            self._start(path, passphrase)

    def _confirm_overwrite(self, path: Path, passphrase: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message("Overwrite existing key?")
        dialog.set_detail(f"{path} already exists.")
        dialog.set_buttons(["Cancel", "Overwrite"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(0)

        def answered(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) == 1:
                    self._start(path, passphrase)
            except Exception:  # noqa: BLE001 — dismissed
                pass

        dialog.choose(self, None, answered)

    def _start(self, path: Path, passphrase: str) -> None:
        index = self._key_type.get_selected()
        _, key_type, bits, _ = _KEY_TYPES[index]

        self._busy = True
        self._generate.set_sensitive(False)
        self._copy_key.set_sensitive(False)
        self._copy_cmd.set_sensitive(False)
        self._pubkey.get_buffer().set_text("")
        self._status.set_text("Generating key pair…")

        def work() -> None:
            try:
                priv, pub = _generate_key(key_type, bits, path, passphrase or None)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._done_error, str(exc))
            else:
                GLib.idle_add(self._done_ok, priv, pub)

        threading.Thread(target=work, daemon=True,
                         name="sshelf-keygen").start()

    def _done_ok(self, priv_path: str, pub_text: str) -> bool:
        self._busy = False
        self._pub_key_text = pub_text
        self._pubkey.get_buffer().set_text(pub_text)
        self._status.set_text(f"Key pair saved to {priv_path}")
        self._generate.set_sensitive(True)
        self._copy_key.set_sensitive(True)
        self._copy_cmd.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _done_error(self, message: str) -> bool:
        self._busy = False
        self._generate.set_sensitive(True)
        self._fail(message)
        return GLib.SOURCE_REMOVE

    def _fail(self, message: str) -> None:
        self._status.set_text(f"Error: {message}")

    def _clipboard(self) -> Gdk.Clipboard:
        return self.get_display().get_clipboard()

    def _on_copy_key(self, button) -> None:
        self._clipboard().set(self._pub_key_text)
        button.set_label("Copied!")
        GLib.timeout_add_seconds(
            2, lambda: (button.set_label("Copy Public Key"), GLib.SOURCE_REMOVE)[1]
        )

    def _on_copy_cmd(self, button) -> None:
        name = self._filename.get_text().strip()
        self._clipboard().set(f"ssh-copy-id -i ~/.ssh/{name}.pub user@hostname")
        button.set_label("Copied!")
        GLib.timeout_add_seconds(
            2,
            lambda: (button.set_label("Copy ssh-copy-id Command"),
                     GLib.SOURCE_REMOVE)[1],
        )
