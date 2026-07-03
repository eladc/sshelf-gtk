"""CLI CRUD commands for connections and snippets.

All handlers receive parsed argparse.Namespace objects and write to stdout
(no Qt dependency). Database / keychain are accessed via existing storage
layer which is fully headless.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional

from src.models.connection import Connection
from src.storage.database import Database


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _open_db() -> Database:
    return Database()


def resolve_connection(db: Database, ref: str) -> Connection:
    """Find a connection by numeric id or by name (exact then prefix match).

    Exits with a helpful message if nothing matches or there is ambiguity.
    """
    # Numeric id lookup
    if ref.isdigit():
        conn = db.get_connection(int(ref))
        if conn:
            return conn
        print(f"[sshelf] No connection with id {ref}.", file=sys.stderr)
        sys.exit(1)

    # Name search (case-insensitive exact, then substring)
    ref_lower = ref.lower()
    all_conns = db.all_connections()

    exact = [c for c in all_conns if c.name.lower() == ref_lower
             or c.display_name().lower() == ref_lower]
    if len(exact) == 1:
        return exact[0]

    if len(exact) > 1:
        _ambiguous(ref, exact)

    # Substring fallback
    partial = [c for c in all_conns if ref_lower in c.name.lower()
               or ref_lower in c.display_name().lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        _ambiguous(ref, partial)

    print(f"[sshelf] No connection named {ref!r}.", file=sys.stderr)
    sys.exit(1)


def _ambiguous(ref: str, matches: list[Connection]) -> None:
    print(
        f"[sshelf] Ambiguous name {ref!r}: matches "
        + ", ".join(f"#{c.id} '{c.name}'" for c in matches),
        file=sys.stderr,
    )
    sys.exit(1)


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    """Prompt for input; return *default* if the user hits Enter without typing."""
    if secret:
        value = getpass.getpass(f"  {label}: ")
    else:
        suffix = f" [{default}]" if default else ""
        value = input(f"  {label}{suffix}: ").strip()
    return value or default


def _print_connection(conn: Connection) -> None:
    auth = conn.auth_method()
    group_tag = f"  ({conn.group})" if conn.group not in ("", "Default") else ""
    print(f"  #{conn.id:<4}  {conn.name or '(unnamed)':<22}  "
          f"{conn.connection_string():<32}  [{auth}]{group_tag}")


# ---------------------------------------------------------------------------
# sshelf list
# ---------------------------------------------------------------------------

def cmd_list() -> None:
    db = _open_db()
    conns = db.all_connections()
    if not conns:
        print("No saved connections.  Run `sshelf add` to create one.")
        return

    current_group: str | None = None
    for conn in conns:
        if conn.group != current_group:
            current_group = conn.group
            print(f"\n── {current_group} {'─' * max(0, 50 - len(current_group))}")
        _print_connection(conn)
    print()


# ---------------------------------------------------------------------------
# sshelf add
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> None:
    db = _open_db()
    print("Add a new SSH connection  (press Enter to accept the shown default)\n")

    name     = args.name  or _prompt("Name")
    host     = args.host  or _prompt("Host / IP")
    if not host:
        print("[sshelf] Host is required.", file=sys.stderr)
        sys.exit(1)

    username = args.user  or _prompt("Username", default=getpass.getuser())
    group    = (getattr(args, "group", None) or _prompt("Group", default="Default"))

    # Port: 0 stored in DB means "use protocol default" (22 for SSH)
    if args.port is not None:
        port = args.port
    else:
        raw_port = _prompt("Port", default="22")
        port = int(raw_port) if raw_port.isdigit() else 22
    port = 0 if port == 22 else port  # store 0 = default

    key_file   = getattr(args, "key",   None) or _prompt("Private key file  (blank → password auth)")
    passphrase = ""
    password   = ""

    if key_file:
        passphrase = _prompt("Key passphrase  (blank if none)", secret=True)
    else:
        password = _prompt("Password  (stored in keychain · blank = agent/key auth)", secret=True)

    tags  = getattr(args, "tags",  None) or _prompt("Tags  (comma-separated, optional)")
    notes = getattr(args, "notes", None) or _prompt("Notes  (optional)")

    conn = Connection(
        name=name,
        host=host,
        username=username,
        port=port,
        group=group,
        private_key_file=key_file,
        passphrase=passphrase,
        password=password,
        tags=tags,
        notes=notes,
        protocol="ssh",
    )
    saved = db.save_connection(conn)
    print(f"\n✓  Connection #{saved.id} '{saved.display_name()}' saved.")


# ---------------------------------------------------------------------------
# sshelf edit
# ---------------------------------------------------------------------------

def cmd_edit(args: argparse.Namespace) -> None:
    db   = _open_db()
    conn = resolve_connection(db, args.ref)
    print(f"Editing  #{conn.id} '{conn.display_name()}'  (Enter = keep current)\n")

    # Apply CLI flags or prompt interactively
    def _field(flag_val, label: str, current: str, secret: bool = False) -> str:
        if flag_val is not None:
            return flag_val
        return _prompt(label, default=current, secret=secret)

    conn.name     = _field(args.name, "Name",           conn.name)
    conn.host     = _field(args.host, "Host / IP",      conn.host)
    conn.username = _field(args.user, "Username",       conn.username)
    conn.group    = _field(args.group, "Group",         conn.group)
    conn.tags     = _field(args.tags,  "Tags",          conn.tags)
    conn.notes    = _field(args.notes, "Notes",         conn.notes)
    conn.private_key_file = _field(args.key, "Private key file", conn.private_key_file)

    if args.port is not None:
        conn.port = args.port
    else:
        raw_port = _prompt("Port", default=str(conn.effective_port()))
        conn.port = int(raw_port) if raw_port.isdigit() else conn.port

    change_pw = _prompt("Update password?  [y/N]").lower()
    if change_pw == "y":
        conn.password = _prompt("New password", secret=True)

    db.save_connection(conn)
    print(f"\n✓  Connection #{conn.id} '{conn.display_name()}' updated.")


# ---------------------------------------------------------------------------
# sshelf delete
# ---------------------------------------------------------------------------

def cmd_delete(args: argparse.Namespace) -> None:
    db   = _open_db()
    conn = resolve_connection(db, args.ref)

    if not args.yes:
        ans = _prompt(
            f"Delete connection #{conn.id} '{conn.display_name()}'?  [y/N]"
        ).lower()
        if ans != "y":
            print("Aborted.")
            return

    db.delete_connection(conn.id)
    print(f"✓  Connection '{conn.display_name()}' deleted.")


# ---------------------------------------------------------------------------
# sshelf snippet *
# ---------------------------------------------------------------------------

def cmd_snippet(args: argparse.Namespace) -> None:
    db     = _open_db()
    action = args.snip_command

    if action == "list":
        _snippet_list(db, args)
    elif action == "add":
        _snippet_add(db, args)
    elif action == "delete":
        _snippet_delete(db, args)
    else:
        print(f"[sshelf] Unknown snippet action: {action}", file=sys.stderr)
        sys.exit(1)


def _snippet_list(db: Database, args: argparse.Namespace) -> None:
    conn_id: Optional[int] = None
    if getattr(args, "conn", None):
        conn_id = resolve_connection(db, args.conn).id

    snippets = db.all_snippets(conn_id)
    if not snippets:
        scope = f" for connection '{args.conn}'" if getattr(args, "conn", None) else " (global)"
        print(f"No snippets{scope}.  Run `sshelf snippet add` to create one.")
        return

    header = f"  {'ID':<5}  {'Title':<28}  {'Scope':<10}  Command"
    print(f"\n{header}")
    print("  " + "─" * 72)
    for s in snippets:
        scope_tag = f"conn #{s['conn_id']}" if s["conn_id"] else "global"
        print(f"  {s['id']:<5}  {s['title']:<28}  {scope_tag:<10}  {s['command']}")
    print()


def _snippet_add(db: Database, args: argparse.Namespace) -> None:
    conn_id: Optional[int] = None
    if getattr(args, "conn", None):
        conn_id = resolve_connection(db, args.conn).id

    title   = getattr(args, "title",   None) or _prompt("Snippet title")
    command = getattr(args, "command", None) or _prompt("Command")
    if not command:
        print("[sshelf] Command is required.", file=sys.stderr)
        sys.exit(1)

    db.save_snippet(title, command, conn_id)
    scope = f"for connection #{conn_id}" if conn_id else "(global)"
    print(f"✓  Snippet '{title}' saved {scope}.")


def _snippet_delete(db: Database, args: argparse.Namespace) -> None:
    db.delete_snippet(args.id)
    print(f"✓  Snippet #{args.id} deleted.")


# ── config ────────────────────────────────────────────────────────────────────

_CONFIG_KEYS = {
    "snippet_palette": {
        "pref": "snippet_palette_ui",
        "values": ("terminal", "graphical"),
        "help": "CLI snippet palette UI (F2 / Ctrl-] in ssh sessions)",
    },
}


def cmd_config(db: Database, args: argparse.Namespace) -> None:
    """Get or set sshelf preferences from the terminal."""
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)

    if not key:
        print("sshelf config — available keys:\n")
        for name, meta in _CONFIG_KEYS.items():
            cur = db.get_pref(meta["pref"], meta["values"][0])
            print(f"  {name:<20}  {cur}  ({meta['help']})")
        print("\nUsage: sshelf config <key> [value]")
        return

    if key not in _CONFIG_KEYS:
        print(f"[sshelf] Unknown config key: {key!r}", file=sys.stderr)
        print(f"  Valid keys: {', '.join(_CONFIG_KEYS)}", file=sys.stderr)
        sys.exit(1)

    meta = _CONFIG_KEYS[key]
    if value is None:
        cur = db.get_pref(meta["pref"], meta["values"][0])
        print(cur)
        return

    if value not in meta["values"]:
        print(
            f"[sshelf] Invalid value {value!r} for {key}. "
            f"Choose: {', '.join(meta['values'])}",
            file=sys.stderr,
        )
        sys.exit(1)

    db.set_pref(meta["pref"], value)
    print(f"✓  {key} = {value}")
