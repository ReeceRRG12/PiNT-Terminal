import socket
import struct
import time

import pytest

from pint_terminal.tftp import TFTPServer


def request(sock, server, name, opcode=1):
    sock.sendto(struct.pack("!H", opcode) + name.encode() + b"\0octet\0", ("127.0.0.1", server.port))
    return sock.recvfrom(2048)


@pytest.fixture
def tftp(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    server = TFTPServer(root, port=0)
    server.timeout = 0.1
    server.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    yield server, sock, root
    sock.close()
    server.stop()


@pytest.mark.parametrize("size", [0, 511, 512, 1024, 1200])
def test_tftp_binary_round_trip(tftp, size):
    server, sock, root = tftp
    expected = bytes(index % 256 for index in range(size))
    (root / "firmware.bin").write_bytes(expected)
    packet, peer = request(sock, server, "firmware.bin")
    output, block = bytearray(), 1
    while True:
        assert packet[:4] == struct.pack("!HH", 3, block)
        output.extend(packet[4:])
        sock.sendto(struct.pack("!HH", 4, block), peer)
        if len(packet) < 516:
            break
        packet, peer = sock.recvfrom(2048)
        block += 1
    assert bytes(output) == expected


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "folder/../../secret", "..\\secret", "C:/secret"])
def test_tftp_denies_escape(tftp, name):
    server, sock, root = tftp
    packet, _ = request(sock, server, name)
    assert packet[:4] == struct.pack("!HH", 5, 2)


def test_tftp_denies_symlink(tftp, tmp_path):
    server, sock, root = tftp
    secret = tmp_path / "secret"
    secret.write_text("private")
    try:
        (root / "link").symlink_to(secret)
    except OSError:
        pytest.skip("Symlinks unavailable on this platform")
    assert request(sock, server, "link")[0][:4] == struct.pack("!HH", 5, 2)


def test_tftp_denies_writes(tftp):
    server, sock, root = tftp
    assert request(sock, server, "upload", opcode=2)[0][:4] == struct.pack("!HH", 5, 2)
    assert not (root / "upload").exists()


def test_tftp_retries_same_block_and_stops(tftp):
    server, sock, root = tftp
    (root / "file").write_bytes(b"payload")
    first, peer = request(sock, server, "file")
    second, repeated_peer = sock.recvfrom(2048)
    assert second == first and peer == repeated_peer
    server.stop()
    assert not server._clients
