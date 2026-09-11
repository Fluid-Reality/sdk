import base64
import hashlib
import struct
import zlib
from collections import deque

import pytest

from fluid_reality import (
    ActuatorState,
    Board,
    ConfigurableNetworkBoard,
    EthernetBoard,
    Lansing,
    NetworkBoard,
    ProtocolError,
    Rockford,
    RockfordConfig,
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
    assert Lansing.not_connected_delta_ma == 0.05
    assert Rockford.not_connected_delta_ma == 0.05
    assert not hasattr(Lansing, "network_status")
    assert hasattr(Rockford, "network_status")


def test_manual_output_current_sets_physical_outputs_and_returns_measurement():
    transport = FakeTransport(["OK:CUR>1.23,TIME>250"])
    board = Rockford(transport=transport)

    current_ma = board.manual_output_current(2, 100, 1, 250)

    assert current_ma == pytest.approx(1.23)
    assert transport.writes == ["OUC 2 100 1 250"]


def test_factory_reset_uses_usb_only_firmware_command_and_clears_cached_states():
    transport = FakeTransport(["OK:CFG_FACTORY_RESET,REBOOT>YES"])
    board = Rockford(transport=transport)
    board._actuator_states[2] = ActuatorState.READY
    board._actuator_detections[2] = object()

    board.factory_reset()

    assert transport.writes == ["CFG FACTORY_RESET"]
    assert board.actuator_states == (ActuatorState.UNKNOWN,) * board.actuator_count
    assert board.last_detection(2) is None


def test_rockford_vt_budget_configuration_and_sticky_marker():
    transport = FakeTransport(
        [
            "OK:VT_LIMIT_VS>10000",
            "OK:CFG_VT_LIMIT,VT_LIMIT_VS>12000,VT_MODIFIED>YES",
            "OK:CFG,VT_LIMIT_VS>12000,VT_MODIFIED>YES,SAFE>ON,DEBUG>OFF,"
            "DET_MIN>0.10,DT0_ERR>10.00,DT1_ERR>3.00",
        ]
    )
    board = Rockford(transport=transport)

    assert board.vt_limit_vs() == 10_000
    assert board.vt_limit_vs(12_000) == 12_000
    assert board.read_config() == RockfordConfig(
        vt_limit_vs=12_000,
        vt_limit_modified=True,
        safe=True,
        debug=False,
        detection_current_limit_ma=0.10,
        dt0_error_threshold_ma=10.0,
        dt1_error_threshold_ma=3.0,
    )
    assert transport.writes == ["CFG VT_LIMIT", "CFG VT_LIMIT 12000", "CFG"]


@pytest.mark.parametrize("value", [True, 1.5, "10000"])
def test_rockford_vt_budget_requires_integer(value):
    board = Rockford(transport=FakeTransport())

    with pytest.raises(TypeError, match="integer"):
        board.vt_limit_vs(value)


@pytest.mark.parametrize("value", [0, 4_294_968])
def test_rockford_vt_budget_range(value):
    board = Rockford(transport=FakeTransport())

    with pytest.raises(ValueError, match="between"):
        board.vt_limit_vs(value)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((0, 0, 0, 0), "measurement_ms must be >= 1"),
        ((0, 256, 0, 1), "value must be 0..255"),
        ((0, 0, 2, 1), "bottom must be 0 or 1"),
    ],
)
def test_manual_output_current_validates_parameters(args, message):
    board = Rockford(transport=FakeTransport())

    with pytest.raises(ValueError, match=message):
        board.manual_output_current(*args)


def test_manual_output_current_rejects_mismatched_interval():
    board = Rockford(transport=FakeTransport(["OK:CUR>1.23,TIME>249"]))

    with pytest.raises(ProtocolError, match="interval mismatch"):
        board.manual_output_current(0, 0, 0, 250)


def test_rockford_initialization_measures_baseline_and_each_physical_step(monkeypatch):
    transport = FakeTransport(
        [
            "OK:200.00",
            "OK:CFG_SAFE",
            "OK:CUR>1.00,TIME>500",
            "OK:CUR>1.20,TIME>500",
            "OK:CUR>0.90,TIME>500",
            "OK:CUR>1.05,TIME>500",
            "OK:OUT",
            "OK:CFG_SAFE",
            "OK:ACT>0,BASE>1.00,FWD>1.20,DIS>1.05",
        ]
    )
    board = Rockford(transport=transport)
    board._actuator_states[0] = ActuatorState.ERROR
    board.initialization_stages_v = (25.0,)
    board.initialization_stage_duration_s = 0.6
    board.initialization_phase_interval_s = 0.5
    clock = [0.0]
    monkeypatch.setattr("fluid_reality.boards.board.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "fluid_reality.boards.board.time.sleep",
        lambda duration: clock.__setitem__(0, clock[0] + duration),
    )
    progress = []

    board.initialize(0, progress_callback=progress.append)

    assert transport.writes[:6] == [
        "VLT",
        "CFG SAFE OFF",
        "OUC 0 0 0 500",
        "OUC 0 31 0 500",
        "OUC 0 224 1 500",
        "OUC 0 0 0 500",
    ]
    assert [item["delta_ma"] for item in progress if "delta_ma" in item] == pytest.approx(
        [0.20]
    )


def test_firmware_update_streams_framed_image_and_verifies(tmp_path):
    image = bytes(range(100)) * 3
    image_path = tmp_path / "rockford.bin"
    image_path.write_bytes(image)
    digest = hashlib.sha256(image).hexdigest()
    transport = FakeTransport(
        [
            f"OK:FWU,STATE>READY,TOTAL>{len(image)},FRAME>192",
            f"OK:FWU,SEQ>0,WRITTEN>192,TOTAL>{len(image)}",
            f"OK:FWU,SEQ>1,WRITTEN>{len(image)},TOTAL>{len(image)}",
            "OK:FWU,STATE>VERIFIED,REBOOT>YES",
        ]
    )
    board = Rockford(transport=transport)
    progress = []

    result = board.update_firmware(
        image_path, progress=lambda written, total: progress.append((written, total))
    )

    assert result.sha256 == digest
    assert transport.writes[0] == f"FWU BEGIN {len(image)} {digest}"
    first_header = struct.pack("<IH", 0, 192)
    assert transport.writes[1] == (
        first_header
        + image[:192]
        + struct.pack("<I", zlib.crc32(first_header + image[:192]) & 0xFFFFFFFF)
    )
    assert transport.writes[-1] == "FWU END"
    assert progress == [(0, len(image)), (192, len(image)), (len(image), len(image))]


def test_firmware_update_rejects_bluetooth(tmp_path):
    class BluetoothFakeTransport(FakeTransport):
        endpoint = "ble://rockford"

    image_path = tmp_path / "rockford.bin"
    image_path.write_bytes(b"firmware")
    board = Rockford(transport=BluetoothFakeTransport())

    with pytest.raises(ValueError, match="not Bluetooth"):
        board.update_firmware(image_path)


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


def test_wifi_mode_and_access_point_configuration():
    transport = FakeTransport(
        [
            "OK:NET,OP>MODE,MODE>CLIENT",
            "OK:NET,OP>AP,MODE>CLIENT,STATE>INACTIVE,SSID64>Um9ja2ZvcmQ=,CHANNEL>6",
            "OK:NET,OP>AP,MODE>CLIENT,STATE>INACTIVE",
            "OK:NET,OP>AP,MODE>CLIENT,STATE>INACTIVE,IP>192.168.50.1,MASK>255.255.255.0",
            "OK:NET,OP>MODE,MODE>ACCESS_POINT",
        ]
    )
    board = Rockford(transport=transport)

    assert board.wifi_mode() == "CLIENT"
    assert board.access_point_status()["CHANNEL"] == "6"
    board.configure_access_point("Rockford Lab", "secret123", channel=6)
    board.configure_access_point_ipv4("192.168.50.1", "255.255.255.0")
    board.set_wifi_mode("access point")

    assert transport.writes == [
        "NET MODE",
        "NET AP STATUS",
        "NET AP CONFIG Um9ja2ZvcmQgTGFi PSK c2VjcmV0MTIz 6",
        "NET AP IP 192.168.50.1 255.255.255.0",
        "NET MODE ACCESS_POINT",
    ]


def test_access_point_configuration_validates_credentials():
    board = Rockford(transport=FakeTransport([]))

    with pytest.raises(ValueError, match="SSID"):
        board.configure_access_point("")
    with pytest.raises(ValueError, match="8 to 63"):
        board.configure_access_point("Rockford", "short")
    with pytest.raises(ValueError, match="between 1 and 13"):
        board.configure_access_point("Rockford", channel=14)
    with pytest.raises(ValueError):
        board.configure_access_point_ipv4("not-an-address", "255.255.255.0")
    with pytest.raises(ValueError):
        board.configure_access_point_ipv4("192.168.4.1", "not-a-mask")


def test_static_network_and_tcp_configuration():
    transport = FakeTransport(
        [
            "OK:NET,OP>IP,MODE>STATIC",
            "OK:NET,OP>TCP,STATE>ON,PORT>49765",
        ]
    )
    board = Rockford(transport=transport)

    board.set_static_ipv4(
        "192.168.1.64", "255.255.255.0", "192.168.1.1", "1.1.1.1", "8.8.8.8"
    )
    board.configure_tcp(port=49765)

    assert transport.writes == [
        "NET IP STATIC 192.168.1.64 255.255.255.0 192.168.1.1 1.1.1.1 8.8.8.8",
        "NET TCP PORT 49765",
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
