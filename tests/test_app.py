"""Integration checks for Qt forms, the encrypted workspace and live transport."""

import asyncio
import os
from pathlib import Path
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import telnetlib3
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from pint_terminal.app import HostDialog, MainWindow, SessionPane
from pint_terminal.models import Host
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


def test_main_window_filters_selects_and_saves(window, ui_workspace):
    assert window.pages.count() == 5
    assert window.host_table.rowCount() == 3
    assert window.transfer_hosts.count() == 2
    window.search.setText("UPLINK")
    assert window.host_table.rowCount() == 1
    assert window.selected_host().name == "Core switch"
    assert window.detail_title.text() == "Core switch"
    window.search.clear()
    window.groups.setCurrentText("Office")
    assert window.host_table.rowCount() == 1
    assert window.selected_host().name == "Archive"
    window.search.setText("missing")
    assert window.host_table.rowCount() == 0
    assert not window.connect_button.isEnabled()
    ui_workspace.hosts[0].name = "Updated core switch"
    window.save_workspace()
    restored = Workspace.open(ui_workspace.path, "integration passphrase")
    assert restored.hosts[0].name == "Updated core switch"


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
