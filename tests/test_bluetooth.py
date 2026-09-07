from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fluid_reality import (
    BluetoothBoard,
    BluetoothTransport,
    ConnectionProfile,
    Rockford,
    TransportError,
    is_bluetooth_endpoint,
)
from fluid_reality.bluetooth import (
    BLE_UART_RX_UUID,
    BLE_UART_SERVICE_UUID,
    bluetooth_identifier,
    discover_bluetooth_boards_async,
)


class FakeBleakScanner:
    @staticmethod
    async def find_device_by_filter(filter_func, timeout):
        device = SimpleNamespace(name="Rockford-3731", address="device-id")
        advertisement = SimpleNamespace(local_name="Rockford-3731")
        return device if filter_func(device, advertisement) else None


class FakeBleakClient:
    def __init__(self, target, disconnected_callback):
        self.target = target
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.callback = None
        self.received = bytearray()
        self.write_sizes: list[int] = []
        self.responses: list[bool] = []

    async def connect(self):
        self.is_connected = True

    async def start_notify(self, _uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == BLE_UART_RX_UUID
        self.responses.append(response)
        self.write_sizes.append(len(data))
        self.received.extend(data)
        while b"\n" in self.received:
            raw, _, remainder = self.received.partition(b"\n")
            self.received[:] = remainder
            replies = {
                "AUT secret": b"OK:AUT\n",
                "CTL ACQ": b"OK:CTL,OWNER>BLE\n",
                "CTL REL": b"OK:CTL,OWNER>NONE\n",
                "VER": b"OK:FW>Rockford,VERSION>1.0,PROTO>0.5\n",
            }
            if self.callback and raw.decode("ascii") in replies:
                self.callback(None, bytearray(replies[raw.decode("ascii")]))

    async def disconnect(self):
        self.is_connected = False
        self.disconnected_callback(self)


def test_discovery_excludes_anonymous_advertisements_and_deduplicates(monkeypatch) -> None:
    class DiscoveryScanner:
        @staticmethod
        async def discover(*, timeout, return_adv):
            assert timeout == 0.1
            assert return_adv is True

            def entry(name, address, rssi, service_uuids=()):
                return (
                    SimpleNamespace(name=name, address=address),
                    SimpleNamespace(
                        local_name=name,
                        rssi=rssi,
                        service_uuids=service_uuids,
                    ),
                )

            return {
                "board-weak": entry("Rockford-A172E0", "board-address", -71),
                "board-strong": entry("Rockford-A172E0", "board-address", -44),
                "anonymous": entry(None, "anonymous-address", -30),
                "anonymous-uart": entry(
                    None,
                    "anonymous-uart-address",
                    -20,
                    (BLE_UART_SERVICE_UUID,),
                ),
                "headphones": entry("Headphones", "headphones-address", -10),
            }

    monkeypatch.setattr(
        "fluid_reality.bluetooth._load_bleak",
        lambda: (FakeBleakClient, DiscoveryScanner),
    )

    devices = asyncio.run(discover_bluetooth_boards_async(timeout=0.1))

    assert [(device.name, device.address, device.rssi) for device in devices] == [
        ("Rockford-A172E0", "board-address", -44)
    ]


def test_bluetooth_endpoint_and_connection_profile() -> None:
    profile = ConnectionProfile(
        transport="bluetooth",
        bluetooth_device="device-id",
        bluetooth_pair=True,
        access_token="secret",
    )

    assert profile.endpoint == "ble://device-id"
    assert profile.board_options() == {"pair": True, "network_token": "secret"}
    assert profile.to_mapping()["device"] == "device-id"
    assert is_bluetooth_endpoint(profile.endpoint)
    assert bluetooth_identifier(profile.endpoint) == "device-id"


def test_bluetooth_transport_frames_commands_and_authenticates(monkeypatch) -> None:
    monkeypatch.setattr(
        "fluid_reality.bluetooth._load_bleak",
        lambda: (FakeBleakClient, FakeBleakScanner),
    )
    transport = BluetoothTransport(
        "ble://Rockford-3731", timeout=0.5, network_token="secret"
    )
    try:
        assert transport._control_acquired is True
        transport.write_line("VER")
        assert transport.read_line() == "OK:FW>Rockford,VERSION>1.0,PROTO>0.5"
        assert max(transport._client.write_sizes) <= 20
        transport.write_bytes(b"\x00\xff")
        assert transport._client.responses[-1] is False
        transport._client.received.clear()
    finally:
        transport.close()


def test_bluetooth_authentication_reports_rejected_token() -> None:
    transport = object.__new__(BluetoothTransport)
    transport.write_line = lambda _line: None
    transport.read_line = lambda: "ER:AUT,REASON>FAILED"

    with pytest.raises(TransportError, match="access token was rejected"):
        transport.authenticate("wrong-token")


def test_rockford_composes_wifi_and_bluetooth_capabilities() -> None:
    assert issubclass(Rockford, BluetoothBoard)
