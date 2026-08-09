from __future__ import annotations

import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone

from apps.device_bridge.bridge import DeviceBridge, TraceEvent


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


def test_trace_event_formats_hex_and_ascii() -> None:
    event = TraceEvent("TX", b"CUR\n\xff", datetime(2026, 8, 8, tzinfo=timezone.utc))

    formatted = event.format()

    assert "TX" in formatted
    assert "43 55 52 0A FF" in formatted
    assert "CUR.." in formatted


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

        with socket.create_connection(bridge.address, timeout=1.0) as second_client:
            second_client.sendall(b"CUR\n")
            fake.wait_for_writes(2)
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
