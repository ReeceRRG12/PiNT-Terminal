"""SSH/Telnet sessions with explicit host trust and cancellable reconnects."""
from __future__ import annotations

import asyncio
from ipaddress import ip_address
import os
import struct
import threading
from pathlib import Path

import asyncssh
import telnetlib3
from PySide6.QtCore import QThread, Signal


class UnknownHostKey(Exception):
    def __init__(self, fingerprint: str, public_key: str):
        self.fingerprint, self.public_key = fingerprint, public_key
        self.key = public_key
        super().__init__(f"Unknown SSH host key: {fingerprint}")


class ChangedHostKey(Exception):
    """An existing host has presented a different SSH identity."""


def _match_known_hosts(path, host, address, port):
    if not Path(path).exists():
        return ((),) * 7
    known_hosts = asyncssh.read_known_hosts(str(path))
    result = list(known_hosts.match(host, address, port))
    # AsyncSSH's unported fallback can discard an entry containing only a
    # port-specific revocation. Match the qualified name directly, without
    # fallback, and retain revocations alongside any fallback trusted keys.
    # Its connection callback uses None to represent the standard SSH port.
    qualified_port = port or 22
    for name in {host, address} - {""}:
        specific = known_hosts.match(f"[{name}]:{qualified_port}", "", None)
        for index in (2, 4, 6):
            result[index] = list(result[index]) + [item for item in specific[index] if item not in result[index]]
    return tuple(result)


def _known_keys(path, host, address, port):
    return _match_known_hosts(path, host, address, port)[:3]


def trust_host_key(host, path: Path, exported_key: str):
    """Append a user-approved key. Never replace a previously trusted key."""
    address, port = host.address, int(host.port)
    if any(c.isspace() for c in address) or any(c in address for c in ",[]"):
        raise ValueError("Invalid SSH host address")
    key = asyncssh.import_public_key(exported_key)
    # Approval is bound to the profile's hostname (or literal IP). No resolved
    # peer address is available here: passing a hostname as AsyncSSH's IP
    # argument makes its known-host matcher reject it before checking trust.
    try:
        ip_address(address)
    except ValueError:
        peer_address = ""
    else:
        peer_address = address
    existing, _, revoked = _known_keys(path, address, peer_address, port)
    if key in revoked or (existing and key not in existing):
        raise ChangedHostKey("SSH host key changed. Verify the device identity before changing trust.")
    if key in existing:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = address if port == 22 else f"[{address}]:{port}"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        file.write(f"{name} {key.export_public_key().decode().strip()}\n")


class _HostKeyClient(asyncssh.SSHClient):
    def __init__(self, path):
        self.path, self.problem = path, None

    def validate_host_public_key(self, host, addr, port, key):
        known, authorities, revoked = _known_keys(self.path, host, addr, port)
        if known or authorities or key in revoked:
            self.problem = ChangedHostKey("SSH host key changed. Connection blocked; verify the device identity.")
        else:
            self.problem = UnknownHostKey(key.get_fingerprint(), key.export_public_key().decode())
        return False


async def connect_ssh(host, known_hosts_path):
    path = Path(known_hosts_path)
    verifier = _HostKeyClient(path)
    kwargs = dict(host=host.address, port=int(host.port),
                  known_hosts=lambda name, address, port: _match_known_hosts(path, name, address, port),
                  client_factory=lambda: verifier, connect_timeout=15,
                  login_timeout=30, keepalive_interval=15, keepalive_count_max=3)
    if host.username:
        kwargs["username"] = host.username
    if host.password:
        kwargs["password"] = host.password
    if host.key_path:
        kwargs["client_keys"] = [str(Path(host.key_path).expanduser())]
    try:
        return await asyncssh.connect(**kwargs)
    except asyncssh.HostKeyNotVerifiable:
        if verifier.problem:
            raise verifier.problem from None
        raise


class SessionWorker(QThread):
    output = Signal(str)
    status = Signal(str)
    error = Signal(str)
    host_key = Signal(str, str)
    ended = Signal()

    def __init__(self, host, known_hosts_path: Path, parent=None):
        super().__init__(parent)
        self.host, self.known_hosts_path = host, Path(known_hosts_path)
        self._loop = self._task = self._writer = self._connection = None
        self._attempt = None
        self._stop = threading.Event()
        self._manual_reconnect = False
        self._restart_requested = False
        self._final_status = "Disconnected"
        self._cols, self._rows = 100, 30
        self.finished.connect(self._restart_after_finish)

    def _restart_after_finish(self):
        if self._restart_requested:
            self._restart_requested = False
            self._stop.clear()
            self.start()

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._task = self._loop.create_task(self._run_sessions())
        try:
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()
            self._loop = self._writer = self._connection = None
            self.status.emit(self._final_status)
            self.ended.emit()

    async def _run_sessions(self):
        failures = 0
        while not self._stop.is_set():
            self._manual_reconnect = False
            self.status.emit("Connecting")
            started = asyncio.get_running_loop().time()
            try:
                self._attempt = asyncio.create_task(self._session())
                result = await self._attempt
                if result:
                    self._final_status = result
                    return
            except asyncio.CancelledError:
                if self._stop.is_set() or not self._manual_reconnect:
                    return
            except UnknownHostKey as exc:
                self._final_status = "Host key approval required"
                self.host_key.emit(exc.fingerprint, exc.public_key)
                return
            except (ChangedHostKey, asyncssh.HostKeyNotVerifiable, asyncssh.PermissionDenied,
                    asyncssh.KeyImportError, ValueError) as exc:
                self._final_status = f"Connection failed: {exc}"
                self.error.emit(str(exc))
                return
            except Exception as exc:
                self._final_status = f"Disconnected: {exc}"
                self.error.emit(str(exc))
            finally:
                self._writer = None
                if self._connection:
                    self._connection.close()
                    try:
                        await self._connection.wait_closed()
                    except Exception:
                        pass
                    self._connection = None
            if self._stop.is_set():
                return
            if self._manual_reconnect:
                failures = 0
                continue
            if not self.host.auto_reconnect:
                return
            if asyncio.get_running_loop().time() - started > 60:
                failures = 0
            delay = min(30, 2 ** min(failures, 5))
            failures += 1
            self.status.emit(f"Reconnecting in {delay}s")
            try:
                self._attempt = asyncio.create_task(asyncio.sleep(delay))
                await self._attempt
            except asyncio.CancelledError:
                if not self._manual_reconnect or self._stop.is_set():
                    return

    async def _session(self):
        protocol = self.host.protocol.lower()
        if protocol == "ssh":
            self._connection = await connect_ssh(self.host, self.known_hosts_path)
            process = await self._connection.create_process(
                term_type="xterm-256color", term_size=(self._cols, self._rows),
                encoding="utf-8", errors="replace", stderr=asyncssh.STDOUT)
            self._writer = process.stdin
            self._process = process
            reader = process.stdout
        elif protocol == "telnet":
            reader, writer = await asyncio.wait_for(telnetlib3.open_connection(
                host=self.host.address, port=int(self.host.port), term="xterm-256color",
                cols=self._cols, rows=self._rows, encoding="utf8", encoding_errors="replace",
                connect_minwait=0.1, connect_maxwait=1.0), timeout=15)
            self._writer = self._connection = writer
            writer.set_ext_send_callback(telnetlib3.telopt.NAWS, lambda: (self._rows, self._cols))
        else:
            raise ValueError("Terminal connections support SSH and Telnet")
        self.status.emit("Connected")
        self._final_status = "Disconnected"
        while not self._stop.is_set():
            data = await reader.read(65536)
            if not data:
                break
            self.output.emit(data)
        if protocol == "ssh":
            await asyncio.wait_for(process.wait_closed(), timeout=5)
            if process.exit_status is not None:
                return f"Session ended (exit {process.exit_status})"
            if self._connection.is_closed():
                return None
        return "Session ended"

    def _dispatch(self, callback):
        loop = self._loop
        if loop and loop.is_running():
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                pass

    def send(self, data: str):
        # Capture the current writer so keystrokes cannot cross reconnects.
        writer = self._writer
        if writer is None or self._stop.is_set():
            return
        def write():
            if self._writer is writer and not self._stop.is_set():
                try:
                    writer.write(data)
                except (OSError, RuntimeError):
                    pass
        self._dispatch(write)

    def resize(self, cols: int, rows: int):
        self._cols, self._rows = max(1, min(cols, 65535)), max(1, min(rows, 65535))
        def update():
            if not self._writer:
                return
            if self.host.protocol.lower() == "ssh":
                self._process.change_terminal_size(self._cols, self._rows)
            elif self._writer.local_option.enabled(telnetlib3.telopt.NAWS):
                # telnetlib3 has no public resize helper; use its public IAC sender.
                size = struct.pack("!HH", self._cols, self._rows).replace(b"\xff", b"\xff\xff")
                self._writer.send_iac(b"\xff\xfa\x1f" + size + b"\xff\xf0")
        self._dispatch(update)

    def disconnect(self):
        self._restart_requested = False
        self._final_status = "Disconnected"
        self._stop.set()
        self._dispatch(lambda: self._task.cancel() if self._task else None)

    def reconnect(self):
        if not self.isRunning():
            self._stop.clear()
            self.start()
            return
        if self._task and self._task.done():
            self._restart_requested = True
            return
        def retry():
            if self._task and self._task.done():
                self._restart_requested = True
                return
            self._manual_reconnect = True
            if self._attempt:
                self._attempt.cancel()
        self._dispatch(retry)
