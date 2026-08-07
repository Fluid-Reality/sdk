"""Transparent SDK redirection to a Lansing simulator over localhost TCP."""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from apps.lansing_simulator.simulator import (
    LansingTcpServer,
    SimulatorConfig,
)
from fluid_reality import Lansing, TransportError, list_ports
from fluid_reality.transport import TRANSPORT_OVERRIDE_ENV


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        name="TCP integration test board",
        psu_voltage_v=200.0,
        psu_voltage_noise_v=0.0,
        psu_base_current_ma=1.0,
        psu_base_current_noise_ma=0.0,
        actuators={},
    )


@contextmanager
def _tcp_simulator(monkeypatch, *, response_delay_s: float = 0.0):
    with LansingTcpServer(
        _config(),
        port=0,
        response_delay_s=response_delay_s,
    ) as server:
        monkeypatch.setenv(TRANSPORT_OVERRIDE_ENV, server.endpoint)
        yield


def test_existing_lansing_call_is_transparently_redirected(monkeypatch):
    with _tcp_simulator(monkeypatch):
        with Lansing("THIS-PORT-DOES-NOT-EXIST", timeout=0.5) as board:
            assert board.transport.redirected is True
            assert board.transport.port == "THIS-PORT-DOES-NOT-EXIST"
            assert board.firmware_version().firmware == "Lansing"

            board.psu_on()
            board.psc_on()
            board.enter_stream_mode()
            board.stream_actuator(7, 233)
            board.exit_stream_mode()
            time.sleep(0.02)

            assert board.get_actuator(7) == 233


def test_tcp_endpoint_can_be_passed_as_the_existing_port_argument(monkeypatch):
    with _tcp_simulator(monkeypatch):
        configured_endpoint = os.environ[TRANSPORT_OVERRIDE_ENV]
        monkeypatch.delenv(TRANSPORT_OVERRIDE_ENV)
        with Lansing(configured_endpoint, timeout=0.5) as board:
            assert board.transport.endpoint == configured_endpoint
            assert board.firmware_version().firmware == "Lansing"


def test_tcp_redirect_preserves_read_timeout(monkeypatch):
    with _tcp_simulator(monkeypatch, response_delay_s=0.2):
        with Lansing("IGNORED", timeout=0.03) as board:
            with pytest.raises(TransportError, match="Timed out"):
                board.version()


def test_tcp_redirect_rejects_invalid_endpoint(monkeypatch):
    monkeypatch.setenv(TRANSPORT_OVERRIDE_ENV, "http://127.0.0.1:8765")

    with pytest.raises(TransportError, match="expected tcp://host:port"):
        Lansing("IGNORED")


def test_tcp_redirect_reports_remote_disconnect(monkeypatch):
    server = socket.create_server(("127.0.0.1", 0))
    host, port = server.getsockname()
    monkeypatch.setenv(TRANSPORT_OVERRIDE_ENV, f"tcp://{host}:{port}")

    def disconnect() -> None:
        connection, _address = server.accept()
        connection.close()
        server.close()

    thread = threading.Thread(target=disconnect, daemon=True)
    thread.start()
    with Lansing("IGNORED", timeout=0.2) as board:
        with pytest.raises(TransportError, match="Could not read from transport"):
            board.version()
    thread.join(timeout=1.0)


def test_list_ports_combines_serial_ports_and_tcp_override(monkeypatch):
    class Port:
        def __init__(self, device: str) -> None:
            self.device = device

    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [Port("COM1"), Port("COM2")],
    )
    monkeypatch.setenv(TRANSPORT_OVERRIDE_ENV, "tcp://127.0.0.1:8765")

    assert list_ports() == ["COM1", "COM2", "tcp://127.0.0.1:8765"]


def test_list_ports_omits_unconfigured_tcp_endpoint(monkeypatch):
    class Port:
        device = "COM1"

    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [Port()])
    monkeypatch.delenv(TRANSPORT_OVERRIDE_ENV, raising=False)

    assert list_ports() == ["COM1"]
