import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import stat
import warnings
import zipfile

import pytest

from pint_terminal.models import Host
from pint_terminal.recordings import RecordingError, RecordStore
from pint_terminal.vault import VaultError, Workspace


@pytest.fixture
def host():
    return Host(name="Bridge switch", address="192.0.2.47", username="engineer", password="private-device-password", group="Private vessel")


@pytest.fixture
def workspace(tmp_path, host):
    workspace = Workspace.create(tmp_path / "devices.pint", "a long test workspace passphrase")
    workspace.hosts.append(host)
    workspace.settings = {"theme": "dark", "tabs": [host.id]}
    workspace.save()
    return workspace


def test_host_roundtrip_and_defaults(host):
    assert Host.from_dict(host.to_dict()) == host
    assert Host(protocol="telnet").port == 23
    assert Host(protocol="ftp").port == 21
    with pytest.raises(ValueError):
        Host(name="invalid", address="host", protocol="http").validate()
    with pytest.raises(ValueError):
        Host(name="invalid", address="host", port=True).validate()
    with pytest.raises(ValueError):
        Host.from_dict({"name": "host", "address": "host", "surprise": True})


def test_workspace_encrypts_every_host_field_and_uses_fresh_nonces(workspace, host):
    previous = workspace.path.read_bytes()
    for value in (host.name, host.address, host.username, host.password, host.group, "theme"):
        assert value.encode() not in previous
    workspace.save()
    assert workspace.path.read_bytes() != previous
    reopened = Workspace.open(workspace.path, "a long test workspace passphrase")
    assert reopened.hosts == workspace.hosts
    assert reopened.settings == workspace.settings
    assert reopened.key == workspace.key
    if os.name == "posix":
        assert stat.S_IMODE(workspace.path.stat().st_mode) == 0o600


def test_wrong_password_and_workspace_tamper_fail_closed(workspace):
    with pytest.raises(VaultError, match="passphrase"):
        Workspace.open(workspace.path, "incorrect password")
    envelope = json.loads(workspace.path.read_bytes())
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode()
    workspace.path.write_text(json.dumps(envelope))
    with pytest.raises(VaultError, match="damaged"):
        Workspace.open(workspace.path, "a long test workspace passphrase")


def test_hostile_kdf_is_rejected_before_derivation(workspace, monkeypatch):
    import pint_terminal.vault as vault
    envelope = json.loads(workspace.path.read_bytes())
    envelope["kdf"]["n"] = 2**50
    workspace.path.write_text(json.dumps(envelope))
    monkeypatch.setattr(vault, "_derive", lambda *_: pytest.fail("Untrusted KDF parameters reached derivation"))
    with pytest.raises(VaultError, match="key derivation"):
        Workspace.open(workspace.path, "anything")


def test_recordings_persist_encrypt_and_page(tmp_path, host):
    path, key = tmp_path / "recordings.sqlite", os.urandom(32)
    with RecordStore(path, key) as store:
        session_id = store.start_session(host)
        for index in range(230):
            store.append_output(session_id, f"secret remote output {index}\r\n")
            store.append_line(session_id, [{"data": str(index), "fg": "00ff00", "bold": False}])
        store.event(session_id, "private reconnect event")
        store.finish_session(session_id, "disconnected")
        assert store.output_count(session_id) == store.line_count(session_id) == 230
        assert store.read_output(session_id, 228, 10) == ["secret remote output 228\r\n", "secret remote output 229\r\n"]
        assert len(store.read_output(session_id)) == 200
        assert store.read_lines(session_id, 220, 2)[0][0]["data"] == "220"
        assert store.read_output(session_id, 1000, 1) == []
        assert store.read_events(session_id)[0]["text"] == "private reconnect event"
        store.verify()
    with RecordStore(path, key) as reopened:
        metadata = reopened.list_sessions()[0]
        assert metadata["name"] == host.name
        assert metadata["status"] == "disconnected"
        assert metadata["ended"] is not None
        assert reopened.output_count(session_id) == 230
    raw = path.read_bytes()
    for value in (host.name, host.address, "secret remote output", "private reconnect event", "disconnected"):
        assert value.encode() not in raw


def test_recordings_wrong_key_and_modified_ciphertext_fail(tmp_path, host):
    path, key = tmp_path / "recordings.sqlite", os.urandom(32)
    with RecordStore(path, key) as store:
        session_id = store.start_session(host)
        store.append_output(session_id, "sensitive result")
    with pytest.raises(RecordingError):
        RecordStore(path, os.urandom(32))
    with sqlite3.connect(path) as connection:
        payload = bytearray(connection.execute("SELECT payload FROM outputs").fetchone()[0])
        payload[-1] ^= 1
        connection.execute("UPDATE outputs SET payload=?", (bytes(payload),))
    with RecordStore(path, key) as store:
        with pytest.raises(RecordingError, match="authenticated"):
            store.read_output(session_id)


def test_ciphertexts_cannot_be_swapped_between_positions(tmp_path, host):
    path, key = tmp_path / "recordings.sqlite", os.urandom(32)
    with RecordStore(path, key) as store:
        session_id = store.start_session(host)
        store.append_output(session_id, "first")
        store.append_output(session_id, "second")
    with sqlite3.connect(path) as connection:
        payloads = connection.execute("SELECT payload FROM outputs ORDER BY position").fetchall()
        connection.execute("UPDATE outputs SET payload=? WHERE position=0", payloads[1])
    with RecordStore(path, key) as store:
        with pytest.raises(RecordingError):
            store.read_output(session_id)


def test_concurrent_recording_and_closed_store(tmp_path, host):
    store = RecordStore(tmp_path / "recordings.sqlite", os.urandom(32))
    session_id = store.start_session(host)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: store.append_output(session_id, str(value)), range(80)))
    assert set(store.read_output(session_id)) == {str(value) for value in range(80)}
    assert store.output_count(session_id) == 80
    with pytest.raises(ValueError):
        store.read_output(session_id, -1)
    with pytest.raises(RecordingError, match="does not exist"):
        store.append_output("missing", "output")
    store.close()
    store.close()
    with pytest.raises(RecordingError, match="closed"):
        store.list_sessions()


def test_recording_batch_commits_together_and_rolls_back_on_failure(tmp_path, host):
    with RecordStore(tmp_path / "batched.sqlite", os.urandom(32)) as store:
        session_id = store.start_session(host)
        with store.transaction():
            store.append_output(session_id, "batch output")
            with store.transaction():
                for index in range(100):
                    store.append_line(session_id, [{"data": str(index)}])
        assert store.line_count(session_id) == 100
        assert store.read_output(session_id) == ["batch output"]
        with pytest.raises(RecordingError):
            with store.transaction():
                store.append_output(session_id, "must be rolled back")
                store.append_line(session_id, [{"data": "must be rolled back"}])
                store.append_output("missing session", "fail")
        assert store.line_count(session_id) == 100
        assert store.read_output(session_id) == ["batch output"]


def test_resumed_recording_clears_ended_timestamp(tmp_path, host):
    with RecordStore(tmp_path / "resumed.sqlite", os.urandom(32)) as store:
        session_id = store.start_session(host)
        store.finish_session(session_id, "Disconnected")
        assert store.list_sessions()[0]["ended"] is not None
        store.set_session_status(session_id, "Connected")
        metadata = store.list_sessions()[0]
        assert metadata["status"] == "Connected"
        assert metadata["ended"] is None


def test_portable_bundle_includes_live_wal_and_reopens(workspace, host, tmp_path):
    with RecordStore(workspace.recordings_path, workspace.key) as store:
        session_id = store.start_session(host)
        store.append_output(session_id, "output still in the WAL")
        store.append_line(session_id, [{"data": "historic line"}])
        bundle = workspace.export_bundle(tmp_path / "backup.pintbundle")
        restored = Workspace.import_bundle(bundle, tmp_path / "restored" / "devices.pint", "a long test workspace passphrase")
    assert restored.hosts == workspace.hosts
    with RecordStore(restored.recordings_path, restored.key) as restored_store:
        assert restored_store.read_output(session_id) == ["output still in the WAL"]
        assert restored_store.read_lines(session_id, 0, 1) == [[{"data": "historic line"}]]


def test_import_wrong_password_leaves_no_destination(workspace, tmp_path):
    bundle = workspace.export_bundle(tmp_path / "backup.zip")
    destination = tmp_path / "restored.pint"
    with pytest.raises(VaultError):
        Workspace.import_bundle(bundle, destination, "wrong")
    assert not destination.exists()
    assert not destination.with_suffix(".sessions.sqlite").exists()


@pytest.mark.parametrize("extra", ["../escaped.txt", "/absolute.txt", "nested/workspace.pint", "sessions.sqlite/", "workspace.pint"])
def test_import_rejects_unsafe_or_duplicate_entries(tmp_path, extra):
    bundle = tmp_path / "hostile.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("workspace.pint", b"not a workspace")
            archive.writestr(extra, b"untrusted")
    with pytest.raises(VaultError):
        Workspace.import_bundle(bundle, tmp_path / "imported.pint", "password")
    assert not (tmp_path / "imported.pint").exists()
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_import_rejects_symlink_and_size_limit(tmp_path, monkeypatch):
    import pint_terminal.vault as vault
    bundle = tmp_path / "hostile.zip"
    member = zipfile.ZipInfo("workspace.pint")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr(member, "outside")
    with pytest.raises(VaultError, match="unsafe"):
        Workspace.import_bundle(bundle, tmp_path / "imported.pint", "password")
    monkeypatch.setattr(vault, "MAX_WORKSPACE_BYTES", 16)
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("workspace.pint", b"x" * 17)
    with pytest.raises(VaultError, match="unsafe"):
        Workspace.import_bundle(bundle, tmp_path / "imported.pint", "password")


def test_relative_workspace_path_can_be_exported(tmp_path, host, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = Workspace.create(Path("relative.pint"), "portable passphrase")
    with RecordStore(workspace.recordings_path, workspace.key) as store:
        store.start_session(host)
        workspace.export_bundle(Path("portable.zip"))
    assert (tmp_path / "portable.zip").exists()


def test_deeply_nested_malformed_workspace_is_reported(tmp_path):
    path = tmp_path / "nested.pint"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(VaultError):
        Workspace.open(path, "password")


def test_import_rejects_recordings_from_another_workspace(workspace, tmp_path, host):
    other = tmp_path / "foreign.sqlite"
    with RecordStore(other, os.urandom(32)) as store:
        store.start_session(host)
    bundle = tmp_path / "mismatch.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(workspace.path, "workspace.pint")
        archive.write(other, "sessions.sqlite")
    destination = tmp_path / "restored.pint"
    with pytest.raises(VaultError, match="do not belong"):
        Workspace.import_bundle(bundle, destination, "a long test workspace passphrase")
    assert not destination.exists()
    assert not destination.with_suffix(".sessions.sqlite").exists()


def test_failed_atomic_save_preserves_previous_workspace(workspace, monkeypatch):
    import pint_terminal.vault as vault
    previous = workspace.path.read_bytes()
    def fail_replace(*_):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(vault.os, "replace", fail_replace)
    with pytest.raises(VaultError, match="could not be saved"):
        workspace.save()
    assert workspace.path.read_bytes() == previous
    assert not list(workspace.path.parent.glob(f".{workspace.path.name}.*"))
