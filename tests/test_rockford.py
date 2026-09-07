import base64
from collections import deque

from fluid_reality import (
    Board,
    ConfigurableNetworkBoard,
    EthernetBoard,
    Lansing,
    NetworkBoard,
    Rockford,
    WifiBoard,
)


class FakeTransport:
    def __init__(self, lines=()):
        self.lines = deque(lines)
        self.writes: list[str | bytes] = []

    def write_line(self, line: str) -> None:
        self.writes.append(line)

    def read_line(self) -> str:
        return self.lines.popleft()

    def write_bytes(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        pass


def test_board_profiles_are_independent():
    assert issubclass(Lansing, Board)
    assert issubclass(NetworkBoard, Board)
    assert Rockford.__mro__[1] is WifiBoard
    assert issubclass(WifiBoard, NetworkBoard)
    assert issubclass(EthernetBoard, NetworkBoard)
    assert Lansing.actuator_count == 24
    assert Rockford.actuator_count == 8
    assert Lansing.not_connected_delta_ma == 0.1
    assert Rockford.not_connected_delta_ma == 0.1
    assert not hasattr(Lansing, "network_status")
    assert hasattr(Rockford, "network_status")


def test_network_extension_can_be_reused_by_an_unrelated_board_profile():
    class FutureBoard(WifiBoard):
        actuator_count = 16

    transport = FakeTransport(["OK:NET,OP>STATUS,STATE>CONNECTED"])
    board = FutureBoard(transport=transport)

    assert board.network_status()["STATE"] == "CONNECTED"
    assert board.actuator_count == 16
    assert transport.writes == ["NET STATUS"]


def test_board_can_combine_multiple_capability_classes():
    class BluetoothBoard(Board):
        def bluetooth_status(self) -> str:
            return "available"

    class FutureBoard(NetworkBoard, BluetoothBoard):
        actuator_count = 12

    board = FutureBoard(transport=FakeTransport())

    assert FutureBoard.__mro__.count(Board) == 1
    assert FutureBoard.supports_network_configuration() is False
    assert not hasattr(board, "network_status")
    assert board.bluetooth_status() == "available"
    assert board.actuator_count == 12


def test_wifi_and_ethernet_capabilities_compose_without_duplicate_board_base():
    class DualNetworkBoard(WifiBoard, EthernetBoard):
        actuator_count = 12

    assert DualNetworkBoard.__mro__.count(NetworkBoard) == 1
    assert DualNetworkBoard.__mro__.count(ConfigurableNetworkBoard) == 1
    assert DualNetworkBoard.__mro__.count(Board) == 1
    assert DualNetworkBoard.declared_network_interfaces() == ("WIFI", "ETH")
    assert hasattr(DualNetworkBoard, "start_wifi_scan")
    assert hasattr(DualNetworkBoard, "ethernet_status")
    assert DualNetworkBoard.supports_network_configuration() is True


def test_ethernet_only_board_has_no_wifi_controls():
    class WiredBoard(EthernetBoard):
        pass

    assert WiredBoard.declared_network_interfaces() == ("ETH",)
    assert hasattr(WiredBoard, "ethernet_status")
    assert not hasattr(WiredBoard, "start_wifi_scan")


def test_interface_scoped_ip_and_tcp_commands():
    transport = FakeTransport(
        [
            "OK:NET,OP>IP,IF>ETH,MODE>DHCP",
            "OK:NET,OP>TCP,BIND>ETH",
        ]
    )
    board = EthernetBoard(transport=transport)

    board.use_dhcp("ETH")
    board.configure_tcp(bind="ETH")

    assert transport.writes == ["NET IF ETH IP DHCP", "NET TCP BIND ETH"]


def test_interface_discovery_uses_firmware_report():
    class DualNetworkBoard(WifiBoard, EthernetBoard):
        pass

    transport = FakeTransport(["OK:NET,OP>IF,IFACES>WIFI|ETH"])
    board = DualNetworkBoard(transport=transport)

    assert board.network_interfaces() == ("WIFI", "ETH")
    assert board.network_interface_commands_supported is True
    assert transport.writes == ["NET IF LIST"]


def test_interface_discovery_falls_back_for_legacy_wifi_firmware():
    transport = FakeTransport(["ER:NET,REASON>UNKNOWN_OPERATION"])
    board = Rockford(transport=transport)

    assert board.network_interfaces() == ("WIFI",)
    assert board.network_interface_commands_supported is False


def test_network_scan_and_results():
    transport = FakeTransport(
        [
            "OK:NET,OP>SCAN,STATE>SCANNING",
            "OK:NET,OP>LIST,COUNT>1",
            "OK:NET,OP>LIST,IDX>0,SSID64>Rmx1aWRSZWFsaXR5,RSSI>-48,SEC>PSK,CH>6",
        ]
    )
    board = Rockford(transport=transport)

    assert board.start_wifi_scan()["STATE"] == "SCANNING"
    networks = board.wifi_networks()

    assert networks[0].ssid == "FluidReality"
    assert networks[0].rssi == -48
    assert transport.writes == ["NET SCAN", "NET LIST", "NET LIST 0"]


def test_join_wifi_encodes_password_without_using_debug_command_path():
    transport = FakeTransport(["OK:NET,OP>JOIN,STATE>CONNECTING"])
    board = Rockford(transport=transport)

    result = board.join_wifi(2, "space allowed")

    assert result["STATE"] == "CONNECTING"
    assert transport.writes == ["NET JOIN 2 PSK c3BhY2UgYWxsb3dlZA=="]


def test_static_network_and_tcp_configuration():
    transport = FakeTransport(
        [
            "OK:NET,OP>IP,MODE>STATIC",
            "OK:NET,OP>TCP,STATE>ON,PORT>8765",
        ]
    )
    board = Rockford(transport=transport)

    board.set_static_ipv4(
        "192.168.1.64", "255.255.255.0", "192.168.1.1", "1.1.1.1", "8.8.8.8"
    )
    board.configure_tcp(port=8765)

    assert transport.writes == [
        "NET IP STATIC 192.168.1.64 255.255.255.0 192.168.1.1 1.1.1.1 8.8.8.8",
        "NET TCP PORT 8765",
    ]


def test_explicit_network_token_is_set_with_staged_protocol_command():
    token = "0123456789abcdef0123456789abcdef"
    transport = FakeTransport([f"OK:NET,OP>KEY,TOKEN>{token}"])
    board = Rockford(transport=transport)

    assert board.set_network_key(token) == token
    assert transport.writes == [f"NET KEY SET {token}"]


def test_tls_certificate_upload_is_chunked_for_serial_and_ble_limits():
    pem = b"A" * 60
    transport = FakeTransport(
        [
            "OK:NET,OP>TLS,UPLOAD>CERT,STATE>READY",
            "OK:NET,OP>TLS,UPLOAD>CERT,RECEIVED>48",
            "OK:NET,OP>TLS,UPLOAD>CERT,RECEIVED>60",
            "OK:NET,OP>TLS,STATE>OFF,READY>NO,CERT>VALID,KEY>MISSING,FP>ABC",
        ]
    )
    board = Rockford(transport=transport)

    result = board.upload_tls_certificate(pem)

    assert result["CERT"] == "VALID"
    assert transport.writes == [
        "NET TLS CERT BEGIN 60",
        f"NET TLS CERT DATA {base64.b64encode(pem[:48]).decode('ascii')}",
        f"NET TLS CERT DATA {base64.b64encode(pem[48:]).decode('ascii')}",
        "NET TLS CERT END",
    ]


def test_tls_configuration_and_key_password_commands():
    transport = FakeTransport(
        [
            "OK:NET,OP>TLS,STATE>OFF,READY>YES",
            "OK:NET,OP>TLS,STATE>ON,READY>YES",
            "OK:NET,OP>TLS,STATE>OFF,READY>NO",
        ]
    )
    board = Rockford(transport=transport)

    board.set_tls_private_key_password("space allowed")
    board.configure_tls(True)
    board.clear_tls()

    assert transport.writes == [
        "NET TLS PASS c3BhY2UgYWxsb3dlZA==",
        "NET TLS ON",
        "NET TLS CLEAR",
    ]
