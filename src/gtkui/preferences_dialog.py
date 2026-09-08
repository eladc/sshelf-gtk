"""App preferences dialog.

Settings live in the database's key/value preference store. Applying writes
them and calls back into the main window so open terminals pick up font and
colour changes immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from src.gtkui.widgets import Form, hint
from src.storage.database import Database
from src.ui.themes import theme_names  # pure data, no Qt import

_LINUX = sys.platform.startswith("linux")
_SYSTEM_DEFAULT = "(system default)"

_APP_THEMES = ["System", "Light", "Dark"]
_APP_THEME_KEYS = ["system", "light", "dark"]
_PALETTE_UIS = ["Terminal", "Graphical"]
_PALETTE_KEYS = ["terminal", "graphical"]


def list_icon_themes() -> list[str]:
    """Names of icon themes installed on this system, newest-first sentinel."""
    search_dirs = [
        Path.home() / ".icons",
        Path.home() / ".local" / "share" / "icons",
        Path("/usr/share/icons"),
        Path("/usr/local/share/icons"),
    ]
    seen: set[str] = set()
    for d in search_dirs:
        try:
            for entry in d.iterdir():
                if (
                    entry.is_dir()
                    and (entry / "index.theme").exists()
                    and not entry.name.startswith(".")
                    and not entry.name.lower().endswith("-cursor")
                    and entry.name.lower() != "default"
                ):
                    seen.add(entry.name)
        except OSError:
            pass
    return [_SYSTEM_DEFAULT] + sorted(seen, key=str.casefold)


class PreferencesDialog(Gtk.Window):
    """Preferences, grouped into General / Protocols / Terminal features."""

    def __init__(
        self,
        db: Database,
        parent: Optional[Gtk.Window] = None,
        on_applied: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(title="Preferences")
        self.db = db
        self._on_applied = on_applied

        self.set_modal(True)
        self.set_default_size(520, 620)
        if parent is not None:
            self.set_transient_for(parent)

        self._icon_themes = list_icon_themes() if _LINUX else []
        self._terminal_themes = theme_names()

        self._build_ui()

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

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", lambda *_: self._apply())
        header.pack_end(apply_btn)

        ok = Gtk.Button(label="OK")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._on_ok)
        header.pack_end(ok)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(box)
        self.set_child(scroller)

        db = self.db

        # ── General ───────────────────────────────────────────────────────
        form = Form()
        self._font_size = Gtk.SpinButton.new_with_range(8, 24, 1)
        self._font_size.set_value(int(db.get_pref("terminal_font_size", "13")))
        self._font_size.set_halign(Gtk.Align.START)
        form.add("Terminal font size:", self._font_size)

        self._keep_alive = Gtk.SpinButton.new_with_range(0, 3600, 5)
        self._keep_alive.set_value(int(db.get_pref("default_keep_alive", "60")))
        self._keep_alive.set_halign(Gtk.Align.START)
        self._keep_alive.set_tooltip_text(
            "Default keep-alive for new connections (0 = off)"
        )
        form.add("Default keep-alive (s):", self._keep_alive)

        self._confirm_delete = Gtk.CheckButton()
        self._confirm_delete.set_active(db.get_pref("confirm_delete", "1") == "1")
        form.add("Confirm before delete:", self._confirm_delete)

        self._theme = Gtk.DropDown.new_from_strings(_APP_THEMES)
        self._theme.set_halign(Gtk.Align.START)
        saved = db.get_pref("app_theme", "system")
        self._theme.set_selected(
            _APP_THEME_KEYS.index(saved) if saved in _APP_THEME_KEYS else 0
        )
        form.add("App theme:", self._theme)

        self._terminal_theme = Gtk.DropDown.new_from_strings(self._terminal_themes)
        self._terminal_theme.set_halign(Gtk.Align.START)
        saved_tt = db.get_pref("terminal_theme", self._terminal_themes[0])
        if saved_tt in self._terminal_themes:
            self._terminal_theme.set_selected(self._terminal_themes.index(saved_tt))
        form.add("Terminal theme:", self._terminal_theme)

        self._icon_theme: Optional[Gtk.DropDown] = None
        if _LINUX:
            self._icon_theme = Gtk.DropDown.new_from_strings(self._icon_themes)
            self._icon_theme.set_halign(Gtk.Align.START)
            saved_it = db.get_pref("icon_theme", "") or _SYSTEM_DEFAULT
            if saved_it in self._icon_themes:
                self._icon_theme.set_selected(self._icon_themes.index(saved_it))
            self._icon_theme.set_tooltip_text(
                "Freedesktop icon theme used for panel icons.\n"
                "Popular choices: Papirus, Adwaita, Tango, Breeze, Numix."
            )
            form.add("Icon theme:", self._icon_theme)

        box.append(form)

        # ── Optional protocols ────────────────────────────────────────────
        box.append(self._section("Optional Protocols"))
        proto = Form()

        self._enable_rdp = Gtk.CheckButton()
        self._enable_rdp.set_active(db.get_pref("enable_rdp", "0") == "1")
        self._enable_rdp.set_tooltip_text(
            "Offer RDP as a protocol when adding connections.\n"
            "Requires xfreerdp on macOS/Linux, or mstsc on Windows."
        )
        proto.add("Enable RDP support:", self._enable_rdp)

        self._enable_vnc = Gtk.CheckButton()
        self._enable_vnc.set_active(db.get_pref("enable_vnc", "0") == "1")
        self._enable_vnc.set_tooltip_text(
            "Offer VNC as a protocol when adding connections.\n"
            "Pure Python RFB 3.8 client — no external software needed."
        )
        proto.add("Enable VNC support:", self._enable_vnc)
        proto.add_wide(hint(
            "RDP and VNC sessions are not available in the GTK build yet; "
            "enabling them only adds the protocol to the connection editor."
        ))
        box.append(proto)

        # ── Terminal features ─────────────────────────────────────────────
        box.append(self._section("Terminal Features"))
        feat = Form()

        self._feat_snippets = self._flag(
            feat, "Commands / Snippets:", "feature_snippets", "1",
            "Show the Commands panel in terminal tabs — save frequently used\n"
            "commands and send them with one click.",
        )
        self._feat_sftp = self._flag(
            feat, "SFTP file browser:", "feature_sftp", "1",
            "Show the SFTP panel in terminal tabs — browse, upload and\n"
            "download files over the existing SSH connection.",
        )
        self._feat_tunnels = self._flag(
            feat, "Port forwarding:", "feature_tunnels", "0",
            "Show the port-forwarding panel in terminal tabs. SSH tunnels\n"
            "forward network ports securely through the SSH connection.",
        )
        self._feat_logging = self._flag(
            feat, "Session logging:", "feature_logging", "0",
            "Show the session logging button — saves terminal output to a\n"
            "plain-text file.",
        )
        self._feat_broadcast = self._flag(
            feat, "Broadcast input:", "feature_broadcast", "0",
            "Mirror keystrokes to all open terminal sessions at once.",
        )
        self._feat_mounts = self._flag(
            feat, "Folder mount:", "feature_mounts", "0",
            "Expose a local directory on the remote host. Requires sshfs on\n"
            "the remote server.",
        )

        self._snippet_palette_ui = Gtk.DropDown.new_from_strings(_PALETTE_UIS)
        self._snippet_palette_ui.set_halign(Gtk.Align.START)
        saved_palette = db.get_pref("snippet_palette_ui", "terminal")
        self._snippet_palette_ui.set_selected(
            _PALETTE_KEYS.index(saved_palette) if saved_palette in _PALETTE_KEYS else 0
        )
        self._snippet_palette_ui.set_tooltip_text(
            "How the CLI snippet palette looks when you press F2 or Ctrl-] in\n"
            "an SSH session started with `sshelf connect`."
        )
        feat.add("CLI snippet palette:", self._snippet_palette_ui)

        box.append(feat)

    def _section(self, title: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        label = Gtk.Label(label=title, xalign=0.0)
        label.add_css_class("heading")
        label.set_margin_start(16)
        label.set_margin_top(4)
        box.append(label)
        return box

    def _flag(self, form: Form, label: str, key: str,
              default: str, tooltip: str) -> Gtk.CheckButton:
        check = Gtk.CheckButton()
        check.set_active(self.db.get_pref(key, default) == "1")
        check.set_tooltip_text(tooltip)
        form.add(label, check)
        return check

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Write every setting; the dialog stays open."""
        db = self.db
        db.set_pref("terminal_font_size", str(int(self._font_size.get_value())))
        db.set_pref("default_keep_alive", str(int(self._keep_alive.get_value())))
        db.set_pref("confirm_delete", _bit(self._confirm_delete))
        db.set_pref("app_theme", _APP_THEME_KEYS[self._theme.get_selected()])
        db.set_pref(
            "terminal_theme", self._terminal_themes[self._terminal_theme.get_selected()]
        )

        if self._icon_theme is not None:
            chosen = self._icon_themes[self._icon_theme.get_selected()]
            db.set_pref("icon_theme", "" if chosen == _SYSTEM_DEFAULT else chosen)

        db.set_pref("enable_rdp", _bit(self._enable_rdp))
        db.set_pref("enable_vnc", _bit(self._enable_vnc))

        db.set_pref("feature_broadcast", _bit(self._feat_broadcast))
        db.set_pref("feature_logging", _bit(self._feat_logging))
        db.set_pref("feature_snippets", _bit(self._feat_snippets))
        db.set_pref("feature_sftp", _bit(self._feat_sftp))
        db.set_pref("feature_tunnels", _bit(self._feat_tunnels))
        db.set_pref("feature_mounts", _bit(self._feat_mounts))
        db.set_pref(
            "snippet_palette_ui", _PALETTE_KEYS[self._snippet_palette_ui.get_selected()]
        )

        if self._on_applied is not None:
            self._on_applied()

    def _on_ok(self, _button) -> None:
        self._apply()
        self.close()


def _bit(check: Gtk.CheckButton) -> str:
    return "1" if check.get_active() else "0"
