"""Tests for the SDK's reusable raw TCP device listener."""

from __future__ import annotations

import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_reality import TcpDeviceListener


def create_test_tls_credentials(directory: Path) -> tuple[Path, Path]:
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    del cryptography
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "bridge-cert.pem"
    key_path = directory / "bridge-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def test_listener_carries_text_and_binary_bytes_without_framing():
    received: list[bytes] = []
    with TcpDeviceListener(port=0) as listener:
        def device() -> None:
            with listener.accept(timeout=0.5) as connection:
                first = connection.read_bytes(2)
                second = connection.read_bytes(4096)
                received.extend((first, second))
                connection.write_bytes(first + second)

        thread = threading.Thread(target=device)
        thread.start()
        with socket.create_connection(listener.address, timeout=0.5) as client:
            client.sendall(b"A\x00")
            client.sendall(bytes([255, 0, 7, 233]))
            echoed = bytearray()
            while len(echoed) < 6:
                echoed.extend(client.recv(6 - len(echoed)))
        thread.join(timeout=1.0)

    assert b"".join(received) == b"A\x00\xff\x00\x07\xe9"
    assert bytes(echoed) == b"A\x00\xff\x00\x07\xe9"


def test_listener_accept_timeout_and_repeated_cleanup():
    for _ in range(3):
        with TcpDeviceListener(port=0) as listener:
            assert listener.endpoint.startswith("tcp://127.0.0.1:")
            with pytest.raises(TimeoutError):
                listener.accept(timeout=0.01)


def test_connection_read_returns_empty_after_disconnect():
    with TcpDeviceListener(port=0) as listener:
        client = socket.create_connection(listener.address, timeout=0.5)
        with listener.accept(timeout=0.5) as connection:
            client.close()
            assert connection.read_bytes() == b""


def test_tls_listener_negotiates_and_carries_raw_bytes(tmp_path: Path):
    certificate, private_key = create_test_tls_credentials(tmp_path)
    received: list[bytes] = []
    with TcpDeviceListener(
        port=0,
        tls_certfile=str(certificate),
        tls_keyfile=str(private_key),
    ) as listener:
        assert listener.endpoint.startswith("tls://127.0.0.1:")

        def device() -> None:
            with listener.accept(timeout=1.0) as connection:
                received.append(connection.read_bytes(4096))
                connection.write_bytes(b"OK\n")

        thread = threading.Thread(target=device)
        thread.start()
        context = ssl.create_default_context(cafile=str(certificate))
        with socket.create_connection(listener.address, timeout=1.0) as raw_client:
            with context.wrap_socket(raw_client, server_hostname="localhost") as client:
                client.sendall(b"CUR\n")
                assert client.recv(3) == b"OK\n"
        thread.join(timeout=1.0)

    assert received == [b"CUR\n"]
