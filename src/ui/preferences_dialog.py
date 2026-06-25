"""App preferences dialog."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QDialogButtonBox,
    QSpinBox, QCheckBox, QLabel, QComboBox, QFrame,
)
from PyQt6.QtCore import Qt

from src.storage.database import Database
from src.app import Application

_LINUX = sys.platform.startswith("linux")

_SYSTEM_DEFAULT = "(system default)"


def _list_icon_themes() -> list[str]:
    """
    Return sorted names of icon themes installed on this Linux system.

    Searches the standard XDG icon directories and returns directory names
    that contain an ``index.theme`` file.  Always starts with the
    ``_SYSTEM_DEFAULT`` sentinel so the user can revert to the DE choice.
    """
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
                    # skip cursor-only themes (they rarely contain app icons)
                    and not entry.name.lower().endswith("-cursor")
                    and entry.name.lower() != "default"
                ):
                    seen.add(entry.name)
        except OSError:
            pass
    return [_SYSTEM_DEFAULT] + sorted(seen, key=str.casefold)


def _section_separator(title: str) -> QLabel:
    """Bold section header label — used as a section divider in the dialog."""
    lbl = QLabel(title)
    lbl.setStyleSheet(
        "font-weight: bold; color: palette(mid); "
        "border-top: 1px solid palette(mid); "
        "padding-top: 8px; margin-top: 4px;"
    )
    lbl.setContentsMargins(16, 0, 16, 0)
    return lbl


class PreferencesDialog(QDialog):
    """Simple preferences dialog backed by DB key/value store."""

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(380)
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)
        form.setContentsMargins(16, 16, 16, 16)

        self._font_size = QSpinBox()
        self._font_size.setRange(8, 24)
        self._font_size.setValue(int(db.get_pref("terminal_font_size", "13")))
        form.addRow("Terminal font size:", self._font_size)

        self._keep_alive = QSpinBox()
        self._keep_alive.setRange(0, 3600)
        self._keep_alive.setSuffix(" s")
        self._keep_alive.setValue(int(db.get_pref("default_keep_alive", "60")))
        self._keep_alive.setToolTip("Default keep-alive for new connections (0 = off)")
        form.addRow("Default keep-alive:", self._keep_alive)

        self._confirm_delete = QCheckBox()
        self._confirm_delete.setChecked(db.get_pref("confirm_delete", "1") == "1")
        form.addRow("Confirm before delete:", self._confirm_delete)

        self._theme = QComboBox()
        self._theme.addItems(["System", "Light", "Dark"])
        saved = db.get_pref("app_theme", "system")
        self._theme.setCurrentIndex({"system": 0, "light": 1, "dark": 2}.get(saved, 0))
        form.addRow("App theme:", self._theme)

        from src.ui.themes import theme_names
        self._terminal_theme = QComboBox()
        self._terminal_theme.addItems(theme_names())
        saved_tt = db.get_pref("terminal_theme", theme_names()[0])
        idx = self._terminal_theme.findText(saved_tt)
        self._terminal_theme.setCurrentIndex(max(0, idx))
        form.addRow("Terminal theme:", self._terminal_theme)

        # Icon theme — Linux / freedesktop only
        self._icon_theme: QComboBox | None = None
        if _LINUX:
            self._icon_theme = QComboBox()
            themes = _list_icon_themes()
            self._icon_theme.addItems(themes)
            saved_it = db.get_pref("icon_theme", "")
            it_label = saved_it if saved_it else _SYSTEM_DEFAULT
            it_idx = self._icon_theme.findText(it_label)
            self._icon_theme.setCurrentIndex(max(0, it_idx))
            self._icon_theme.setToolTip(
                "Freedesktop icon theme used for toolbar and panel icons.\n"
                "Popular choices: Papirus, Adwaita, Tango, Breeze, Numix."
            )
            form.addRow("Icon theme:", self._icon_theme)

        layout.addLayout(form)

        # ── Features & Plugins ────────────────────────────────────────────────
        layout.addWidget(_section_separator("Optional Protocols"))

        proto_form = QFormLayout()
        proto_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        proto_form.setSpacing(8)
        proto_form.setContentsMargins(16, 4, 16, 4)

        self._enable_rdp = QCheckBox()
        self._enable_rdp.setChecked(db.get_pref("enable_rdp", "0") == "1")
        self._enable_rdp.setToolTip(
            "Show RDP (Remote Desktop) as a protocol option when adding connections.\n"
            "Requires xfreerdp on macOS/Linux or uses built-in mstsc on Windows."
        )
        proto_form.addRow("Enable RDP support:", self._enable_rdp)

        self._enable_vnc = QCheckBox()
        self._enable_vnc.setChecked(db.get_pref("enable_vnc", "0") == "1")
        self._enable_vnc.setToolTip(
            "Show VNC as a protocol option when adding connections.\n"
            "Pure Python RFB 3.8 client — no external software needed."
        )
        proto_form.addRow("Enable VNC support:", self._enable_vnc)

        layout.addLayout(proto_form)

        layout.addWidget(_section_separator("Terminal Features"))

        feat_form = QFormLayout()
        feat_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        feat_form.setSpacing(8)
        feat_form.setContentsMargins(16, 4, 16, 4)

        self._feat_broadcast = QCheckBox()
        self._feat_broadcast.setChecked(db.get_pref("feature_broadcast", "0") == "1")
        self._feat_broadcast.setToolTip(
            "Show the 📡 Broadcast button in the toolbar.\n"
            "Broadcast mirrors your keystrokes to ALL open terminal sessions at once.\n"
            "Useful for running the same command on multiple servers simultaneously."
        )
        feat_form.addRow("Broadcast input (📡):", self._feat_broadcast)

        self._feat_logging = QCheckBox()
        self._feat_logging.setChecked(db.get_pref("feature_logging", "0") == "1")
        self._feat_logging.setToolTip(
            "Show the ⏺ Session Logging button in terminal tabs.\n"
            "Saves everything printed in the terminal to a plain-text file."
        )
        feat_form.addRow("Session logging (⏺):", self._feat_logging)

        self._feat_snippets = QCheckBox()
        self._feat_snippets.setChecked(db.get_pref("feature_snippets", "1") == "1")
        self._feat_snippets.setToolTip(
            "Show the ⚡ Commands panel button in terminal tabs.\n"
            "Lets you save frequently-used commands and send them with one click.\n"
            "Example: save 'sudo systemctl restart nginx' as 'Restart Nginx'."
        )
        feat_form.addRow("Commands / Snippets (⚡):", self._feat_snippets)

        self._feat_sftp = QCheckBox()
        self._feat_sftp.setChecked(db.get_pref("feature_sftp", "1") == "1")
        self._feat_sftp.setToolTip(
            "Show the 📁 SFTP panel button in terminal tabs.\n"
            "Built-in file browser for uploading/downloading files over SSH."
        )
        feat_form.addRow("SFTP file browser (📁):", self._feat_sftp)

        self._feat_tunnels = QCheckBox()
        self._feat_tunnels.setChecked(db.get_pref("feature_tunnels", "0") == "1")
        self._feat_tunnels.setToolTip(
            "Show the 🔀 Port Forwarding panel button in terminal tabs.\n"
            "SSH tunnels let you securely forward network ports through the SSH connection.\n"
            "Example: forward localhost:5432 → remote-db:5432 so your local DB client\n"
            "can reach a database that is not exposed to the internet."
        )
        feat_form.addRow("Port forwarding / Tunnels (🔀):", self._feat_tunnels)

        self._feat_mounts = QCheckBox()
        self._feat_mounts.setChecked(db.get_pref("feature_mounts", "0") == "1")
        self._feat_mounts.setToolTip(
            "Show the ⊕ Folder Mount panel button in terminal tabs.\n"
            "Exposes a local directory on the remote host (bidirectional).\n"
            "Requires sshfs on the remote server: sudo apt install sshfs"
        )
        feat_form.addRow("Folder mount (⊕):", self._feat_mounts)

        layout.addLayout(feat_form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Apply |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        layout.addWidget(btns)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Persist all settings and apply them immediately — dialog stays open."""
        self.db.set_pref("terminal_font_size", str(self._font_size.value()))
        self.db.set_pref("default_keep_alive", str(self._keep_alive.value()))
        self.db.set_pref("confirm_delete", "1" if self._confirm_delete.isChecked() else "0")

        theme = ["system", "light", "dark"][self._theme.currentIndex()]
        self.db.set_pref("app_theme", theme)
        Application.apply_theme(theme)

        terminal_theme_name = self._terminal_theme.currentText()
        self.db.set_pref("terminal_theme", terminal_theme_name)

        # Apply icon theme immediately (Linux only)
        if _LINUX and self._icon_theme is not None:
            chosen = self._icon_theme.currentText()
            it_name = "" if chosen == _SYSTEM_DEFAULT else chosen
            self.db.set_pref("icon_theme", it_name)
            Application.apply_icon_theme(it_name)

        # Optional protocols
        self.db.set_pref("enable_rdp", "1" if self._enable_rdp.isChecked() else "0")
        self.db.set_pref("enable_vnc", "1" if self._enable_vnc.isChecked() else "0")

        # Feature flags
        self.db.set_pref("feature_broadcast", "1" if self._feat_broadcast.isChecked() else "0")
        self.db.set_pref("feature_logging",   "1" if self._feat_logging.isChecked() else "0")
        self.db.set_pref("feature_snippets",  "1" if self._feat_snippets.isChecked() else "0")
        self.db.set_pref("feature_sftp",      "1" if self._feat_sftp.isChecked() else "0")
        self.db.set_pref("feature_tunnels",   "1" if self._feat_tunnels.isChecked() else "0")
        self.db.set_pref("feature_mounts",    "1" if self._feat_mounts.isChecked() else "0")

        # Ask the main window to refresh terminal colours and icons
        win = self.parent()
        if win is not None:
            if hasattr(win, "_apply_terminal_theme_from_prefs"):
                win._apply_terminal_theme_from_prefs()
            if hasattr(win, "_refresh_icons"):
                win._refresh_icons()
            if hasattr(win, "_apply_feature_prefs"):
                win._apply_feature_prefs()

    def _save(self) -> None:
        """Apply all settings and close the dialog (OK button)."""
        self._apply()
        self.accept()
