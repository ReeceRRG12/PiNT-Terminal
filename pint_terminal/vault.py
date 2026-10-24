"""Authenticated local workspaces and bounded, portable backup bundles."""

from __future__ import annotations

import base64
import binascii
from contextlib import closing
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

from .models import Host


_FORMAT = "pint-workspace"
_VERSION = 1
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
    def __init__(self, path: Path, key: bytes, salt: bytes, hosts: list[Host], settings: dict[str, Any]):
        self.path = Path(path)
        self.key = key
        self.hosts = hosts
        self.settings = settings
        self._salt = salt
        self._lock = threading.RLock()

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
            if not isinstance(payload, dict) or payload.get("version") != _VERSION:
                raise VaultError("The encrypted workspace content is unsupported.")
            host_data, settings = payload.get("hosts"), payload.get("settings")
            if not isinstance(host_data, list) or not isinstance(settings, dict):
                raise VaultError("The encrypted workspace content is invalid.")
            hosts = [Host.from_dict(host) for host in host_data]
            if len({host.id for host in hosts}) != len(hosts):
                raise VaultError("The workspace contains duplicate host IDs.")
            return cls(path, key, salt, hosts, settings)
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
                payload = _json({"version": _VERSION, "hosts": [host.to_dict() for host in self.hosts], "settings": self.settings})
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
            _atomic_write(self.path, encoded)

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
