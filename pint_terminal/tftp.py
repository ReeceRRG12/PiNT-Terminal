"""Small, explicitly started, read-only RFC 1350 TFTP host."""
from __future__ import annotations

import ipaddress
import os
import socket
import struct
import threading
import time
from pathlib import Path, PurePosixPath


def _error(code, message):
    return struct.pack("!HH", 5, code) + message.encode("ascii", "replace") + b"\0"


def _is_link(path):
    junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or junction()


class TFTPServer:
    def __init__(self, root, host="127.0.0.1", port=1069, on_event=None):
        self.root = Path(root).absolute()
        if _is_link(self.root) or not self.root.is_dir():
            raise ValueError("Choose a real directory as the TFTP root")
        self.root = self.root.resolve()
        ipaddress.IPv4Address(host)
        self.host, self.port = host, int(port)
        self.on_event = on_event or (lambda message: None)
        self.timeout, self.retries = 1.0, 5
        self._stop = threading.Event()
        self._socket = self._thread = None
        self._clients = {}
        self._lock = threading.Lock()

    def _event(self, message):
        try:
            self.on_event(message)
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except BaseException:
            sock.close()
            raise
        self.port = sock.getsockname()[1]
        sock.settimeout(0.2)
        self._socket = sock
        self._thread = threading.Thread(target=self._serve, daemon=True, name="pint-tftp")
        self._thread.start()
        self._event(f"Read-only TFTP listening on {self.host}:{self.port}")

    def stop(self):
        self._stop.set()
        if self._socket:
            self._socket.close()
        with self._lock:
            clients = list(self._clients.values())
        for sock, thread in clients:
            sock.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)
        for sock, thread in clients:
            if thread is not threading.current_thread():
                thread.join(2)
        self._socket = None
        self._event("TFTP stopped")

    def _path(self, filename):
        if "\\" in filename or "\x00" in filename or ":" in filename:
            raise PermissionError("Invalid path")
        relative = PurePosixPath(filename)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise PermissionError("Path outside TFTP root")
        path = self.root
        for part in relative.parts:
            path = path / part
            if _is_link(path):
                raise PermissionError("Symbolic links are not served")
        path.resolve().relative_to(self.root)
        if not path.is_file():
            raise FileNotFoundError(filename)
        return path

    def _open_file(self, path):
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            # Descriptor-relative traversal closes the symlink replacement race.
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = path.relative_to(self.root).parts
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                return os.fdopen(file_fd, "rb")
            finally:
                os.close(fd)
        return self._path(path.relative_to(self.root).as_posix()).open("rb")

    def _reply(self, payload, peer):
        try:
            self._socket.sendto(payload, peer)
        except (OSError, AttributeError):
            pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                packet, peer = self._socket.recvfrom(2048)
            except socket.timeout:
                continue
            except (OSError, AttributeError):
                return
            if len(packet) < 2:
                continue
            opcode = struct.unpack("!H", packet[:2])[0]
            if opcode != 1:
                self._reply(_error(2 if opcode == 2 else 4, "Read-only server" if opcode == 2 else "Illegal operation"), peer)
                continue
            fields = packet[2:].split(b"\0")
            if len(fields) < 3 or fields[-1] != b"" or fields[1].lower() != b"octet":
                self._reply(_error(4, "Only octet mode is supported"), peer)
                continue
            try:
                filename = fields[0].decode("utf-8")
                path = self._path(filename)
            except (ValueError, UnicodeError, PermissionError):
                self._reply(_error(2, "Access denied"), peer)
                continue
            except FileNotFoundError:
                self._reply(_error(1, "File not found"), peer)
                continue
            with self._lock:
                if self._stop.is_set():
                    return
                if peer in self._clients:
                    continue
                if len(self._clients) >= 16:
                    self._reply(_error(0, "Server busy"), peer)
                    continue
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self.host, 0))
                thread = threading.Thread(target=self._transfer, args=(sock, peer, path), daemon=True)
                self._clients[peer] = (sock, thread)
                thread.start()

    def _transfer(self, sock, peer, path):
        try:
            # Recheck immediately before opening in case a path was replaced.
            path = self._path(path.relative_to(self.root).as_posix())
            with self._open_file(path) as file:
                block, sent = 1, 0
                while not self._stop.is_set():
                    data = file.read(512)
                    packet = struct.pack("!HH", 3, block) + data
                    acknowledged = False
                    for attempt in range(self.retries):
                        if self._stop.is_set():
                            return
                        sock.sendto(packet, peer)
                        deadline = time.monotonic() + self.timeout
                        while time.monotonic() < deadline and not self._stop.is_set():
                            sock.settimeout(max(0.001, deadline - time.monotonic()))
                            try:
                                reply, sender = sock.recvfrom(2048)
                            except socket.timeout:
                                break
                            if sender != peer:
                                sock.sendto(_error(5, "Unknown transfer ID"), sender)
                                continue
                            if reply == struct.pack("!HH", 4, block):
                                acknowledged = True
                                break
                            if reply[:2] == b"\x00\x05":
                                return
                        if acknowledged:
                            break
                    if not acknowledged:
                        self._event(f"Transfer timed out: {path.name}")
                        return
                    sent += len(data)
                    if len(data) < 512:
                        self._event(f"Sent {path.name}: {sent} bytes to {peer[0]}")
                        return
                    block = (block + 1) % 65536
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                try:
                    sock.sendto(_error(2, "Cannot read file"), peer)
                except OSError:
                    pass
                self._event(f"Transfer failed: {path.name}")
        finally:
            sock.close()
            with self._lock:
                self._clients.pop(peer, None)
