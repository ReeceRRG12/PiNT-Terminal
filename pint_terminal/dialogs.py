"""Device and group forms for the encrypted connection vault."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from .models import DEFAULT_PORTS, Group, Host


def _label(text, kind=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(True)
    if kind:
        widget.setObjectName(kind)
    return widget


def _key_row(owner, path):
    owner.key = QLineEdit(path)
    owner.key.setPlaceholderText("Optional private key file")
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(owner.key, 1)
    owner.key_browse = QPushButton("Browse")
    owner.key_browse.clicked.connect(owner.choose_key)
    layout.addWidget(owner.key_browse)
    return row


class HostDialog(QDialog):
    def __init__(self, parent=None, host=None, groups=None, selected_group=None):
        super().__init__(parent)
        self.host = host
        self.available_groups = list(groups or [])
        self.result_host = None
        self.setWindowTitle("Edit device" if host else "New device")
        self.setMinimumWidth(520)
        self.resize(580, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(_label(self.windowTitle(), "section"))

        # Keep Save and Cancel visible on smaller screens and at larger scaling.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        fields = QVBoxLayout(content)
        fields.setContentsMargins(0, 0, 8, 0)
        fields.setSpacing(14)
        form = QFormLayout()
        form.setSpacing(10)
        self.name = QLineEdit(host.name if host else "")
        self.name.setPlaceholderText("Device name")
        self.address = QLineEdit(host.address if host else "")
        self.address.setPlaceholderText("Hostname or IP address")
        self.protocol = QComboBox()
        self.protocol.addItems(["SSH", "Telnet", "SFTP", "FTP"])
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.protocol.currentTextChanged.connect(
            lambda protocol: self.port.setValue(DEFAULT_PORTS[protocol.lower()]))
        if host:
            self.protocol.setCurrentText("Telnet" if host.protocol == "telnet" else host.protocol.upper())
        self.port.setValue(host.port if host else 22)

        self.group = QComboBox()
        names = sorted({group.name for group in self.available_groups}, key=str.casefold)
        current_group = host.group if host else selected_group
        if current_group and current_group not in names:
            names.append(current_group)
        self.group.addItems(names or ["Ungrouped"])
        if current_group:
            self.group.setCurrentText(current_group)

        self.credential_source = QComboBox()
        self.credential_source.addItems(["Use group credentials", "Custom credentials"])
        inherit = host.inherit_credentials if host else bool(self.available_groups)
        self.credential_source.setCurrentIndex(0 if inherit else 1)
        self.username = QLineEdit(host.username if host else "")
        self.password = QLineEdit(host.password if host else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Saved in the encrypted workspace")
        key_row = _key_row(self, host.key_path if host else "")
        self.tags = QLineEdit(host.tags if host else "")
        self.tags.setPlaceholderText("Optional, for example: switch, core")
        for title, field in [
            ("Group", self.group), ("Name", self.name), ("Address", self.address),
            ("Protocol", self.protocol), ("Port", self.port),
            ("Credentials", self.credential_source),
        ]:
            form.addRow(title, field)
        self.credential_note = _label("", "muted")
        form.addRow(self.credential_note)
        for title, field in [
            ("Username", self.username), ("Password", self.password),
            ("SSH key file", key_row), ("Tags", self.tags),
        ]:
            form.addRow(title, field)
        self.credential_form = form
        self.custom_credential_rows = (self.username, self.password, key_row)
        fields.addLayout(form)
        self.reconnect = QCheckBox("Reconnect after an interrupted connection")
        self.reconnect.setChecked(host.auto_reconnect if host else True)
        fields.addWidget(self.reconnect)
        fields.addWidget(_label(
            "Telnet login is interactive; saved credentials are not sent automatically. "
            "Telnet and FTP use unencrypted network connections.\n"
            "SSH key files stay on this computer and are not included in workspace exports.", "muted"))
        fields.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.credential_source.currentIndexChanged.connect(self.update_credentials)
        self.group.currentTextChanged.connect(self.update_credentials)
        self.update_credentials()

    def update_credentials(self, *_):
        inherited = self.credential_source.currentIndex() == 0
        for field in (self.username, self.password, self.key, self.key_browse):
            field.setEnabled(not inherited)
        for field in self.custom_credential_rows:
            self.credential_form.setRowVisible(field, not inherited)
        if inherited:
            group_name = self.group.currentText()
            group = next((item for item in self.available_groups if item.name == group_name), None)
            username = group.username if group and group.username else "No default username"
            self.credential_note.setText(
                f"Using the full credential set from {group_name}.\n"
                f"Username: {username}. New sessions and file operations use these defaults. "
                "Existing sessions keep their credentials.")
        else:
            self.credential_note.setText(
                "These credentials apply only to this device. Blank fields stay blank; "
                "group credentials are not added.")

    def choose_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH private key")
        if path:
            self.key.setText(path)

    def save(self):
        try:
            values = dict(
                name=self.name.text().strip() or self.address.text().strip(),
                address=self.address.text().strip(), protocol=self.protocol.currentText().lower(),
                port=self.port.value(), username=self.username.text().strip(),
                password=self.password.text(), key_path=self.key.text().strip(),
                group=self.group.currentText(), tags=self.tags.text().strip(),
                auto_reconnect=self.reconnect.isChecked(),
                inherit_credentials=self.credential_source.currentIndex() == 0,
            )
            if self.host:
                values["id"] = self.host.id
            self.result_host = Host(**values).validate()
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Device details", str(exc))


class GroupDialog(QDialog):
    def __init__(self, parent=None, group=None, groups=None, device_count=0):
        super().__init__(parent)
        self.original_group = group
        self.available_groups = list(groups or [])
        self.result_group = None
        self.setWindowTitle("Edit group" if group else "New group")
        self.setMinimumWidth(520)
        self.resize(580, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(_label(self.windowTitle(), "section"))
        layout.addWidget(_label(
            "Set the standard credentials for devices in this group. "
            "Each device can use these defaults or its own custom credentials.", "muted"))
        form = QFormLayout()
        form.setSpacing(10)
        self.name = QLineEdit(group.name if group else "")
        self.name.setPlaceholderText("For example: Home lab")
        self.username = QLineEdit(group.username if group else "")
        self.password = QLineEdit(group.password if group else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("Saved in the encrypted workspace")
        key_row = _key_row(self, group.key_path if group else "")
        for title, field in [
            ("Group name", self.name), ("Username", self.username),
            ("Password", self.password), ("SSH key file", key_row),
        ]:
            form.addRow(title, field)
        for field in (self.name, self.username, self.password, self.key):
            field.setMinimumHeight(42)
        layout.addLayout(form)
        note = (f"This group contains {device_count} device(s). " if group else "")
        layout.addWidget(_label(
            note + "New sessions and file operations use these defaults. "
            "Existing sessions keep their credentials. "
            "Devices using custom credentials keep their own settings.\n"
            "Telnet login remains interactive. SSH key files are not included in workspace exports.", "muted"))
        layout.addStretch()
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
            name = self.name.text().strip()
            if not self.original_group or name != self.original_group.name:
                for group in self.available_groups:
                    if self.original_group and group.name == self.original_group.name:
                        continue
                    if group.name.casefold() == name.casefold():
                        raise ValueError("A group with this name already exists. Choose another name.")
            self.result_group = Group(
                name=name, username=self.username.text().strip(),
                password=self.password.text(), key_path=self.key.text().strip(),
            ).validate()
            self.accept()
        except ValueError as exc:
            QMessageBox.warning(self, "Group details", str(exc))
