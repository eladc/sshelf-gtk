"""
Embedded SSH terminal widget backed by a pyte VT100 screen buffer.

Architecture
------------
  Raw bytes (SSH) ──► pyte.ByteStream ──► _Screen (pyte.HistoryScreen)
                                                 │
                          QTimer (16 ms) ─────────┘ triggers _render()
                                                 │
                      _PyteTerminal (QPlainTextEdit) ◄── draws screen

Alt-screen mode (vim, htop, …)
  - Detected via DECSET/DECRST 1049 → _Screen.in_alt_screen flag.
  - _render() does a clean full-redraw of the 50-line live buffer only;
    no history rows are touched, so vim can never "stack" frames.

Normal mode
  - History rows appended once (never redrawn).
  - Live screen section replaced on every frame.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path
from typing import Optional

_LINUX = sys.platform.startswith("linux")


def _ico(emoji: str, text: str) -> str:
    """Return plain text on Linux (emoji may not render), emoji elsewhere."""
    return text if _LINUX else emoji


def _apply_icon(btn, icon_name: str, text_fallback: str = "", size: int = 16) -> None:
    """
    On Linux set a freedesktop theme icon on *btn* (replacing its text).
    Falls back to *text_fallback* when the icon is not in the current theme.
    On macOS this is a no-op (emoji text is already set on the button).
    """
    if not _LINUX:
        return
    icon = QIcon.fromTheme(icon_name)
    if not icon.isNull():
        btn.setIcon(icon)
        btn.setIconSize(QSize(size, size))
        btn.setText("")
    elif text_fallback:
        btn.setText(text_fallback)

import pyte
from PyQt6.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal, QObject, QSize
from PyQt6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QKeyEvent, QPalette,
    QTextCharFormat, QTextCursor, QTextDocument,
)
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from src.models.connection import Connection
from src.protocols.ssh import SSHWorker

# ── Terminal dimensions ──────────────────────────────────────────────────────
_PTY_COLS_DEFAULT = 220
_PTY_ROWS_DEFAULT = 50
_HISTORY          = 2000   # scrollback lines kept in pyte
_FONT_SIZE_DEFAULT = 13
_FONT_SIZE_MIN     = 6
_FONT_SIZE_MAX     = 36

# ── Colour defaults (One Dark — overridden by apply_terminal_theme()) ─────────
_DEFAULT_FG = "#d4d4d4"
_DEFAULT_BG = "#1e1e1e"
_CURSOR_BG  = "#528bff"

# Named ANSI colours → One Dark palette
_NAMED: dict[str, str] = {
    "black":         "#282c34", "red":           "#e06c75",
    "green":         "#98c379", "yellow":        "#e5c07b",
    "blue":          "#61afef", "magenta":       "#c678dd",
    "cyan":          "#56b6c2", "white":         "#abb2bf",
    "brightblack":   "#5c6370", "brightred":     "#e06c75",
    "brightgreen":   "#98c379", "brightyellow":  "#e5c07b",
    "brightblue":    "#61afef", "brightmagenta": "#c678dd",
    "brightcyan":    "#56b6c2", "brightwhite":   "#ffffff",
}


# ── Theme application ─────────────────────────────────────────────────────────

def apply_terminal_theme(theme) -> None:
    """
    Patch the module-level colour globals from a ``Theme`` object.

    Call this before (or after) opening terminals; existing terminals
    should call ``_PyteTerminal.refresh_theme()`` to repaint.
    """
    global _DEFAULT_FG, _DEFAULT_BG, _CURSOR_BG, _NAMED
    _DEFAULT_FG = theme.fg
    _DEFAULT_BG = theme.bg
    _CURSOR_BG  = theme.cursor
    _NAMED = {
        "black":         theme.black,
        "red":           theme.red,
        "green":         theme.green,
        "yellow":        theme.yellow,
        "blue":          theme.blue,
        "magenta":       theme.magenta,
        "cyan":          theme.cyan,
        "white":         theme.white,
        "brightblack":   theme.bright_black,
        "brightred":     theme.bright_red,
        "brightgreen":   theme.bright_green,
        "brightyellow":  theme.bright_yellow,
        "brightblue":    theme.bright_blue,
        "brightmagenta": theme.bright_magenta,
        "brightcyan":    theme.bright_cyan,
        "brightwhite":   theme.bright_white,
    }

# Strip ANSI escape codes from raw bytes (for session log)
_ANSI_RE = re.compile(rb"\x1b(?:[@-Z\\-_]|\[[0-9;]*[ -/]*[@-~])")

# OSC 7: shells that emit "file://hostname/path" CWD notifications
_OSC7_RE = re.compile(rb"\x1b\]7;file://[^\x07/]*(/[^\x07]*)\x07")


def _fmt_bytes(n: int) -> str:
    """Format a byte count as a human-readable string."""
    if n < 1024:
        return f"{n} B"
    if n < 1_048_576:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1_048_576:.1f} MB"


# ── Colour helpers ────────────────────────────────────────────────────────────

def _pyte_color(value: object, is_fg: bool) -> str:
    """Convert a pyte colour value (string, int, tuple) to #RRGGBB."""
    if not value or value == "default":
        return _DEFAULT_FG if is_fg else _DEFAULT_BG
    if isinstance(value, int):
        return _xterm256(value)
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return "#{:02x}{:02x}{:02x}".format(int(value[0]), int(value[1]), int(value[2]))
    if isinstance(value, str):
        if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
            return f"#{value}"
        return _NAMED.get(value, _DEFAULT_FG if is_fg else _DEFAULT_BG)
    return _DEFAULT_FG if is_fg else _DEFAULT_BG


def _xterm256(n: int) -> str:
    """xterm 256-colour index → #RRGGBB."""
    if n < 16:
        return list(_NAMED.values())[n % 16]
    if n < 232:
        n -= 16
        b = n % 6; n //= 6
        g = n % 6; r = n // 6
        def v(x: int) -> int: return 0 if x == 0 else 55 + x * 40
        return f"#{v(r):02x}{v(g):02x}{v(b):02x}"
    grey = 8 + (n - 232) * 10
    return f"#{grey:02x}{grey:02x}{grey:02x}"


# ── Patched pyte screen ───────────────────────────────────────────────────────

class _Screen(pyte.HistoryScreen):
    """
    Extends HistoryScreen with two fixes:

    1.  select_graphic_rendition(*attrs, private=False)
        pyte's stream dispatcher calls this with private=True for DEC private
        CSI sequences ending in 'm' (e.g. \x1b[?1m).  pyte's own Screen does
        not accept that keyword → TypeError crash.  We accept and ignore it.

    2.  in_alt_screen flag
        We intercept set_mode / reset_mode for private mode 1049
        (the "save cursor + switch to alternate screen" sequence used by vim,
        htop, less, etc.) and expose a plain boolean.  The renderer uses this
        to skip history and do a clean full-redraw instead.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.in_alt_screen: bool = False

    # ── Fix 1: accept private kwarg on SGR ───────────────────────────────────

    def select_graphic_rendition(self, *attrs: int, private: bool = False) -> None:
        if not private:
            super().select_graphic_rendition(*attrs)

    # ── Fix 2: track alternate-screen mode ───────────────────────────────────

    def set_mode(self, *modes: int, private: bool = False) -> None:
        super().set_mode(*modes, private=private)
        if private and 1049 in modes:
            self.in_alt_screen = True

    def reset_mode(self, *modes: int, private: bool = False) -> None:
        super().reset_mode(*modes, private=private)
        if private and 1049 in modes:
            self.in_alt_screen = False


# ── CWD detection worker ──────────────────────────────────────────────────────

class _GetCwdWorker(QObject):
    """
    Opens a short-lived exec channel on the existing SSH transport and runs a
    shell one-liner that finds the interactive shell's CWD via /proc.

    Two common sshd process layouts:

      OpenSSH (two-level):
        sshd-session (GPPID)
          ├── bash          ← interactive shell  (PPid = GPPID)
          └── exec-sshd     ← exec channel fork  (PPid = GPPID, PID = $PPID)
                └── sh      ← this probe         (PPid = $PPID)

      dropbear / flat (one-level):
        dropbear (= $PPID)
          ├── bash          ← interactive shell  (PPid = $PPID)
          └── sh            ← this probe         (PPid = $PPID)

    Stage 1 (Linux/Android): scan /proc — GPPID children first (OpenSSH),
    then $PPID children (dropbear).  Exits immediately with the CWD.
    Stage 2 (macOS/BSD): /proc is absent; use `ps` to locate the shell PID
    and `lsof` to read its open CWD file descriptor.
    If both stages fail we emit the OSC-7 fallback or nothing.
    """

    cwd_found = pyqtSignal(str)
    finished  = pyqtSignal()

    # Two-stage probe that works on Linux (/proc) and macOS (ps + lsof):
    #
    #   Stage 1 – Linux/Android: scan /proc for a shell whose PPid matches
    #             GPPID (OpenSSH) or $PPID (dropbear/flat sshd).
    #   Stage 2 – macOS/BSD: use `ps` to find the shell PID, then `lsof`
    #             to read its CWD (macOS has no /proc/$pid/cwd).
    #
    # GPPID is obtained from /proc on Linux or `ps -o ppid=` on macOS.
    # If neither yields a result we exit silently; the caller falls back
    # to the OSC-7 path stored in _remote_cwd.
    _CMD = (
        # Resolve GPPID: common ancestor of interactive shell and exec channel.
        # Linux gets it from /proc; macOS/BSD falls back to `ps`.
        "GPPID=$(awk '/^PPid:/{print $2}' /proc/$PPID/status 2>/dev/null);"
        "[ -z \"$GPPID\" ]"
        " && GPPID=$(ps -o ppid= -p \"$PPID\" 2>/dev/null | tr -d ' ');"
        "[ -z \"$GPPID\" ] && exit 1;"
        # Stage 1: Linux /proc — try GPPID children (OpenSSH), then $PPID (dropbear)
        "for pv in \"$GPPID\" \"$PPID\"; do"
        " for pid in $(ls /proc 2>/dev/null | grep '^[0-9]'); do"
        "  [ \"$pid\" = \"$$\" ] && continue;"
        "  [ \"$pid\" = \"$PPID\" ] && continue;"
        "  pp=$(awk '/^PPid:/{print $2}' /proc/$pid/status 2>/dev/null);"
        "  [ \"$pp\" = \"$pv\" ] || continue;"
        "  comm=$(cat /proc/$pid/comm 2>/dev/null);"
        "  case \"$comm\" in bash|zsh|sh|fish|dash|-bash|-zsh)"
        "   cwd=$(readlink /proc/$pid/cwd 2>/dev/null);"
        "   [ -n \"$cwd\" ] && echo \"$cwd\" && exit 0;;"
        "  esac;"
        " done;"
        "done;"
        # Stage 2: macOS/BSD — find shell via ps, read CWD via lsof
        "spid=$(ps -axo pid=,ppid=,comm= 2>/dev/null | awk"
        " -v g=\"$GPPID\" -v p=\"$PPID\" -v me=\"$$\""
        " '($2==g||$2==p)&&$1!=me&&$1!=p"
        "  &&($3==\"bash\"||$3==\"zsh\"||$3==\"sh\""
        "    ||$3==\"fish\"||$3==\"dash\"||$3==\"-bash\"||$3==\"-zsh\")"
        "  {print $1;exit}');"
        "[ -z \"$spid\" ] && exit 1;"
        "lsof -p \"$spid\" -a -d cwd -Fn 2>/dev/null"
        " | awk '/^n/{print substr($0,2);exit}'"
    )

    def __init__(self, worker: SSHWorker, fallback: str = "") -> None:
        super().__init__()
        self._worker   = worker
        self._fallback = fallback   # OSC-7 path to use when /proc is absent

    def run(self) -> None:
        cwd = ""
        try:
            transport = self._worker.get_transport()
            if transport:
                chan = transport.open_session()
                chan.settimeout(5)
                chan.exec_command(self._CMD)
                out = chan.makefile("r")
                line = out.readline()
                cwd = line.strip() if line else ""
                chan.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.cwd_found.emit(cwd or self._fallback)
            self.finished.emit()


# ── SFTP setup worker ─────────────────────────────────────────────────────────

class _OpenSFTPWorker(QObject):
    """Opens an SFTP session on the existing SSH connection (background thread)."""

    ready    = pyqtSignal(object)   # paramiko.SFTPClient
    finished = pyqtSignal()

    def __init__(self, worker: SSHWorker) -> None:
        super().__init__()
        self._worker = worker

    def run(self) -> None:
        try:
            sftp = self._worker.open_sftp()
            if sftp:
                self.ready.emit(sftp)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.finished.emit()


# ── Main widget ───────────────────────────────────────────────────────────────

class TerminalWidget(QWidget):
    """
    Right-panel widget that opens an interactive SSH session and embeds a
    full VT100 terminal emulator (pyte) so programs like vim / htop work.

    New in this revision
    --------------------
    - Auto-reconnect bar shown on unexpected disconnect (errors).
    - Ctrl+F search bar to find text in the scrollback.
    - Session logging toggle (⏺/⏹) in the header — writes plain text to
      ~/Library/Application Support/sshelf/logs/.
    - Side panel (⚡ / 📁 buttons) with a Commands tab (SnippetsPanel)
      and an SFTP tab (SFTPPanel).
    """

    status_message  = pyqtSignal(str)
    disconnected    = pyqtSignal(str)
    split_requested = pyqtSignal()          # user clicked ⊞ Split
    health_changed  = pyqtSignal(int, str)  # (conn_id, "connected"|"error"|"disconnected")
    key_input       = pyqtSignal(bytes)     # emitted when broadcast is active

    def __init__(self, connection: Connection, db=None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._conn  = connection
        self._db    = db
        self._thread: Optional[QThread] = None
        self._worker: Optional[SSHWorker] = None
        self._sftp_thread: Optional[QThread] = None
        self._sftp_worker = None
        self._had_error = False
        self._closing   = False   # set by _on_disconnect to suppress reconnect bar
        self._log_file  = None
        self._bytes_rx: int = 0
        self._bytes_tx: int = 0
        self._stats_timer: Optional[QTimer] = None
        self._remote_cwd: str = ""
        self._cwd_thread: Optional[QThread] = None
        self._cwd_worker = None
        self._build_ui()
        self._restore_font_size()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet("background: #2b2b2b;")
        header.setFixedHeight(32)
        hbar = QHBoxLayout(header)
        hbar.setContentsMargins(10, 0, 10, 0)
        hbar.setSpacing(4)

        self._title_lbl = QLabel(
            f"{'🔑  ' if not _LINUX else ''}{self._conn.connection_string()}"
        )
        self._title_lbl.setStyleSheet("color: #ccc; font-size: 12px;")
        hbar.addWidget(self._title_lbl)
        hbar.addStretch()

        # Byte counter (hidden until connected)
        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet("color: #666; font-size: 11px; padding: 0 6px;")
        self._stats_lbl.setToolTip("Bytes received / sent this session")
        self._stats_lbl.hide()
        hbar.addWidget(self._stats_lbl)

        # Search toggle (🔍 is emoji-range → use theme icon on Linux)
        self._btn_search = QPushButton("" if _LINUX else "🔍")
        self._btn_search.setToolTip("Search (Ctrl+F)")
        self._btn_search.setFixedSize(26, 26)
        self._btn_search.setStyleSheet(self._hdr_btn_style())
        self._btn_search.clicked.connect(self._toggle_search)
        _apply_icon(self._btn_search, "edit-find", "·/·")
        hbar.addWidget(self._btn_search)

        # Logging toggle (⏺ may not render in all Linux fonts → use theme icon)
        self._log_btn = QPushButton("" if _LINUX else "⏺")
        self._log_btn.setToolTip("Start session logging")
        self._log_btn.setFixedSize(26, 26)
        self._log_btn.setStyleSheet(self._hdr_btn_style())
        self._log_btn.clicked.connect(self._toggle_logging)
        _apply_icon(self._log_btn, "media-record", "●")
        hbar.addWidget(self._log_btn)
        if self._db and self._db.get_pref("feature_logging", "0") != "1":
            self._log_btn.hide()

        # Commands / snippets toggle (⚡ is BMP, but system-run is cleaner on Linux)
        self._btn_cmds = QPushButton("" if _LINUX else "⚡")
        self._btn_cmds.setToolTip("Commands panel")
        self._btn_cmds.setFixedSize(26, 26)
        self._btn_cmds.setStyleSheet(self._hdr_btn_style())
        _apply_icon(self._btn_cmds, "system-run", "⚡")
        hbar.addWidget(self._btn_cmds)
        # connection wired below after side panel tabs are counted

        # SFTP toggle (📁 is emoji → folder theme icon)
        self._btn_sftp = QPushButton("" if _LINUX else "📁")
        self._btn_sftp.setToolTip("SFTP file browser")
        self._btn_sftp.setFixedSize(26, 26)
        self._btn_sftp.setStyleSheet(self._hdr_btn_style())
        _apply_icon(self._btn_sftp, "folder", "/")
        hbar.addWidget(self._btn_sftp)
        # connection wired below after side panel tabs are counted

        # Tunnel panel toggle (🔀 is emoji → network icon)
        self._btn_tunnels = QPushButton("" if _LINUX else "🔀")
        self._btn_tunnels.setToolTip("Port forwarding tunnels")
        self._btn_tunnels.setFixedSize(26, 26)
        self._btn_tunnels.setStyleSheet(self._hdr_btn_style())
        _apply_icon(self._btn_tunnels, "network-transmit-receive", "⇄")
        hbar.addWidget(self._btn_tunnels)
        # connection wired below after side panel tabs are counted

        # Mount panel toggle (⊕ — expose local dir on remote)
        self._btn_mounts = QPushButton("" if _LINUX else "⊕")
        self._btn_mounts.setToolTip("Folder mounts (local dir → remote)")
        self._btn_mounts.setFixedSize(26, 26)
        self._btn_mounts.setStyleSheet(self._hdr_btn_style())
        _apply_icon(self._btn_mounts, "folder-remote", "⊕")
        hbar.addWidget(self._btn_mounts)
        # connection wired below after side panel tabs are counted

        # Split pane (⊞ is BMP U+229E — renders fine in standard Linux fonts)
        btn_split = QPushButton("⊞")
        btn_split.setToolTip("Split pane — open a new terminal alongside this one")
        btn_split.setFixedSize(26, 26)
        btn_split.setStyleSheet(self._hdr_btn_style())
        btn_split.clicked.connect(self.split_requested)
        hbar.addWidget(btn_split)

        # Separator
        sep = QWidget(); sep.setFixedWidth(6)
        hbar.addWidget(sep)

        btn_disc = QPushButton("✕ Disconnect")
        btn_disc.setStyleSheet(
            "QPushButton{color:#f55;background:transparent;border:none;}"
            "QPushButton:hover{color:#fff;}"
        )
        btn_disc.clicked.connect(self._on_disconnect)
        hbar.addWidget(btn_disc)
        layout.addWidget(header)

        # ── Search bar (hidden) ───────────────────────────────────────────────
        self._search_bar = QWidget()
        self._search_bar.setStyleSheet(
            "background: #333; border-bottom: 1px solid #555;"
        )
        self._search_bar.setFixedHeight(36)
        sbar = QHBoxLayout(self._search_bar)
        sbar.setContentsMargins(8, 2, 8, 2)
        sbar.setSpacing(4)
        sbar.addWidget(QLabel("Find:"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search in scrollback…")
        self._search_input.setFixedWidth(240)
        self._search_input.returnPressed.connect(self._search_next)
        self._search_input.textChanged.connect(self._on_search_text_changed)
        sbar.addWidget(self._search_input)
        btn_prev = QPushButton("◀")
        btn_prev.setFixedSize(26, 26)
        btn_prev.setToolTip("Previous match")
        btn_prev.clicked.connect(self._search_prev)
        sbar.addWidget(btn_prev)
        btn_next_s = QPushButton("▶")
        btn_next_s.setFixedSize(26, 26)
        btn_next_s.setToolTip("Next match")
        btn_next_s.clicked.connect(self._search_next)
        sbar.addWidget(btn_next_s)
        self._search_count_lbl = QLabel("")
        self._search_count_lbl.setStyleSheet("color: #888; font-size: 11px;")
        sbar.addWidget(self._search_count_lbl)
        sbar.addStretch()
        btn_close_s = QPushButton("✕")
        btn_close_s.setFixedSize(24, 24)
        btn_close_s.setStyleSheet("QPushButton{color:#aaa;background:transparent;border:none;}")
        btn_close_s.clicked.connect(self._hide_search)
        sbar.addWidget(btn_close_s)
        self._search_bar.hide()
        layout.addWidget(self._search_bar)

        # ── Content: terminal + side panel ───────────────────────────────────
        self._content_splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self._content_splitter, stretch=1)

        # Terminal
        self._output = _PyteTerminal(self)
        self._output.key_pressed.connect(self._on_key)
        self._output.resize_pty.connect(self._on_resize_pty)
        self._output.search_requested.connect(self._toggle_search)
        self._output.font_size_changed.connect(self._on_font_size_changed)
        self._content_splitter.addWidget(self._output)

        # Side panel (hidden by default)
        self._side_panel = QWidget()
        self._side_panel.setMinimumWidth(240)
        self._side_panel.setStyleSheet("background: #1e1e1e;")
        sp_layout = QVBoxLayout(self._side_panel)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        sp_layout.setSpacing(0)

        from PyQt6.QtWidgets import QTabWidget
        self._side_tabs = QTabWidget()
        self._side_tabs.setStyleSheet(
            "QTabWidget::pane { border: none; }"
            "QTabBar::tab { background: #2b2b2b; color: #ccc; padding: 4px 8px; }"
            "QTabBar::tab:selected { background: #1e1e1e; }"
        )
        sp_layout.addWidget(self._side_tabs)

        # Read feature prefs (default all on when db unavailable)
        _db = self._db
        _feat_snippets = (not _db) or _db.get_pref("feature_snippets", "1") == "1"
        _feat_sftp     = (not _db) or _db.get_pref("feature_sftp",     "1") == "1"
        _feat_tunnels  = (not _db) or _db.get_pref("feature_tunnels",  "0") == "1"
        _feat_mounts   = (not _db) or _db.get_pref("feature_mounts",   "0") == "1"

        _tab_idx = 0  # tracks actual index as we conditionally add tabs

        # ── Snippets tab ─────────────────────────────────────────────────────
        if _feat_snippets:
            from src.ui.snippets_panel import SnippetsPanel
            self._snippets_panel = SnippetsPanel(
                db=self._db,
                conn_id=self._conn.id,
                parent=self,
            )
            self._snippets_panel.send_command.connect(
                lambda cmd: self._on_key(cmd.encode("utf-8", errors="replace"))
            )
            if _LINUX:
                self._side_tabs.addTab(
                    self._snippets_panel,
                    QIcon.fromTheme("system-run"),
                    "Commands",
                )
            else:
                self._side_tabs.addTab(self._snippets_panel, "⚡ Commands")
            _cmds_idx = _tab_idx
            _tab_idx += 1
            self._btn_cmds.clicked.connect(
                lambda checked=False, i=_cmds_idx: self._show_side_tab(i)
            )
        else:
            self._snippets_panel = None
            self._btn_cmds.hide()

        # ── SFTP tab ──────────────────────────────────────────────────────────
        if _feat_sftp:
            from src.ui.sftp_panel import SFTPPanel
            self._sftp_panel = SFTPPanel(self)
            if _LINUX:
                self._side_tabs.addTab(
                    self._sftp_panel,
                    QIcon.fromTheme("folder"),
                    "SFTP",
                )
            else:
                self._side_tabs.addTab(self._sftp_panel, "📁 SFTP")
            _sftp_idx = _tab_idx
            _tab_idx += 1
            self._btn_sftp.clicked.connect(
                lambda checked=False, i=_sftp_idx: self._show_side_tab(i)
            )
        else:
            self._sftp_panel = None
            self._btn_sftp.hide()

        # ── Tunnels tab ───────────────────────────────────────────────────────
        if _feat_tunnels:
            from src.ui.tunnel_panel import TunnelPanel
            self._tunnel_panel = TunnelPanel(
                db=self._db,
                conn_id=self._conn.id,
                parent=self,
            )
            if _LINUX:
                self._side_tabs.addTab(
                    self._tunnel_panel,
                    QIcon.fromTheme("network-transmit-receive"),
                    "Tunnels",
                )
            else:
                self._side_tabs.addTab(self._tunnel_panel, "🔀 Tunnels")
            _tunnels_idx = _tab_idx
            self._btn_tunnels.clicked.connect(
                lambda checked=False, i=_tunnels_idx: self._show_side_tab(i)
            )
        else:
            self._tunnel_panel = None
            self._btn_tunnels.hide()

        # ── Mounts tab ────────────────────────────────────────────────────────
        if _feat_mounts:
            from src.ui.mount_panel import MountPanel
            self._mount_panel = MountPanel(
                db=self._db,
                conn_id=self._conn.id,
                parent=self,
            )
            if _LINUX:
                self._side_tabs.addTab(
                    self._mount_panel,
                    QIcon.fromTheme("folder-remote"),
                    "Mounts",
                )
            else:
                self._side_tabs.addTab(self._mount_panel, "⊕ Mounts")
            _mounts_idx = _tab_idx
            self._btn_mounts.clicked.connect(
                lambda checked=False, i=_mounts_idx: self._show_side_tab(i)
            )
        else:
            self._mount_panel = None
            self._btn_mounts.hide()

        self._content_splitter.addWidget(self._side_panel)
        self._side_panel.hide()

        # ── Reconnect bar (hidden) ────────────────────────────────────────────
        self._reconnect_bar = QWidget()
        self._reconnect_bar.setStyleSheet(
            "background: #2d1515; border-top: 1px solid #7a2020;"
        )
        self._reconnect_bar.setFixedHeight(36)
        rbar = QHBoxLayout(self._reconnect_bar)
        rbar.setContentsMargins(10, 4, 10, 4)
        self._reconnect_msg = QLabel("Connection lost")
        self._reconnect_msg.setStyleSheet("color: #ff7070;")
        rbar.addWidget(self._reconnect_msg)
        rbar.addStretch()
        btn_reconnect = QPushButton("↺  Reconnect")
        btn_reconnect.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;border-radius:4px;border:none;padding:4px 10px;}"
            "QPushButton:hover{background:#e74c3c;}"
        )
        btn_reconnect.clicked.connect(self._on_reconnect)
        rbar.addWidget(btn_reconnect)
        self._reconnect_bar.hide()
        layout.addWidget(self._reconnect_bar)

        # ── Status bar ───────────────────────────────────────────────────────
        self._status = QLabel("Connecting…")
        self._status.setStyleSheet(
            "background:#1e1e1e;color:#888;padding:2px 8px;font-size:11px;"
        )
        layout.addWidget(self._status)

    @staticmethod
    def _hdr_btn_style() -> str:
        return (
            "QPushButton{background:transparent;border:none;color:#aaa;font-size:14px;}"
            "QPushButton:hover{color:#fff;background:#444;border-radius:4px;}"
        )

    def _set_status(self, msg: str) -> None:
        self._status.setText(msg)
        self.status_message.emit(msg)

    # ── Search ───────────────────────────────────────────────────────────────

    def _toggle_search(self) -> None:
        if self._search_bar.isVisible():
            self._hide_search()
        else:
            self._search_bar.show()
            self._search_input.setFocus()
            self._search_input.selectAll()

    def _hide_search(self) -> None:
        self._search_bar.hide()
        # Clear search highlight
        self._output.setExtraSelections([])
        self._search_count_lbl.setText("")
        self._output.setFocus()

    def _on_search_text_changed(self, text: str) -> None:
        if text:
            self._search_next(wrap=False)

    def _search_next(self, wrap: bool = True) -> None:
        text = self._search_input.text()
        if not text:
            return
        found = self._output.find(text)
        if not found and wrap:
            cur = self._output.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.Start)
            self._output.setTextCursor(cur)
            self._output.find(text)

    def _search_prev(self) -> None:
        text = self._search_input.text()
        if not text:
            return
        found = self._output.find(text, QTextDocument.FindFlag.FindBackward)
        if not found:
            cur = self._output.textCursor()
            cur.movePosition(QTextCursor.MoveOperation.End)
            self._output.setTextCursor(cur)
            self._output.find(text, QTextDocument.FindFlag.FindBackward)

    # ── Logging ──────────────────────────────────────────────────────────────

    def _toggle_logging(self) -> None:
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
            # Restore "start recording" icon/text
            if _LINUX:
                _apply_icon(self._log_btn, "media-record", "●")
            else:
                self._log_btn.setIcon(QIcon())
                self._log_btn.setText("⏺")
            self._log_btn.setToolTip("Start session logging")
            self._set_status("Logging stopped.")
        else:
            log_dir = (
                Path.home() / "Library" / "Application Support"
                / "sshelf" / "logs"
            )
            log_dir.mkdir(parents=True, exist_ok=True)
            ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            name = re.sub(r"[^\w.-]", "_", self._conn.display_name())
            path = log_dir / f"{name}_{ts}.log"
            try:
                self._log_file = open(path, "wb")  # noqa: WPS515
                # Switch to "stop recording" icon/text
                if _LINUX:
                    _apply_icon(self._log_btn, "media-playback-stop", "■")
                else:
                    self._log_btn.setIcon(QIcon())
                    self._log_btn.setText("⏹")
                self._log_btn.setToolTip(f"Stop logging  ({path.name})")
                self._set_status(f"Logging to {path.name}")
            except OSError as exc:
                self._set_status(f"Cannot open log file: {exc}")

    # ── Side panel ───────────────────────────────────────────────────────────

    def _show_side_tab(self, index: int) -> None:
        """Show the side panel and switch to the given tab index."""
        if self._side_panel.isVisible() and self._side_tabs.currentIndex() == index:
            self._side_panel.hide()
        else:
            self._side_panel.show()
            self._side_tabs.setCurrentIndex(index)
            sizes = self._content_splitter.sizes()
            total = sum(sizes)
            if sizes[1] < 200:
                self._content_splitter.setSizes([total - 280, 280])
            # When opening SFTP panel: run the exec-channel /proc probe to get
            # the shell's actual current directory (always fresh, not stale).
            # OSC 7 (_remote_cwd) is the fallback when no worker is available.
            if index == 1:
                if self._worker:
                    self._detect_cwd_async()
                elif self._remote_cwd and self._sftp_panel:
                    self._sftp_panel.navigate_to(self._remote_cwd)

    def refresh_icons(self) -> None:
        """
        Re-apply header button icons from the current freedesktop icon theme.

        Called by MainWindow after the user changes the icon theme in
        Preferences so that already-open terminal panes update immediately.
        Has no effect on non-Linux platforms.
        """
        if not _LINUX:
            return
        _apply_icon(self._btn_search, "edit-find", "·/·")
        # Only refresh the log button back to "record" if not currently logging
        if not self._log_file:
            _apply_icon(self._log_btn, "media-record", "●")
        else:
            _apply_icon(self._log_btn, "media-playback-stop", "■")
        _apply_icon(self._btn_cmds,    "system-run",                "⚡")
        _apply_icon(self._btn_sftp,    "folder",                    "/")
        _apply_icon(self._btn_tunnels, "network-transmit-receive",  "⇄")
        # Refresh side-panel tab icons (indices depend on which tabs are enabled)
        for i in range(self._side_tabs.count()):
            txt = self._side_tabs.tabText(i)
            if "Commands" in txt or "⚡" in txt:
                self._side_tabs.setTabIcon(i, QIcon.fromTheme("system-run"))
            elif "SFTP" in txt or "📁" in txt:
                self._side_tabs.setTabIcon(i, QIcon.fromTheme("folder"))
        self._side_tabs.setTabIcon(2, QIcon.fromTheme("network-transmit-receive"))

    # ── Per-connection font size ──────────────────────────────────────────────

    def _restore_font_size(self) -> None:
        """Apply the saved per-connection font size (if any) after UI is built."""
        if self._conn.id is None or self._db is None:
            return
        raw = self._db.get_pref(f"font_size_{self._conn.id}")
        if not raw:
            return
        try:
            sz = int(raw)
            font = self._output.font()
            font.setPointSize(max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, sz)))
            self._output.setFont(font)
            self._output._sync_pty_size()
        except (ValueError, AttributeError):
            pass

    def _on_font_size_changed(self, size: int) -> None:
        """Persist the new font size for this connection in preferences."""
        if self._conn.id is not None and self._db is not None:
            self._db.set_pref(f"font_size_{self._conn.id}", str(size))

    def _detect_cwd_async(self) -> None:
        """Open an exec channel in the background to detect the shell's CWD."""
        if self._cwd_thread and self._cwd_thread.isRunning():
            return
        self._cwd_thread = QThread(self)
        self._cwd_worker = _GetCwdWorker(self._worker, fallback=self._remote_cwd)
        self._cwd_worker.moveToThread(self._cwd_thread)
        self._cwd_thread.started.connect(self._cwd_worker.run)
        self._cwd_worker.cwd_found.connect(self._on_cwd_detected)
        self._cwd_worker.finished.connect(self._cwd_thread.quit)
        self._cwd_thread.start()

    def _on_cwd_detected(self, cwd: str) -> None:
        """Called when the exec-channel CWD probe finishes."""
        if not cwd:
            return
        self._remote_cwd = cwd
        if self._sftp_panel:
            self._sftp_panel.navigate_to(cwd)

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def start_connection(self) -> None:
        self._had_error = False
        self._thread = QThread(self)
        self._worker = SSHWorker(self._conn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.connected.connect(self._on_connected)
        self._worker.data_received.connect(self._on_data)
        self._worker.error.connect(self._on_error)
        self._worker.host_key_prompt.connect(self._on_host_key_prompt)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_host_key_prompt(self, hostname: str, keytype: str, fingerprint: str) -> None:
        """Ask whether to trust a host key we have never seen before.

        Runs on the GUI thread; the worker thread is blocked until answered.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unknown host key")
        box.setText(f"The authenticity of host '{hostname}' can't be established.")
        box.setInformativeText(
            f"{keytype} key fingerprint:\n{fingerprint}\n\n"
            "This is expected the first time you connect to this host. If you "
            "did not expect it, someone may be impersonating the host."
        )
        trust = box.addButton("Trust and connect", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if self._worker:
            self._worker.answer_host_key_prompt(box.clickedButton() is trust)

    def _on_disconnect(self) -> None:
        """User-initiated disconnect — always closes the tab."""
        self._reconnect_bar.hide()
        self._closing = True
        if self._had_error or (self._thread and not self._thread.isRunning()):
            # Thread already finished (after error or before connect) — close now.
            self._do_close()
            return
        # Thread still running (connecting or connected): tell worker to stop.
        # _on_finished will call _do_close() once the thread actually ends.
        if self._worker:
            self._worker.disconnect()

    def _do_close(self) -> None:
        """Emit the signals that cause SplitView to remove this pane."""
        if self._stats_timer:
            self._stats_timer.stop()
            self._stats_timer = None
        if self._conn.id is not None:
            self.health_changed.emit(self._conn.id, "disconnected")
        self.disconnected.emit("Disconnected.")

    # ── Worker signals ────────────────────────────────────────────────────────

    def _on_connected(self) -> None:
        self._set_status(f"Connected to {self._conn.connection_string()}")
        # Delay focus slightly so any key held to activate the command palette
        # (e.g. Enter) is released before the terminal starts consuming input.
        QTimer.singleShot(150, self._output.setFocus)
        if self._conn.id is not None:
            self.health_changed.emit(self._conn.id, "connected")
        # Start byte-counter ticker
        self._bytes_rx = 0
        self._bytes_tx = 0
        self._stats_lbl.show()
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start()
        # Open SFTP in background 800 ms after connect (non-blocking)
        if self._sftp_panel:
            QTimer.singleShot(800, self._setup_sftp)
        # Start port-forwarding tunnels
        if self._tunnel_panel:
            self._tunnel_panel.set_worker(self._worker)
        # Start reverse mounts
        if self._mount_panel:
            self._mount_panel.set_worker(self._worker)

    def _setup_sftp(self) -> None:
        if not self._worker or not self._sftp_panel:
            return
        self._sftp_thread = QThread(self)
        self._sftp_worker = _OpenSFTPWorker(self._worker)
        self._sftp_worker.moveToThread(self._sftp_thread)
        self._sftp_thread.started.connect(self._sftp_worker.run)
        self._sftp_worker.ready.connect(self._sftp_panel.connect_sftp)
        self._sftp_worker.finished.connect(self._sftp_thread.quit)
        self._sftp_thread.start()

    def _update_stats(self) -> None:
        self._stats_lbl.setText(f"↓ {_fmt_bytes(self._bytes_rx)}  ↑ {_fmt_bytes(self._bytes_tx)}")

    def _on_data(self, raw: bytes) -> None:
        self._bytes_rx += len(raw)
        self._output.feed(raw)
        # Track remote CWD from OSC 7 escape sequences emitted by the shell
        m = _OSC7_RE.search(raw)
        if m:
            try:
                self._remote_cwd = m.group(1).decode("utf-8", errors="replace").rstrip("\x00")
            except Exception:
                pass
        if self._log_file:
            try:
                self._log_file.write(_ANSI_RE.sub(b"", raw))
                self._log_file.flush()
            except OSError:
                pass

    def _on_error(self, msg: str) -> None:
        self._had_error = True
        if self._closing:
            # User already clicked Disconnect — _on_finished will close the tab.
            return
        self._set_status(f"Error: {msg}")
        self._output.feed(f"\r\n\x1b[31m*** {msg} ***\x1b[0m\r\n".encode())
        self._reconnect_msg.setText(f"Connection lost: {msg}")
        self._reconnect_bar.show()
        if self._conn.id is not None:
            self.health_changed.emit(self._conn.id, "error")
        # Do NOT emit disconnected — keep the tab open for reconnect

    def _on_finished(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)   # join thread before any deleteLater() can fire
        if self._stats_timer:
            self._stats_timer.stop()
            self._stats_timer = None
        if self._tunnel_panel:
            self._tunnel_panel.set_worker(None)
        if self._mount_panel:
            self._mount_panel.set_worker(None)
        if self._closing:
            # User clicked Disconnect while thread was running — close tab now.
            self._do_close()
            return
        if self._had_error:
            return  # reconnect bar is already shown — nothing else to do
        self._set_status("Session closed.")
        self._output.feed(b"\r\n[Session closed]\r\n")
        if self._conn.id is not None:
            self.health_changed.emit(self._conn.id, "disconnected")
        self.disconnected.emit("Session closed.")

    # ── Reconnect ─────────────────────────────────────────────────────────────

    def _on_reconnect(self) -> None:
        self._reconnect_bar.hide()
        self._had_error = False

        # Disconnect signals from old worker so stale callbacks don't fire
        if self._worker:
            for sig in (
                self._worker.connected, self._worker.data_received,
                self._worker.error, self._worker.finished,
            ):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
            self._worker.disconnect()
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        self._worker = None
        self._thread = None

        self._remote_cwd = ""
        self._output.feed(b"\r\n\x1b[33m[Reconnecting\xe2\x80\xa6]\x1b[0m\r\n")
        self.start_connection()

    # ── Key / resize forwarding ───────────────────────────────────────────────

    def _on_key(self, data: bytes) -> None:
        self._bytes_tx += len(data)
        if self._worker:
            self._worker.send(data)
        self.key_input.emit(data)   # picked up by MainWindow broadcast logic

    def _on_resize_pty(self, cols: int, rows: int) -> None:
        if self._worker:
            self._worker.resize(cols, rows)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def send_data(self, data: bytes) -> None:
        """Send raw bytes to SSH without emitting key_input (broadcast target slot)."""
        self._bytes_tx += len(data)
        if self._worker:
            self._worker.send(data)

    def shutdown(self) -> None:
        """Gracefully stop the SSH session and worker thread."""
        if self._stats_timer:
            self._stats_timer.stop()
            self._stats_timer = None
        if self._log_file:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None
        if self._tunnel_panel:
            self._tunnel_panel.set_worker(None)
        if self._mount_panel:
            self._mount_panel.set_worker(None)
        if self._worker:
            self._worker.disconnect()
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        if self._conn.id is not None:
            self.health_changed.emit(self._conn.id, "disconnected")

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)


# ── pyte-backed QPlainTextEdit ────────────────────────────────────────────────

class _PyteTerminal(QPlainTextEdit):
    """
    QPlainTextEdit driven by a _Screen (pyte.HistoryScreen) state machine.

    Rendering strategy
    ------------------
    Alt-screen mode  (vim, htop, less, …)
        Detected via _Screen.in_alt_screen.  The entire document is cleared
        and the 50-line live buffer is redrawn on every frame.  History rows
        are never written, so TUI redraws can never stack.

    Normal mode  (plain shell output)
        History rows appended once and never touched again.
        The live screen section (last _PTY_ROWS lines) is replaced each frame.

    Resize
        resizeEvent() recalculates PTY dimensions from the font metrics and
        viewport size, resizes the pyte screen in-place, and emits resize_pty
        so TerminalWidget can forward the new size to paramiko.

    Shortcuts
        event() intercepts Qt ShortcutOverride events for Ctrl/Meta combos so
        they reach keyPressEvent() rather than triggering menu actions.
    """

    key_pressed      = pyqtSignal(bytes)
    resize_pty       = pyqtSignal(int, int)   # cols, rows
    search_requested = pyqtSignal()
    font_size_changed = pyqtSignal(int)        # emitted after every zoom change

    # Qt key → ANSI/VT sequence
    _KEY_MAP: dict[int, bytes] = {
        Qt.Key.Key_Up:       b"\x1b[A",
        Qt.Key.Key_Down:     b"\x1b[B",
        Qt.Key.Key_Right:    b"\x1b[C",
        Qt.Key.Key_Left:     b"\x1b[D",
        Qt.Key.Key_Home:     b"\x1b[H",
        Qt.Key.Key_End:      b"\x1b[F",
        Qt.Key.Key_PageUp:   b"\x1b[5~",
        Qt.Key.Key_PageDown: b"\x1b[6~",
        Qt.Key.Key_Delete:   b"\x1b[3~",
        Qt.Key.Key_Insert:   b"\x1b[2~",
        Qt.Key.Key_F1:       b"\x1bOP",
        Qt.Key.Key_F2:       b"\x1bOQ",
        Qt.Key.Key_F3:       b"\x1bOR",
        Qt.Key.Key_F4:       b"\x1bOS",
        Qt.Key.Key_F5:       b"\x1b[15~",
        Qt.Key.Key_F6:       b"\x1b[17~",
        Qt.Key.Key_F7:       b"\x1b[18~",
        Qt.Key.Key_F8:       b"\x1b[19~",
        Qt.Key.Key_F9:       b"\x1b[20~",
        Qt.Key.Key_F10:      b"\x1b[21~",
        Qt.Key.Key_F11:      b"\x1b[23~",
        Qt.Key.Key_F12:      b"\x1b[24~",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # pyte state machine (uses patched _Screen subclass)
        self.screen = _Screen(_PTY_COLS_DEFAULT, _PTY_ROWS_DEFAULT, history=_HISTORY)
        self.stream = pyte.ByteStream(self.screen)

        # Render throttle
        self._pending = False
        self._timer   = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._render)

        # How many history rows are already written into the document
        self._rendered_history = 0

        # Widget appearance
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        font = QFont("Menlo", 13)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(_DEFAULT_BG))
        pal.setColor(QPalette.ColorRole.Text, QColor(_DEFAULT_FG))
        self.setPalette(pal)
        self.setStyleSheet("border: none; padding: 4px;")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    # ── Theme ─────────────────────────────────────────────────────────────────

    def refresh_theme(self) -> None:
        """Re-apply the current module-level colour globals to this widget's palette."""
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base, QColor(_DEFAULT_BG))
        pal.setColor(QPalette.ColorRole.Text, QColor(_DEFAULT_FG))
        self.setPalette(pal)
        self._render()

    # ── Font zoom ─────────────────────────────────────────────────────────────

    def _zoom_font(self, delta: int) -> None:
        """Increase/decrease font size by delta pt; delta=0 resets to default."""
        font = self.font()
        if delta == 0:
            font.setPointSize(_FONT_SIZE_DEFAULT)
        else:
            font.setPointSize(max(_FONT_SIZE_MIN, min(_FONT_SIZE_MAX, font.pointSize() + delta)))
        self.setFont(font)
        self._sync_pty_size()
        self.font_size_changed.emit(font.pointSize())

    # ── Clipboard ─────────────────────────────────────────────────────────────

    def _copy_selection(self) -> None:
        """Copy selected text to the system clipboard."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            QApplication.clipboard().setText(cursor.selectedText())

    def _paste_clipboard(self) -> None:
        """Send clipboard text to the SSH channel as if typed."""
        text = QApplication.clipboard().text()
        if text:
            self.key_pressed.emit(text.encode("utf-8", errors="replace"))

    def contextMenuEvent(self, event) -> None:
        """Right-click context menu with Copy and Paste actions."""
        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        copy_action.setShortcut("Ctrl+Shift+C")
        copy_action.setEnabled(self.textCursor().hasSelection())
        paste_action = menu.addAction("Paste")
        paste_action.setShortcut("Ctrl+Shift+V")
        paste_action.setEnabled(bool(QApplication.clipboard().text()))
        chosen = menu.exec(event.globalPos())
        if chosen == copy_action:
            self._copy_selection()
        elif chosen == paste_action:
            self._paste_clipboard()

    # ── Public API ────────────────────────────────────────────────────────────

    def feed(self, raw: bytes) -> None:
        """Feed raw bytes from the SSH channel; schedule a redraw."""
        self.stream.feed(raw)
        if not self._pending:
            self._pending = True
            self._timer.start()

    # ── PTY resize ────────────────────────────────────────────────────────────

    def _sync_pty_size(self) -> None:
        """Recalculate PTY cols/rows from font metrics and emit resize_pty."""
        fm   = QFontMetrics(self.font())
        char_w = fm.horizontalAdvance(" ")
        char_h = fm.height()
        if char_w == 0 or char_h == 0:
            return

        vp   = self.viewport()
        cols = max(40, vp.width()  // char_w)
        rows = max(10, vp.height() // char_h)

        if cols == self.screen.columns and rows == self.screen.lines:
            return

        self.screen.resize(rows, cols)
        self.resize_pty.emit(cols, rows)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_pty_size()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self) -> None:
        """
        Choose rendering strategy based on whether pyte is in alt-screen mode.

        Alt-screen (vim / htop / less):
            Clear the whole document and redraw the live 50-line screen.
            History rows are never written, so frames can never stack.

        Normal shell:
            Append any new history rows once, then replace the live screen
            section at the end of the document.
        """
        self._pending = False

        vbar      = self.verticalScrollBar()
        at_bottom = vbar.value() >= vbar.maximum() - 4
        doc       = self.document()

        if self.screen.in_alt_screen:
            # ── Alt-screen: clean full redraw ─────────────────────────────
            self._rendered_history = 0

            cx = self.screen.cursor.x
            cy = self.screen.cursor.y

            cur = QTextCursor(doc)
            cur.select(QTextCursor.SelectionType.Document)
            cur.beginEditBlock()
            cur.removeSelectedText()

            first = True
            for y in range(self.screen.lines):
                if not first:
                    cur.insertBlock()
                first = False
                self._render_row(cur, self.screen.buffer[y], cx if y == cy else -1)

            cur.endEditBlock()
            # In alt screen let pyte control cursor position; don't force scroll.
            return

        # ── Normal mode: incremental history + live screen ────────────────

        # 1. Append new history rows (written once, never redrawn)
        history_rows = list(self.screen.history.top)
        new_count    = len(history_rows)

        if new_count > self._rendered_history:
            new_rows = history_rows[self._rendered_history:]

            hist_block = doc.findBlockByNumber(self._rendered_history)
            if hist_block.isValid():
                ins = QTextCursor(hist_block)
                ins.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            else:
                ins = QTextCursor(doc)
                ins.movePosition(QTextCursor.MoveOperation.End)

            ins.beginEditBlock()
            for row in new_rows:
                self._render_row(ins, row, -1)
                ins.insertBlock()
            ins.endEditBlock()

            self._rendered_history = new_count

        # 2. Replace the live screen section
        cx = self.screen.cursor.x
        cy = self.screen.cursor.y

        screen_block = doc.findBlockByNumber(self._rendered_history)
        cur = QTextCursor(doc)
        cur.beginEditBlock()

        if screen_block.isValid():
            cur.setPosition(screen_block.position())
        else:
            cur.movePosition(QTextCursor.MoveOperation.End)

        cur.movePosition(
            QTextCursor.MoveOperation.End,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cur.removeSelectedText()

        for y in range(self.screen.lines):
            if y > 0 or self._rendered_history > 0 or doc.blockCount() > 1:
                cur.insertBlock()
            self._render_row(cur, self.screen.buffer[y], cx if y == cy else -1)

        cur.endEditBlock()

        if at_bottom:
            lines_below = self.screen.lines - 1 - cy
            if lines_below > 0:
                line_h = QFontMetrics(self.font()).lineSpacing()
                vbar.setValue(max(0, vbar.maximum() - lines_below * line_h))
            else:
                vbar.setValue(vbar.maximum())

    def _render_row(
        self,
        cursor: QTextCursor,
        row: object,
        cursor_x: int,
    ) -> None:
        """
        Write one terminal row into the document, merging adjacent cells
        with identical formatting into a single QTextCharFormat run.
        """
        x    = 0
        cols = self.screen.columns

        while x < cols:
            # Cursor cell — highlighted block
            if x == cursor_x:
                cfmt = QTextCharFormat()
                cfmt.setBackground(QColor(_CURSOR_BG))
                cfmt.setForeground(QColor(_DEFAULT_BG))
                cfmt.setFontWeight(700)
                cursor.insertText(row[x].data or " ", cfmt)
                x += 1
                continue

            # Start of a new run
            first     = row[x]
            fg        = _pyte_color(first.fg, True)
            bg        = _pyte_color(first.bg, False)
            bold      = first.bold
            italic    = first.italics
            underline = first.underscore
            if first.reverse:
                fg, bg = bg, fg

            run: list[str] = []

            while x < cols and x != cursor_x:
                c   = row[x]
                cfg = _pyte_color(c.fg, True)
                cbg = _pyte_color(c.bg, False)
                if c.reverse:
                    cfg, cbg = cbg, cfg
                if (cfg, cbg, c.bold, c.italics, c.underscore) != \
                   (fg,  bg,  bold,  italic,    underline):
                    break
                run.append(c.data or " ")
                x += 1

            fmt = QTextCharFormat()
            fmt.setForeground(QColor(fg))
            fmt.setBackground(QColor(bg))
            if bold:      fmt.setFontWeight(700)
            if italic:    fmt.setFontItalic(True)
            if underline: fmt.setFontUnderline(True)
            cursor.insertText("".join(run), fmt)

    # ── Keyboard handling ─────────────────────────────────────────────────────

    def event(self, event: QEvent) -> bool:
        """
        Intercept keys that Qt would otherwise steal before keyPressEvent:
        - ShortcutOverride for Ctrl/Meta combos
        - Tab / Shift+Tab (Qt uses these for focus chain navigation)
        """
        if event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.keyPressEvent(event)
                return True
        if event.type() == QEvent.Type.ShortcutOverride:
            mods = event.modifiers()
            # Accept Ctrl+* so terminal control sequences (Ctrl+C, Ctrl+Z…) reach
            # keyPressEvent instead of firing menu actions.
            # Do NOT accept Meta (Cmd on macOS) — that lets registered menu
            # shortcuts like Cmd+P (command palette) fire normally.
            if mods & Qt.KeyboardModifier.ControlModifier:
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key  = event.key()
        mods = event.modifiers()
        text = event.text()

        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        meta  = bool(mods & Qt.KeyboardModifier.MetaModifier)

        # Ctrl+Shift+C/V or Cmd+C/V (macOS) → copy/paste, never send to SSH
        if key == Qt.Key.Key_C:
            if (ctrl and shift) or (meta and not ctrl):
                self._copy_selection()
                return
        if key == Qt.Key.Key_V:
            if (ctrl and shift) or (meta and not ctrl):
                self._paste_clipboard()
                return

        # Cmd+= / Cmd++ → zoom in  |  Cmd+- → zoom out  |  Cmd+0 → reset
        # Any other Cmd+key is silently dropped (never sent to SSH) so that
        # menu shortcuts such as Cmd+P can fire when the terminal has focus.
        if meta and not ctrl:
            if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
                self._zoom_font(+1)
                return
            if key == Qt.Key.Key_Minus:
                self._zoom_font(-1)
                return
            if key == Qt.Key.Key_0:
                self._zoom_font(0)
                return
            return  # unknown Cmd+key — don't forward to SSH

        # Ctrl+F → open search bar (do NOT send \x06 to SSH)
        if ctrl and key == Qt.Key.Key_F:
            self.search_requested.emit()
            return

        # Ctrl+[A-Z] → control bytes
        if ctrl:
            char = text.lower()
            if char:
                code = ord(char) - ord("a") + 1
                if 1 <= code <= 26:
                    self.key_pressed.emit(bytes([code]))
                    return

        if key in self._KEY_MAP:
            self.key_pressed.emit(self._KEY_MAP[key])
            return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.key_pressed.emit(b"\r")
            return
        if key == Qt.Key.Key_Backspace:
            self.key_pressed.emit(b"\x7f")
            return
        if key == Qt.Key.Key_Tab:
            # Shift+Tab = reverse tab (CSI Z), plain Tab = \t (autocomplete)
            if shift:
                self.key_pressed.emit(b"\x1b[Z")
            else:
                self.key_pressed.emit(b"\t")
            return
        if key == Qt.Key.Key_Backtab:   # Qt alias for Shift+Tab on some platforms
            self.key_pressed.emit(b"\x1b[Z")
            return
        if key == Qt.Key.Key_Escape:
            self.key_pressed.emit(b"\x1b")
            return

        if text:
            self.key_pressed.emit(text.encode("utf-8", errors="replace"))
