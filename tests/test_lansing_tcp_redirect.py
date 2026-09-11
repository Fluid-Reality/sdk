"""Transparent SDK redirection to a Lansing simulator over localhost TCP."""

from __future__ import annotations

import hashlib
import socket
import ssl
import threading
import time
from contextlib import contextmanager

import pytest

from apps.lansing_simulator.simulator import (
    LansingTcpServer,
    SimulatorConfig,
)
from fluid_reality import Lansing, TransportError, is_virtual_port, list_ports
from fluid_reality.transport import SerialTransport, VIRTUAL_PORTS_ENV


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
        monkeypatch.setenv(VIRTUAL_PORTS_ENV, f"COM66={server.endpoint}")
        yield server.endpoint


def test_existing_lansing_call_is_transparently_redirected(monkeypatch):
    with _tcp_simulator(monkeypatch):
        with Lansing("COM66", timeout=0.5) as board:
            assert board.transport.redirected is True
            assert board.transport.port == "COM66"
            assert board.firmware_version().firmware == "Lansing"

            board.psu_on()
            board.psc_on()
            board.enter_stream_mode()
            board.stream_actuator(7, 233)
            board.exit_stream_mode()
            time.sleep(0.02)

            assert board.get_actuator(7) == 233


def test_tcp_endpoint_can_be_passed_as_the_existing_port_argument(monkeypatch):
    with _tcp_simulator(monkeypatch) as configured_endpoint:
        monkeypatch.delenv(VIRTUAL_PORTS_ENV)
        with Lansing(configured_endpoint, timeout=0.5) as board:
            assert board.transport.endpoint == configured_endpoint
            assert board.firmware_version().firmware == "Lansing"


def test_tcp_redirect_preserves_read_timeout(monkeypatch):
    with _tcp_simulator(monkeypatch, response_delay_s=0.2):
        with Lansing("COM66", timeout=0.03) as board:
            with pytest.raises(TransportError, match="Timed out"):
                board.version()


def test_tcp_connect_timeout_is_separate_from_command_timeout(monkeypatch):
    opened: dict[str, object] = {}

    class FakeSocket:
        timeout = None

        def settimeout(self, value):
            self.timeout = value

        def close(self):
            pass

    fake_socket = FakeSocket()

    def create_connection(address, *, timeout):
        opened.update(address=address, timeout=timeout)
        return fake_socket

    monkeypatch.setattr(
        "fluid_reality.transport.socket.create_connection", create_connection
    )

    transport = SerialTransport(
        "tcp://127.0.0.1:49765", timeout=7.0, connect_timeout=0.4
    )
    try:
        assert opened == {"address": ("127.0.0.1", 49765), "timeout": 0.4}
        assert fake_socket.timeout == 7.0
    finally:
        transport.close()


def test_tcp_redirect_rejects_invalid_endpoint(monkeypatch):
    monkeypatch.setenv(VIRTUAL_PORTS_ENV, "COM66=http://127.0.0.1:49765")

    with pytest.raises(TransportError, match="expected tcp://host:port"):
        Lansing("COM66")


@pytest.mark.parametrize(
    "mapping",
    [
        "COM66=tcp://127.0.0.1:not-a-port",
        "COM66=tcp://127.0.0.1:49765/path",
        "missing-equals-sign",
    ],
)
def test_virtual_port_mapping_rejects_malformed_values(monkeypatch, mapping):
    monkeypatch.setenv(VIRTUAL_PORTS_ENV, mapping)
    with pytest.raises(TransportError):
        list_ports()


def test_tcp_redirect_reports_remote_disconnect(monkeypatch):
    server = socket.create_server(("127.0.0.1", 0))
    host, port = server.getsockname()
    monkeypatch.setenv(VIRTUAL_PORTS_ENV, f"COM66=tcp://{host}:{port}")

    def disconnect() -> None:
        connection, _address = server.accept()
        connection.close()
        server.close()

    thread = threading.Thread(target=disconnect, daemon=True)
    thread.start()
    with Lansing("COM66", timeout=0.2) as board:
        with pytest.raises(TransportError, match="Could not read from transport"):
            board.version()
    thread.join(timeout=1.0)


def test_list_ports_combines_serial_ports_and_endpoint_aliases(monkeypatch):
    class Port:
        def __init__(self, device: str) -> None:
            self.device = device

    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [Port("COM1"), Port("COM2")],
    )
    monkeypatch.setenv(
        VIRTUAL_PORTS_ENV,
        "COM66=tcp://127.0.0.1:49765;SIM2=tcp://127.0.0.1:8766",
    )

    assert list_ports() == ["COM1", "COM2", "COM66", "SIM2"]
    assert is_virtual_port("com66") is True
    assert is_virtual_port("COM1") is False


def test_list_ports_omits_unconfigured_tcp_endpoint(monkeypatch):
    class Port:
        device = "COM1"

    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [Port()])
    monkeypatch.delenv(VIRTUAL_PORTS_ENV, raising=False)

    assert list_ports() == ["COM1"]


def test_unmapped_com_port_uses_physical_serial(monkeypatch):
    opened: dict[str, object] = {}

    class FakeSerial:
        timeout = 0.5

        def __init__(self, **kwargs):
            opened.update(kwargs)

        def close(self):
            pass

    monkeypatch.setenv(VIRTUAL_PORTS_ENV, "COM66=tcp://127.0.0.1:49765")
    monkeypatch.setattr("serial.Serial", FakeSerial)

    transport = SerialTransport("COM9", timeout=0.5)
    try:
        assert transport.redirected is False
        assert opened["port"] == "COM9"
    finally:
        transport.close()


def test_tcp_transport_authenticates_without_logging_token():
    server = socket.create_server(("127.0.0.1", 0))
    host, port = server.getsockname()
    received: list[bytes] = []

    def authenticate() -> None:
        connection, _address = server.accept()
        with connection:
            received.append(connection.recv(256))
            connection.sendall(b"OK:NET,OP>AUTH\n")
        server.close()

    thread = threading.Thread(target=authenticate, daemon=True)
    thread.start()
    transport = SerialTransport(f"tcp://{host}:{port}", network_token="secret", timeout=0.5)
    transport.close()
    thread.join(timeout=1.0)

    assert received == [b"NET AUTH secret\n"]


def test_tcp_transport_rejects_failed_authentication():
    server = socket.create_server(("127.0.0.1", 0))
    host, port = server.getsockname()

    def reject() -> None:
        connection, _address = server.accept()
        with connection:
            connection.recv(256)
            connection.sendall(b"ER:NET,OP>AUTH,REASON>FAILED\n")
        server.close()

    thread = threading.Thread(target=reject, daemon=True)
    thread.start()
    with pytest.raises(TransportError, match="authentication failed"):
        SerialTransport(f"tcp://{host}:{port}", network_token="wrong", timeout=0.5)
    thread.join(timeout=1.0)


def test_redirected_transport_drains_delayed_text_mode_response():
    server = socket.create_server(("127.0.0.1", 0))
    host, port = server.getsockname()

    def delayed_response() -> None:
        connection, _address = server.accept()
        with connection:
            connection.recv(256)
            time.sleep(0.12)
            connection.sendall(b"ER:BAD_COMMAND\n")
            time.sleep(0.35)
        server.close()

    thread = threading.Thread(target=delayed_response, daemon=True)
    thread.start()
    transport = SerialTransport(f"tcp://{host}:{port}", timeout=0.5)
    board = Lansing(transport=transport)
    try:
        assert board.force_text_mode() == ("ER:BAD_COMMAND",)
    finally:
        board.close()
    thread.join(timeout=1.0)


def test_tls_transport_accepts_a_pinned_sha256_fingerprint(monkeypatch):
    certificate_der = b"test board certificate"

    class FakeSocket:
        def __init__(self):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def gettimeout(self):
            return self.timeout

        def getpeercert(self, *, binary_form=False):
            return certificate_der if binary_form else {}

        def close(self):
            pass

    class FakeContext:
        check_hostname = True
        verify_mode = None

        def wrap_socket(self, raw_socket, *, server_hostname):
            assert server_hostname == "rockford.local"
            return raw_socket

    monkeypatch.setattr("fluid_reality.transport.socket.create_connection", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("fluid_reality.transport.ssl.SSLContext", lambda _protocol: FakeContext())
    fingerprint = hashlib.sha256(certificate_der).hexdigest()

    transport = SerialTransport(
        "tls://rockford.local:49765",
        tls_fingerprint=fingerprint,
        timeout=0.5,
    )
    transport.close()


def test_tls_transport_can_skip_hostname_check_without_disabling_trust(monkeypatch):
    class FakeSocket:
        def settimeout(self, _value):
            pass

        def close(self):
            pass

    class FakeContext:
        def __init__(self):
            self.check_hostname = True

        def wrap_socket(self, raw_socket, *, server_hostname):
            assert server_hostname == "10.0.6.143"
            return raw_socket

    context = FakeContext()
    monkeypatch.setattr(
        "fluid_reality.transport.socket.create_connection",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    monkeypatch.setattr(
        "fluid_reality.transport.ssl.create_default_context",
        lambda **_kwargs: context,
    )

    transport = SerialTransport(
        "tls://10.0.6.143:49765",
        tls_ca_file="shared.pem",
        tls_check_hostname=False,
        timeout=0.5,
    )

    assert context.check_hostname is False
    transport.close()


def test_tls_transport_can_encrypt_without_verifying_a_certificate(monkeypatch):
    class FakeSocket:
        def settimeout(self, _value):
            pass

        def close(self):
            pass

    class FakeContext:
        def __init__(self):
            self.check_hostname = True
            self.verify_mode = None

        def wrap_socket(self, raw_socket, *, server_hostname):
            assert server_hostname == "10.0.6.143"
            return raw_socket

    context = FakeContext()
    monkeypatch.setattr(
        "fluid_reality.transport.socket.create_connection",
        lambda *_args, **_kwargs: FakeSocket(),
    )
    monkeypatch.setattr(
        "fluid_reality.transport.ssl.SSLContext", lambda _protocol: context,
    )

    transport = SerialTransport(
        "tls://10.0.6.143:49765",
        tls_verify_certificate=False,
        timeout=0.5,
    )

    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE
    transport.close()


def test_tls_transport_rejects_the_wrong_pinned_fingerprint(monkeypatch):
    class FakeSocket:
        def settimeout(self, _value):
            pass

        def getpeercert(self, *, binary_form=False):
            return b"actual certificate" if binary_form else {}

        def close(self):
            pass

    class FakeContext:
        check_hostname = True
        verify_mode = None

        def wrap_socket(self, raw_socket, *, server_hostname):
            return raw_socket

    monkeypatch.setattr("fluid_reality.transport.socket.create_connection", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("fluid_reality.transport.ssl.SSLContext", lambda _protocol: FakeContext())

    with pytest.raises(TransportError, match="fingerprint does not match"):
        SerialTransport(
            "tls://rockford.local:49765",
            tls_fingerprint="00" * 32,
            timeout=0.5,
        )
