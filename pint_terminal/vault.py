"""Authenticated local workspaces and bounded, portable backup bundles."""

from __future__ import annotations

import base64
import binascii
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
from typing import Any
import zipfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .models import Group, Host


_FORMAT = "pint-workspace"
_VERSION = 1
_PAYLOAD_VERSION = 2
_KDF = {"name": "scrypt", "n": 131072, "r": 8, "p": 1}
_AAD = b"PiNT Terminal workspace v1"
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
MAX_RECORDINGS_BYTES = 8 * 1024 * 1024 * 1024
_ARCHIVE_NAMES = {"workspace.pint", "sessions.sqlite"}


class VaultError(ValueError):
    """A workspace could not be read, authenticated or safely written."""


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _decode(value: Any, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise VaultError("The workspace envelope is invalid.")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultError("The workspace envelope is invalid.") from exc
    if expected_length is not None and len(decoded) != expected_length:
        raise VaultError("The workspace envelope is invalid.")
    return decoded


def _derive(password: str, salt: bytes) -> bytes:
    if not isinstance(password, str) or not password:
        raise VaultError("Enter a workspace passphrase.")
    encoded = password.encode("utf-8")
    if len(encoded) > 4096:
        raise VaultError("The workspace passphrase is too long.")
    return Scrypt(salt=salt, length=32, n=_KDF["n"], r=_KDF["r"], p=_KDF["p"]).derive(encoded)


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise VaultError("The workspace could not be saved. Check its location and available disk space.") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class Workspace:
    def __init__(self, path: Path, key: bytes, salt: bytes, hosts: list[Host], settings: dict[str, Any],
                 groups: list[Group] | None = None):
        self.path = Path(path)
        self.key = key
        self.hosts = hosts
        self.settings = settings
        self.groups = list(groups) if groups is not None else []
        self.migration_backup_path: Path | None = None
        self._legacy_workspace_bytes: bytes | None = None
        self._salt = salt
        self._lock = threading.RLock()
        self._validate_groups(allow_new=groups is None)

    def find_group(self, name: str) -> Group | None:
        return next((group for group in self.groups if group.name == name), None)

    def group_names(self) -> list[str]:
        return sorted((group.name for group in self.groups), key=str.casefold)

    def _validate_groups(self, *, allow_new: bool) -> None:
        names = set()
        for group in self.groups:
            group.validate()
            if group.name in names:
                raise VaultError("The workspace contains duplicate group names.")
            names.add(group.name)
        for host in self.hosts:
            host.validate()
            if host.group not in names:
                if not allow_new or host.inherit_credentials:
                    raise VaultError(f'The group for device "{host.name}" is missing.')
                group = Group(name=host.group).validate()
                self.groups.append(group)
                names.add(group.name)

    def resolve_host(self, host: Host) -> Host:
        """Take a connection snapshot without copying defaults into the saved device."""
        with self._lock:
            host.validate()
            if not host.inherit_credentials:
                return replace(host)
            group = self.find_group(host.group)
            if group is None:
                raise VaultError(f'The group for device "{host.name}" is missing.')
            group.validate()
            return replace(host, username=group.username, password=group.password, key_path=group.key_path)

    @property
    def recordings_path(self) -> Path:
        return self.path.with_suffix(".sessions.sqlite")

    @classmethod
    def create(cls, path: str | Path, password: str) -> Workspace:
        path = Path(path)
        if path.exists() or path.with_suffix(".sessions.sqlite").exists():
            raise VaultError("A workspace or recordings file already exists at this location.")
        salt = os.urandom(16)
        workspace = cls(path, _derive(password, salt), salt, [], {})
        workspace.save()
        return workspace

    @classmethod
    def open(cls, path: str | Path, password: str) -> Workspace:
        path = Path(path)
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_WORKSPACE_BYTES + 1)
        except OSError as exc:
            raise VaultError("The workspace file could not be opened.") from exc
        if len(raw) > MAX_WORKSPACE_BYTES:
            raise VaultError("The workspace file exceeds the supported size.")
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("format") != _FORMAT or envelope.get("version") != _VERSION:
                raise VaultError("This is not a supported PiNT workspace.")
            # Fixed parameters prevent an untrusted file choosing excessive KDF work.
            if envelope.get("kdf") != _KDF:
                raise VaultError("The workspace uses unsupported key derivation parameters.")
            salt = _decode(envelope.get("salt"), 16)
            nonce = _decode(envelope.get("nonce"), 12)
            ciphertext = _decode(envelope.get("ciphertext"))
            key = _derive(password, salt)
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
            payload = json.loads(plaintext)
            if (not isinstance(payload, dict) or type(payload.get("version")) is not int
                    or payload["version"] not in (1, _PAYLOAD_VERSION)):
                raise VaultError("The encrypted workspace content is unsupported.")
            host_data, settings = payload.get("hosts"), payload.get("settings")
            if not isinstance(host_data, list) or not isinstance(settings, dict):
                raise VaultError("The encrypted workspace content is invalid.")
            hosts = [Host.from_dict(host) for host in host_data]
            if len({host.id for host in hosts}) != len(hosts):
                raise VaultError("The workspace contains duplicate host IDs.")
            if payload["version"] == 1:
                if any("inherit_credentials" in host for host in host_data):
                    raise VaultError("The legacy workspace contains unsupported credential settings.")
                # Older profiles allowed an empty group label. Keep their own
                # credentials and give those devices a visible home in the vault.
                hosts = [replace(host, group=host.group if host.group.strip() else "Ungrouped") for host in hosts]
                groups = None
            else:
                group_data = payload.get("groups")
                if not isinstance(group_data, list):
                    raise VaultError("The encrypted workspace groups are invalid.")
                groups = [Group.from_dict(group) for group in group_data]
            workspace = cls(path, key, salt, hosts, settings, groups)
            if payload["version"] == 1:
                workspace._legacy_workspace_bytes = raw
            return workspace
        except InvalidTag as exc:
            raise VaultError("The passphrase is incorrect or the workspace has been damaged.") from exc
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, RecursionError) as exc:
            if isinstance(exc, VaultError):
                raise
            raise VaultError("The workspace content is invalid.") from exc

    def save(self) -> None:
        with self._lock:
            try:
                if not isinstance(self.settings, dict):
                    raise VaultError("Workspace settings must be an object.")
                if len({host.id for host in self.hosts}) != len(self.hosts):
                    raise VaultError("The workspace contains duplicate host IDs.")
                self._validate_groups(allow_new=True)
                payload = _json({"version": _PAYLOAD_VERSION, "hosts": [host.to_dict() for host in self.hosts],
                                 "groups": [group.to_dict() for group in self.groups], "settings": self.settings})
                nonce = os.urandom(12)
                ciphertext = AESGCM(self.key).encrypt(nonce, payload, _AAD)
                encoded = _json({
                    "format": _FORMAT, "version": _VERSION, "kdf": _KDF,
                    "salt": base64.b64encode(self._salt).decode("ascii"),
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                })
            except (TypeError, ValueError) as exc:
                if isinstance(exc, VaultError):
                    raise
                raise VaultError("The workspace contains invalid host details or settings.") from exc
            if len(encoded) > MAX_WORKSPACE_BYTES:
                raise VaultError("The workspace exceeds the supported size.")
            self._backup_legacy_workspace()
            _atomic_write(self.path, encoded)
            self._legacy_workspace_bytes = None

    def _backup_legacy_workspace(self) -> None:
        """Preserve the original encrypted file before its first schema upgrade."""
        original = self._legacy_workspace_bytes
        if original is None:
            return
        if self.migration_backup_path is not None:
            try:
                with self.migration_backup_path.open("rb") as stream:
                    if stream.read(MAX_WORKSPACE_BYTES + 1) == original:
                        return
            except OSError:
                pass
        candidate: Path | None = None
        created = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            index = 1
            while True:
                suffix = "" if index == 1 else f"-{index}"
                candidate = self.path.with_name(f"{self.path.stem}.v1-backup{suffix}.pint")
                try:
                    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    break
                except FileExistsError:
                    index += 1
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            self.migration_backup_path = candidate
        except OSError as exc:
            if created and candidate is not None:
                try:
                    candidate.unlink()
                except OSError:
                    pass
            raise VaultError("The original workspace backup could not be saved. "
                             "The workspace has not been upgraded. Check its location and available disk space.") from exc

    def export_bundle(self, destination: str | Path) -> Path:
        destination = Path(destination)
        if destination.resolve() in {self.path.resolve(), self.recordings_path.resolve()}:
            raise VaultError("Choose a separate location for the portable bundle.")
        with self._lock:
            self.save()
            temporary: str | None = None
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="pint-export-") as staging:
                    snapshot = Path(staging) / "sessions.sqlite"
                    if self.recordings_path.exists():
                        with closing(sqlite3.connect(self.recordings_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                            with closing(sqlite3.connect(snapshot)) as target:
                                source.backup(target)
                        if snapshot.stat().st_size > MAX_RECORDINGS_BYTES:
                            raise VaultError("The recordings exceed the portable bundle size limit.")
                    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
                    os.close(fd)
                    os.chmod(temporary, 0o600)
                    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                        archive.write(self.path, "workspace.pint")
                        if snapshot.exists():
                            archive.write(snapshot, "sessions.sqlite")
                    # Windows requires write access when flushing a file handle.
                    with open(temporary, "r+b") as stream:
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                    temporary = None
                return destination
            except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
                raise VaultError("The portable bundle could not be exported.") from exc
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    @classmethod
    def import_bundle(cls, source: str | Path, destination: str | Path, password: str) -> Workspace:
        destination = Path(destination)
        recordings = destination.with_suffix(".sessions.sqlite")
        if destination.exists() or recordings.exists():
            raise VaultError("Choose a new workspace location for this import.")
        installed_recordings = False
        try:
            # Bound both archive input size and declared expanded member sizes.
            if Path(source).stat().st_size > MAX_WORKSPACE_BYTES + MAX_RECORDINGS_BYTES + 65536:
                raise VaultError("The portable bundle exceeds the supported size.")
            with zipfile.ZipFile(source, "r") as archive:
                members = archive.infolist()
                names = [member.filename for member in members]
                if len(members) not in (1, 2) or len(set(names)) != len(names) or "workspace.pint" not in names or set(names) - _ARCHIVE_NAMES:
                    raise VaultError("The portable bundle has unexpected or duplicate entries.")
                for member in members:
                    maximum = MAX_WORKSPACE_BYTES if member.filename == "workspace.pint" else MAX_RECORDINGS_BYTES
                    mode = member.external_attr >> 16
                    if (member.is_dir() or stat.S_ISLNK(mode) or member.flag_bits & 1
                            or member.file_size > maximum
                            or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                            or member.file_size > max(1024 * 1024, member.compress_size * 200)):
                        raise VaultError("The portable bundle contains an unsafe entry.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".pint-import-", dir=destination.parent) as staging:
                    staging_path = Path(staging)
                    for member in members:
                        target = staging_path / member.filename
                        # Never extract user-controlled paths. Names were matched exactly above.
                        maximum = MAX_WORKSPACE_BYTES if member.filename == "workspace.pint" else MAX_RECORDINGS_BYTES
                        copied = 0
                        with archive.open(member) as incoming, target.open("xb") as outgoing:
                            os.chmod(target, 0o600)
                            while chunk := incoming.read(1024 * 1024):
                                copied += len(chunk)
                                if copied > maximum or copied > member.file_size:
                                    raise VaultError("The portable bundle entry exceeds its size limit.")
                                outgoing.write(chunk)
                            outgoing.flush()
                            os.fsync(outgoing.fileno())
                    workspace = cls.open(staging_path / "workspace.pint", password)
                    database = staging_path / "sessions.sqlite"
                    if database.exists():
                        # Verify schema and authenticated payloads before committing either file.
                        from .recordings import RecordStore, RecordingError
                        try:
                            store = RecordStore(database, workspace.key, existing_only=True)
                            try:
                                store.verify()
                            finally:
                                store.close()
                        except RecordingError as exc:
                            raise VaultError("The bundle recordings are damaged or do not belong to this workspace.") from exc
                        os.replace(database, recordings)
                        installed_recordings = True
                    os.replace(staging_path / "workspace.pint", destination)
                    workspace.path = destination
                    return workspace
        except (OSError, sqlite3.Error, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
            if installed_recordings and not destination.exists():
                try:
                    recordings.unlink()
                except OSError:
                    pass
            if isinstance(exc, VaultError):
                raise
            raise VaultError("The portable bundle could not be imported.") from exc
