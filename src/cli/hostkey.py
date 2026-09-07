"""Terminal prompts for SSH host key verification.

Used by the CLI paths (connect / mount) as the trust-on-first-use callback
handed to ssh_core.
"""

from __future__ import annotations

import sys

import paramiko

from src.protocols import ssh_core


def confirm_host_key(hostname: str, keytype: str, fingerprint: str) -> bool:
    """Ask on the terminal whether to trust a host key we have not seen before.

    Fails closed when there is no TTY to ask on, so scripted runs refuse an
    unknown host instead of trusting it silently.
    """
    if not sys.stdin.isatty():
        print(
            f"[sshelf] Unknown host key for {hostname} ({keytype} {fingerprint}); "
            "no terminal available to confirm on — refusing to connect.",
            file=sys.stderr,
        )
        return False

    print(
        f"\nThe authenticity of host '{hostname}' can't be established.\n"
        f"  {keytype} key fingerprint is {fingerprint}\n"
        "Expected the first time you connect to this host. If you did not\n"
        "expect it, someone may be impersonating the host — answer 'no'.",
        file=sys.stderr,
    )
    try:
        while True:
            answer = input("Trust this host and continue (yes/no)? ").strip().lower()
            if answer in ("yes", "y"):
                return True
            if answer in ("no", "n", ""):
                return False
            print("Please answer 'yes' or 'no'.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False


def bad_host_key_message(exc: paramiko.BadHostKeyException) -> str:
    """Loud warning for a host key that conflicts with a known_hosts entry."""
    return (
        "\n[sshelf] WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\n"
        f"  Host:     {exc.hostname}\n"
        f"  Expected: {ssh_core.fingerprint(exc.expected_key)}\n"
        f"  Received: {ssh_core.fingerprint(exc.key)}\n"
        "Someone may be eavesdropping on you right now (man-in-the-middle\n"
        "attack), or the host was rebuilt/rekeyed. If you are sure the change\n"
        "is legitimate, drop the old entry and reconnect:\n"
        f"  ssh-keygen -R '{exc.hostname}'\n"
    )


def describe_connect_error(exc: Exception) -> str:
    """Message for a failed connect, spelling out host key problems."""
    if isinstance(exc, paramiko.BadHostKeyException):
        return bad_host_key_message(exc)
    if isinstance(exc, ssh_core.UnknownHostKeyError):
        return str(exc)
    return f"Connection failed: {exc}"
