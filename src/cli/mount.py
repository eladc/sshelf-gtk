"""CLI commands for reverse directory mounts (local dir → remote host).

sshelf mount <conn> <local-dir> <remote-dir> [--save LABEL]
    Mount local-dir on the remote host at remote-dir.
    Blocks until Ctrl-C, then unmounts cleanly.
    --save LABEL  persist this config so it can be reused with --use.

sshelf mount <conn> --use LABEL
    Mount using a previously saved config (identified by label).

sshelf mounts [--conn NAME_OR_ID]
    List saved mount configs for a connection.

sshelf unmount <conn> <remote-dir>
    Best-effort unmount of an orphaned mount on the remote.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

from src.cli.commands import resolve_connection
from src.cli.hostkey import confirm_host_key, describe_connect_error
from src.models.mount import Mount
from src.protocols.sshfs_mount import (
    ReverseMount,
    ReverseMountError,
    remote_has_sshfs,
)
from src.protocols import ssh_core
from src.storage.database import Database


# ---------------------------------------------------------------------------
# cmd_mount
# ---------------------------------------------------------------------------

def cmd_mount(args) -> None:
    """Mount a local directory on the remote host."""
    db   = Database()
    conn = resolve_connection(db, args.ref)

    # Determine local_dir / remote_dir from args or saved config
    if getattr(args, "use", None):
        mounts = [
            Mount.from_dict(m) for m in db.all_mounts(conn.id)
            if m["label"].lower() == args.use.lower()
        ]
        if not mounts:
            print(
                f"[sshelf] No saved mount with label '{args.use}' for '{conn.display_name()}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        cfg = mounts[0]
        local_dir  = cfg.local_dir
        remote_dir = cfg.remote_dir
    elif getattr(args, "local_dir", None) and getattr(args, "remote_dir", None):
        local_dir  = str(Path(args.local_dir).expanduser().resolve())
        remote_dir = args.remote_dir
    else:
        # No positional dirs and no --use: list saved mounts or show help
        saved = db.all_mounts(conn.id)
        if len(saved) == 1:
            cfg = Mount.from_dict(saved[0])
            local_dir  = cfg.local_dir
            remote_dir = cfg.remote_dir
            print(f"[sshelf] Using saved mount: {cfg.display()}")
        else:
            print(
                "[sshelf] Provide LOCAL and REMOTE dirs, or --use LABEL to pick a saved config.\n"
                f"  Saved configs for '{conn.display_name()}':",
                file=sys.stderr,
            )
            for m in saved:
                print(f"    [{m['id']}] {m['label'] or '(no label)'}  "
                      f"{m['local_dir']} → {m['remote_dir']}", file=sys.stderr)
            sys.exit(1)

    # Connect
    print(f"\n── Mounting on {conn.display_name()} ({conn.host}:{conn.effective_port()}) ──\n")
    try:
        client = ssh_core.establish(conn, confirm_host_key=confirm_host_key)
    except Exception as exc:  # noqa: BLE001
        print(f"[sshelf] {describe_connect_error(exc)}", file=sys.stderr)
        sys.exit(1)

    transport = client.get_transport()

    # Check remote sshfs
    print("[sshelf] Checking remote sshfs ...", end="", flush=True)
    if not remote_has_sshfs(transport):
        print(" NOT FOUND")
        print(
            "\n[sshelf] sshfs is not installed on the remote server.\n"
            "  Install it with:\n"
            "    sudo apt install sshfs          # Debian/Ubuntu\n"
            "    sudo yum install fuse-sshfs     # RHEL/CentOS\n"
            "    sudo pacman -S sshfs            # Arch",
            file=sys.stderr,
        )
        client.close()
        sys.exit(1)
    print(" OK")

    # Start mount
    mount = ReverseMount(transport, local_dir, remote_dir)
    try:
        print(f"[sshelf] Mounting {local_dir}  →  {conn.display_name()}:{remote_dir} ...",
              end="", flush=True)
        mount.start()
    except ReverseMountError as exc:
        print(f" FAILED\n[sshelf] {exc}", file=sys.stderr)
        client.close()
        sys.exit(1)

    print(" MOUNTED")
    _print_mount_banner(local_dir, remote_dir, conn.display_name())

    # Optionally save this config
    if getattr(args, "save", None):
        m = Mount(
            conn_id=conn.id,
            label=args.save,
            local_dir=local_dir,
            remote_dir=remote_dir,
        )
        db.save_mount(m)
        print(f"[sshelf] Saved mount config as '{args.save}'.")

    # Block until Ctrl-C
    def _on_sigint(sig, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    if sys.platform != "win32":
        signal.signal(signal.SIGINT, _on_sigint)

    try:
        print("\n[sshelf] Press Ctrl-C to unmount.\n")
        while mount.is_alive:
            signal.pause() if sys.platform != "win32" else __import__("time").sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[sshelf] Unmounting ...", end="", flush=True)
        mount.stop()
        print(" done.")
        client.close()
        print("── Mount session ended ──\n")


# ---------------------------------------------------------------------------
# cmd_unmount
# ---------------------------------------------------------------------------

def cmd_unmount(args) -> None:
    """Best-effort unmount of an orphaned mount on the remote."""
    db   = Database()
    conn = resolve_connection(db, args.ref)

    print(f"[sshelf] Connecting to {conn.display_name()} to unmount {args.remote_dir} ...")
    try:
        client = ssh_core.establish(conn, confirm_host_key=confirm_host_key)
    except Exception as exc:  # noqa: BLE001
        print(f"[sshelf] {describe_connect_error(exc)}", file=sys.stderr)
        sys.exit(1)

    from src.protocols.sshfs_mount import _shell_remote_path
    safe = _shell_remote_path(args.remote_dir)
    transport = client.get_transport()
    try:
        chan = transport.open_session()
        chan.settimeout(15)
        chan.exec_command(
            f"fusermount -u {safe} 2>/dev/null || umount {safe} 2>/dev/null && echo OK || echo FAIL"
        )
        output = chan.makefile("r").read().strip()
        chan.close()
        if "OK" in output:
            print(f"[sshelf] Unmounted {args.remote_dir}")
        else:
            print(f"[sshelf] Unmount may have failed — check manually on the remote.",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[sshelf] Unmount command failed: {exc}", file=sys.stderr)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# cmd_mounts  (list saved mount configs)
# ---------------------------------------------------------------------------

def cmd_mounts(args) -> None:
    """List saved mount configs, optionally filtered to a connection."""
    db = Database()

    if getattr(args, "conn", None):
        conn  = resolve_connection(db, args.conn)
        rows  = db.all_mounts(conn.id)
        title = f"Saved mounts for '{conn.display_name()}'"
    else:
        # All connections
        rows  = []
        title = "Saved mounts (all connections)"
        for c in db.all_connections():
            for m in db.all_mounts(c.id):
                m["_conn_name"] = c.display_name()
                rows.append(m)

    if not rows:
        print(f"[sshelf] {title}: none saved.")
        return

    print(f"\n{title}:")
    print(f"  {'ID':>4}  {'Connection':<20}  {'Label':<16}  Mount")
    print(f"  {'──':>4}  {'──────────────────':<20}  {'────────────────'}  ─────────────────────")
    for m in rows:
        conn_name = m.get("_conn_name", "")
        label     = m.get("label") or ""
        display   = f"{m['local_dir']}  →  {m['remote_dir']}"
        print(f"  {m['id']:>4}  {conn_name:<20}  {label:<16}  {display}")
    print()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_mount_banner(local_dir: str, remote_dir: str, conn_name: str) -> None:
    width = 60
    bar   = "─" * width
    print(f"\033[1m{bar}\033[0m")
    print(f"  Mount active on  \033[1;32m{conn_name}\033[0m")
    print(f"  Local            {local_dir}")
    print(f"  Remote           {remote_dir}")
    print(f"\033[1m{bar}\033[0m")
