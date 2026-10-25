"""Integration checks for Qt forms, the encrypted workspace and live transport."""

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import telnetlib3
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from pint_terminal.app import GroupDialog, HostDialog, MainWindow, SessionPane
from pint_terminal.transport import SessionWorker
from pint_terminal.models import Group, Host
from pint_terminal.recordings import RecordStore
from pint_terminal.vault import Workspace


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def notices(monkeypatch):
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text, *_: messages.append((title, text)))
    monkeypatch.setattr(QMessageBox, "information", lambda *_: QMessageBox.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.Yes)
    return messages


@pytest.fixture
def ui_workspace(tmp_path):
    workspace = Workspace.create(tmp_path / "integration.pint", "integration passphrase")
    workspace.hosts = [
        Host(name="Core switch", address="192.0.2.1", group="Lab", tags="uplink, network", password="private-password"),
        Host(name="Archive", address="192.0.2.2", protocol="sftp", group="Office", tags="storage"),
        Host(name="Legacy console", address="192.0.2.3", protocol="telnet", group="Lab"),
    ]
    workspace.save()
    return workspace


@pytest.fixture
def window(qt_app, notices, ui_workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    main = MainWindow(ui_workspace)
    yield main
    if not main.store._closed:
        main.close()
    qt_app.processEvents()
    main.deleteLater()
    qt_app.processEvents()


def test_host_dialog_validates_and_preserves_id_on_edit(qt_app, notices):
    dialog = HostDialog()
    dialog.address.setText("not a host")
    dialog.save()
    assert dialog.result() != QDialog.Accepted
    assert notices and "without spaces" in notices[-1][1]
    dialog.address.setText(" 192.0.2.88 ")
    dialog.protocol.setCurrentText("Telnet")
    assert dialog.port.value() == 23
    dialog.username.setText(" operator ")
    dialog.password.setText(" keep surrounding spaces ")
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    saved = dialog.result_host
    assert saved.name == saved.address == "192.0.2.88"
    assert saved.username == "operator"
    assert saved.password == " keep surrounding spaces "
    edited = HostDialog(host=saved)
    edited.name.setText("Renamed console")
    edited.port.setValue(2023)
    edited.save()
    assert edited.result_host.id == saved.id
    assert edited.result_host.port == 2023
    assert saved.name == "192.0.2.88"
    dialog.deleteLater()
    edited.deleteLater()


def group_item(window, name):
    return next(window.host_tree.topLevelItem(index)
                for index in range(window.host_tree.topLevelItemCount())
                if window.host_tree.topLevelItem(index).data(0, Qt.UserRole) == name)


def device_item(window, host):
    group = group_item(window, host.group)
    return next(group.child(index) for index in range(group.childCount())
                if group.child(index).data(0, Qt.UserRole) == host.id)


def test_main_window_filters_selects_and_saves(window, ui_workspace):
    assert window.pages.count() == 5
    assert window.host_tree.topLevelItemCount() == 2
    assert group_item(window, "Lab").childCount() == 2
    assert group_item(window, "Office").childCount() == 1
    assert window.transfer_hosts.count() == 2
    window.host_tree.setCurrentItem(group_item(window, "Lab"))
    assert window.selected_group().name == "Lab"
    assert window.selected_host() is None
    assert not window.connect_button.isEnabled()
    window.search.setText("UPLINK")
    assert window.host_tree.topLevelItemCount() == 1
    assert group_item(window, "Lab").childCount() == 1
    window.host_tree.setCurrentItem(device_item(window, ui_workspace.hosts[0]))
    assert window.selected_host().name == "Core switch"
    assert window.detail_title.text() == "Core switch"
    window.search.setText("office")
    assert window.host_tree.topLevelItemCount() == 1
    assert group_item(window, "Office").childCount() == 1
    window.host_tree.setCurrentItem(device_item(window, ui_workspace.hosts[1]))
    assert window.selected_host().name == "Archive"
    window.search.setText("missing")
    assert window.host_tree.topLevelItemCount() == 0
    assert not window.connect_button.isEnabled()
    ui_workspace.hosts[0].name = "Updated core switch"
    window.save_workspace()
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    assert restored.hosts[0].name == "Updated core switch"


def test_empty_groups_remain_visible_and_group_search_includes_children(window, ui_workspace):
    ui_workspace.groups.append(Group(name="Empty lab"))
    window.refresh_hosts()
    assert group_item(window, "Empty lab").childCount() == 0
    window.host_tree.setCurrentItem(group_item(window, "Empty lab"))
    assert window.selected_group().name == "Empty lab"
    assert window.selected_host() is None
    window.search.setText("Lab")
    assert window.host_tree.topLevelItemCount() == 2
    assert group_item(window, "Lab").childCount() == 2
    assert group_item(window, "Empty lab").childCount() == 0
    window.search.setText("192.0.2.2")
    assert window.host_tree.topLevelItemCount() == 1
    assert group_item(window, "Office").childCount() == 1


def test_device_dialog_defaults_to_group_credentials_and_allows_custom_override(qt_app, notices):
    groups = [Group(name="Lab", username="group-user", password="group-secret", key_path="group-key")]
    dialog = HostDialog(groups=groups, selected_group="Lab")
    dialog.name.setText("New switch")
    dialog.address.setText("192.0.2.8")
    assert dialog.group.currentText() == "Lab"
    assert dialog.credential_source.currentText() == "Use group credentials"
    assert not dialog.username.isEnabled()
    assert not dialog.password.isEnabled()
    assert not dialog.key.isEnabled()
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert dialog.result_host.inherit_credentials
    assert dialog.result_host.group == "Lab"
    assert dialog.result_host.password != "group-secret"
    dialog.deleteLater()

    custom = HostDialog(host=Host(name="Custom switch", address="192.0.2.9", group="Lab",
                                  username="own-user", password=" own secret "), groups=groups)
    assert custom.credential_source.currentText() == "Custom credentials"
    assert custom.username.isEnabled()
    assert custom.password.text() == " own secret "
    custom.credential_source.setCurrentText("Use group credentials")
    assert not custom.username.isEnabled()
    custom.credential_source.setCurrentText("Custom credentials")
    assert custom.username.isEnabled()
    assert custom.password.text() == " own secret "
    custom.password.setText("")
    custom.save()
    assert not custom.result_host.inherit_credentials
    assert custom.result_host.username == "own-user"
    assert custom.result_host.password == ""
    custom.deleteLater()


def test_group_dialog_preserves_password_spaces_and_rejects_duplicate_names(qt_app, notices):
    groups = [Group(name="Lab")]
    dialog = GroupDialog(groups=groups)
    dialog.name.setText("Lab")
    dialog.save()
    assert dialog.result() != QDialog.Accepted
    assert notices
    dialog.name.setText(" Office ")
    dialog.username.setText(" operator ")
    dialog.password.setText(" keep secret spaces ")
    dialog.save()
    assert dialog.result() == QDialog.Accepted
    assert dialog.result_group.name == "Office"
    assert dialog.result_group.username == "operator"
    assert dialog.result_group.password == " keep secret spaces "
    dialog.deleteLater()


def test_group_create_rename_and_empty_delete_persist_without_changing_custom_devices(window, ui_workspace, monkeypatch):
    def create_group(dialog):
        dialog.name.setText("Bench")
        dialog.username.setText("bench-user")
        dialog.password.setText("bench-secret")
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(GroupDialog, "exec", create_group)
    window.new_group()
    assert group_item(window, "Bench").childCount() == 0
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    assert next(group for group in restored.groups if group.name == "Bench").password == "bench-secret"

    window.host_tree.setCurrentItem(group_item(window, "Lab"))
    before = [(host.id, host.username, host.password, host.inherit_credentials) for host in ui_workspace.hosts]
    def rename_group(dialog):
        dialog.name.setText("Network lab")
        dialog.username.setText("new-group-user")
        dialog.password.setText("new-group-secret")
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(GroupDialog, "exec", rename_group)
    window.edit_group()
    assert group_item(window, "Network lab").childCount() == 2
    assert ui_workspace.hosts[0].group == ui_workspace.hosts[2].group == "Network lab"
    assert [(host.id, host.username, host.password, host.inherit_credentials) for host in ui_workspace.hosts] == before
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    assert restored.hosts[0].group == "Network lab"

    window.host_tree.setCurrentItem(group_item(window, "Network lab"))
    window.delete_group()
    assert any(group.name == "Network lab" for group in ui_workspace.groups)
    window.host_tree.setCurrentItem(group_item(window, "Bench"))
    window.delete_group()
    assert all(group.name != "Bench" for group in ui_workspace.groups)
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    assert all(group.name != "Bench" for group in restored.groups)


def test_new_device_uses_selected_group_and_moving_it_updates_inherited_credentials(window, ui_workspace, monkeypatch):
    lab = next(group for group in ui_workspace.groups if group.name == "Lab")
    office = next(group for group in ui_workspace.groups if group.name == "Office")
    lab.username, lab.password = "lab-user", "lab-secret"
    office.username, office.password = "office-user", "office-secret"
    window.host_tree.setCurrentItem(group_item(window, "Lab"))
    def add_device(dialog):
        assert dialog.group.currentText() == "Lab"
        assert dialog.credential_source.currentText() == "Use group credentials"
        dialog.name.setText("Added device")
        dialog.address.setText("192.0.2.25")
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(HostDialog, "exec", add_device)
    window.new_host()
    added = window.selected_host()
    assert added.name == "Added device"
    assert added.group == "Lab" and added.inherit_credentials
    assert ui_workspace.resolve_host(added).password == "lab-secret"
    def move_device(dialog):
        dialog.group.setCurrentText("Office")
        dialog.save()
        return dialog.result()
    monkeypatch.setattr(HostDialog, "exec", move_device)
    window.edit_host()
    moved = window.selected_host()
    assert moved.id == added.id
    assert moved.group == "Office" and moved.inherit_credentials
    assert ui_workspace.resolve_host(moved).password == "office-secret"
    assert device_item(window, moved).parent().data(0, Qt.UserRole) == "Office"
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    saved = next(host for host in restored.hosts if host.id == added.id)
    assert saved.group == "Office" and saved.inherit_credentials
    assert restored.resolve_host(saved).password == "office-secret"


def test_failed_group_rename_restores_devices_and_saved_credentials(window, ui_workspace, monkeypatch, notices):
    window.host_tree.setCurrentItem(group_item(window, "Lab"))
    saved_bytes = ui_workspace.path.read_bytes()
    original_hosts = [host.to_dict() for host in ui_workspace.hosts]
    original_groups = [group.to_dict() for group in ui_workspace.groups]
    def rename_group(dialog):
        dialog.name.setText("Unsaved lab")
        dialog.password.setText("unsaved secret")
        dialog.save()
        return dialog.result()
    def fail_save():
        raise OSError("simulated disk failure")
    monkeypatch.setattr(GroupDialog, "exec", rename_group)
    with monkeypatch.context() as scoped:
        scoped.setattr(ui_workspace, "save", fail_save)
        window.edit_group()
    assert [host.to_dict() for host in ui_workspace.hosts] == original_hosts
    assert [group.to_dict() for group in ui_workspace.groups] == original_groups
    assert ui_workspace.path.read_bytes() == saved_bytes
    assert notices and "simulated disk failure" in notices[-1][1]


def test_details_identify_credential_source_without_showing_password(window, ui_workspace):
    group = next(group for group in ui_workspace.groups if group.name == "Lab")
    group.username, group.password = "group-user", "never-display-this-secret"
    host = ui_workspace.hosts[0]
    host.inherit_credentials = True
    window.refresh_hosts()
    window.host_tree.setCurrentItem(device_item(window, host))
    assert "group-user" in window.detail_body.text()
    assert "group" in window.detail_body.text().casefold()
    assert "never-display-this-secret" not in window.detail_body.text()
    host.inherit_credentials = False
    host.username = "custom-user"
    window.update_details()
    assert "custom-user" in window.detail_body.text()
    assert "custom" in window.detail_body.text().casefold()
    assert "private-password" not in window.detail_body.text()


def test_terminal_credentials_are_snapshotted_per_tab_without_mutating_profile(window, ui_workspace, monkeypatch):
    group = next(group for group in ui_workspace.groups if group.name == "Lab")
    group.username, group.password, group.key_path = "group-user", "group-secret", "group-key"
    host = ui_workspace.hosts[0]
    host.inherit_credentials = True
    original = host.to_dict()
    monkeypatch.setattr(SessionWorker, "start", lambda _self: None)
    def open_pane(profile):
        pane = SessionPane(window, profile)
        window.sessions.append(pane)
        window.session_tabs.addTab(pane, profile.name)
        return pane
    pane = open_pane(host)
    first = pane.worker.host
    assert (first.username, first.password, first.key_path) == ("group-user", "group-secret", "group-key")
    assert host.to_dict() == original
    group.password = "rotated-secret"
    pane.reconnect()
    assert pane.worker.host.password == "group-secret"
    assert open_pane(host).worker.host.password == "rotated-secret"
    assert host.password == "private-password"
    custom = replace(host, inherit_credentials=False, username="custom-user", password="")
    ui_workspace.hosts[0] = custom
    assert open_pane(custom).worker.host.password == ""
    ui_workspace.hosts = [saved for saved in ui_workspace.hosts if saved.group != "Lab"]
    ui_workspace.groups.remove(group)
    pane.reconnect()
    assert pane.worker.host.username == "group-user"
    assert pane.worker.host.password == "group-secret"
    assert pane.worker.host.address == host.address


@pytest.mark.asyncio
async def test_file_operations_use_group_defaults_and_device_overrides(window, ui_workspace, qt_app, monkeypatch):
    group = next(group for group in ui_workspace.groups if group.name == "Office")
    group.username, group.password, group.key_path = "file-user", "file-secret", "file-key"
    host = ui_workspace.hosts[1]
    host.inherit_credentials = True
    window.transfer_hosts.setCurrentIndex(window.transfer_hosts.findData(host.id))
    received = []
    completed = []
    class LocalClient:
        def __init__(self, effective_host, _known_hosts_path):
            self.host = effective_host
            received.append(effective_host)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    monkeypatch.setattr("pint_terminal.app.TransferClient", LocalClient)
    window.run_transfer(lambda client, _progress: client.host.username, completed.append, "Checking credentials")
    await pump_until(qt_app, lambda: bool(completed) and not window.jobs)
    assert completed == ["file-user"]
    assert (received[0].password, received[0].key_path) == ("file-secret", "file-key")
    assert host.password == ""
    group.password = "changed-secret"
    assert received[0].password == "file-secret"
    host.inherit_credentials = False
    host.username = "custom-file-user"
    host.password = ""
    window.run_transfer(lambda client, _progress: client.host.username, completed.append, "Checking override")
    await pump_until(qt_app, lambda: len(completed) == 2 and not window.jobs)
    assert completed[-1] == "custom-file-user"
    assert received[-1].password == received[-1].key_path == ""


def test_recordings_page_paginates_persistent_output(window, ui_workspace, qt_app):
    session_id = window.store.start_session(ui_workspace.hosts[0])
    with window.store.transaction():
        for index in range(105):
            window.store.append_output(session_id, f"\x1b[32mrow {index:03d}\x1b[0m\r\n")
        window.store.finish_session(session_id, "Disconnected")
    window.show_page(2)
    window.records.selectRow(0)
    assert "row 000" in window.record_view.toPlainText()
    assert "row 099" in window.record_view.toPlainText()
    assert "row 100" not in window.record_view.toPlainText()
    assert "\x1b" not in window.record_view.toPlainText()
    window.read_record_page(1)
    assert "row 100" in window.record_view.toPlainText()
    assert "row 104" in window.record_view.toPlainText()
    assert not window.record_next.isEnabled()
    assert window.record_prev.isEnabled()
    window.close()
    qt_app.processEvents()
    with RecordStore(ui_workspace.recordings_path, ui_workspace.key) as reopened:
        assert reopened.output_count(session_id) == 105
        assert reopened.list_sessions()[0]["status"] == "Disconnected"


def test_recordings_render_edits_and_pager_cleanup_without_changing_raw_data(window, ui_workspace, qt_app, monkeypatch, tmp_path):
    session_id = window.store.start_session(ui_workspace.hosts[0])
    pager = "--More--, next page: Space, quit: Control-c"
    chunks = [
        "switch# show int eth 1/2\b \b1\r\n",
        "Status: bad\rStatus: up\x1b[K\r\n",
        pager, "\b \b" * len(pager),
        "  0 errors\x07\x00\r\n",
        "\x1b]0;device title", "\x07switch# café",
    ]
    for chunk in chunks:
        window.store.append_output(session_id, chunk)
    window.store.finish_session(session_id, "Disconnected")
    # The fix must work for already-saved recordings when the app is reopened.
    window.close()
    qt_app.processEvents()
    reopened = MainWindow(ui_workspace)
    try:
        reopened.show_page(2)
        expected = "switch# show int eth 1/1\nStatus: up\n  0 errors\nswitch# café"
        assert reopened.record_view.toPlainText() == expected
        assert reopened.store.read_output(session_id) == chunks
        exported = tmp_path / "raw-session.log"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_: (str(exported), ""))
        reopened.export_record()
        assert exported.read_bytes().decode("utf-8") == "".join(chunks)
    finally:
        reopened.close()
        reopened.deleteLater()
        qt_app.processEvents()


def test_recordings_keep_control_state_across_pages_and_refresh(window, ui_workspace):
    session_id = window.store.start_session(ui_workspace.hosts[0])
    for index in range(99):
        window.store.append_output(session_id, f"row {index:03d}\r\n")
    window.store.append_output(session_id, "switch# wrong\r\x1b[")
    window.store.append_output(session_id, "2Kswitch# right\r\n\x1b]0;hidden title")
    window.store.append_output(session_id, "\x07done\r\n")
    window.store.finish_session(session_id, "Disconnected")
    window.show_page(2)
    assert window.record_view.toPlainText().endswith("switch# wrong")
    reads = []
    read_output = window.store.read_output
    def tracked_read(sid, offset=0, limit=200):
        reads.append((offset, limit))
        return read_output(sid, offset, limit)
    window.store.read_output = tracked_read
    window.read_record_page(1)
    assert window.record_view.toPlainText() == "switch# right\ndone\n"
    assert reads == [(100, 100)]  # Forward paging continues without rereading history.
    window.read_record_page(-1)
    assert window.record_view.toPlainText().endswith("switch# wrong")
    window.read_record_page(1)
    window.read_record_page(0)
    assert window.record_view.toPlainText() == "switch# right\ndone\n"
    assert all(limit <= 100 for _, limit in reads)


async def pump_until(qt_app, predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Qt transport integration did not reach the expected state.")
        qt_app.processEvents()
        await asyncio.sleep(0.01)
    qt_app.processEvents()


@pytest.mark.asyncio
async def test_session_pane_telnet_records_real_output_and_history(window, ui_workspace, qt_app, notices):
    shell_closed = asyncio.Event()
    async def shell(reader, writer):
        try:
            writer.write("".join(f"loopback row {index:03d}\r\n" for index in range(60)))
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            shell_closed.set()
    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=shell, connect_maxwait=0.1)
    host = Host(name="Local Telnet", address="127.0.0.1", protocol="telnet", port=server.sockets[0].getsockname()[1], auto_reconnect=False)
    pane = SessionPane(window, host)
    window.sessions.append(pane)
    window.session_tabs.addTab(pane, host.name)
    try:
        await pump_until(qt_app, lambda: window.store.output_count(pane.session_id) > 0 and pane.terminal.history_count > 0)
        assert "loopback row 059" in "".join(window.store.read_output(pane.session_id))
        assert "loopback row 000" in "".join(cell["data"] for cell in window.store.read_lines(pane.session_id, 0, 1)[0])
        assert window.store.list_sessions()[0]["status"] == "Connected"
        pane.disconnect()
        await pump_until(qt_app, lambda: not pane.worker.isRunning() and window.store.list_sessions()[0]["ended"] is not None)
        assert not notices
        window.close()
        qt_app.processEvents()
        with RecordStore(ui_workspace.recordings_path, ui_workspace.key) as reopened:
            assert "loopback row 059" in "".join(reopened.read_output(pane.session_id))
    finally:
        if pane.worker.isRunning():
            pane.stop_and_wait()
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(shell_closed.wait(), 2)


@pytest.mark.asyncio
async def test_transport_output_survives_close_before_gui_delivery(window, ui_workspace, qt_app, notices):
    emitted = threading.Event()
    async def shell(_reader, writer):
        writer.write("persist before GUI delivery\r\n")
        await writer.drain()
        writer.close()
    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=shell, connect_maxwait=0.1)
    host = Host(name="Fast Telnet", address="127.0.0.1", protocol="telnet", port=server.sockets[0].getsockname()[1], auto_reconnect=False)
    pane = SessionPane(window, host)
    window.sessions.append(pane)
    window.session_tabs.addTab(pane, host.name)
    pane.worker.output.connect(lambda _data: emitted.set(), Qt.DirectConnection)
    try:
        deadline = time.monotonic() + 6
        # Run the server loop while intentionally withholding GUI event delivery.
        while not emitted.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert emitted.is_set()
        window.close()
        qt_app.processEvents()
        with RecordStore(ui_workspace.recordings_path, ui_workspace.key) as reopened:
            assert "persist before GUI delivery" in "".join(reopened.read_output(pane.session_id))
            assert reopened.list_sessions()[0]["ended"] is not None
        assert not notices
    finally:
        if pane.worker.isRunning():
            pane.stop_and_wait()
        server.close()
        await server.wait_closed()
