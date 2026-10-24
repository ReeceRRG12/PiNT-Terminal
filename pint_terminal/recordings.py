"""Disk-backed, authenticated terminal output and rendered history.

Only remote output is recorded. Callers must never pass local input to this store.
SQLite retains opaque identifiers and ordering; all user content is encrypted.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import Host


_PROOF = b"PiNT Terminal recordings v1"
_TABLES = {"store_meta", "sessions", "outputs", "events", "lines"}
_SCHEMA = """
CREATE TABLE store_meta (name TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE sessions (id TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE outputs (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    position INTEGER NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (session_id, position)
);
CREATE TABLE events (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    position INTEGER NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (session_id, position)
);
CREATE TABLE lines (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    position INTEGER NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (session_id, position)
);
PRAGMA user_version=1;
"""


class RecordingError(RuntimeError):
    """Recording failed or an encrypted recording could not be authenticated."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RecordStore:
    def __init__(self, path: str | Path, key: bytes, *, existing_only: bool = False):
        if not isinstance(key, bytes) or len(key) != 32:
            raise RecordingError("Recordings require the workspace encryption key.")
        self.path = Path(path)
        self._cipher = AESGCM(key)
        self._lock = threading.RLock()
        self._closed = False
        self._read_only = existing_only
        self._transaction_depth = 0
        self._connection: sqlite3.Connection | None = None
        try:
            if existing_only:
                self._connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=10)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Reserve a new file with restrictive permissions before SQLite writes.
                if not self.path.exists():
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                os.chmod(self.path, 0o600)
                self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._connection.execute("PRAGMA foreign_keys=ON")
            entries = self._connection.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if not entries and not existing_only:
                self._connection.executescript(_SCHEMA)
                self._connection.execute("INSERT INTO store_meta VALUES (?, ?)", ("proof", self._encrypt(_PROOF, b"proof")))
                self._connection.commit()
            else:
                tables = {name for name, kind in entries if kind == "table"}
                if tables != _TABLES or any(kind not in {"table", "index"} for _, kind in entries):
                    raise RecordingError("This is not a supported PiNT recordings database.")
                if self._connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise RecordingError("This recordings database version is unsupported.")
                row = self._connection.execute("SELECT payload FROM store_meta WHERE name='proof'").fetchone()
                if row is None or self._decrypt(row[0], b"proof") != _PROOF:
                    raise RecordingError("The recordings do not belong to this workspace.")
            if existing_only:
                self._connection.execute("PRAGMA query_only=ON")
            else:
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
        except (OSError, sqlite3.Error, InvalidTag, RecordingError) as exc:
            if self._connection is not None:
                self._connection.close()
            self._closed = True
            if isinstance(exc, RecordingError):
                raise
            raise RecordingError("The recordings could not be opened or authenticated.") from exc

    def _encrypt(self, data: bytes, context: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, data, b"pint-recordings-v1:" + context)

    def _decrypt(self, data: bytes, context: bytes) -> bytes:
        if not isinstance(data, bytes) or len(data) < 28:
            raise RecordingError("A recording is damaged.")
        try:
            return self._cipher.decrypt(data[:12], data[12:], b"pint-recordings-v1:" + context)
        except InvalidTag as exc:
            raise RecordingError("A recording could not be authenticated. It may have been damaged.") from exc

    @staticmethod
    def _context(table: str, session_id: str, position: int | None = None) -> bytes:
        return json.dumps([table, session_id, position], separators=(",", ":")).encode("utf-8")

    def _pack(self, value: Any, context: bytes) -> bytes:
        return self._encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"), context)

    def _unpack(self, value: bytes, context: bytes) -> Any:
        return json.loads(self._decrypt(value, context))

    @contextmanager
    def _operation(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed or self._connection is None:
                raise RecordingError("The recordings store is closed.")
            if write and self._read_only:
                raise RecordingError("The recordings store is read-only.")
            try:
                yield self._connection
                if write and not self._transaction_depth:
                    self._connection.commit()
            except (sqlite3.Error, OSError, UnicodeError, json.JSONDecodeError) as exc:
                if write and not self._transaction_depth:
                    self._connection.rollback()
                raise RecordingError("The recording operation failed. Check the file and available disk space.") from exc
            except BaseException:
                if write and not self._transaction_depth:
                    self._connection.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit one received output chunk and its history rows together.

        The re-entrant lock keeps a batch on one thread. SQLite still fsyncs each
        completed batch, rather than every rendered row within that batch.
        """
        with self._operation(write=True) as connection:
            if not self._transaction_depth:
                connection.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
            finally:
                self._transaction_depth -= 1

    @staticmethod
    def _page(start: int, limit: int) -> None:
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("The page position must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 10000:
            raise ValueError("The page size must be between 0 and 10000.")

    @staticmethod
    def _require_session(connection: sqlite3.Connection, session_id: str) -> None:
        if connection.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise RecordingError("The recording session does not exist.")

    def start_session(self, host: Host) -> str:
        host.validate()
        session_id = str(uuid4())
        metadata = {
            "id": session_id, "name": host.name, "protocol": host.protocol,
            "address": host.address, "started": _now(), "ended": None, "status": "connecting",
        }
        with self._operation(write=True) as connection:
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, self._pack(metadata, self._context("sessions", session_id))))
        return session_id

    def _append(self, table: str, session_id: str, value: Any, *, raw: bool = False) -> None:
        if table not in {"outputs", "events", "lines"}:
            raise ValueError("Invalid recording section.")
        with self._operation(write=True) as connection:
            self._require_session(connection, session_id)
            position = connection.execute(f"SELECT COALESCE(MAX(position), -1) + 1 FROM {table} WHERE session_id=?", (session_id,)).fetchone()[0]
            context = self._context(table, session_id, position)
            payload = self._encrypt(value.encode("utf-8"), context) if raw else self._pack(value, context)
            connection.execute(f"INSERT INTO {table} VALUES (?, ?, ?)", (session_id, position, payload))

    def append_output(self, session_id: str, data: str) -> None:
        if not isinstance(data, str):
            raise ValueError("Terminal output must be text.")
        if data:
            self._append("outputs", session_id, data, raw=True)

    def event(self, session_id: str, text: str) -> None:
        if not isinstance(text, str):
            raise ValueError("A recording event must be text.")
        self._append("events", session_id, {"time": _now(), "text": text})

    def set_session_status(self, session_id: str, status: str) -> None:
        """Update a live session, clearing an earlier end time on reconnect."""
        if not isinstance(status, str) or not status:
            raise ValueError("A session status is required.")
        with self._operation(write=True) as connection:
            self._require_session(connection, session_id)
            row = connection.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
            context = self._context("sessions", session_id)
            metadata = self._unpack(row[0], context)
            metadata.update(ended=None, status=status)
            connection.execute("UPDATE sessions SET payload=? WHERE id=?", (self._pack(metadata, context), session_id))

    def finish_session(self, session_id: str, status: str) -> None:
        if not isinstance(status, str) or not status:
            raise ValueError("A session status is required.")
        with self._operation(write=True) as connection:
            self._require_session(connection, session_id)
            row = connection.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
            context = self._context("sessions", session_id)
            metadata = self._unpack(row[0], context)
            metadata.update(ended=_now(), status=status)
            connection.execute("UPDATE sessions SET payload=? WHERE id=?", (self._pack(metadata, context), session_id))

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._operation() as connection:
            sessions = [self._unpack(payload, self._context("sessions", session_id)) for session_id, payload in connection.execute("SELECT id, payload FROM sessions")]
        return sorted(sessions, key=lambda session: session["started"], reverse=True)

    def _count(self, table: str, session_id: str) -> int:
        with self._operation() as connection:
            self._require_session(connection, session_id)
            return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)).fetchone()[0]

    def output_count(self, session_id: str) -> int:
        return self._count("outputs", session_id)

    def line_count(self, session_id: str) -> int:
        return self._count("lines", session_id)

    def _read(self, table: str, session_id: str, start: int, limit: int, *, raw: bool = False) -> list[Any]:
        self._page(start, limit)
        with self._operation() as connection:
            self._require_session(connection, session_id)
            rows = connection.execute(f"SELECT position, payload FROM {table} WHERE session_id=? AND position>=? ORDER BY position LIMIT ?", (session_id, start, limit))
            result = []
            for position, payload in rows:
                context = self._context(table, session_id, position)
                result.append(self._decrypt(payload, context).decode("utf-8") if raw else self._unpack(payload, context))
            return result

    def read_output(self, session_id: str, offset: int = 0, limit: int = 200) -> list[str]:
        return self._read("outputs", session_id, offset, limit, raw=True)

    def read_events(self, session_id: str, offset: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self._read("events", session_id, offset, limit)

    def append_line(self, session_id: str, cells: list[dict[str, Any]]) -> None:
        if not isinstance(cells, list) or any(not isinstance(cell, dict) for cell in cells):
            raise ValueError("A history line must be a list of cell objects.")
        self._append("lines", session_id, cells)

    def read_lines(self, session_id: str, start: int, limit: int) -> list[list[dict[str, Any]]]:
        return self._read("lines", session_id, start, limit)

    def verify(self) -> None:
        """Authenticate a portable database without loading its history into memory."""
        with self._operation() as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
                raise RecordingError("The recordings database is damaged.")
            for session_id, payload in connection.execute("SELECT id, payload FROM sessions"):
                metadata = self._unpack(payload, self._context("sessions", session_id))
                if not isinstance(metadata, dict) or metadata.get("id") != session_id or not {"name", "protocol", "address", "started", "ended", "status"} <= metadata.keys():
                    raise RecordingError("A recording has invalid metadata.")
            for table in ("outputs", "events", "lines"):
                for session_id, position, payload in connection.execute(f"SELECT session_id, position, payload FROM {table}"):
                    data = self._decrypt(payload, self._context(table, session_id, position))
                    if table == "outputs":
                        data.decode("utf-8")
                    else:
                        value = json.loads(data)
                        if table == "events" and (not isinstance(value, dict) or not {"time", "text"} <= value.keys()):
                            raise RecordingError("A recording event is invalid.")
                        if table == "lines" and (not isinstance(value, list) or any(not isinstance(cell, dict) for cell in value)):
                            raise RecordingError("A recording history line is invalid.")

    def close(self) -> None:
        with self._lock:
            if not self._closed and self._connection is not None:
                try:
                    self._connection.close()
                except sqlite3.Error as exc:
                    raise RecordingError("The recordings store could not be closed.") from exc
                finally:
                    self._closed = True

    def __enter__(self) -> RecordStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
