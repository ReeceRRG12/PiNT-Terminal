import asyncio
from pathlib import Path

import asyncssh
import pytest
import telnetlib3

from pint_terminal.models import Host
from pint_terminal.transfers import TransferClient
from pint_terminal.transport import (SessionWorker, UnknownHostKey, ChangedHostKey,
                                     connect_ssh, trust_host_key, _match_known_hosts)


class PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "tester" and password == "correct"


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "localhost"])
async def test_ssh_unknown_trusted_changed_and_bad_password(tmp_path, address):
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(PasswordServer, "127.0.0.1", 0, server_host_keys=[key])
    host = Host(name="Test", address=address, port=server.get_port(), username="tester", password="correct")
    known = tmp_path / "known_hosts"
    known.touch()  # The UI creates this file even before any keys are trusted.
    try:
        with pytest.raises(UnknownHostKey) as error:
            await connect_ssh(host, known)
        assert error.value.fingerprint == key.get_fingerprint()
        trust_host_key(host, known, error.value.public_key)
        connection = await connect_ssh(host, known)
        connection.close()
        await connection.wait_closed()
        host.password = "wrong"
        with pytest.raises(asyncssh.PermissionDenied):
            await connect_ssh(host, known)
        with pytest.raises(ChangedHostKey):
            trust_host_key(host, known, asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode())
        changed = asyncssh.generate_private_key("ssh-ed25519")
        known.write_text(f"[{address}]:{host.port} {changed.export_public_key().decode()}")
        with pytest.raises(ChangedHostKey):
            await connect_ssh(host, known)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("address,port", [("switch.example.test", 22), ("switch.example.test", 2222), ("2001:db8::1", 2222)])
def test_named_host_approval_preserves_existing_and_revoked_trust(tmp_path, address, port):
    host = Host(name="Named device", address=address, port=port)
    known = tmp_path / "known_hosts"
    known.touch()
    key = asyncssh.generate_private_key("ssh-ed25519")
    exported = key.export_public_key().decode()
    trust_host_key(host, known, exported)
    first = known.read_text()
    trust_host_key(host, known, exported)
    assert known.read_text() == first
    replacement = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    with pytest.raises(ChangedHostKey):
        trust_host_key(host, known, replacement)
    assert known.read_text() == first
    revoked = "@revoked " + first
    known.write_text(revoked)
    with pytest.raises(ChangedHostKey):
        trust_host_key(host, known, exported)
    assert known.read_text() == revoked


@pytest.mark.parametrize("address,network", [("192.0.2.10", "192.0.2.0/24"), ("2001:db8::1", "2001:db8::/32")])
def test_literal_ip_approval_honours_cidr_trust_and_revocation(tmp_path, address, network):
    host = Host(name="Literal address", address=address, port=2222)
    known = tmp_path / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    trusted = f"{network} {key}"
    known.write_text(trusted)
    trust_host_key(host, known, key)
    assert known.read_text() == trusted
    known.write_text("@revoked " + trusted)
    with pytest.raises(ChangedHostKey):
        trust_host_key(host, known, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_trust", [False, True])
@pytest.mark.parametrize("revoked_name", ["localhost", "127.0.0.1"])
async def test_ssh_port_specific_revocation_cannot_be_bypassed(tmp_path, fallback_trust, revoked_name):
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(PasswordServer, "127.0.0.1", 0, server_host_keys=[key])
    host = Host(name="Revoked host", address="localhost", port=server.get_port(), username="tester", password="correct")
    known = tmp_path / "known_hosts"
    exported = key.export_public_key().decode()
    known.write_text(f"@revoked [{revoked_name}]:{host.port} {exported}" + (f"localhost {exported}" if fallback_trust else ""))
    try:
        if revoked_name == host.address:
            with pytest.raises(ChangedHostKey):
                trust_host_key(host, known, exported)
        with pytest.raises((ChangedHostKey, asyncssh.HostKeyNotVerifiable)):
            await connect_ssh(host, known)
    finally:
        server.close()
        await server.wait_closed()


def test_default_port_callback_retains_explicit_revocation(tmp_path):
    known = tmp_path / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    known.write_text(f"@revoked [localhost]:22 {key.export_public_key().decode()}")
    result = _match_known_hosts(known, "localhost", "127.0.0.1", None)
    assert key.convert_to_public() in result[2]


@pytest.mark.asyncio
async def test_sftp_hostname_can_be_approved_and_reconnected(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    (root / "example.bin").write_bytes(b"hostname transfer\x00\xff")
    key = asyncssh.generate_private_key("ssh-ed25519")
    server = await asyncssh.create_server(PasswordServer, "127.0.0.1", 0,
        server_host_keys=[key],
        sftp_factory=lambda channel: asyncssh.SFTPServer(channel, chroot=str(root)))
    host = Host(name="Named SFTP", address="localhost", protocol="sftp", port=server.get_port(),
                username="tester", password="correct")
    known = tmp_path / "known_hosts"
    known.touch()
    def transfer():
        with TransferClient(host, known) as client:
            assert "example.bin" in [entry["name"] for entry in client.listdir()]
            client.download("example.bin", tmp_path / "download.bin")
    try:
        with pytest.raises(UnknownHostKey) as error:
            await asyncio.to_thread(transfer)
        trust_host_key(host, known, error.value.public_key)
        await asyncio.to_thread(transfer)
        assert (tmp_path / "download.bin").read_bytes() == (root / "example.bin").read_bytes()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ssh_terminal_output_and_dimensions(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    dimensions = []
    async def shell(process):
        dimensions.append(process.get_terminal_size()[:2])
        process.stdout.write("hello terminal\r\n")
        process.exit(0)
    server = await asyncssh.create_server(PasswordServer, "127.0.0.1", 0, server_host_keys=[key], process_factory=shell)
    host = Host(name="Test", address="127.0.0.1", port=server.get_port(), username="tester", password="correct", auto_reconnect=False)
    known = tmp_path / "known_hosts"
    trust_host_key(host, known, key.export_public_key().decode())
    worker = SessionWorker(host, known)
    worker.resize(110, 42)
    output = []
    worker.output.connect(output.append)
    try:
        result = await worker._session()
        assert "hello terminal" in "".join(output)
        assert dimensions == [(110, 42)]
        assert result == "Session ended (exit 0)"
    finally:
        if worker._connection:
            worker._connection.close()
            await worker._connection.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_telnet_iac_negotiation_and_output(tmp_path):
    async def shell(reader, writer):
        writer.write("login: ")
        await writer.drain()
        writer.close()
    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=shell, connect_maxwait=0.1)
    host = Host(name="Telnet", address="127.0.0.1", protocol="telnet", port=server.sockets[0].getsockname()[1], auto_reconnect=False)
    worker = SessionWorker(host, tmp_path / "known_hosts")
    output = []
    worker.output.connect(output.append)
    try:
        await worker._session()
        assert "login: " in "".join(output)
        assert "\xff" not in "".join(output)
    finally:
        if worker._connection:
            worker._connection.close()
            await worker._connection.wait_closed()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_reconnect_cancel_and_no_disconnected_input(tmp_path):
    host = Host(name="Test", address="127.0.0.1")
    worker = SessionWorker(host, tmp_path / "known_hosts")
    worker._loop = asyncio.get_running_loop()
    calls = []
    async def broken():
        calls.append(1)
        raise OSError("offline")
    worker._session = broken
    worker._task = asyncio.create_task(worker._run_sessions())
    worker.send("must not be buffered")
    await asyncio.sleep(0.02)
    worker.disconnect()
    await worker._task
    assert len(calls) == 1
    assert worker._writer is None


@pytest.mark.asyncio
async def test_normal_logout_does_not_reconnect_and_fatal_error_is_retained(tmp_path):
    worker = SessionWorker(Host(name="Test", address="localhost"), tmp_path / "known_hosts")
    calls = []
    async def logout():
        calls.append(1)
        return "Session ended (exit 0)"
    worker._session = logout
    await worker._run_sessions()
    assert calls == [1]
    assert worker._final_status == "Session ended (exit 0)"
    async def bad_key():
        raise ChangedHostKey("Changed identity")
    worker._session = bad_key
    await worker._run_sessions()
    assert "Changed identity" in worker._final_status
