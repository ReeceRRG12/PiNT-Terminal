"""Portable connection profiles, independent of the user interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any
from uuid import uuid4


DEFAULT_PORTS = {"ssh": 22, "telnet": 23, "sftp": 22, "ftp": 21}


@dataclass
class Host:
    id: str = field(default_factory=lambda: str(uuid4()))
    name: str = ""
    address: str = ""
    protocol: str = "ssh"
    port: int | None = None
    username: str = ""
    password: str = ""
    key_path: str = ""
    group: str = "Ungrouped"
    tags: str = ""
    auto_reconnect: bool = True

    def __post_init__(self) -> None:
        if self.port is None:
            self.port = DEFAULT_PORTS.get(self.protocol, 22)

    def validate(self) -> Host:
        for name in ("id", "name", "address", "protocol", "username", "password", "key_path", "group", "tags"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError(f"Host {name} must be text without null characters.")
        if not self.id or not self.name.strip() or not self.address.strip():
            raise ValueError("A host needs an ID, a name and an address.")
        if self.protocol not in DEFAULT_PORTS:
            raise ValueError("Choose SSH, Telnet, SFTP or FTP.")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("The port must be between 1 and 65535.")
        if not isinstance(self.auto_reconnect, bool):
            raise ValueError("Automatic reconnect must be a boolean.")
        if any(character.isspace() for character in self.address):
            raise ValueError("Use a hostname or IP address without spaces.")
        return self

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Host:
        if not isinstance(value, dict):
            raise ValueError("A host must be an object.")
        known = {item.name for item in fields(cls)}
        if set(value) - known:
            raise ValueError("The host contains unsupported fields.")
        try:
            return cls(**value).validate()
        except TypeError as exc:
            raise ValueError("The host profile is invalid.") from exc
