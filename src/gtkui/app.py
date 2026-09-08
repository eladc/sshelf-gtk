"""Gtk.Application entry point for the GTK build."""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    # Allow `python3 src/gtkui/app.py` directly (e.g. from the .desktop
    # launcher) without an editable install: put the repo root on sys.path
    # so the absolute `src.*` imports below resolve.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, Gtk  # noqa: E402

from src.gtkui.main_window import MainWindow
from src.storage.database import Database

APP_ID = "io.github.sshelf"


class Application(Gtk.Application):
    """sshelf GTK application."""

    def __init__(self, name: str | None = None) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self._name = name
        self._db: Database | None = None
        self._window: MainWindow | None = None

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        self._db = Database()

    def do_activate(self) -> None:
        if self._window is None:
            self._window = MainWindow(self, self._db, name=self._name)
        self._window.present()

    def do_shutdown(self) -> None:
        if self._db is not None:
            self._db.close()
        Gtk.Application.do_shutdown(self)


def run(name: str | None = None, argv: list[str] | None = None) -> int:
    """Run the GTK app; returns the process exit code."""
    return Application(name=name).run(argv if argv is not None else sys.argv[:1])


if __name__ == "__main__":
    sys.exit(run())
