"""Interactive SSH session for the CLI.

Connects to a saved connection using the headless paramiko layer (ssh_core)
and runs a raw passthrough loop between the local terminal and the remote
shell. Features:
  - Local terminal title set to [connection-name]
  - Connect banner printed once
  - Best-effort prompt tag injected into the remote shell (bash or zsh)
  - Ctrl-G → snippet palette (prompt_toolkit or menu fallback)
  - Ctrl-X → typed snippet picker
  - Both pickers insert the command WITHOUT a trailing newline — user presses Enter
  - SIGWINCH handler (POSIX) keeps remote PTY size in sync with the local terminal
"""

from __future__ import annotations

import os
import signal
import sys
import time

import paramiko

from src.cli.commands import resolve_connection
from src.cli.ptyio import (
    channel_ready,
    exit_raw,
    pause_raw,
    raw_terminal,
    read_stdin_byte,
    resume_raw,
    set_terminal_title,
    write_stdout,
)
from src.protocols import ssh_core
from src.storage.database import Database

# Hotkeys (raw bytes / single character)
_CTRL_G = b"\x07"  # Ctrl-G → palette picker
_CTRL_X = b"\x18"  # Ctrl-X → typed picker

_SHELL_MARKER = b"__SSHELF__:"


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
    try:
        with raw_terminal():
            snippets = _passthrough_loop(chan, db, conn.id, snippets)
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
    print(f"  Ctrl-G        open snippet palette")
    print(f"  Ctrl-X        type snippet name / # to insert")
    print(f"\033[1m{bar}\033[0m\n")


def _startup_sequence(chan, name: str) -> None:
    """Display MOTD, inject PS1 tag silently, show the new prompt.

    Flow:
      1. Read & display the initial MOTD / welcome text (0.5 s window).
      2. Detect the remote shell (no bash escape sequences on the wire).
      3. Run shell-specific prompt injection with PTY echo disabled.
      4. Send a bare newline to trigger the new prompt.
      5. Read & display the new prompt line.
    """
    _read_channel_for(chan, duration=0.5, display=True)
    shell = _detect_remote_shell(chan)
    _inject_ps1_tag(chan, name, shell)
    try:
        chan.sendall(b"\n")
    except OSError:
        pass
    _read_channel_for(chan, duration=0.35, display=True)


def _detect_remote_shell(chan) -> str:
    """Return 'zsh', 'bash', or 'other' without sending bash PS1 escapes."""
    body = (
        "if [ -n \"$ZSH_VERSION\" ]; then printf '%s' '__SSHELF__:zsh'; "
        "elif [ -n \"$BASH_VERSION\" ]; then printf '%s' '__SSHELF__:bash'; "
        "else printf '%s' '__SSHELF__:other'; fi"
    )
    cmd = f" stty -echo 2>/dev/null; {body}; stty echo 2>/dev/null\n"
    try:
        chan.sendall(cmd.encode("utf-8", errors="replace"))
    except OSError:
        return "other"
    output = _collect_channel(chan, duration=0.6)
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
    """Prepend a cyan connection tag to the remote prompt (bash or zsh).

    Bash: uses double-quoted "$PS1" so the current value is captured at
    assignment time — avoids self-referencing recursion from '${PS1:-...}'.
    \\[ and \\] are non-printing markers; \\e is ESC. Color 36 = cyan.

    Zsh / oh-my-zsh: registers a precmd hook that prepends to PROMPT after
    the theme runs. Direct PROMPT= assignment breaks complex themes and can
    leave the shell in a broken PS2 (``\\ ``) state when PROMPT contains
    quotes. Bash PS1 escapes must never be sent to zsh servers at all.

    Leading space keeps the command out of shell history on most servers.
    """
    name_safe = name.replace("'", r"'\''")  # safe for shell single-quoting
    if shell == "zsh":
        body = (
            f"__sshelf_tag='{name_safe}'; "
            "__sshelf_precmd() { "
            '[[ "$PROMPT" == "%F{cyan}(${__sshelf_tag})%f "* ]] && return; '
            'PROMPT="%F{cyan}(${__sshelf_tag})%f ${PROMPT}"; '
            'print -Pn "\\e]0;${__sshelf_tag} %n@%m: %~\\a"; '
            "}; precmd_functions+=__sshelf_precmd"
        )
    elif shell == "bash":
        bash_ps1 = (
            f"'\\[\\e[0;36m\\]({name_safe})\\[\\e[0m\\] '\"$PS1\""
            f"'\\[\\e]0;({name_safe}) \\u@\\h: \\w\\007\\]'"
        )
        body = f"export PS1={bash_ps1} 2>/dev/null"
    else:
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
) -> list[dict]:
    """Raw stdin ↔ channel passthrough with snippet hotkey interception.

    Returns the (possibly updated) snippet list.
    """
    while True:
        # --- Channel → local stdout ---
        if channel_ready(chan):
            try:
                data = chan.recv(4096)
                if not data:
                    break
                write_stdout(data)
            except OSError:
                break

        # --- Check if the remote session has ended ---
        if chan.closed or chan.exit_status_ready():
            _drain_channel(chan)
            break

        # --- Local stdin → channel (with hotkey intercept) ---
        byte = read_stdin_byte()
        if byte is None:
            continue

        if byte == _CTRL_G:
            snippets = _run_picker(chan, db, conn_id, snippets, mode="palette")
            continue

        if byte == _CTRL_X:
            snippets = _run_picker(chan, db, conn_id, snippets, mode="typed")
            continue

        # Forward everything else verbatim
        try:
            chan.sendall(byte)
        except OSError:
            break

    return snippets


def _drain_channel(chan) -> None:
    """Flush any remaining channel output to stdout before closing."""
    while chan.recv_ready():
        try:
            chunk = chan.recv(4096)
            if chunk:
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
    # Step out of raw mode so the picker UI can use normal cooked I/O
    pause_raw()
    print()  # blank line separator before picker UI

    from src.cli.palette import pick_snippet_palette, pick_snippet_typed

    if mode == "palette":
        command, snippets = pick_snippet_palette(snippets, db, conn_id)
    else:
        command, snippets = pick_snippet_typed(snippets, db, conn_id)

    print()  # blank line after picker UI

    # Return to raw mode before touching the channel
    resume_raw()

    if command:
        try:
            # Insert without newline — user presses Enter to run
            chan.sendall(command.encode("utf-8", errors="replace"))
        except OSError:
            pass

    return snippets
