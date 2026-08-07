"""End-to-end tests for transparent SDK redirection to the Lansing TCP simulator."""

from __future__ import annotations

import socket
import time

import pytest

from apps.lansing_simulator.simulator import LansingTcpServer, SimulatorConfig
from fluid_reality import Lansing, TransportError


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        name="TCP integration board",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={},
    )


@pytest.fixture
def tcp_server():
    with LansingTcpServer(_config(), port=0) as server:
        yield server


def _connect(server: LansingTcpServer, *, timeout: float = 0.5) -> socket.socket:
    connection = socket.create_connection(server.address, timeout=timeout)
    connection.settimeout(timeout)
    return connection


def _receive_lines(connection: socket.socket, count: int) -> list[bytes]:
    buffer = bytearray()
    while buffer.count(b"\n") < count:
        buffer.extend(connection.recv(4096))
    lines = bytes(buffer).splitlines(keepends=True)
    return lines[:count]


def test_sdk_lansing_port_call_is_transparently_redirected(tcp_server, monkeypatch):
    monkeypatch.setenv("FLUID_REALITY_TRANSPORT", tcp_server.endpoint)

    with Lansing("THIS-PORT-IS-INTENTIONALLY-IGNORED", timeout=0.5) as board:
        version = board.firmware_version()
        assert version.firmware == "Lansing"
        assert version.version == "0.1"
        assert board.transport.redirected is True
        assert board.transport.port == "THIS-PORT-IS-INTENTIONALLY-IGNORED"
        assert board.transport.endpoint == tcp_server.endpoint


def test_fragmented_text_command(tcp_server):
    with _connect(tcp_server) as connection:
        for fragment in (b"V", b"E", b"R", b"\n"):
            connection.sendall(fragment)
        assert _receive_lines(connection, 1) == [b"OK:FW>Lansing,VERSION>0.1,PROTO>0.1\r\n"]


def test_multiple_commands_in_one_read_and_multiple_status_response_lines(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"VER\nCUR\n")
        lines = _receive_lines(connection, 2)
        assert lines[0].startswith(b"OK:FW>Lansing")
        assert lines[1] == b"OK:0.000\r\n"

        connection.sendall(b"STS\n")
        status_lines = _receive_lines(connection, 7)
        assert len(status_lines) == 7
        assert status_lines[0].startswith(b"OK:PSU>OFF,PSC>OFF")
        assert status_lines[-1].startswith(b"OK:DISCHARGE_MS_LEFT>")
        assert all(line.endswith(b"\r\n") for line in status_lines)


def test_fragmented_binary_stream_packet(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"STR\n")
        assert _receive_lines(connection, 1) == [b"OK:STR\r\n"]
        connection.sendall(bytes([7]))
        time.sleep(0.01)
        connection.sendall(bytes([0xE9]))
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if tcp_server.last_engine and tcp_server.last_engine.values[7] == 0xE9:
                break
            time.sleep(0.005)
        assert tcp_server.last_engine is not None
        assert tcp_server.last_engine.values[7] == 0xE9


def test_stream_mode_exit_restores_text_framing(tcp_server):
    with _connect(tcp_server) as connection:
        connection.sendall(b"STR\n")
        assert _receive_lines(connection, 1) == [b"OK:STR\r\n"]
        connection.sendall(bytes([255]))
        connection.sendall(bytes([0]))
        connection.sendall(b"VER\n")
        assert _receive_lines(connection, 1)[0].startswith(b"OK:FW>Lansing")


def test_sdk_read_timeout(tcp_server, monkeypatch):
    monkeypatch.setenv("FLUID_REALITY_TRANSPORT", tcp_server.endpoint)
    with Lansing("IGNORED", timeout=0.03) as board:
        # A complete command is deliberately not sent, so the engine has no response.
        board.transport.write_bytes(b"VE")
        with pytest.raises(TransportError, match="Timed out"):
            board.transport.read_line()


def test_client_disconnect_does_not_terminate_server(tcp_server):
    connection = _connect(tcp_server)
    connection.sendall(b"VER\n")
    assert _receive_lines(connection, 1)[0].startswith(b"OK:FW>Lansing")
    connection.close()

    deadline = time.monotonic() + 0.5
    while tcp_server._connection is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert tcp_server._thread is not None and tcp_server._thread.is_alive()


def test_accepts_new_sdk_connection_after_disconnect(tcp_server, monkeypatch):
    monkeypatch.setenv("FLUID_REALITY_TRANSPORT", tcp_server.endpoint)
    with Lansing("FIRST", timeout=0.5) as first:
        assert first.version()["FW"] == "Lansing"

    with Lansing("SECOND", timeout=0.5) as second:
        assert second.current() == 0.0
        assert second.transport.redirected is True
