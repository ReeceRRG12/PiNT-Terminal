"""Native PiNT Terminal desktop shell."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from datetime import datetime
import posixpath
import queue
import sys
import tempfile

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSettings
from PySide6.QtGui import QFont, QCloseEvent, QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QLineEdit,
    QComboBox, QSpinBox, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QMessageBox, QInputDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSplitter, QPlainTextEdit, QProgressBar, QScrollArea, QLayout,
)

from . import __version__
from .models import Host
from .vault import Workspace
from .recordings import RecordStore
from .transcript import TranscriptRenderer
from .terminal import TerminalWidget
from .transport import SessionWorker, trust_host_key
from .transfers import TransferClient, UnknownHostKey
from .tftp import TFTPServer
from .theme import apply_theme


def label(text, kind=None):
    result = QLabel(text)
    if kind:
        result.setObjectName(kind)
    return result


def button(text, callback=None, primary=False):
    result = QPushButton(text)
    if primary:
        result.setObjectName("primary")
    if callback:
        result.clicked.connect(callback)
    return result


def table(headers):
    result = QTableWidget(0, len(headers))
    result.setHorizontalHeaderLabels(headers)
    result.verticalHeader().hide()
    result.verticalHeader().setDefaultSectionSize(54)
    result.setSelectionBehavior(QAbstractItemView.SelectRows)
    result.setSelectionMode(QAbstractItemView.SingleSelection)
    result.setEditTriggers(QAbstractItemView.NoEditTriggers)
    result.setShowGrid(False)
    result.setAlternatingRowColors(True)
    result.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    result.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return result


def put_row(widget, values, data=None):
    row = widget.rowCount()
    widget.insertRow(row)
    for column, value in enumerate(values):
        item = QTableWidgetItem(str(value))
        if column == 0:
            item.setData(Qt.UserRole, data)
        widget.setItem(row, column, item)


def selected_data(widget):
    row = widget.currentRow()
    return widget.item(row, 0).data(Qt.UserRole) if row >= 0 and widget.item(row, 0) else None


def ask_password(parent, title, create=False):
    password, ok = QInputDialog.getText(parent, title, "Workspace passphrase:", QLineEdit.Password)
    if not ok:
        return None
    if create:
        if len(password) < 12:
            raise ValueError("Use a passphrase of at least 12 characters.")
        again, ok = QInputDialog.getText(parent, title, "Confirm passphrase:", QLineEdit.Password)
        if not ok:
            return None
        if password != again:
            raise ValueError("The passphrases do not match.")
    return password


class Welcome(QDialog):
    def __init__(self):
        super().__init__()
        self.workspace = None
        self.setWindowTitle("PiNT Terminal")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 36)
        layout.setSpacing(16)
        layout.addWidget(label("PiNT", "brand"))
        layout.addWidget(label("TERMINAL", "eyebrow"))
        layout.addSpacing(12)
        layout.addWidget(label("Your network. Your workspace.", "title"))
        intro = label("SSH, Telnet and file transfer in one place.\nYour connections and recordings stay encrypted on your device.", "muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addSpacing(12)
        layout.addWidget(button("Create encrypted workspace", self.create, True))
        layout.addWidget(button("Open workspace", self.open))
        layout.addWidget(button("Import portable workspace", self.import_bundle))
        self.recent = QSettings().value("last_workspace", "")
        if self.recent and Path(self.recent).is_file():
            layout.addWidget(button(f"Unlock {Path(self.recent).stem}", lambda: self.open(self.recent)))
        note = label(f"Keep your passphrase safe. There is no account recovery.\nPiNT Terminal {__version__} • Early Access Beta", "muted")
        note.setWordWrap(True)
        layout.addSpacing(8)
        layout.addWidget(note)

    def finish(self, workspace):
        self.workspace = workspace
        QSettings().setValue("last_workspace", str(workspace.path))
        self.accept()

    def create(self):
        path, _ = QFileDialog.getSaveFileName(self, "Create workspace", "My network.pint", "PiNT workspace (*.pint)")
        if not path:
            return
        try:
            password = ask_password(self, "New workspace", True)
            if password is not None:
                self.finish(Workspace.create(Path(path), password))
        except Exception as exc:
            QMessageBox.warning(self, "Workspace", str(exc))

    def open(self, path=None):
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open workspace", "", "PiNT workspace (*.pint)")
        if not path:
            return
        try:
            password = ask_password(self, "Unlock workspace")
            if password is not None:
                self.finish(Workspace.open(Path(path), password))
        except Exception as exc:
            QMessageBox.warning(self, "Cannot unlock workspace", str(exc))

    def import_bundle(self):
        source, _ = QFileDialog.getOpenFileName(self, "Import workspace", "", "PiNT portable workspace (*.pintbundle)")
        if not source:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "Save imported workspace", "Imported network.pint", "PiNT workspace (*.pint)")
        if not destination:
            return
        try:
            password = ask_password(self, "Unlock portable workspace")
            if password is not None:
                self.finish(Workspace.import_bundle(Path(source), Path(destination), password))
        except Exception as exc:
            QMessageBox.warning(self, "Cannot import workspace", str(exc))


class HostDialog(QDialog):
    def __init__(self, parent=None, host=None):
        super().__init__(parent)
        self.host = host
        self.result_host = None
        self.setWindowTitle("Edit connection" if host else "New connection")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(label(self.windowTitle(), "section"))
        form = QFormLayout()
        self.name = QLineEdit(host.name if host else "")
        self.address = QLineEdit(host.address if host else "")
        self.address.setPlaceholderText("Hostname or IP address")
        self.protocol = QComboBox()
        self.protocol.addItems(["SSH", "Telnet", "SFTP", "FTP"])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.protocol.currentTextChanged.connect(lambda p: self.port.setValue({"SSH": 22, "Telnet": 23, "SFTP": 22, "FTP": 21}[p]))
        self.protocol.setCurrentText(host.protocol.upper() if host and host.protocol != "telnet" else "Telnet" if host else "SSH")
        self.port.setValue(host.port if host else 22)
        self.username = QLineEdit(host.username if host else "")
        self.password = QLineEdit(host.password if host else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Optional: saved inside the encrypted workspace")
        self.key = QLineEdit(host.key_path if host else "")
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key)
        key_layout.addWidget(button("Browse", self.choose_key))
        self.group = QLineEdit(host.group if host else "Ungrouped")
        self.tags = QLineEdit(host.tags if host else "")
        self.reconnect = QCheckBox("Reconnect after an interrupted connection")
        self.reconnect.setChecked(host.auto_reconnect if host else True)
        for title, field in [("Name", self.name), ("Address", self.address), ("Protocol", self.protocol),
                             ("Port", self.port), ("Username", self.username), ("Password", self.password),
                             ("SSH key file", key_row), ("Group", self.group), ("Tags", self.tags)]:
            form.addRow(title, field)
        layout.addLayout(form)
        layout.addWidget(self.reconnect)
        note = label("Telnet login is interactive. Telnet and FTP use unencrypted network connections.\nSSH key files stay on this device and are not included in workspace exports.", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH private key")
        if path:
            self.key.setText(path)

    def save(self):
        try:
            values = dict(name=self.name.text().strip() or self.address.text().strip(),
                          address=self.address.text().strip(), protocol=self.protocol.currentText().lower(),
                          port=self.port.value(), username=self.username.text().strip(), password=self.password.text(),
                          key_path=self.key.text().strip(), group=self.group.text().strip() or "Ungrouped",
                          tags=self.tags.text().strip(), auto_reconnect=self.reconnect.isChecked())
            if self.host:
                values["id"] = self.host.id
            self.result_host = Host(**values)
            self.result_host.validate()
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Connection details", str(exc))


class Job(QThread):
    completed = Signal(object)
    failed = Signal(object)
    progress = Signal(object, object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.completed.emit(self.function(self.progress.emit))
        except Exception as exc:
            self.failed.emit(exc)


class SessionPane(QWidget):
    storage_error = Signal(str)

    def __init__(self, main, host, demo=False):
        super().__init__()
        self.main, self.host = main, host
        self.session_id = main.store.start_session(host)
        self.worker = None
        self.closed_record = False
        self.recording_error = None
        self._closing = False
        self._output_queue = queue.Queue(maxsize=16)
        self.storage_error.connect(self.recording_failed)
        self.output_timer = QTimer(self)
        self.output_timer.setInterval(16)
        self.output_timer.timeout.connect(self.drain_output)
        self.output_timer.start()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        controls.setContentsMargins(12, 8, 12, 8)
        self.state = label("Ready", "muted")
        controls.addWidget(self.state)
        controls.addStretch()
        controls.addWidget(label("●  Recording encrypted output", "eyebrow"))
        self.reconnect_button = button("Reconnect", self.reconnect)
        self.disconnect_button = button("Disconnect", self.disconnect)
        controls.addWidget(self.reconnect_button)
        controls.addWidget(self.disconnect_button)
        layout.addLayout(controls)
        self.terminal = TerminalWidget(main.store, self.session_id)
        self.terminal.failed.connect(self.recording_failed)
        layout.addWidget(self.terminal, 1)
        if demo:
            self.reconnect_button.setEnabled(False)
            self.disconnect_button.setEnabled(False)
            self.receive("\x1b[36mPiNT Terminal\x1b[0m  •  Local demonstration\r\n\r\n"
                         "This is a sample terminal. No network connection is open.\r\n"
                         "\r\n\x1b[32mlab-switch#\x1b[0m show interfaces status\r\n"
                         "Port       Name                 Status       VLAN     Speed\r\n"
                         "Gi1/0/1    Office uplink         connected    trunk    1000\r\n"
                         "Gi1/0/2    Wireless AP           connected    20       1000\r\n"
                         "Gi1/0/3    Meeting room          connected    30       1000\r\n"
                         "Gi1/0/4    Spare                 notconnect   1        auto\r\n"
                         "\r\n\x1b[32mlab-switch#\x1b[0m ")
            self.state.setText("DEMO • No connection")
            self.main.store.finish_session(self.session_id, "Demo output")
        else:
            self.start()

    def start(self):
        if self.worker and self.worker.isRunning():
            return
        self.worker = SessionWorker(self.host, self.main.known_hosts_path, self)
        # Persist synchronously in the transport thread. Only rendering is queued,
        # so closing a window cannot discard output waiting for the GUI event loop.
        self.worker.output.connect(self.enqueue_output, Qt.DirectConnection)
        self.worker.status.connect(self.status_changed)
        self.worker.error.connect(self.show_error)
        self.worker.host_key.connect(self.unknown_key)
        self.worker.ended.connect(self.ended)
        self.terminal.input_ready.connect(self.worker.send)
        self.terminal.resized.connect(self.worker.resize)
        self.worker.resize(self.terminal.screen.columns, self.terminal.screen.lines)
        self.worker.start()

    def enqueue_output(self, data):
        try:
            self.main.store.append_output(self.session_id, data)
        except Exception as exc:
            self.worker.disconnect()
            self.storage_error.emit(str(exc))
            return
        while not self._closing:
            try:
                self._output_queue.put(data, timeout=0.05)
                return
            except queue.Full:
                continue

    def drain_output(self):
        if self.closed_record:
            return
        for _ in range(4):
            try:
                data = self._output_queue.get_nowait()
            except queue.Empty:
                break
            self.terminal.feed(data)

    def receive(self, data):
        try:
            self.main.store.append_output(self.session_id, data)
            self.terminal.feed(data)
        except Exception as exc:
            self.recording_failed(str(exc))

    def status_changed(self, status):
        if self.closed_record:
            return
        self.state.setText(self.recording_error or status)
        self.terminal.set_connected(status.lower() == "connected")
        try:
            self.main.store.event(self.session_id, status)
            self.main.store.set_session_status(self.session_id, self.recording_error or status)
        except Exception as exc:
            self.recording_failed(str(exc))

    def show_error(self, error):
        if self.closed_record:
            return
        self.state.setText(error)
        self.state.setToolTip(error)
        self.terminal.set_connected(False)

    def unknown_key(self, fingerprint, public_key):
        if self.closed_record:
            return
        if self.main.confirm_key(self.host, fingerprint, public_key):
            QTimer.singleShot(200, self.reconnect)

    def ended(self):
        if self.closed_record:
            return
        self.drain_output()
        self.terminal.set_connected(False)
        try:
            self.main.store.finish_session(self.session_id, self.state.text())
        except Exception as exc:
            self.show_error(str(exc))
        self.main.refresh_records()

    def recording_failed(self, error):
        if self.closed_record:
            return
        self.recording_error = "Recording failed: " + error
        self.disconnect()
        self.state.setText(self.recording_error)
        self.main.statusBar().showMessage("Session stopped because its recording could not be saved.")

    def disconnect(self):
        if self.worker:
            self.worker.disconnect()
        self.terminal.set_connected(False)

    def reconnect(self):
        if self.closed_record:
            return
        try:
            self.main.store.event(self.session_id, "Manual reconnect requested")
        except Exception as exc:
            self.recording_failed(str(exc))
            return
        self.recording_error = None
        self._closing = False
        if self.worker and self.worker.isRunning():
            self.worker.reconnect()
        else:
            if self.worker:
                try:
                    self.terminal.input_ready.disconnect(self.worker.send)
                    self.terminal.resized.disconnect(self.worker.resize)
                except RuntimeError:
                    pass
            self.start()

    def stop_and_wait(self):
        self._closing = True
        self.disconnect()
        stopped = not self.worker or self.worker.wait(4000)
        if stopped:
            self.output_timer.stop()
            self.drain_output()
            self.main.store.finish_session(self.session_id, "Disconnected")
            self.closed_record = True
        return stopped


class MainWindow(QMainWindow):
    tftp_event = Signal(str)

    def __init__(self, workspace, demo=False):
        super().__init__()
        self.workspace = workspace
        self.demo = demo
        self.store = RecordStore(workspace.recordings_path, workspace.key)
        self.runtime_dir = tempfile.TemporaryDirectory(prefix="pint-runtime-")
        self.known_hosts_path = Path(self.runtime_dir.name) / "known_hosts"
        self.known_hosts_path.write_text(workspace.settings.get("known_hosts", ""), encoding="utf-8")
        os.chmod(self.known_hosts_path, 0o600)
        self.jobs = []
        self.sessions = []
        self.tftp_server = None
        self.remote_path = "."
        self.remote_host_id = None
        self.active_transfer_client = None
        self.transfer_cancelled = False
        self.setWindowTitle(f"PiNT Terminal • {workspace.path.stem}" + (" • Demo" if demo else ""))
        self.resize(1440, 900)
        self.setMinimumSize(1100, 720)
        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(212)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 26, 16, 16)
        side.setSpacing(8)
        side.addWidget(label("PiNT", "brand"))
        side.addWidget(label("TERMINAL", "eyebrow"))
        side.addSpacing(30)
        side.addWidget(label("WORKSPACE", "muted"))
        self.nav = []
        for index, title in enumerate(["Hosts", "Terminal sessions", "Recordings", "File transfer", "TFTP host"]):
            nav = button(title, lambda checked=False, i=index: self.show_page(i))
            nav.setObjectName("nav")
            nav.setCheckable(True)
            self.nav.append(nav)
            side.addWidget(nav)
        side.addStretch()
        side.addWidget(label("LOCAL & ENCRYPTED", "eyebrow"))
        workspace_name = label(workspace.path.stem)
        workspace_name.setWordWrap(True)
        side.addWidget(workspace_name)
        side.addWidget(button("Export workspace", self.export_workspace))
        side.addWidget(button("Lock && close", self.close))
        side.addSpacing(8)
        side.addWidget(label(f"{__version__}\nEarly Access Beta", "muted"))
        shell.addWidget(sidebar)
        self.pages = QStackedWidget()
        shell.addWidget(self.pages, 1)
        self.make_hosts_page()
        self.make_sessions_page()
        self.make_recordings_page()
        self.make_transfer_page()
        self.make_tftp_page()
        self.statusBar().showMessage("Workspace unlocked • Changes save automatically" + (" • Demo data only" if demo else ""))
        self.refresh_hosts()
        self.show_page(0)
        new_action = QAction("New connection", self)
        new_action.setShortcut(QKeySequence.New if sys.platform == "darwin" else QKeySequence("Ctrl+Shift+N"))
        new_action.triggered.connect(self.new_host)
        self.addAction(new_action)
        self.tftp_event.connect(self.tftp_log.appendPlainText)

    def page(self, eyebrow, title, subtitle):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(28, 26, 28, 22)
        layout.setSpacing(18)
        layout.addWidget(label(eyebrow, "eyebrow"))
        layout.addWidget(label(title, "title"))
        layout.addWidget(label(subtitle, "muted"))
        self.pages.addWidget(widget)
        return layout

    def show_page(self, index):
        self.pages.setCurrentIndex(index)
        for i, nav in enumerate(self.nav):
            nav.setChecked(index == i)
        if index == 2:
            self.refresh_records()

    def make_hosts_page(self):
        layout = self.page("CONNECTION LIBRARY", "Your network, within reach.", "Save a connection once. Keep its history every time.")
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search hosts, addresses, groups or tags…")
        self.search.textChanged.connect(self.filter_hosts)
        self.groups = QComboBox()
        self.groups.setMinimumWidth(160)
        self.groups.currentTextChanged.connect(self.filter_hosts)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.groups)
        bar.addWidget(button("+ New connection", self.new_host, True))
        layout.addLayout(bar)
        self.host_count = label("", "muted")
        layout.addWidget(self.host_count)
        split = QSplitter()
        self.host_table = table(["HOST", "ADDRESS", "PROTOCOL", "GROUP"])
        self.host_table.itemSelectionChanged.connect(self.update_details)
        self.host_table.itemDoubleClicked.connect(lambda _: self.connect_selected())
        split.addWidget(self.host_table)
        details = QFrame()
        details.setObjectName("panel")
        details.setMinimumWidth(270)
        details.setMaximumWidth(340)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(22, 22, 22, 22)
        detail_layout.setSpacing(18)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        detail_scroll.setFocusPolicy(Qt.StrongFocus)
        detail_scroll.setAccessibleName("Connection details")
        detail_scroll.setStyleSheet("QScrollArea { background: transparent; border: 0; }")
        detail_scroll.viewport().setAutoFillBackground(False)
        detail_content = QWidget()
        content_layout = QVBoxLayout(detail_content)
        content_layout.setContentsMargins(0, 0, 6, 0)
        content_layout.setSpacing(18)
        # Preserve wrapped label heights, allowing the viewport to scroll instead
        # of clipping metadata when Windows scaling reduces the available space.
        content_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
        content_layout.addWidget(label("CONNECTION DETAILS", "eyebrow"))
        self.detail_title = label("Ready when you are", "section")
        self.detail_title.setWordWrap(True)
        content_layout.addWidget(self.detail_title)
        self.detail_body = label("Add your first host to get started.\n\nProfiles and credentials are saved in your encrypted workspace.", "muted")
        self.detail_body.setWordWrap(True)
        self.detail_body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        content_layout.addWidget(self.detail_body)
        content_layout.addStretch()
        note = label("SESSION HISTORY\nTerminal output is recorded automatically and remains available after you disconnect.", "muted")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        detail_scroll.setWidget(detail_content)
        detail_content.setAutoFillBackground(False)
        detail_layout.addWidget(detail_scroll, 1)
        self.connect_button = button("Connect", self.connect_selected, True)
        self.edit_button = button("Edit connection", self.edit_host)
        self.delete_button = button("Delete connection", self.delete_host)
        self.delete_button.setObjectName("danger")
        for control in [self.connect_button, self.edit_button, self.delete_button]:
            detail_layout.addWidget(control)
        split.addWidget(details)
        split.setSizes([750, 300])
        layout.addWidget(split, 1)

    def selected_host(self):
        host_id = selected_data(self.host_table)
        return next((h for h in self.workspace.hosts if h.id == host_id), None)

    def refresh_hosts(self):
        current_group = self.groups.currentText()
        self.groups.blockSignals(True)
        self.groups.clear()
        self.groups.addItems(["All groups"] + sorted({h.group for h in self.workspace.hosts}))
        if self.groups.findText(current_group) >= 0:
            self.groups.setCurrentText(current_group)
        self.groups.blockSignals(False)
        self.filter_hosts()
        current_id = self.transfer_hosts.currentData()
        self.transfer_hosts.clear()
        for host in self.workspace.hosts:
            if host.protocol in ("ssh", "sftp", "ftp"):
                self.transfer_hosts.addItem(f"{host.name}  /  {'FTP' if host.protocol == 'ftp' else 'SFTP'}", host.id)
        if self.transfer_hosts.findData(current_id) >= 0:
            self.transfer_hosts.setCurrentIndex(self.transfer_hosts.findData(current_id))

    def filter_hosts(self, *_):
        query = self.search.text().casefold()
        group = self.groups.currentText()
        self.host_table.setRowCount(0)
        for host in sorted(self.workspace.hosts, key=lambda h: (h.group.casefold(), h.name.casefold())):
            if group not in ("All groups", "", host.group):
                continue
            if query and query not in " ".join([host.name, host.address, host.group, host.tags]).casefold():
                continue
            put_row(self.host_table, [host.name, host.address, host.protocol.upper(), host.group], host.id)
        self.host_count.setText(f"{len(self.workspace.hosts)} saved connections    ·    {len({h.group for h in self.workspace.hosts})} groups    ·    Stored locally")
        if self.host_table.rowCount():
            self.host_table.selectRow(0)
        self.update_details()

    def update_details(self):
        host = self.selected_host()
        for control in [self.connect_button, self.edit_button, self.delete_button]:
            control.setEnabled(host is not None)
        if not host:
            self.detail_title.setText("No host selected")
            self.detail_body.setText("Add a connection or change your search to find a saved host.")
            return
        self.detail_title.setText(host.name)
        self.detail_body.setText(f"{host.protocol.upper()}  /  {host.address}:{host.port}\n\n"
                                 f"Group: {host.group}\n\nUser: {host.username or 'Interactive / SSH agent'}\n\n"
                                 f"Tags: {host.tags or 'None'}\n\n"
                                 f"Reconnect: {'automatic' if host.auto_reconnect else 'manual'}")

    def save_workspace(self):
        self.workspace.save()
        self.statusBar().showMessage("Workspace saved • Credentials encrypted")

    def new_host(self):
        dialog = HostDialog(self)
        if dialog.exec() == QDialog.Accepted:
            self.workspace.hosts.append(dialog.result_host)
            try:
                self.save_workspace()
            except Exception as exc:
                self.workspace.hosts.pop()
                QMessageBox.warning(self, "Cannot save connection", str(exc))
            self.refresh_hosts()

    def edit_host(self):
        host = self.selected_host()
        if not host:
            return
        dialog = HostDialog(self, host)
        if dialog.exec() == QDialog.Accepted:
            index = self.workspace.hosts.index(host)
            self.workspace.hosts[index] = dialog.result_host
            try:
                self.save_workspace()
            except Exception as exc:
                self.workspace.hosts[index] = host
                QMessageBox.warning(self, "Cannot save connection", str(exc))
            self.refresh_hosts()

    def delete_host(self):
        host = self.selected_host()
        if not host or QMessageBox.question(self, "Delete connection", f"Delete {host.name}? Its recordings will remain available.") != QMessageBox.Yes:
            return
        self.workspace.hosts.remove(host)
        try:
            self.save_workspace()
        except Exception as exc:
            self.workspace.hosts.append(host)
            QMessageBox.warning(self, "Cannot delete connection", str(exc))
        self.refresh_hosts()

    def make_sessions_page(self):
        layout = self.page("LIVE TERMINALS", "Stay connected.", "Each tab keeps its own recording and disk-backed scrollback.")
        self.session_tabs = QTabWidget()
        self.session_tabs.setTabsClosable(True)
        self.session_tabs.tabCloseRequested.connect(self.close_session)
        layout.addWidget(self.session_tabs, 1)
        layout.addWidget(label("Select a host to open a terminal. Copy: Ctrl/Cmd+Shift+C  ·  Paste: Ctrl/Cmd+Shift+V", "muted"))

    def connect_selected(self):
        host = self.selected_host()
        if not host:
            return
        if self.demo:
            QMessageBox.information(self, "Demo workspace", "Demo hosts are examples. Create a real workspace to connect to your devices.")
            return
        if host.protocol in ("ftp", "sftp"):
            self.transfer_hosts.setCurrentIndex(self.transfer_hosts.findData(host.id))
            self.show_page(3)
            self.list_remote()
            return
        try:
            pane = SessionPane(self, host)
            self.sessions.append(pane)
            index = self.session_tabs.addTab(pane, host.name)
            self.session_tabs.setCurrentIndex(index)
            self.show_page(1)
            pane.terminal.setFocus()
        except Exception as exc:
            QMessageBox.warning(self, "Cannot start session", str(exc))

    def close_session(self, index):
        pane = self.session_tabs.widget(index)
        if pane.worker and pane.worker.isRunning():
            if QMessageBox.question(self, "Close session", f"Disconnect {pane.host.name} and close this tab?") != QMessageBox.Yes:
                return
        try:
            stopped = pane.stop_and_wait()
        except Exception as exc:
            QMessageBox.warning(self, "Cannot close session safely", str(exc))
            return
        if not stopped:
            QMessageBox.warning(self, "Session is stopping", "The connection is still closing. Try again in a moment.")
            return
        self.session_tabs.removeTab(index)
        self.sessions.remove(pane)
        pane.deleteLater()

    def confirm_key(self, host, fingerprint, public_key):
        answer = QMessageBox.question(self, "Verify SSH host key", f"First connection to {host.address}:{host.port}\n\n"
            f"Host key fingerprint:\n{fingerprint}\n\nVerify this fingerprint with the device administrator before trusting it.\nTrust this key in this workspace?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return False
        try:
            trust_host_key(host, self.known_hosts_path, public_key)
            self.workspace.settings["known_hosts"] = self.known_hosts_path.read_text(encoding="utf-8")
            self.save_workspace()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "Cannot save host key", str(exc))
            return False

    def make_recordings_page(self):
        layout = self.page("SESSION ARCHIVE", "Nothing lost on disconnect.", "Encrypted output from every terminal session, ready to revisit.")
        self.records = table(["HOST", "PROTOCOL", "STARTED", "STATUS"])
        self.records.itemSelectionChanged.connect(self.read_record)
        layout.addWidget(self.records, 2)
        self.record_view = QPlainTextEdit()
        self.record_view.setReadOnly(True)
        self.record_view.setFont(QFont("Menlo" if sys.platform == "darwin" else "Consolas", 12))
        self.record_view.setPlaceholderText("Select a session to read its recorded output.")
        layout.addWidget(self.record_view, 3)
        row = QHBoxLayout()
        self.record_prev = button("Previous output", lambda: self.read_record_page(-1))
        self.record_next = button("Next output", lambda: self.read_record_page(1))
        self.record_position = label("", "muted")
        row.addWidget(self.record_prev)
        row.addWidget(self.record_next)
        row.addWidget(self.record_position)
        row.addStretch()
        row.addWidget(button("Export raw transcript", self.export_record))
        row.addWidget(button("Refresh", self.refresh_records))
        layout.addLayout(row)
        self.record_offset = 0
        self._record_session_id = None
        self._record_renderer = None
        self._record_next_offset = 0

    def refresh_records(self):
        if not hasattr(self, "records"):
            return
        selected = selected_data(self.records)
        self.records.setRowCount(0)
        try:
            for record in self.store.list_sessions():
                started = datetime.fromisoformat(record["started"]).astimezone().strftime("%d %b %Y  %H:%M:%S")
                put_row(self.records, [record["name"], record["protocol"].upper(), started, record["status"]], record["id"])
                if record["id"] == selected:
                    self.records.selectRow(self.records.rowCount() - 1)
            if selected is None and self.records.rowCount():
                self.records.selectRow(0)
        except Exception as exc:
            self.statusBar().showMessage("Could not read recordings: " + str(exc))

    def read_record(self):
        self.record_offset = 0
        self.read_record_page(0)

    def read_record_page(self, direction):
        session_id = selected_data(self.records)
        if not session_id:
            self.record_view.clear()
            self._record_session_id = None
            self._record_renderer = None
            self._record_next_offset = 0
            return
        try:
            count = self.store.output_count(session_id)
            self.record_offset = max(0, min(max(0, ((count - 1) // 100) * 100), self.record_offset + direction * 100))
            if (self._record_renderer is None or self._record_session_id != session_id
                    or self._record_next_offset > self.record_offset):
                self._record_renderer = TranscriptRenderer()
                self._record_next_offset = 0
                self._record_session_id = session_id
            renderer = self._record_renderer
            # Reconstruct control/unfinished-line state when navigating backwards.
            # Discard completed prefix lines instead of retaining the whole archive.
            for offset in range(self._record_next_offset, self.record_offset, 100):
                for chunk in self.store.read_output(session_id, offset, min(100, self.record_offset - offset)):
                    renderer.feed(chunk)
                    renderer.drain()
            chunks = self.store.read_output(session_id, self.record_offset, 100)
            output = []
            for chunk in chunks:
                renderer.feed(chunk)
                output.append(renderer.drain())
            output.append(renderer.current_line)
            self._record_next_offset = self.record_offset + len(chunks)
            self.record_view.setPlainText("".join(output))
            self.record_position.setText(f"Chunks {self.record_offset + 1 if count else 0}–{min(count, self.record_offset + 100)} of {count}")
            self.record_prev.setEnabled(self.record_offset > 0)
            self.record_next.setEnabled(self.record_offset + 100 < count)
        except Exception as exc:
            self._record_renderer = None
            QMessageBox.warning(self, "Recording", str(exc))

    def export_record(self):
        session_id = selected_data(self.records)
        if not session_id:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export unencrypted transcript", "session.log", "Terminal log (*.log)")
        if not path:
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                for offset in range(0, self.store.output_count(session_id), 100):
                    stream.writelines(self.store.read_output(session_id, offset, 100))
            self.statusBar().showMessage("Transcript exported as an unencrypted file.")
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def make_transfer_page(self):
        layout = self.page("FILE TRANSFER", "From here to there.", "Browse local and remote files, then upload or download over SFTP or FTP.")
        bar = QHBoxLayout()
        self.transfer_hosts = QComboBox()
        self.transfer_hosts.setMinimumWidth(260)
        self.remote_location = QLineEdit(".")
        self.remote_location.returnPressed.connect(self.list_remote)
        bar.addWidget(self.transfer_hosts)
        bar.addWidget(self.remote_location, 1)
        bar.addWidget(button("Connect / refresh", self.list_remote, True))
        layout.addLayout(bar)
        panes = QHBoxLayout()
        local = QVBoxLayout()
        local.addWidget(label("LOCAL DEVICE", "eyebrow"))
        local_bar = QHBoxLayout()
        self.local_path = QLineEdit(str(Path.home()))
        self.local_path.returnPressed.connect(self.list_local)
        local_bar.addWidget(self.local_path)
        local_bar.addWidget(button("Browse", self.choose_local))
        local.addLayout(local_bar)
        self.local_files = table(["NAME", "SIZE"])
        self.local_files.itemDoubleClicked.connect(self.open_local_folder)
        local.addWidget(self.local_files)
        remote = QVBoxLayout()
        remote.addWidget(label("REMOTE HOST", "eyebrow"))
        remote_bar = QHBoxLayout()
        remote_bar.addWidget(button("Parent folder", self.remote_parent))
        remote_bar.addWidget(button("New folder", self.remote_mkdir))
        remote.addLayout(remote_bar)
        self.remote_files = table(["NAME", "SIZE"])
        self.remote_files.itemDoubleClicked.connect(self.open_remote_folder)
        remote.addWidget(self.remote_files)
        panes.addLayout(local, 1)
        panes.addLayout(remote, 1)
        layout.addLayout(panes, 1)
        actions = QHBoxLayout()
        actions.addWidget(button("Upload selected →", self.upload_file, True))
        actions.addWidget(button("← Download selected", self.download_file))
        self.cancel_transfer_button = button("Cancel", self.cancel_transfer)
        self.cancel_transfer_button.setEnabled(False)
        actions.addWidget(self.cancel_transfer_button)
        self.transfer_status = label("Choose a saved host to browse its files.", "muted")
        actions.addWidget(self.transfer_status, 1)
        layout.addLayout(actions)
        self.transfer_progress = QProgressBar()
        self.transfer_progress.hide()
        layout.addWidget(self.transfer_progress)
        self.transfer_hosts.currentIndexChanged.connect(self.reset_remote)
        self.list_local()

    def reset_remote(self, *_):
        self.remote_files.setRowCount(0)
        self.remote_location.setText(".")
        self.remote_host_id = None

    def choose_local(self):
        path = QFileDialog.getExistingDirectory(self, "Local folder", self.local_path.text())
        if path:
            self.local_path.setText(path)
            self.list_local()

    def list_local(self):
        try:
            folder = Path(self.local_path.text()).expanduser().resolve()
            self.local_files.setRowCount(0)
            put_row(self.local_files, ["..", "Folder"], str(folder.parent))
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
                if entry.name.startswith("."):
                    continue
                put_row(self.local_files, [entry.name, "Folder" if entry.is_dir() else f"{entry.stat().st_size:,} B"], str(entry))
        except OSError as exc:
            self.transfer_status.setText(str(exc)) if hasattr(self, "transfer_status") else None

    def open_local_folder(self, _):
        path = selected_data(self.local_files)
        if path and Path(path).is_dir():
            self.local_path.setText(path)
            self.list_local()

    def transfer_host(self):
        host_id = self.transfer_hosts.currentData()
        return next((h for h in self.workspace.hosts if h.id == host_id), None)

    def run_transfer(self, operation, completed, description):
        host = self.transfer_host()
        if not host or self.demo:
            self.transfer_status.setText("Select a real saved host first.")
            return
        if any(job.isRunning() for job in self.jobs):
            self.transfer_status.setText("A transfer operation is already running.")
            return
        def execute(progress):
            client = TransferClient(host, self.known_hosts_path)
            self.active_transfer_client = client
            with client:
                return operation(client, progress)
        job = Job(execute, self)
        self.jobs.append(job)
        self.transfer_status.setText(description)
        self.transfer_cancelled = False
        self.cancel_transfer_button.setEnabled(True)
        self.transfer_progress.setRange(0, 0)
        self.transfer_progress.show()
        job.progress.connect(self.update_transfer_progress)
        job.completed.connect(completed)
        job.failed.connect(lambda exc: self.transfer_error(host, exc))
        job.finished.connect(lambda: self.transfer_progress.hide())
        job.finished.connect(self.transfer_finished)
        job.finished.connect(lambda: self.jobs.remove(job))
        job.finished.connect(job.deleteLater)
        job.start()

    def cancel_transfer(self):
        self.transfer_cancelled = True
        self.cancel_transfer_button.setEnabled(False)
        self.transfer_status.setText("Cancelling file operation…")
        if self.active_transfer_client:
            self.active_transfer_client.cancel()

    def transfer_finished(self):
        self.active_transfer_client = None
        self.cancel_transfer_button.setEnabled(False)

    def update_transfer_progress(self, transferred, total):
        if total > 0:
            self.transfer_progress.setRange(0, 100)
            self.transfer_progress.setValue(int(transferred * 100 / total))

    def transfer_error(self, host, exc):
        if self.transfer_cancelled:
            self.transfer_status.setText("File operation cancelled.")
            return
        self.transfer_status.setText(str(exc))
        if isinstance(exc, UnknownHostKey):
            if self.confirm_key(host, exc.fingerprint, exc.public_key):
                self.transfer_status.setText("Host key saved. Connect again to continue.")
        else:
            QMessageBox.warning(self, "File transfer", str(exc))

    def list_remote(self):
        path = self.remote_location.text().strip() or "."
        host = self.transfer_host()
        host_id = host.id if host else None
        def completed(entries):
            self.remote_path, self.remote_host_id = path, host_id
            self.remote_files.setRowCount(0)
            for entry in sorted(entries, key=lambda x: (not x["is_dir"], x["name"].casefold())):
                put_row(self.remote_files, [entry["name"], "Folder" if entry["is_dir"] else f"{entry.get('size', 0):,} B"], entry)
            self.transfer_status.setText(f"{len(entries)} items • {path}")
        self.run_transfer(lambda client, _: client.listdir(path), completed, "Reading remote folder…")

    def open_remote_folder(self, _):
        entry = selected_data(self.remote_files)
        if entry and entry["is_dir"]:
            self.remote_location.setText(posixpath.join(self.remote_path, entry["name"]))
            self.list_remote()

    def remote_parent(self):
        self.remote_location.setText(posixpath.normpath(posixpath.join(self.remote_location.text(), "..")))
        self.list_remote()

    def remote_mkdir(self):
        name, ok = QInputDialog.getText(self, "New remote folder", "Folder name:")
        if ok and name and "/" not in name and name not in (".", ".."):
            path = posixpath.join(self.remote_location.text(), name)
            self.run_transfer(lambda client, _: client.mkdir(path), lambda _: self.transfer_status.setText("Folder created. Refresh to view it."), "Creating folder…")

    def upload_file(self):
        local = selected_data(self.local_files)
        if not local or not Path(local).is_file():
            self.transfer_status.setText("Select a local file to upload.")
            return
        remote = posixpath.join(self.remote_location.text(), Path(local).name)
        if QMessageBox.question(self, "Upload file", f"Upload {Path(local).name} to {remote}? An existing remote file with this name will be replaced.") != QMessageBox.Yes:
            return
        self.run_transfer(lambda client, progress: client.upload(local, remote, progress),
                          lambda _: self.transfer_status.setText("Upload complete. Refresh to view remote changes."), "Uploading…")

    def download_file(self):
        entry = selected_data(self.remote_files)
        if not entry or entry["is_dir"]:
            self.transfer_status.setText("Select a remote file to download.")
            return
        if self.remote_host_id != self.transfer_hosts.currentData():
            self.transfer_status.setText("Refresh this host before downloading.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Download file", str(Path(self.local_path.text()) / entry["name"]))
        if path:
            remote = posixpath.join(self.remote_path, entry["name"])
            def completed(_):
                self.transfer_status.setText("Download complete.")
                self.list_local()
            self.run_transfer(lambda client, progress: client.download(remote, path, progress), completed, "Downloading…")

    def make_tftp_page(self):
        layout = self.page("NETWORK UTILITIES", "A file host for the job.", "Start a read-only TFTP host for device configuration and firmware downloads.")
        panel = QFrame()
        panel.setObjectName("panel")
        form = QFormLayout(panel)
        form.setContentsMargins(24, 24, 24, 24)
        form.setSpacing(18)
        self.tftp_root = QLineEdit()
        self.tftp_root.setPlaceholderText("Choose a folder to serve")
        root_row = QWidget()
        row = QHBoxLayout(root_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.tftp_root)
        row.addWidget(button("Browse", self.choose_tftp_root))
        self.tftp_address = QLineEdit("127.0.0.1")
        self.tftp_port = QSpinBox()
        self.tftp_port.setRange(1, 65535)
        self.tftp_port.setValue(1069)
        form.addRow("Folder", root_row)
        form.addRow("Bind IPv4 address", self.tftp_address)
        form.addRow("UDP port", self.tftp_port)
        layout.addWidget(panel)
        note = label("Use your network interface address for device access. 127.0.0.1 is local only.\n"
                     "TFTP has no authentication or encryption. Only files in the selected folder are served; uploads are disabled.\n"
                     "Most devices expect UDP 69. Binding that port may need OS configuration; the app does not elevate itself.", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tftp_toggle = button("Start TFTP host", self.toggle_tftp, True)
        layout.addWidget(self.tftp_toggle, alignment=Qt.AlignLeft)
        self.tftp_log = QPlainTextEdit()
        self.tftp_log.setReadOnly(True)
        self.tftp_log.setMaximumBlockCount(1000)
        self.tftp_log.setPlaceholderText("The host is stopped. Activity will appear here.")
        layout.addWidget(self.tftp_log, 1)

    def choose_tftp_root(self):
        path = QFileDialog.getExistingDirectory(self, "Choose TFTP root")
        if path:
            self.tftp_root.setText(path)

    def toggle_tftp(self):
        if self.tftp_server:
            self.tftp_server.stop()
            self.tftp_server = None
            self.tftp_toggle.setText("Start TFTP host")
            for field in [self.tftp_root, self.tftp_address, self.tftp_port]:
                field.setEnabled(True)
            return
        try:
            if not self.tftp_root.text().strip():
                raise ValueError("Choose a folder to serve first.")
            self.tftp_server = TFTPServer(Path(self.tftp_root.text()), host=self.tftp_address.text().strip(),
                                           port=self.tftp_port.value(), on_event=self.tftp_event.emit)
            self.tftp_server.start()
            self.tftp_toggle.setText("Stop TFTP host")
            for field in [self.tftp_root, self.tftp_address, self.tftp_port]:
                field.setEnabled(False)
        except Exception as exc:
            self.tftp_server = None
            QMessageBox.warning(self, "Cannot start TFTP host", str(exc))

    def export_workspace(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export encrypted workspace and recordings", self.workspace.path.stem + ".pintbundle", "PiNT portable workspace (*.pintbundle)")
        if not path:
            return
        try:
            self.save_workspace()
            self.workspace.export_bundle(Path(path))
            self.statusBar().showMessage("Portable workspace exported. Use the same passphrase to import it on another device.")
        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def closeEvent(self, event: QCloseEvent):
        if any(job.isRunning() for job in self.jobs):
            QMessageBox.information(self, "Transfer in progress", "Wait for the current file operation to finish before closing the workspace.")
            event.ignore()
            return
        active = [pane for pane in self.sessions if pane.worker and pane.worker.isRunning()]
        if active and QMessageBox.question(self, "Close workspace", f"Disconnect {len(active)} active session(s) and lock the workspace?") != QMessageBox.Yes:
            event.ignore()
            return
        try:
            for pane in self.sessions:
                if not pane.stop_and_wait():
                    event.ignore()
                    self.statusBar().showMessage("Connections are stopping. Close again in a moment.")
                    return
            if self.tftp_server:
                self.tftp_server.stop()
            self.workspace.settings["window_size"] = [self.width(), self.height()]
            self.workspace.settings["last_page"] = self.pages.currentIndex()
            self.workspace.save()
            self.store.close()
            self.runtime_dir.cleanup()
        except Exception as exc:
            QMessageBox.warning(self, "Could not close safely", str(exc))
            event.ignore()
            return
        event.accept()


def demo_workspace(folder):
    workspace = Workspace.create(Path(folder) / "PiNT demo.pint", "local-demo-passphrase")
    workspace.hosts = [
        Host(name="Core switch", address="192.0.2.10", username="admin", group="Home lab", tags="switch, core"),
        Host(name="Edge router", address="192.0.2.1", username="admin", group="Home lab", tags="router"),
        Host(name="Storage server", address="192.0.2.20", protocol="sftp", username="reece", group="Home lab", tags="files"),
        Host(name="Access switch 01", address="198.51.100.11", username="admin", group="Project Atlas", tags="access"),
        Host(name="Access switch 02", address="198.51.100.12", username="admin", group="Project Atlas", tags="access"),
        Host(name="AV controller", address="198.51.100.30", protocol="telnet", group="Project Atlas", tags="av, control"),
        Host(name="Firmware archive", address="203.0.113.20", protocol="ftp", username="operator", group="Workshop", tags="firmware"),
        Host(name="Bench switch", address="203.0.113.10", protocol="telnet", group="Workshop", tags="bench"),
    ]
    workspace.save()
    return workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description="PiNT Terminal")
    parser.add_argument("--demo", action="store_true", help="Open a disposable workspace with example hosts; no connections are made")
    parser.add_argument("--screenshot", type=Path, help="Save a demo window screenshot and exit")
    parser.add_argument("--page", type=int, default=0, choices=range(5), help="Demo page to display")
    args = parser.parse_args(argv)
    if args.screenshot and not args.demo:
        parser.error("--screenshot requires --demo")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("PiNT Terminal")
    app.setOrganizationName("PiNT")
    apply_theme(app)
    demo_dir = None
    if args.demo:
        demo_dir = tempfile.TemporaryDirectory(prefix="pint-demo-")
        workspace = demo_workspace(demo_dir.name)
    else:
        welcome = Welcome()
        if welcome.exec() != QDialog.Accepted:
            return 0
        workspace = welcome.workspace
    try:
        window = MainWindow(workspace, demo=args.demo)
    except Exception as exc:
        QMessageBox.critical(None, "Cannot open PiNT Terminal", str(exc))
        if demo_dir:
            demo_dir.cleanup()
        return 1
    if args.demo:
        pane = SessionPane(window, workspace.hosts[0], demo=True)
        window.sessions.append(pane)
        window.session_tabs.addTab(pane, "Core switch • Demo")
        window.show_page(args.page)
    else:
        size = workspace.settings.get("window_size", [1440, 900])
        if isinstance(size, list) and len(size) == 2 and all(isinstance(n, int) for n in size):
            window.resize(max(1100, min(size[0], 3840)), max(720, min(size[1], 2160)))
        window.show_page(min(4, max(0, int(workspace.settings.get("last_page", 0)))))
    window.show()
    if args.screenshot:
        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))
            window.close()
        QTimer.singleShot(1000, capture)
    result = app.exec()
    if demo_dir:
        demo_dir.cleanup()
    return result
