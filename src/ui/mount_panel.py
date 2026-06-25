"""Reverse-mount side panel.

Lists saved local→remote mount configs for the current connection and lets
the user add / remove / toggle them.  Mount workers are started/stopped
as the SSH session connects or disconnects.
"""

from __future__ import annotations

import threading
from typing import Optional

from PyQt6.QtCore import Qt, QMetaObject, Q_ARG, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from src.models.mount import Mount


# ── Add-mount dialog ──────────────────────────────────────────────────────────

class _MountDialog(QDialog):
    """Dialog for creating or editing a Mount config."""

    def __init__(self, mount: Mount | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Mount" if mount is None else "Edit Mount")
        self.setMinimumWidth(400)
        self.setModal(True)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(8)
        form.setContentsMargins(12, 12, 12, 8)

        self._label = QLineEdit(mount.label if mount else "")
        self._label.setPlaceholderText("e.g. Work files")
        form.addRow("Label:", self._label)

        self._local_dir = QLineEdit(mount.local_dir if mount else "")
        self._local_dir.setPlaceholderText("e.g. ~/my-project")
        form.addRow("Local dir:", self._local_dir)

        self._remote_dir = QLineEdit(mount.remote_dir if mount else "")
        self._remote_dir.setPlaceholderText("e.g. /mnt/my-project")
        form.addRow("Remote dir:", self._remote_dir)

        layout.addLayout(form)

        note = QLabel(
            "<small><i>Remote host needs: <b>sudo apt install sshfs</b></i></small>"
        )
        note.setStyleSheet("color: #888; padding: 0 12px 4px;")
        layout.addWidget(note)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def result_mount(self) -> Optional[Mount]:
        """Return a new Mount from the form, or None if fields are missing."""
        local  = self._local_dir.text().strip()
        remote = self._remote_dir.text().strip()
        if not local or not remote:
            return None
        return Mount(
            id=None,
            conn_id=0,
            label=self._label.text().strip() or "Mount",
            local_dir=local,
            remote_dir=remote,
            enabled=True,
        )


# ── Active-mount runner ───────────────────────────────────────────────────────

class _MountRunner:
    """Runs ReverseMount in a background daemon thread.

    Matches the .start() / .stop() / .is_alive interface of tunnel workers.
    """

    def __init__(self, transport, mount: Mount, on_error=None) -> None:
        from src.protocols.sshfs_mount import ReverseMount
        self._rm       = ReverseMount(transport, mount.local_dir, mount.remote_dir)
        self._on_error = on_error or (lambda msg: None)
        self._thread   = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._rm.stop()

    @property
    def is_alive(self) -> bool:
        return self._rm.is_alive

    def _run(self) -> None:
        try:
            self._rm.start()
        except Exception as exc:  # noqa: BLE001
            self._on_error(str(exc))


# ── Mount panel ───────────────────────────────────────────────────────────────

class MountPanel(QWidget):
    """
    Side panel listing reverse-mount configs for one SSH connection.

    Usage
    -----
    panel = MountPanel(db=db, conn_id=conn.id, parent=self)
    # When SSH connects:
    panel.set_worker(ssh_worker)
    # When SSH disconnects:
    panel.set_worker(None)
    """

    def __init__(self, db, conn_id: int | None, parent=None) -> None:
        super().__init__(parent)
        self._db       = db
        self._conn_id  = conn_id
        self._worker   = None
        self._active: list[_MountRunner] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._status_lbl = QLabel("SSH not connected")
        self._status_lbl.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self._status_lbl)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background: #1e1e1e; border: 1px solid #333; }"
            "QListWidget::item { padding: 4px 6px; color: #ccc; }"
            "QListWidget::item:selected { background: #2c5282; }"
        )
        layout.addWidget(self._list, stretch=1)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("＋ Add")
        btn_add.clicked.connect(self._on_add)
        btn_row.addWidget(btn_add)

        self._btn_remove = QPushButton("⌫ Remove")
        self._btn_remove.setEnabled(False)
        self._btn_remove.clicked.connect(self._on_remove)
        btn_row.addWidget(self._btn_remove)

        self._btn_toggle = QPushButton("Enable")
        self._btn_toggle.setEnabled(False)
        self._btn_toggle.clicked.connect(self._on_toggle)
        btn_row.addWidget(self._btn_toggle)

        layout.addLayout(btn_row)

        self._list.currentRowChanged.connect(self._on_selection_changed)
        self._reload()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_worker(self, worker) -> None:
        """Call with SSHWorker on connect, None on disconnect."""
        self._worker = worker
        self._stop_all_active()
        if worker is not None:
            self._status_lbl.setText("Connected — mounts active")
            self._status_lbl.setStyleSheet("color: #98c379; font-size: 11px;")
            self._start_enabled_mounts()
        else:
            self._status_lbl.setText("SSH not connected")
            self._status_lbl.setStyleSheet("color: #888; font-size: 11px;")
        self._reload()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _reload(self) -> None:
        self._list.clear()
        if self._conn_id is None or self._db is None:
            return
        for row in self._db.all_mounts(self._conn_id):
            m = Mount.from_dict(row)
            item = QListWidgetItem(self._item_text(m))
            item.setData(Qt.ItemDataRole.UserRole, m)
            if not m.enabled:
                item.setForeground(Qt.GlobalColor.darkGray)
            self._list.addItem(item)
        self._on_selection_changed(self._list.currentRow())

    def _item_text(self, m: Mount) -> str:
        active = any(r.is_alive for r in self._active)
        status = "●" if (m.enabled and self._worker is not None and active) else "○"
        return f"{status}  {m.label}   {m.local_dir}  →  {m.remote_dir}"

    def _on_selection_changed(self, row: int) -> None:
        has = row >= 0
        self._btn_remove.setEnabled(has)
        if has:
            item = self._list.item(row)
            m: Mount = item.data(Qt.ItemDataRole.UserRole)
            self._btn_toggle.setEnabled(True)
            self._btn_toggle.setText("Disable" if m.enabled else "Enable")
        else:
            self._btn_toggle.setEnabled(False)
            self._btn_toggle.setText("Enable")

    def _on_add(self) -> None:
        if self._conn_id is None:
            return
        dlg = _MountDialog(parent=self)
        if dlg.exec():
            m = dlg.result_mount()
            if m is None:
                return
            m.conn_id = self._conn_id
            self._db.save_mount(m)
            if m.enabled and self._worker is not None:
                self._launch_runner(m)
            self._reload()

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        m: Mount = item.data(Qt.ItemDataRole.UserRole)
        if m.id is not None:
            self._db.delete_mount(m.id)
        self._reload()

    def _on_toggle(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        m: Mount = item.data(Qt.ItemDataRole.UserRole)
        m.enabled = not m.enabled
        if m.id is not None:
            self._db.save_mount(m)
        if m.enabled and self._worker is not None:
            self._launch_runner(m)
        self._reload()

    # ── Runner management ──────────────────────────────────────────────────────

    def _start_enabled_mounts(self) -> None:
        if self._conn_id is None or self._db is None:
            return
        for row in self._db.all_mounts(self._conn_id):
            m = Mount.from_dict(row)
            if m.enabled:
                self._launch_runner(m)

    def _launch_runner(self, mount: Mount) -> None:
        if self._worker is None:
            return
        transport = self._worker.get_transport()
        if transport is None:
            return
        try:
            def _on_err(msg: str, lbl=self._status_lbl) -> None:
                QMetaObject.invokeMethod(
                    lbl, "setText",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, f"Mount error: {msg[:60]}"),
                )
                QMetaObject.invokeMethod(
                    lbl, "setStyleSheet",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, "color: #e06c75; font-size: 11px;"),
                )

            runner = _MountRunner(transport, mount, on_error=_on_err)
            runner.start()
            self._active.append(runner)
        except Exception:  # noqa: BLE001
            pass

    def _stop_all_active(self) -> None:
        for r in list(self._active):
            try:
                r.stop()
            except Exception:  # noqa: BLE001
                pass
        self._active.clear()
