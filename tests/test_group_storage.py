"""Group credentials stay encrypted, explicit and compatible with old workspaces."""

import base64
from dataclasses import replace
import json
import os
import stat
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from pint_terminal.models import Group, Host
from pint_terminal.vault import VaultError, Workspace
from pint_terminal import vault


PASSPHRASE = "group credentials test passphrase"


@pytest.fixture
def workspace(tmp_path):
    return Workspace.create(tmp_path / "groups.pint", PASSPHRASE)


def rewrite_payload(workspace, payload):
    envelope = json.loads(workspace.path.read_bytes())
    nonce = os.urandom(12)
    envelope["nonce"] = base64.b64encode(nonce).decode()
    ciphertext = AESGCM(workspace.key).encrypt(nonce, json.dumps(payload).encode(), vault._AAD)
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode()
    workspace.path.write_text(json.dumps(envelope))


def read_payload(workspace):
    envelope = json.loads(workspace.path.read_bytes())
    return json.loads(AESGCM(workspace.key).decrypt(
        base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["ciphertext"]), vault._AAD))


def test_group_credentials_are_encrypted_and_round_trip_with_empty_groups(workspace):
    workspace.groups = [
        Group(name="Private group", username="group-engineer", password=" private group password ", key_path="/private/key"),
        Group(name="Empty group"),
    ]
    workspace.hosts = [Host(name="Switch", address="192.0.2.1", group="Private group", inherit_credentials=True)]
    workspace.save()
    raw = workspace.path.read_bytes()
    for value in ("Private group", "group-engineer", "private group password", "/private/key", "Empty group"):
        assert value.encode() not in raw
    restored = Workspace.open(workspace.path, PASSPHRASE)
    assert restored.groups == workspace.groups
    assert restored.hosts == workspace.hosts
    assert restored.resolve_host(restored.hosts[0]).password == " private group password "
    assert restored.group_names() == ["Empty group", "Private group"]
    assert restored.find_group("unknown") is None


@pytest.mark.parametrize("protocol", ["ssh", "sftp", "ftp", "telnet"])
def test_resolution_uses_complete_current_group_credentials_without_mutating_devices(tmp_path, protocol):
    group = Group(name="Lab", username="shared", password="shared-secret", key_path="")
    host = Host(name="Switch", address="192.0.2.1", protocol=protocol, group="Lab", inherit_credentials=True,
                username="stale-user", password="stale-password", key_path="stale-key")
    workspace = Workspace(tmp_path / "unused.pint", os.urandom(32), os.urandom(16), [host], {}, [group])
    original = replace(host)
    first = workspace.resolve_host(host)
    assert (first.username, first.password, first.key_path) == ("shared", "shared-secret", "")
    assert first.id == host.id and first.address == host.address and first.protocol == protocol
    assert first is not host
    assert host == original
    group.username, group.password = "updated", ""
    second = workspace.resolve_host(host)
    assert (second.username, second.password, second.key_path) == ("updated", "", "")
    assert first.password == "shared-secret"  # Existing connection snapshots remain stable.
    host.inherit_credentials = False
    custom = workspace.resolve_host(host)
    assert custom == host and custom is not host
    assert custom.password == "stale-password"


def test_device_can_use_explicitly_blank_custom_credentials(tmp_path):
    group = Group(name="Lab", username="shared", password="shared-secret", key_path="shared-key")
    host = Host(name="Switch", address="192.0.2.1", group="Lab", inherit_credentials=False)
    workspace = Workspace(tmp_path / "unused.pint", os.urandom(32), os.urandom(16), [host], {}, [group])
    custom = workspace.resolve_host(host)
    assert (custom.username, custom.password, custom.key_path) == ("", "", "")


def test_legacy_workspace_migration_preserves_credentials_and_recording_key(workspace):
    hosts = [
        Host(name="Switch", address="192.0.2.1", group="Lab", username="old-user", password="old-secret"),
        Host(name="Router", address="192.0.2.2", group="Lab", username="other-user", key_path="old-key"),
        Host(name="Ungrouped", address="192.0.2.3", group=""),
    ]
    host_data = [host.to_dict() for host in hosts]
    for data in host_data:
        del data["inherit_credentials"]
    rewrite_payload(workspace, {"version": 1, "hosts": host_data, "settings": {"known_hosts": "preserve"}})
    original = workspace.path.read_bytes()
    restored = Workspace.open(workspace.path, PASSPHRASE)
    assert restored.key == workspace.key
    assert restored.settings == {"known_hosts": "preserve"}
    assert restored.path.read_bytes() == original  # Opening alone does not write a migration.
    assert restored.group_names() == ["Lab", "Ungrouped"]
    assert restored.hosts[:2] == hosts[:2]
    assert all(not host.inherit_credentials for host in restored.hosts)
    assert restored.hosts[-1].group == "Ungrouped"
    assert restored.resolve_host(restored.hosts[0]).password == "old-secret"
    assert restored.migration_backup_path is None
    restored.save()
    assert restored.migration_backup_path == restored.path.with_name("groups.v1-backup.pint")
    assert restored.migration_backup_path.read_bytes() == original
    if os.name == "posix":
        assert stat.S_IMODE(restored.migration_backup_path.stat().st_mode) == 0o600
    assert restored._legacy_workspace_bytes is None
    assert json.loads(restored.path.read_bytes())["version"] == 1
    # Original releases require inner version 1 and therefore fail closed here.
    assert read_payload(restored)["version"] == 2
    assert Workspace.open(restored.path, PASSPHRASE).hosts == restored.hosts


def test_group_credentials_and_overrides_survive_portable_bundle(workspace, tmp_path):
    workspace.groups = [Group(name="Lab", username="group-user", password="group-secret"), Group(name="Empty")]
    workspace.hosts = [
        Host(name="Shared", address="192.0.2.1", group="Lab", inherit_credentials=True),
        Host(name="Custom", address="192.0.2.2", group="Lab", username="own-user", password="own-secret"),
    ]
    bundle = workspace.export_bundle(tmp_path / "portable.pintbundle")
    restored = Workspace.import_bundle(bundle, tmp_path / "imported.pint", PASSPHRASE)
    assert restored.groups == workspace.groups
    assert restored.hosts == workspace.hosts
    assert [restored.resolve_host(host).password for host in restored.hosts] == ["group-secret", "own-secret"]


def test_migration_backup_does_not_overwrite_existing_files_or_repeat_after_save(workspace):
    rewrite_payload(workspace, {"version": 1, "hosts": [], "settings": {"old": True}})
    original = workspace.path.read_bytes()
    previous_backup = workspace.path.with_name("groups.v1-backup.pint")
    previous_backup.write_bytes(b"keep previous backup")
    restored = Workspace.open(workspace.path, PASSPHRASE)
    restored.settings["old"] = False
    restored.save()
    assert restored.migration_backup_path == workspace.path.with_name("groups.v1-backup-2.pint")
    assert restored.migration_backup_path.read_bytes() == original
    assert previous_backup.read_bytes() == b"keep previous backup"
    restored.save()
    assert len(list(workspace.path.parent.glob("groups.v1-backup*.pint"))) == 2
    assert Workspace.open(restored.path, PASSPHRASE).settings == {"old": False}


def test_failed_backup_aborts_upgrade_and_removes_partial_backup(workspace, monkeypatch):
    rewrite_payload(workspace, {"version": 1, "hosts": [], "settings": {}})
    original = workspace.path.read_bytes()
    restored = Workspace.open(workspace.path, PASSPHRASE)
    def fail_sync(_descriptor):
        raise OSError("simulated backup flush failure")
    monkeypatch.setattr(vault.os, "fsync", fail_sync)
    with pytest.raises(VaultError, match="backup could not be saved"):
        restored.save()
    assert restored.path.read_bytes() == original
    assert restored._legacy_workspace_bytes == original
    assert restored.migration_backup_path is None
    assert not list(restored.path.parent.glob("groups.v1-backup*.pint"))


def test_backup_failure_before_creation_keeps_existing_files(workspace, monkeypatch):
    rewrite_payload(workspace, {"version": 1, "hosts": [], "settings": {}})
    original = workspace.path.read_bytes()
    restored = Workspace.open(workspace.path, PASSPHRASE)
    def fail_open(*_):
        raise PermissionError("simulated backup permission failure")
    monkeypatch.setattr(vault.os, "open", fail_open)
    with pytest.raises(VaultError, match="not been upgraded"):
        restored.save()
    assert restored.path.read_bytes() == original
    assert restored._legacy_workspace_bytes == original


def test_upgrade_save_failure_keeps_backup_and_allows_retry(workspace, monkeypatch):
    rewrite_payload(workspace, {"version": 1, "hosts": [], "settings": {}})
    original = workspace.path.read_bytes()
    restored = Workspace.open(workspace.path, PASSPHRASE)
    write = vault._atomic_write
    def fail_write(*_):
        raise VaultError("simulated save failure")
    monkeypatch.setattr(vault, "_atomic_write", fail_write)
    with pytest.raises(VaultError, match="save failure"):
        restored.save()
    assert restored.path.read_bytes() == original
    assert restored.migration_backup_path.read_bytes() == original
    assert restored._legacy_workspace_bytes == original
    monkeypatch.setattr(vault, "_atomic_write", write)
    restored.save()
    assert restored._legacy_workspace_bytes is None
    assert len(list(restored.path.parent.glob("groups.v1-backup*.pint"))) == 1
    assert read_payload(restored)["version"] == 2


def test_imported_legacy_bundle_backs_up_at_final_workspace_location(workspace, tmp_path):
    rewrite_payload(workspace, {"version": 1, "hosts": [], "settings": {"portable": True}})
    original = workspace.path.read_bytes()
    bundle = tmp_path / "legacy.pintbundle"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(workspace.path, "workspace.pint")
    destination = tmp_path / "imported" / "devices.pint"
    restored = Workspace.import_bundle(bundle, destination, PASSPHRASE)
    assert restored.path == destination
    assert restored.migration_backup_path is None
    restored.save()
    assert restored.migration_backup_path == destination.with_name("devices.v1-backup.pint")
    assert restored.migration_backup_path.read_bytes() == original
    assert read_payload(restored)["version"] == 2


@pytest.mark.parametrize("groups", [None, {}, [None], [{}], [{"name": ""}], [{"name": "Lab", "password": 7}],
                                   [{"name": "Lab", "unexpected": True}], [{"name": "Lab"}, {"name": "Lab"}]])
def test_rejects_malformed_group_payloads(workspace, groups):
    rewrite_payload(workspace, {"version": 2, "hosts": [], "groups": groups, "settings": {}})
    with pytest.raises(VaultError):
        Workspace.open(workspace.path, PASSPHRASE)


def test_inherited_device_cannot_silently_fall_back_when_group_is_missing(workspace):
    host = Host(name="Switch", address="192.0.2.1", group="Missing", inherit_credentials=True,
                username="stale-user", password="stale-secret")
    workspace.hosts = [host]
    before = workspace.path.read_bytes()
    with pytest.raises(VaultError, match="missing"):
        workspace.resolve_host(host)
    with pytest.raises(VaultError, match="missing"):
        workspace.save()
    assert workspace.path.read_bytes() == before
    rewrite_payload(workspace, {"version": 2, "hosts": [host.to_dict()], "groups": [], "settings": {}})
    with pytest.raises(VaultError, match="missing"):
        Workspace.open(workspace.path, PASSPHRASE)


def test_own_credential_devices_create_groups_when_added_by_existing_code(workspace):
    workspace.hosts = [Host(name="Switch", address="192.0.2.1", group="Lab", password="own-password")]
    workspace.save()
    assert workspace.groups == [Group(name="Lab")]
    assert Workspace.open(workspace.path, PASSPHRASE).hosts == workspace.hosts


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_inheritance_mode_must_be_boolean(value):
    with pytest.raises(ValueError, match="boolean"):
        Host(name="Switch", address="192.0.2.1", inherit_credentials=value).validate()
