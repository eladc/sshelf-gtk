"""Cross-platform raw terminal I/O for the CLI SSH session.

POSIX (Linux/macOS):
    Uses termios/tty for raw mode + select for non-blocking stdin reads.

Windows (cmd / PowerShell):
    Uses ctypes SetConsoleMode for VT-input mode + msvcrt for non-blocking
    keyboard reads, with a scan-code → ANSI escape mapping so arrow keys
    and function keys pass through correctly to the SSH channel.

Public API
----------
enter_raw()         Put local terminal into raw / no-echo mode.
exit_raw()          Restore the terminal to the state before enter_raw().
pause_raw()         Temporarily restore cooked mode (call inside raw session).
resume_raw()        Re-enter raw mode after pause_raw().
drain_stdin()       Discard pending stdin bytes (after cooked-mode dialogs).
reset_display()     Reset SGR/cursor after a dialog left the screen dirty.
raw_terminal()      Context manager wrapping enter_raw / exit_raw.
read_stdin_byte()   Non-blocking: return next byte(s) from stdin or None.
read_stdin_key()    Non-blocking: detect snippet hotkeys or return bytes to forward.
channel_ready(ch)   True when paramiko channel has data waiting.
write_stdout(data)  Write raw bytes to stdout, flush immediately.
set_terminal_title(t) Set terminal window/tab title via OSC escape.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator

_IS_WIN = sys.platform == "win32"

# ---------------------------------------------------------------------------
# POSIX implementation
# ---------------------------------------------------------------------------

if not _IS_WIN:
    import os
    import select
    import termios
    import time
    import tty

    # Module-level saved terminal attrs (cooked state before raw was entered)
    _saved_attrs: list | None = None

    def enter_raw() -> None:
        """Put stdin into raw mode, saving previous attrs."""
        global _saved_attrs
        fd = sys.stdin.fileno()
        _saved_attrs = termios.tcgetattr(fd)
        tty.setraw(fd)

    def exit_raw() -> None:
        """Restore stdin to attrs saved by enter_raw()."""
        global _saved_attrs
        if _saved_attrs is not None:
            fd = sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSADRAIN, _saved_attrs)

    def pause_raw() -> None:
        """Temporarily restore cooked mode. Must be called inside raw_terminal()."""
        exit_raw()

    def resume_raw() -> None:
        """Re-enter raw mode without overwriting attrs saved at session start."""
        tty.setraw(sys.stdin.fileno())

    def drain_stdin() -> None:
        """Discard any bytes waiting on stdin."""
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if not r:
                break
            os.read(sys.stdin.fileno(), 4096)

    def read_stdin_byte() -> bytes | None:
        """Non-blocking: return one byte from stdin, or None if nothing ready."""
        r, _, _ = select.select([sys.stdin], [], [], 0.05)
        if r:
            return os.read(sys.stdin.fileno(), 1)
        return None

    def _read_stdin_esc_tail() -> bytes:
        """Read bytes that may follow ESC in a key sequence."""
        parts: list[bytes] = []
        deadline = time.monotonic() + 0.04
        while time.monotonic() < deadline:
            r, _, _ = select.select([sys.stdin], [], [], 0.008)
            if not r:
                if parts:
                    break
                continue
            parts.append(os.read(sys.stdin.fileno(), 32))
        return b"".join(parts)

    def read_stdin_key(
        *,
        ctrl_palette: bytes,
        ctrl_typed: bytes,
        seq_palette: frozenset[bytes],
        seq_typed: frozenset[bytes],
    ) -> tuple[str | None, bytes]:
        """Return (action, forward_bytes).

        action is ``'palette'``, ``'typed'``, ``'forward'``, or ``None``.
        When action is ``'forward'``, *forward_bytes* is the raw input to send.
        """
        byte = read_stdin_byte()
        if byte is None:
            return None, b""
        if byte == ctrl_palette:
            return "palette", b""
        if byte == ctrl_typed:
            return "typed", b""
        if byte != b"\x1b":
            return "forward", byte
        seq = byte + _read_stdin_esc_tail()
        if seq in seq_palette:
            return "palette", b""
        if seq in seq_typed:
            return "typed", b""
        return "forward", seq

# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------

else:
    import ctypes
    import msvcrt  # type: ignore[import]

    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _STD_INPUT_HANDLE = -10
    _STD_OUTPUT_HANDLE = -11
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    # Saved console modes
    _saved_in_mode: int | None = None
    _saved_out_mode: int | None = None

    # Scan-code → ANSI escape mapping for Windows extended keys
    _SCAN_MAP: dict[tuple[bytes, bytes], bytes] = {
        # Arrow keys (0xe0 prefix)
        (b"\xe0", b"H"): b"\x1b[A",   # Up
        (b"\xe0", b"P"): b"\x1b[B",   # Down
        (b"\xe0", b"M"): b"\x1b[C",   # Right
        (b"\xe0", b"K"): b"\x1b[D",   # Left
        (b"\xe0", b"G"): b"\x1b[H",   # Home
        (b"\xe0", b"O"): b"\x1b[F",   # End
        (b"\xe0", b"S"): b"\x1b[3~",  # Delete
        (b"\xe0", b"I"): b"\x1b[5~",  # Page Up
        (b"\xe0", b"Q"): b"\x1b[6~",  # Page Down
        # Function keys (0x00 prefix)
        (b"\x00", b";"): b"\x1bOP",   # F1
        (b"\x00", b"<"): b"\x1bOQ",   # F2
        (b"\x00", b"="): b"\x1bOR",   # F3
        (b"\x00", b">"): b"\x1bOS",   # F4
        (b"\x00", b"?"): b"\x1b[15~", # F5
        (b"\x00", b"@"): b"\x1b[17~", # F6
        (b"\x00", b"A"): b"\x1b[18~", # F7
        (b"\x00", b"B"): b"\x1b[19~", # F8
        (b"\x00", b"C"): b"\x1b[20~", # F9
        (b"\x00", b"D"): b"\x1b[21~", # F10
    }

    def _get_mode(handle) -> int:
        mode = ctypes.c_ulong()
        _kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        return mode.value

    def enter_raw() -> None:
        """Enable VT input on Windows and disable echo/line-input."""
        global _saved_in_mode, _saved_out_mode
        in_h = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        out_h = _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        _saved_in_mode = _get_mode(in_h)
        _saved_out_mode = _get_mode(out_h)
        _kernel32.SetConsoleMode(in_h, _ENABLE_VIRTUAL_TERMINAL_INPUT)
        _kernel32.SetConsoleMode(out_h, _saved_out_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    def exit_raw() -> None:
        """Restore saved console modes."""
        global _saved_in_mode, _saved_out_mode
        if _saved_in_mode is not None:
            _kernel32.SetConsoleMode(
                _kernel32.GetStdHandle(_STD_INPUT_HANDLE), _saved_in_mode
            )
        if _saved_out_mode is not None:
            _kernel32.SetConsoleMode(
                _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE), _saved_out_mode
            )

    def pause_raw() -> None:
        exit_raw()

    def resume_raw() -> None:
        """Re-enter raw mode without overwriting attrs saved at session start."""
        in_h = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        out_h = _kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        _kernel32.SetConsoleMode(in_h, _ENABLE_VIRTUAL_TERMINAL_INPUT)
        if _saved_out_mode is not None:
            _kernel32.SetConsoleMode(out_h, _saved_out_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    def drain_stdin() -> None:
        """Discard any bytes waiting on stdin."""
        while msvcrt.kbhit():
            msvcrt.getch()

    def read_stdin_byte() -> bytes | None:
        """Non-blocking: return next byte(s) from stdin or None.

        Handles Windows extended key sequences (arrow keys, function keys)
        by mapping them to their ANSI escape equivalents.
        """
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            # Extended key — read scan code and map to ANSI
            if msvcrt.kbhit():
                scan = msvcrt.getch()
                return _SCAN_MAP.get((ch, scan), ch + scan)
            return ch
        return ch

    def read_stdin_key(
        *,
        ctrl_palette: bytes,
        ctrl_typed: bytes,
        seq_palette: frozenset[bytes],
        seq_typed: frozenset[bytes],
    ) -> tuple[str | None, bytes]:
        byte = read_stdin_byte()
        if byte is None:
            return None, b""
        if byte == ctrl_palette:
            return "palette", b""
        if byte == ctrl_typed:
            return "typed", b""
        if byte in seq_palette:
            return "palette", b""
        if byte in seq_typed:
            return "typed", b""
        return "forward", byte


# ---------------------------------------------------------------------------
# Shared helpers (both platforms)
# ---------------------------------------------------------------------------

@contextmanager
def raw_terminal() -> Iterator[None]:
    """Context manager: enter raw mode, restore on exit (even if an exception occurs)."""
    enter_raw()
    try:
        yield
    finally:
        exit_raw()


def channel_ready(chan) -> bool:
    """True when the paramiko channel has data waiting to be read."""
    return bool(chan.recv_ready())


def write_stdout(data: bytes) -> None:
    """Write raw bytes to stdout and flush immediately."""
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def reset_display() -> None:
    """Best-effort terminal cleanup after a cooked-mode dialog (e.g. snippet picker)."""
    sys.stdout.write("\033[0m\033[?25h")
    sys.stdout.flush()


def set_terminal_title(title: str) -> None:
    """Set the terminal window/tab title via OSC escape sequence.

    Works on macOS Terminal, iTerm2, Windows Terminal, most Linux terminals.
    Silently does nothing if the terminal does not support OSC.
    """
    sys.stdout.write(f"\033]0;{title}\007")
    sys.stdout.flush()
