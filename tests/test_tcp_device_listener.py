"""Tests for the SDK's reusable raw TCP device listener."""

from __future__ import annotations

import socket
import threading

import pytest

from fluid_reality import TcpDeviceListener


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
