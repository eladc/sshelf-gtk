"""SQLite persistence layer for connections and app preferences."""

from __future__ import annotations

import sqlite3
import os
from pathlib import Path
from typing import List, Optional

from src.models.connection import Connection
from src.storage import keychain


# Store DB in ~/Library/Application Support/sshelf/
APP_DATA_DIR = Path.home() / "Library" / "Application Support" / "sshelf"
DB_PATH = APP_DATA_DIR / "connections.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL DEFAULT '',
    group_name          TEXT    NOT NULL DEFAULT 'Default',
    protocol            TEXT    NOT NULL DEFAULT 'ssh',
    host                TEXT    NOT NULL DEFAULT '',
    port                INTEGER NOT NULL DEFAULT 0,
    username            TEXT    NOT NULL DEFAULT '',
    password            TEXT    NOT NULL DEFAULT '',
    private_key_file    TEXT    NOT NULL DEFAULT '',
    passphrase          TEXT    NOT NULL DEFAULT '',
    jump_host           TEXT    NOT NULL DEFAULT '',
    startup_command     TEXT    NOT NULL DEFAULT '',
    keep_alive_interval INTEGER NOT NULL DEFAULT 60,
    forward_agent       INTEGER NOT NULL DEFAULT 0,
    x11_forward         INTEGER NOT NULL DEFAULT 0,
    compression         INTEGER NOT NULL DEFAULT 0,
    rdp_domain          TEXT    NOT NULL DEFAULT '',
    rdp_width           INTEGER NOT NULL DEFAULT 1920,
    rdp_height          INTEGER NOT NULL DEFAULT 1080,
    rdp_color_depth     INTEGER NOT NULL DEFAULT 32,
    vnc_view_only       INTEGER NOT NULL DEFAULT 0,
    notes               TEXT    NOT NULL DEFAULT '',
    tags                TEXT    NOT NULL DEFAULT '',
    color               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snippets (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    title   TEXT    NOT NULL DEFAULT '',
    command TEXT    NOT NULL DEFAULT '',
    conn_id INTEGER
);

CREATE TABLE IF NOT EXISTS tunnels (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id      INTEGER NOT NULL,
    label        TEXT    NOT NULL DEFAULT '',
    type         TEXT    NOT NULL DEFAULT 'local',
    local_port   INTEGER NOT NULL DEFAULT 0,
    remote_host  TEXT    NOT NULL DEFAULT '',
    remote_port  INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS mounts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conn_id      INTEGER NOT NULL,
    label        TEXT    NOT NULL DEFAULT '',
    local_dir    TEXT    NOT NULL DEFAULT '',
    remote_dir   TEXT    NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1
);
"""


class Database:
    """Thin wrapper around a SQLite database for sshelf data."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema (idempotent)."""
        existing = {
            r[1]
            for r in self._conn.execute("PRAGMA table_info(connections)")
        }
        new_cols = {
            "protocol":        "TEXT    NOT NULL DEFAULT 'ssh'",
            "rdp_domain":      "TEXT    NOT NULL DEFAULT ''",
            "rdp_width":       "INTEGER NOT NULL DEFAULT 1920",
            "rdp_height":      "INTEGER NOT NULL DEFAULT 1080",
            "rdp_color_depth": "INTEGER NOT NULL DEFAULT 32",
            "vnc_view_only":   "INTEGER NOT NULL DEFAULT 0",
        }
        for col, typedef in new_cols.items():
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE connections ADD COLUMN {col} {typedef}"
                )
        # Migrate legacy port=22 default → 0 so effective_port() works correctly
        self._conn.execute(
            "UPDATE connections SET port = 0 WHERE port = 22 AND protocol = 'ssh'"
        )

    # ------------------------------------------------------------------
    # Connection CRUD
    # ------------------------------------------------------------------

    def all_connections(self) -> List[Connection]:
        rows = self._conn.execute(
            "SELECT * FROM connections ORDER BY group_name, name"
        ).fetchall()
        conns = [Connection.from_dict(dict(r)) for r in rows]
        for c in conns:
            self._fill_password(c)
        return conns

    def get_connection(self, conn_id: int) -> Optional[Connection]:
        row = self._conn.execute(
            "SELECT * FROM connections WHERE id = ?", (conn_id,)
        ).fetchone()
        if not row:
            return None
        c = Connection.from_dict(dict(row))
        self._fill_password(c)
        return c

    def _fill_password(self, conn: Connection) -> None:
        """If the DB password is empty, try to load it from Keychain."""
        if not conn.password and conn.id is not None:
            conn.password = keychain.retrieve(conn.id)

    def save_connection(self, conn: Connection) -> Connection:
        """Insert or update a connection. Returns the connection with its id set.

        Passwords are stored in the macOS Keychain when available; the DB
        column is kept empty so credentials never sit in plaintext on disk.
        """
        password = conn.password  # keep in memory; don't write to DB

        d = conn.to_dict()
        d["password"] = ""  # always blank in SQLite

        if conn.id is None:
            cur = self._conn.execute(
                """INSERT INTO connections
                   (name, group_name, protocol, host, port, username, password,
                    private_key_file, passphrase, jump_host, startup_command,
                    keep_alive_interval, forward_agent, x11_forward, compression,
                    rdp_domain, rdp_width, rdp_height, rdp_color_depth,
                    vnc_view_only, notes, tags, color)
                   VALUES
                   (:name, :group_name, :protocol, :host, :port, :username, :password,
                    :private_key_file, :passphrase, :jump_host, :startup_command,
                    :keep_alive_interval, :forward_agent, :x11_forward, :compression,
                    :rdp_domain, :rdp_width, :rdp_height, :rdp_color_depth,
                    :vnc_view_only, :notes, :tags, :color)""",
                d,
            )
            conn.id = cur.lastrowid
        else:
            self._conn.execute(
                """UPDATE connections SET
                   name=:name, group_name=:group_name, protocol=:protocol,
                   host=:host, port=:port, username=:username, password=:password,
                   private_key_file=:private_key_file, passphrase=:passphrase,
                   jump_host=:jump_host, startup_command=:startup_command,
                   keep_alive_interval=:keep_alive_interval,
                   forward_agent=:forward_agent, x11_forward=:x11_forward,
                   compression=:compression,
                   rdp_domain=:rdp_domain, rdp_width=:rdp_width,
                   rdp_height=:rdp_height, rdp_color_depth=:rdp_color_depth,
                   vnc_view_only=:vnc_view_only,
                   notes=:notes, tags=:tags, color=:color
                   WHERE id=:id""",
                d,
            )
        self._conn.commit()

        # Persist the password in Keychain (after we have a valid id)
        if password and conn.id is not None:
            keychain.store(conn.id, password)
            conn.password = password  # restore on the in-memory object

        return conn

    def delete_connection(self, conn_id: int) -> None:
        self._conn.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
        self._conn.commit()
        keychain.delete(conn_id)

    def groups(self) -> List[str]:
        """Return distinct group names, sorted."""
        rows = self._conn.execute(
            "SELECT DISTINCT group_name FROM connections ORDER BY group_name"
        ).fetchall()
        return [r[0] for r in rows] or ["Default"]

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def get_pref(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM preferences WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_pref(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?)"
            "  ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Snippets CRUD
    # ------------------------------------------------------------------

    def all_snippets(self, conn_id: int | None = None) -> list[dict]:
        """Return global snippets plus connection-specific ones if conn_id given."""
        if conn_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM snippets WHERE conn_id IS NULL OR conn_id = ? ORDER BY id",
                (conn_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM snippets WHERE conn_id IS NULL ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def save_snippet(self, title: str, command: str,
                     conn_id: int | None = None) -> None:
        self._conn.execute(
            "INSERT INTO snippets (title, command, conn_id) VALUES (?, ?, ?)",
            (title, command, conn_id),
        )
        self._conn.commit()

    def delete_snippet(self, snippet_id: int) -> None:
        self._conn.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Tunnels CRUD
    # ------------------------------------------------------------------

    def all_tunnels(self, conn_id: int) -> list[dict]:
        """Return all tunnel records for a connection, ordered by id."""
        rows = self._conn.execute(
            "SELECT * FROM tunnels WHERE conn_id = ? ORDER BY id", (conn_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_tunnel(self, tunnel) -> object:
        """Insert or update a Tunnel dataclass. Returns the tunnel with id set."""
        d = tunnel.to_dict()
        if tunnel.id is None:
            cur = self._conn.execute(
                """INSERT INTO tunnels
                   (conn_id, label, type, local_port, remote_host, remote_port, enabled)
                   VALUES
                   (:conn_id, :label, :type, :local_port, :remote_host, :remote_port, :enabled)""",
                d,
            )
            tunnel.id = cur.lastrowid
        else:
            self._conn.execute(
                """UPDATE tunnels SET
                   conn_id=:conn_id, label=:label, type=:type,
                   local_port=:local_port, remote_host=:remote_host,
                   remote_port=:remote_port, enabled=:enabled
                   WHERE id=:id""",
                d,
            )
        self._conn.commit()
        return tunnel

    def delete_tunnel(self, tunnel_id: int) -> None:
        self._conn.execute("DELETE FROM tunnels WHERE id = ?", (tunnel_id,))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Mounts CRUD
    # ------------------------------------------------------------------

    def all_mounts(self, conn_id: int) -> list[dict]:
        """Return all mount records for a connection, ordered by id."""
        rows = self._conn.execute(
            "SELECT * FROM mounts WHERE conn_id = ? ORDER BY id", (conn_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def save_mount(self, mount) -> object:
        """Insert or update a Mount dataclass. Returns the mount with id set."""
        d = mount.to_dict()
        if mount.id is None:
            cur = self._conn.execute(
                """INSERT INTO mounts
                   (conn_id, label, local_dir, remote_dir, enabled)
                   VALUES
                   (:conn_id, :label, :local_dir, :remote_dir, :enabled)""",
                d,
            )
            mount.id = cur.lastrowid
        else:
            self._conn.execute(
                """UPDATE mounts SET
                   conn_id=:conn_id, label=:label,
                   local_dir=:local_dir, remote_dir=:remote_dir, enabled=:enabled
                   WHERE id=:id""",
                d,
            )
        self._conn.commit()
        return mount

    def delete_mount(self, mount_id: int) -> None:
        self._conn.execute("DELETE FROM mounts WHERE id = ?", (mount_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
