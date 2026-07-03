"""Interactive SSH session for the CLI.

Connects to a saved connection using the headless paramiko layer (ssh_core)
and runs a raw passthrough loop between the local terminal and the remote
shell. Features:
  - Local terminal title set to [connection-name]
  - Connect banner printed once
  - Best-effort prompt tag on bash; zsh uses banner + terminal title only
  - F2 / Ctrl-] / F12 → snippet palette (shell prompt only)
  - F3 / Ctrl-_ → typed snippet picker (shell prompt only)
  - Both pickers insert the command WITHOUT a trailing newline — user presses Enter
  - SIGWINCH handler (POSIX) keeps remote PTY size in sync with the local terminal
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time

import paramiko

from src.cli.commands import resolve_connection
from src.cli.ptyio import (
    channel_ready,
    drain_stdin,
    exit_raw,
    pause_raw,
    raw_terminal,
    read_stdin_key,
    reset_display,
    resume_raw,
    set_terminal_title,
    write_stdout,
)
from src.protocols import ssh_core
from src.storage.database import Database

# Hotkeys (raw bytes / single character)
# Ctrl-] — palette; avoid Ctrl-G (0x07 BEL) which collides with terminal bell.
_CTRL_PALETTE = b"\x1d"
# Ctrl-_ (0x1f); never Ctrl-\ (0x1c) — that is SIGQUIT and kills the session.
_CTRL_TYPED = b"\x1f"

# Function-key fallbacks (xterm / iTerm / macOS Terminal)
_SEQ_PALETTE = frozenset({b"\x1bOQ", b"\x1b[12~", b"\x1b[24~"})  # F2, F12
_SEQ_TYPED = frozenset({b"\x1bOR", b"\x1b[13~"})                  # F3

_SHELL_MARKER = b"__SSHELF__:"

# Alternate-screen modes (vim, nano, less, htop…)
_APP_MODES = frozenset({47, 1047, 1049})
_CSI_MODE_RE = re.compile(rb"(?:\x1b|\x9b)\[(?P<p>\?)?(?P<n>\d+)(?P<op>[hl])")
_OSC_TITLE_RE = re.compile(rb"\x1b\]0;([^\x07\x1b]+)")
_APP_TITLE_RE = re.compile(
    rb"(?:^|[\s\"'/(])(?:nano|vim|less|htop)\b",
    re.IGNORECASE,
)


class _AppModeTracker:
    """Track alternate-screen / application-keypad mode from PTY output.

    vim, nano, less, htop, etc. enable these modes; snippet hotkeys are
    suppressed while any of them are active so local intercept does not
    steal keys from the remote program.
    """

    def __init__(self) -> None:
        self._modes: set[int] = set()
        self._title_app = False
        self._carry = b""

    @property
    def active(self) -> bool:
        return bool(self._modes) or self._title_app

    def feed(self, data: bytes) -> None:
        buf = self._carry + data
        for m in _CSI_MODE_RE.finditer(buf):
            mode = int(m.group("n"))
            if mode not in _APP_MODES:
                continue
            if m.group("op") == b"h":
                self._modes.add(mode)
            else:
                self._modes.discard(mode)
                if mode in (47, 1047, 1049) and not self._modes.intersection(
                    (47, 1047, 1049)
                ):
                    self._title_app = False
        for m in _OSC_TITLE_RE.finditer(buf):
            title = m.group(1)
            if _APP_TITLE_RE.search(title):
                self._title_app = True
            elif b"@" in title:
                self._title_app = False
        esc = max(buf.rfind(b"\x1b"), buf.rfind(b"\x9b"))
        if esc >= 0 and esc >= len(buf) - 12:
            self._carry = buf[esc:]
        else:
            self._carry = b""


def cmd_connect(ref: str) -> None:
    """Resolve *ref*, open an interactive SSH session, then block until exit."""
    db   = Database()
    conn = resolve_connection(db, ref)

    if conn.protocol != "ssh":
        print(
            f"[sshelf] '{conn.display_name()}' uses protocol '{conn.protocol}'. "
            "CLI connect only supports SSH.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\n── Connecting to {conn.display_name()} ({conn.host}:{conn.effective_port()}) ──\n")

    # Establish the paramiko connection (credentials auto-loaded from keychain)
    try:
        client = ssh_core.establish(conn)
    except paramiko.AuthenticationException as exc:
        print(f"[sshelf] Authentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except paramiko.SSHException as exc:
        print(f"[sshelf] SSH error: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"[sshelf] Network error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Detect current terminal size
    try:
        ts = os.get_terminal_size()
        cols, rows = ts.columns, ts.lines
    except OSError:
        cols, rows = 220, 50

    chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
    chan.setblocking(False)

    # Set local terminal title and print banner (done before raw mode)
    set_terminal_title(f"[{conn.display_name()}]")
    _print_banner(conn)

    # SIGWINCH handler: keep remote PTY size in sync (POSIX only)
    if sys.platform != "win32":
        def _on_resize(sig, frame):  # noqa: ANN001
            try:
                new_ts = os.get_terminal_size()
                chan.resize_pty(width=new_ts.columns, height=new_ts.lines)
            except OSError:
                pass
        signal.signal(signal.SIGWINCH, _on_resize)

    # Show MOTD, inject PS1, drain echo — leaves a clean prompt on screen
    _startup_sequence(chan, conn.display_name())

    # Send startup_command if configured on the connection
    if conn.startup_command:
        chan.sendall((conn.startup_command + "\n").encode("utf-8", errors="replace"))

    # Pre-load snippets (refreshed on every picker open)
    snippets: list[dict] = db.all_snippets(conn.id)

    # Run the main raw passthrough loop
    app_mode = _AppModeTracker()
    try:
        with raw_terminal():
            snippets = _passthrough_loop(chan, db, conn.id, snippets, app_mode)
    finally:
        # Always restore the title and clean up
        set_terminal_title("")
        if sys.platform != "win32":
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        try:
            chan.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        print("\n── Session closed ──\n")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_banner(conn) -> None:
    name  = conn.display_name()
    user  = conn.username or ""
    host  = conn.host
    port  = conn.effective_port()
    width = 60
    bar   = "─" * width
    print(f"\033[1m{bar}\033[0m")
    print(f"  Connected to  \033[1;32m{name}\033[0m")
    print(f"  Host          {user}@{host}:{port}")
    print(f"  F2 / Ctrl-]    open snippet palette  (shell prompt only)")
    print(f"  F3 / Ctrl-_    type snippet name / #  (shell prompt only)")
    print(f"\033[1m{bar}\033[0m\n")


def _startup_sequence(chan, name: str) -> None:
    """Display MOTD, inject PS1 tag silently, show the new prompt.

    Flow:
      1. Read & display the initial MOTD / welcome text (0.5 s window).
      2. Detect the remote shell (no bash escape sequences on the wire).
      3. Inject prompt tag on bash only (zsh skipped — banner + tab title).
      4. Send a bare newline to trigger the new prompt.
      5. Read & display the new prompt line.
    """
    _read_channel_for(chan, duration=0.75, display=True)
    shell = _detect_remote_shell(chan)
    _drain_channel_silent(chan, 0.2)
    _inject_ps1_tag(chan, name, shell)
    try:
        chan.sendall(b"\n")
    except OSError:
        pass
    _read_channel_for(chan, duration=0.35, display=True)


def _detect_remote_shell(chan) -> str:
    """Return 'zsh', 'bash', or 'other' without sending bash PS1 escapes."""
    body = (
        "if [ -n \"${ZSH_VERSION:-}\" ]; then printf '%s\\n' '__SSHELF__:zsh'; "
        "elif [ -n \"${BASH_VERSION:-}\" ]; then printf '%s\\n' '__SSHELF__:bash'; "
        "else printf '%s\\n' '__SSHELF__:other'; fi"
    )
    cmd = f" stty -echo 2>/dev/null; {body}; stty echo 2>/dev/null\n"
    try:
        chan.sendall(cmd.encode("utf-8", errors="replace"))
    except OSError:
        return "other"
    output = _collect_channel(chan, duration=1.0, idle=0.12)
    if _SHELL_MARKER + b"zsh" in output:
        return "zsh"
    if _SHELL_MARKER + b"bash" in output:
        return "bash"
    return "other"


def _run_remote_silent(chan, body: str) -> None:
    """Run *body* on the remote shell with PTY echo off.

    Injection commands contain bash PS1 escapes (\\[, \\e, …). If the PTY
    echoes them back they corrupt the local terminal and break line editing.
    Wrapping with stty -echo prevents the echo; stty echo restores it.
    """
    cmd = f" stty -echo 2>/dev/null; {body}; stty echo 2>/dev/null\n"
    try:
        chan.sendall(cmd.encode("utf-8", errors="replace"))
    except OSError:
        return
    _drain_channel_silent(chan, duration=0.8)


def _inject_ps1_tag(chan, name: str, shell: str) -> None:
    """Prepend a cyan connection tag to the remote bash prompt.

    Bash: snapshots the current PS1, then registers PROMPT_COMMAND (appended
    last) to rebuild ``(name) + original PS1`` before every prompt — Ubuntu
    and other distros often reset PS1 after login, so a one-shot export is
    not enough.

    Zsh / oh-my-zsh: skipped — remote PROMPT hooks clash with themes.
    Connection name is shown via the sshelf banner and tab title ``[name]``.

    Leading space keeps the command out of shell history on most servers.
    """
    if shell == "zsh":
        return
    name_safe = name.replace("'", r"'\''")  # safe for shell single-quoting
    if shell == "bash":
        body = (
            f"__sshelf_tag='{name_safe}'; "
            "__sshelf_base_ps1=\"$PS1\"; "
            "__sshelf_prompt() { "
            "PS1='\\[\\e[0;36m\\]('\"$__sshelf_tag\"')\\[\\e[0m\\] '\"$__sshelf_base_ps1\"; "
            "printf '\\033]0;(%s) %s@%s: %s\\007' \"$__sshelf_tag\" "
            '"${USER:-root}" "${HOSTNAME:-host}" "${PWD/#$HOME/\\~}"; '
            "}; "
            'PROMPT_COMMAND="${PROMPT_COMMAND%;}"; '
            'PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }__sshelf_prompt"; '
            "__sshelf_prompt"
        )
    else:
        print(
            f"[sshelf] unknown remote shell ({shell!r}) — prompt tag skipped",
            file=sys.stderr,
        )
        return
    _run_remote_silent(chan, body)


def _collect_channel(chan, duration: float, idle: float = 0.08) -> bytes:
    """Read channel data without displaying; stop after *duration* or *idle* silence."""
    buf = b""
    deadline = time.monotonic() + duration
    last_data = time.monotonic()
    while time.monotonic() < deadline:
        if channel_ready(chan):
            try:
                chunk = chan.recv(4096)
                if chunk:
                    buf += chunk
                    last_data = time.monotonic()
            except OSError:
                break
        elif buf and time.monotonic() - last_data >= idle:
            break
        else:
            time.sleep(0.03)
    return buf


def _drain_channel_silent(chan, duration: float) -> None:
    """Discard all channel output for up to *duration* seconds."""
    _collect_channel(chan, duration)


def _read_channel_for(chan, duration: float, display: bool) -> None:
    """Poll the channel for up to *duration* seconds, optionally writing to stdout."""
    if not display:
        _collect_channel(chan, duration)
        return
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if channel_ready(chan):
            try:
                chunk = chan.recv(4096)
                if chunk and display:
                    write_stdout(chunk)
            except OSError:
                break
        else:
            time.sleep(0.05)


def _passthrough_loop(
    chan,
    db: Database,
    conn_id: int | None,
    snippets: list[dict],
    app_mode: _AppModeTracker,
) -> list[dict]:
    """Raw stdin ↔ channel passthrough with snippet hotkey interception.

    Snippet hotkeys are honoured only at the shell prompt. While vim, nano,
    less, or similar apps hold the alternate screen, keys pass through.

    Returns the (possibly updated) snippet list.
    """
    while True:
        # --- Channel → local stdout ---
        if channel_ready(chan):
            try:
                data = chan.recv(4096)
                if not data:
                    break
                app_mode.feed(data)
                write_stdout(data)
            except OSError:
                break

        # --- Check if the remote session has ended ---
        if chan.closed or chan.exit_status_ready():
            _drain_channel(chan, app_mode)
            break

        # --- Local stdin → channel (with hotkey intercept) ---
        action, forward = read_stdin_key(
            ctrl_palette=_CTRL_PALETTE,
            ctrl_typed=_CTRL_TYPED,
            seq_palette=_SEQ_PALETTE,
            seq_typed=_SEQ_TYPED,
        )
        if action is None:
            continue

        if not app_mode.active:
            if action == "palette":
                snippets = _run_picker(chan, db, conn_id, snippets, mode="palette")
                continue
            if action == "typed":
                snippets = _run_picker(chan, db, conn_id, snippets, mode="typed")
                continue

        # Forward everything else verbatim (including hotkeys inside apps)
        if forward:
            try:
                chan.sendall(forward)
            except OSError:
                break

    return snippets


def _drain_channel(chan, app_mode: _AppModeTracker | None = None) -> None:
    """Flush any remaining channel output to stdout before closing."""
    while chan.recv_ready():
        try:
            chunk = chan.recv(4096)
            if chunk:
                if app_mode is not None:
                    app_mode.feed(chunk)
                write_stdout(chunk)
        except OSError:
            break


def _run_picker(
    chan,
    db: Database,
    conn_id: int | None,
    snippets: list[dict],
    mode: str,
) -> list[dict]:
    """Pause raw mode, show snippet picker, inject chosen command, resume.

    Returns the refreshed snippet list (may have grown if user added one).
    """
    # Cooked mode restores ISIG — Ctrl-\ would SIGQUIT without this guard.
    prev_quit = None
    if sys.platform != "win32":
        prev_quit = signal.signal(signal.SIGQUIT, signal.SIG_IGN)

    pause_raw()
    print()  # blank line separator before picker UI

    command = None
    try:
        from src.cli.palette import pick_snippet_palette, pick_snippet_typed

        if mode == "palette":
            command, snippets = pick_snippet_palette(snippets, db, conn_id)
        else:
            command, snippets = pick_snippet_typed(snippets, db, conn_id)
    finally:
        print()  # blank line after picker UI
        reset_display()
        drain_stdin()
        resume_raw()
        if prev_quit is not None:
            signal.signal(signal.SIGQUIT, prev_quit)

    if command:
        try:
            # Insert without newline — user presses Enter to run
            chan.sendall(command.encode("utf-8", errors="replace"))
        except OSError:
            pass

    return snippets
