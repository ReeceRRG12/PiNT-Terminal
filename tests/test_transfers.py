import asyncio
import builtins
import os
import threading

import asyncssh
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler, ThrottledDTPHandler
from pyftpdlib.servers import FTPServer

from pint_terminal.models import Host
from pint_terminal.transport import UnknownHostKey, trust_host_key
from pint_terminal.transfers import TransferClient, TransferCancelled


def start_ftp_server(server):
    stop = threading.Event()
    def serve():
        try:
            while not stop.is_set():
                server.ioloop.loop(timeout=0.05, blocking=False)
        finally:
            server.close_all()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return stop, thread


def exercise_transfer(host, known, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 1000)
    progress = []
    with TransferClient(host, known) as client:
        client.mkdir("folder")
        client.upload(source, "folder/remote.bin", lambda done, total: progress.append((done, total)))
        assert any(row["name"] == "folder" and row["is_dir"] for row in client.listdir("."))
        assert client.listdir("folder")[0]["size"] == source.stat().st_size
        result = tmp_path / "download.bin"
        client.download("folder/remote.bin", result)
        assert result.read_bytes() == source.read_bytes()
        with pytest.raises(Exception):
            client.download("missing.bin", result)
        assert result.read_bytes() == source.read_bytes()
        assert not list(tmp_path.glob("*.part"))
        assert progress[-1] == (source.stat().st_size, source.stat().st_size)
        client.delete("folder/remote.bin")
        client.delete("folder", is_dir=True)
        assert client.listdir(".") == []


@pytest.mark.parametrize("legacy", [False, True])
def test_ftp_round_trip(tmp_path, legacy):
    root = tmp_path / "ftp"
    root.mkdir()
    authoriser = DummyAuthorizer()
    authoriser.add_user("tester", "password", str(root), perm="elradfmwMT")
    class Handler(FTPHandler):
        authorizer = authoriser
        def ftp_MLSD(self, path):
            if legacy:
                self.respond("502 MLSD unavailable")
            else:
                super().ftp_MLSD(path)
    server = FTPServer(("127.0.0.1", 0), Handler)
    stop, thread = start_ftp_server(server)
    host = Host(name="FTP", address="127.0.0.1", protocol="ftp", port=server.socket.getsockname()[1], username="tester", password="password")
    try:
        exercise_transfer(host, tmp_path / "known_hosts", tmp_path)
    finally:
        stop.set()
        thread.join(2)


@pytest.mark.asyncio
async def test_sftp_round_trip_and_unknown_key(tmp_path):
    root = tmp_path / "sftp"
    root.mkdir()
    key = asyncssh.generate_private_key("ssh-ed25519")
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False
    server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key],
                    sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(root)))
    host = Host(name="SFTP", address="127.0.0.1", protocol="sftp", port=server.get_port())
    known = tmp_path / "known_hosts"
    def unknown():
        with pytest.raises(UnknownHostKey):
            with TransferClient(host, known):
                pass
    try:
        await asyncio.to_thread(unknown)
        trust_host_key(host, known, key.export_public_key().decode())
        await asyncio.to_thread(exercise_transfer, host, known, tmp_path)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_sftp_download_preserves_existing_file(tmp_path, interruption, monkeypatch):
    root = tmp_path / "server"
    root.mkdir()
    (root / "remote.bin").write_bytes(b"replacement" * 1000)
    local = tmp_path / "local.bin"
    local.write_bytes(b"keep existing file")
    partial_handles = []
    real_open, real_unlink = builtins.open, os.unlink
    def track_open(path, *args, **kwargs):
        file = real_open(path, *args, **kwargs)
        if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).endswith(".part"):
            partial_handles.append((os.fsdecode(path), file))
        return file
    def unlink_closed_file(path, *args, **kwargs):
        name = os.fsdecode(path)
        assert all(file.closed for opened, file in partial_handles if opened == name), "Temporary download file must be closed before deletion"
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", track_open)
    monkeypatch.setattr(os, "unlink", unlink_closed_file)
    key = asyncssh.generate_private_key("ssh-ed25519")
    reading, release = asyncio.Event(), asyncio.Event()
    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False
    class SlowSFTP(asyncssh.SFTPServer):
        async def read(self, file_obj, offset, size):
            reading.set()
            await release.wait()
            return super().read(file_obj, offset, size)
    server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=[key],
                     sftp_factory=lambda channel: SlowSFTP(channel, chroot=str(root)))
    host = Host(name="SFTP", address="127.0.0.1", protocol="sftp", port=server.get_port())
    known = tmp_path / "known_hosts"
    trust_host_key(host, known, key.export_public_key().decode())
    holder = {}
    def download():
        with TransferClient(host, known) as client:
            holder["client"] = client
            if interruption == "timeout":
                client.timeout = 0.1
            client.download("remote.bin", local)
    task = asyncio.create_task(asyncio.to_thread(download))
    try:
        await asyncio.wait_for(reading.wait(), 5)
        if interruption == "cancel":
            holder["client"].cancel()
        with pytest.raises(TransferCancelled if interruption == "cancel" else TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 5)
        assert local.read_bytes() == b"keep existing file"
        assert partial_handles and all(file.closed for _, file in partial_handles)
        assert not list(tmp_path.glob("*.part"))
    finally:
        release.set()
        if "client" in holder:
            holder["client"].cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()


def test_cancel_ftp_download_preserves_existing_file(tmp_path):
    root = tmp_path / "server"
    root.mkdir()
    (root / "remote.bin").write_bytes(b"replacement" * 100000)
    local = tmp_path / "local.bin"
    local.write_bytes(b"keep existing file")
    authoriser = DummyAuthorizer()
    authoriser.add_user("tester", "password", str(root), perm="elr")
    class SlowData(ThrottledDTPHandler):
        write_limit = 4096
    class Handler(FTPHandler):
        authorizer = authoriser
        dtp_handler = SlowData
    server = FTPServer(("127.0.0.1", 0), Handler)
    stop, server_thread = start_ftp_server(server)
    host = Host(name="FTP", address="127.0.0.1", protocol="ftp", port=server.socket.getsockname()[1], username="tester", password="password")
    reading = threading.Event()
    errors, holder = [], {}
    def download():
        try:
            with TransferClient(host, tmp_path / "known_hosts") as client:
                holder["client"] = client
                client.download("remote.bin", local, lambda *_: reading.set())
        except Exception as error:
            errors.append(error)
    download_thread = threading.Thread(target=download, daemon=True)
    download_thread.start()
    try:
        assert reading.wait(5), "FTP download did not start"
        holder["client"].cancel()
        download_thread.join(5)
        assert not download_thread.is_alive(), "FTP cancellation did not unblock the transfer"
        assert errors, "Cancelled download unexpectedly completed"
        assert local.read_bytes() == b"keep existing file"
        assert not list(tmp_path.glob("*.part"))
    finally:
        if "client" in holder:
            holder["client"].cancel()
        stop.set()
        server_thread.join(2)
        download_thread.join(2)
