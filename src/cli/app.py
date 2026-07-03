"""SSHelf CLI entry point.

Installed as the `sshelf` console command via setup.py.

Usage
-----
sshelf                           show help
sshelf gui [-n NAME]             launch the GUI (same as `python main.py`)
sshelf list                      list saved connections
sshelf add [--name …] [--host …] add a connection (interactive or flag-based)
sshelf edit <name|id> [flags]    edit a connection
sshelf delete <name|id> [-y]     delete a connection (confirms unless -y)
sshelf connect <name|id>         open an interactive SSH session
sshelf snippet list [--conn …]   list saved commands / snippets
sshelf snippet add [flags]       add a snippet
sshelf snippet delete <id>       delete a snippet by id
sshelf config [KEY] [VALUE]      view or set preferences (e.g. snippet_palette)
sshelf mount <conn> <local> <remote> [--save LABEL]
                                 mount local dir on remote host (blocks until Ctrl-C)
sshelf mount <conn> --use LABEL  mount using a saved config
sshelf unmount <conn> <remote>   unmount an orphaned mount on the remote
sshelf mounts [--conn …]         list saved mount configs
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sshelf",
        description="SSHelf — SSH connection manager  (GUI + CLI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  sshelf gui                         # launch the GUI\n"
            "  sshelf list                        # list connections\n"
            "  sshelf add                         # add interactively\n"
            "  sshelf add --name 'pi' --host 192.168.1.10 --user pi\n"
            "  sshelf edit web-prod               # edit by name\n"
            "  sshelf edit 3                      # edit by id\n"
            "  sshelf delete web-prod             # delete (confirms)\n"
            "  sshelf delete web-prod -y          # delete, skip confirm\n"
            "  sshelf connect web-prod            # open SSH session\n"
            "    (in session)  F2 or Ctrl-]       # open snippet palette\n"
            "    (in session)  F3 or Ctrl-_       # type snippet name/# to insert\n"
            "  sshelf snippet list                # global snippets\n"
            "  sshelf snippet list --conn pi      # global + pi's snippets\n"
            "  sshelf snippet add -t 'Update' -c 'apt update && apt upgrade -y'\n"
            "  sshelf snippet delete 5\n"
            "  sshelf config snippet_palette terminal   # numbered palette in SSH\n"
            "  sshelf config snippet_palette graphical  # popup dialog palette\n"
            "  sshelf mount web ~/work /mnt/work  # mount local dir on remote\n"
            "  sshelf mount web ~/work /mnt/work --save mywork  # save config\n"
            "  sshelf mount web --use mywork      # reuse saved config\n"
            "  sshelf unmount web /mnt/work       # unmount orphaned mount\n"
            "  sshelf mounts --conn web           # list saved mount configs\n"
        ),
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = False  # no subcommand → print help

    # ── gui ────────────────────────────────────────────────────────────────
    p_gui = sub.add_parser("gui", help="launch the SSHelf GUI")
    p_gui.add_argument(
        "-n", "--name", metavar="NAME", default=None,
        help="custom window title suffix (e.g. 'Work', 'Personal')",
    )

    # ── list ───────────────────────────────────────────────────────────────
    sub.add_parser("list", help="list all saved connections")

    # ── add ────────────────────────────────────────────────────────────────
    p_add = sub.add_parser("add", help="add a new SSH connection")
    p_add.add_argument("--name",  metavar="NAME")
    p_add.add_argument("--host",  metavar="HOST")
    p_add.add_argument("--user",  metavar="USERNAME")
    p_add.add_argument("--port",  metavar="PORT", type=int)
    p_add.add_argument("--group", metavar="GROUP", default=None)
    p_add.add_argument("--key",   metavar="KEY_FILE", help="path to private key file")
    p_add.add_argument("--tags",  metavar="TAGS")
    p_add.add_argument("--notes", metavar="NOTES")

    # ── edit ───────────────────────────────────────────────────────────────
    p_edit = sub.add_parser("edit", help="edit a saved connection")
    p_edit.add_argument("ref", metavar="NAME_OR_ID")
    p_edit.add_argument("--name",  metavar="NAME")
    p_edit.add_argument("--host",  metavar="HOST")
    p_edit.add_argument("--user",  metavar="USERNAME")
    p_edit.add_argument("--port",  metavar="PORT", type=int)
    p_edit.add_argument("--group", metavar="GROUP")
    p_edit.add_argument("--key",   metavar="KEY_FILE")
    p_edit.add_argument("--tags",  metavar="TAGS")
    p_edit.add_argument("--notes", metavar="NOTES")

    # ── delete ─────────────────────────────────────────────────────────────
    p_del = sub.add_parser("delete", help="delete a connection (asks for confirmation)")
    p_del.add_argument("ref", metavar="NAME_OR_ID")
    p_del.add_argument("-y", "--yes", action="store_true",
                       help="skip the confirmation prompt")

    # ── connect ────────────────────────────────────────────────────────────
    p_con = sub.add_parser("connect", help="start an interactive SSH session")
    p_con.add_argument("ref", metavar="NAME_OR_ID",
                       help="connection name or numeric id")

    # ── mount ──────────────────────────────────────────────────────────────
    p_mnt = sub.add_parser(
        "mount",
        help="mount a local directory on the remote host (blocks; Ctrl-C to unmount)",
    )
    p_mnt.add_argument("ref", metavar="NAME_OR_ID",
                       help="connection name or numeric id")
    p_mnt.add_argument("local_dir",  metavar="LOCAL",  nargs="?",
                       help="local directory to expose on the remote")
    p_mnt.add_argument("remote_dir", metavar="REMOTE", nargs="?",
                       help="absolute mountpoint path on the remote host")
    p_mnt.add_argument("--save", metavar="LABEL",
                       help="save this config for reuse with --use")
    p_mnt.add_argument("--use", metavar="LABEL",
                       help="mount using a previously saved config")

    # ── unmount ────────────────────────────────────────────────────────────
    p_umnt = sub.add_parser(
        "unmount",
        help="best-effort unmount of an orphaned mount on the remote",
    )
    p_umnt.add_argument("ref",        metavar="NAME_OR_ID")
    p_umnt.add_argument("remote_dir", metavar="REMOTE",
                        help="remote mountpoint to unmount")

    # ── mounts ─────────────────────────────────────────────────────────────
    p_mnts = sub.add_parser("mounts", help="list saved mount configs")
    p_mnts.add_argument("--conn", metavar="NAME_OR_ID",
                        help="filter to a specific connection")

    # ── snippet ────────────────────────────────────────────────────────────
    p_snip = sub.add_parser("snippet", help="manage saved command snippets")
    snip_sub = p_snip.add_subparsers(dest="snip_command", metavar="ACTION")
    snip_sub.required = True

    # snippet list
    p_sl = snip_sub.add_parser("list", help="list snippets")
    p_sl.add_argument("--conn", metavar="NAME_OR_ID",
                      help="include snippets for this connection (plus global)")

    # snippet add
    p_sa = snip_sub.add_parser("add", help="add a snippet")
    p_sa.add_argument("-t", "--title",   metavar="TITLE")
    p_sa.add_argument("-c", "--command", metavar="COMMAND")
    p_sa.add_argument("--conn", metavar="NAME_OR_ID",
                      help="tie snippet to a specific connection (default: global)")

    # snippet delete
    p_sd = snip_sub.add_parser("delete", help="delete a snippet by id")
    p_sd.add_argument("id", metavar="ID", type=int)

    # ── config ─────────────────────────────────────────────────────────────
    p_config = sub.add_parser(
        "config",
        help="view or set sshelf preferences",
        description="View or set sshelf preferences from the terminal.",
    )
    p_config.add_argument(
        "key",
        nargs="?",
        help="preference key (e.g. snippet_palette)",
    )
    p_config.add_argument(
        "value",
        nargs="?",
        help="new value (omit to print current value)",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def cli_main() -> None:
    """Console entry point installed as the `sshelf` command."""
    parser = _build_parser()
    args   = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    cmd = args.command

    if cmd == "gui":
        _launch_gui(args)

    elif cmd == "list":
        from src.cli.commands import cmd_list
        cmd_list()

    elif cmd == "add":
        from src.cli.commands import cmd_add
        cmd_add(args)

    elif cmd == "edit":
        from src.cli.commands import cmd_edit
        cmd_edit(args)

    elif cmd == "delete":
        from src.cli.commands import cmd_delete
        cmd_delete(args)

    elif cmd == "connect":
        from src.cli.session import cmd_connect
        cmd_connect(args.ref)

    elif cmd == "mount":
        from src.cli.mount import cmd_mount
        cmd_mount(args)

    elif cmd == "unmount":
        from src.cli.mount import cmd_unmount
        cmd_unmount(args)

    elif cmd == "mounts":
        from src.cli.mount import cmd_mounts
        cmd_mounts(args)

    elif cmd == "snippet":
        from src.cli.commands import cmd_snippet
        cmd_snippet(args)

    elif cmd == "config":
        from src.cli.commands import cmd_config
        from src.storage.database import Database
        cmd_config(Database(), args)

    else:
        parser.print_help()
        sys.exit(1)


def _launch_gui(args: argparse.Namespace) -> None:
    """Import and run the Qt GUI (same path as `python main.py`)."""
    from src.app import Application
    app = Application(sys.argv, name=getattr(args, "name", None))
    sys.exit(app.exec())
