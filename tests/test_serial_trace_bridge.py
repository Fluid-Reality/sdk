from __future__ import annotations

import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.device_bridge.bridge import DeviceBridge, DeviceBridgeBoard, TraceEvent
from apps.device_bridge.bridge_network import (
    BridgeNetProtocol,
    BridgeNetworkSettings,
    HostInterface,
)
from fluid_reality import FirmwareError, NetworkBoard
from fluid_reality.transport import SerialTransport


class FakeSerial:
    def __init__(self, **settings: object) -> None:
        self.settings = settings
        self.writes: list[bytes] = []
        self._reads: deque[bytes] = deque()
        self._condition = threading.Condition()
        self.closed = False

    def read(self, _maximum: int) -> bytes:
        with self._condition:
            self._condition.wait_for(lambda: self._reads or self.closed, timeout=0.05)
            return self._reads.popleft() if self._reads else b""

    def write(self, data: bytes) -> int:
        with self._condition:
            self.writes.append(bytes(data))
            self._condition.notify_all()
        return len(data)

    def flush(self) -> None:
        pass

    def inject(self, data: bytes) -> None:
        with self._condition:
            self._reads.append(bytes(data))
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._condition.notify_all()

    def wait_for_writes(self, count: int) -> None:
        with self._condition:
            assert self._condition.wait_for(lambda: len(self.writes) >= count, timeout=1.0)


def receive_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        result.extend(connection.recv(size - len(result)))
    return bytes(result)


def create_test_tls_credentials(directory: Path) -> tuple[Path, Path]:
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

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


def test_trace_event_formats_hex_and_ascii() -> None:
    event = TraceEvent("TX", b"CUR\n\xff", datetime(2026, 8, 8, tzinfo=timezone.utc))

    formatted = event.format()

    assert "TX" in formatted
    assert "43 55 52 0A FF" in formatted
    assert "CUR.." in formatted


def test_device_bridge_board_is_locally_configurable_network_board() -> None:
    assert issubclass(DeviceBridgeBoard, NetworkBoard)
    assert DeviceBridgeBoard.supports_network_configuration() is True
    assert DeviceBridgeBoard.declared_network_interfaces() == ("HOST",)


def test_bridge_reports_and_describes_every_available_host_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interfaces = (
        HostInterface("ETHERNET_3", "Ethernet 3", "10.0.1.7", "58-11-22-AC-AA-6B"),
        HostInterface(
            "VETHERNET_DEFAULT_SWITCH",
            "vEthernet (Default Switch)",
            "172.24.64.1",
            "00-15-5D-68-AA-11",
        ),
    )
    monkeypatch.setattr(
        "apps.device_bridge.bridge_network.available_host_interfaces",
        lambda: interfaces,
    )
    protocol = BridgeNetProtocol(BridgeNetworkSettings(), None)

    listed, restart = protocol.handle(b"NET IF LIST\n")
    status, _ = protocol.handle(b"NET IF ETHERNET_3 STATUS\n")
    configured, configure_restart = protocol.handle(
        b"NET TCP SET KEEP KEEP ETHERNET_3\n"
    )
    token_response, token_restart = protocol.handle(
        b"NET KEY SET 0123456789abcdef0123456789abcdef\n"
    )

    assert restart is False
    assert b"IFACES>HOST|ETHERNET_3|VETHERNET_DEFAULT_SWITCH" in listed
    assert b"IF>ETHERNET_3" in status
    assert b"IP>10.0.1.7" in status
    assert b"MAC>58-11-22-AC-AA-6B" in status
    assert configure_restart is True
    assert b"BIND>ETHERNET_3" in configured
    assert protocol.settings.host == "10.0.1.7"
    assert token_restart is False
    assert b"TOKEN>0123456789abcdef0123456789abcdef" in token_response
    assert protocol.settings.network_token == "0123456789abcdef0123456789abcdef"


def test_bridge_forwards_raw_bytes_and_accepts_reconnection() -> None:
    fake = FakeSerial()
    traces: list[TraceEvent] = []
    statuses: list[str] = []
    bridge = DeviceBridge(
        "COM9",
        port=0,
        serial_factory=lambda **_settings: fake,
        trace=traces.append,
        status=statuses.append,
    )

    with bridge:
        assert fake.settings == {}
        with socket.create_connection(bridge.address, timeout=1.0) as client:
            client.sendall(b"OUT 0 255 0\n" + bytes([0, 255]))
            fake.wait_for_writes(1)
            assert b"".join(fake.writes) == b"OUT 0 255 0\n\x00\xff"

            response = b"OK:OUT\r\n" + bytes([255, 0])
            fake.inject(response)
            assert receive_exact(client, len(response)) == response

        deadline = time.monotonic() + 1.0
        while "Client disconnected; waiting for another client" not in statuses:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        previous_writes = len(fake.writes)
        with socket.create_connection(bridge.address, timeout=1.0) as second_client:
            second_client.sendall(b"CUR\n")
            fake.wait_for_writes(previous_writes + 1)
            assert fake.writes[-1] == b"CUR\n"

    assert b"".join(event.data for event in traces if event.direction == "TX") == (
        b"OUT 0 255 0\n\x00\xffCUR\n"
    )
    assert b"".join(event.data for event in traces if event.direction == "RX") == (
        b"OK:OUT\r\n\xff\x00"
    )
    assert fake.closed


def test_bridge_passes_serial_settings_and_closes_on_stop() -> None:
    created: list[FakeSerial] = []

    def factory(**settings: object) -> FakeSerial:
        instance = FakeSerial(**settings)
        created.append(instance)
        return instance

    bridge = DeviceBridge("COM12", baudrate=115200, port=0, serial_factory=factory)
    bridge.start()
    bridge.stop()

    assert created[0].settings == {
        "port": "COM12",
        "baudrate": 115200,
        "timeout": 0.1,
        "write_timeout": 1.0,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    assert created[0].closed


def test_bridge_handles_sdk_token_authentication_without_forwarding_it() -> None:
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        port=0,
        network_token="bridge-secret",
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        with socket.create_connection(bridge.address, timeout=1.0) as client:
            client.sendall(b"NET AUTH bridge-secret\n")
            assert receive_exact(client, len(b"OK:NET,OP>AUTH\n")) == b"OK:NET,OP>AUTH\n"
            client.sendall(b"CUR\n")
            fake.wait_for_writes(1)

    assert fake.writes == [b"CUR\n"]


def test_bridge_rejects_invalid_token_without_touching_serial() -> None:
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        port=0,
        network_token="bridge-secret",
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        with socket.create_connection(bridge.address, timeout=1.0) as client:
            client.sendall(b"NET AUTH wrong\n")
            response = receive_exact(
                client, len(b"ER:NET,OP>AUTH,REASON>DENIED\n")
            )

    assert response == b"ER:NET,OP>AUTH,REASON>DENIED\n"
    assert fake.writes == []


def test_sdk_authenticated_tcp_transport_operates_through_bridge() -> None:
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        port=0,
        network_token="bridge-secret",
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        transport = SerialTransport(
            bridge.endpoint,
            network_token="bridge-secret",
            timeout=1.0,
        )
        try:
            transport.write_line("CUR")
            fake.wait_for_writes(1)
            fake.inject(b"OK:1.23\n")
            assert transport.read_line() == "OK:1.23"
        finally:
            transport.close()

    assert fake.writes == [b"CUR\n"]


def test_bridge_opens_network_board_over_authenticated_tcp() -> None:
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        port=0,
        network_token="bridge-secret",
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        board = bridge.open_board(timeout=1.0)
        try:
            board.transport.write_line("CUR")
            fake.wait_for_writes(1)
            fake.inject(b"OK:1.23\n")
            assert board.transport.read_line() == "OK:1.23"
        finally:
            board.close()

    assert fake.writes == [b"CUR\n"]


def test_bridge_processes_net_commands_locally_and_rotates_config(tmp_path: Path) -> None:
    fake = FakeSerial()
    config = tmp_path / "device_bridge.json"
    bridge = DeviceBridge(
        "COM9",
        port=0,
        config_file=str(config),
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        board = bridge.open_board(timeout=1.0)
        try:
            status = board.network_status()
            interfaces = board.network_interfaces()
            features = board.network_configuration_features
            hostname = board.network_hostname("lab-bridge")
            with pytest.raises(FirmwareError) as unsupported:
                board.raw_command("NET", "SCAN")
        finally:
            board.close()

    assert status["IF"] == "HOST"
    assert interfaces[0] == "HOST"
    assert len(interfaces) == len(set(interfaces))
    assert features == ("TCP", "TLS", "AUTH")
    assert hostname["HOST"] == "lab-bridge"
    assert unsupported.value.fields["REASON"] == "UNSUPPORTED"
    assert fake.writes == []
    assert config.exists()
    saved = config.read_text(encoding="utf-8")
    assert '"hostname": "lab-bridge"' in saved
    assert list(tmp_path.glob("device_bridge.*.json"))


def test_net_tcp_settings_are_applied_atomically_and_restart_listener(
    tmp_path: Path,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        new_port = int(probe.getsockname()[1])
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        port=0,
        config_file=str(tmp_path / "bridge.json"),
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        board = bridge.open_board(timeout=1.0)
        board.configure_tcp(enabled=True, port=new_port, bind="HOST")
        board.close()

        deadline = time.monotonic() + 2.0
        while True:
            try:
                address = bridge.address
            except RuntimeError:
                address = ("", 0)
            if address[1] == new_port:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        reconnected = bridge.open_board(timeout=1.0)
        try:
            status = reconnected.network_status()
        finally:
            reconnected.close()

    assert status["PORT"] == str(new_port)
    assert fake.writes == []


def test_no_token_option_clears_persisted_authentication(tmp_path: Path) -> None:
    config = tmp_path / "bridge.json"
    first_serial = FakeSerial()
    with DeviceBridge(
        "COM9",
        port=0,
        network_token="saved-secret",
        config_file=str(config),
        serial_factory=lambda **_settings: first_serial,
    ):
        pass

    second_serial = FakeSerial()
    with DeviceBridge(
        "COM9",
        clear_network_token=True,
        config_file=str(config),
        serial_factory=lambda **_settings: second_serial,
    ) as bridge:
        assert bridge.network_token is None

    assert '"network_token": null' in config.read_text(encoding="utf-8")
    assert list(tmp_path.glob("bridge.*.json"))


def test_bridge_opens_network_board_over_tls(tmp_path: Path) -> None:
    certificate, private_key = create_test_tls_credentials(tmp_path)
    fake = FakeSerial()
    bridge = DeviceBridge(
        "COM9",
        host="127.0.0.1",
        port=0,
        tls_certfile=str(certificate),
        tls_keyfile=str(private_key),
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        assert bridge.endpoint.startswith("tls://")
        board = bridge.open_board(
            timeout=2.0,
            tls_ca_file=str(certificate),
            tls_server_hostname="localhost",
        )
        try:
            board.transport.write_line("CUR")
            fake.wait_for_writes(1)
            fake.inject(b"OK:2.34\n")
            assert board.transport.read_line() == "OK:2.34"
        finally:
            board.close()

    assert fake.writes == [b"CUR\n"]


def test_net_tls_provisioning_restarts_bridge_as_tls(tmp_path: Path) -> None:
    certificate, private_key = create_test_tls_credentials(tmp_path)
    fake = FakeSerial()
    config = tmp_path / "bridge-settings.json"
    bridge = DeviceBridge(
        "COM9",
        port=0,
        config_file=str(config),
        serial_factory=lambda **_settings: fake,
    )

    with bridge:
        board = bridge.open_board(timeout=2.0)
        board.provision_tls(
            certificate.read_bytes(), private_key.read_bytes(), enable=True
        )
        board.close()

        deadline = time.monotonic() + 2.0
        while True:
            try:
                endpoint = bridge.endpoint
            except RuntimeError:
                endpoint = ""
            if endpoint.startswith("tls://"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        secure_board = bridge.open_board(
            timeout=2.0,
            tls_ca_file=str(certificate),
            tls_server_hostname="localhost",
        )
        try:
            assert secure_board.network_status()["TLS"] == "ON"
        finally:
            secure_board.close()

    assert fake.writes == []
    assert '"tls_enabled": true' in config.read_text(encoding="utf-8")
    assert list(tmp_path.glob("bridge-settings.*.json"))
