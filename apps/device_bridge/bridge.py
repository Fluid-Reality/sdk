"""Raw, bidirectional TCP-to-serial device bridge."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fluid_reality import TcpDeviceConnection, TcpDeviceListener, TransportError


@dataclass(frozen=True)
class TraceEvent:
    """One byte chunk crossing the bridge."""

    direction: Literal["TX", "RX"]
    data: bytes
    timestamp: datetime

    def format(self, style: Literal["hex", "ascii", "both"] = "both", *, timestamp: bool = True) -> str:
        prefix = self.timestamp.astimezone().isoformat(timespec="milliseconds") + " " if timestamp else ""
        header = f"{prefix}{self.direction} {len(self.data):4d} B"
        hexadecimal = " ".join(f"{value:02X}" for value in self.data)
        text = "".join(chr(value) if 32 <= value <= 126 else "." for value in self.data)
        if style == "hex":
            return f"{header} | {hexadecimal}"
        if style == "ascii":
            return f"{header} | {text}"
        return f"{header} | {hexadecimal} | {text}"


class DeviceBridge:
    """Forward an unmodified byte stream between one TCP client and serial."""

    def __init__(
        self,
        serial_port: str,
        *,
        baudrate: int = 250000,
        host: str = "127.0.0.1",
        port: int = 8765,
        serial_timeout: float = 0.1,
        write_timeout: float = 1.0,
        read_size: int = 4096,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        trace: Callable[[TraceEvent], None] | None = None,
        status: Callable[[str], None] | None = None,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if read_size <= 0:
            raise ValueError("read_size must be greater than zero")
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.host = host
        self.port = port
        self.serial_timeout = serial_timeout
        self.write_timeout = write_timeout
        self.read_size = read_size
        self.serial_settings = {
            "bytesize": bytesize,
            "parity": parity,
            "stopbits": stopbits,
            "xonxoff": xonxoff,
            "rtscts": rtscts,
            "dsrdtr": dsrdtr,
        }
        self._trace_callback = trace
        self._status_callback = status
        self._serial_factory = serial_factory
        self._listener: TcpDeviceListener | None = None
        self._serial: Any | None = None
        self._client: TcpDeviceConnection | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tx_bytes = 0
        self.rx_bytes = 0

    @property
    def endpoint(self) -> str:
        if self._listener is None:
            raise RuntimeError("Device bridge is not running")
        return self._listener.endpoint

    @property
    def address(self) -> tuple[str, int]:
        if self._listener is None:
            raise RuntimeError("Device bridge is not running")
        return self._listener.address

    def _status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _trace(self, direction: Literal["TX", "RX"], data: bytes) -> None:
        if direction == "TX":
            self.tx_bytes += len(data)
        else:
            self.rx_bytes += len(data)
        if data and self._trace_callback is not None:
            self._trace_callback(TraceEvent(direction, bytes(data), datetime.now().astimezone()))

    def start(self) -> DeviceBridge:
        if self._thread is not None:
            raise RuntimeError("Device bridge is already running")
        factory = self._serial_factory
        if factory is None:
            import serial

            factory = serial.Serial
        self._serial = factory(
            port=self.serial_port,
            baudrate=self.baudrate,
            timeout=self.serial_timeout,
            write_timeout=self.write_timeout,
            **self.serial_settings,
        )
        try:
            self._listener = TcpDeviceListener(self.host, self.port).start()
        except BaseException:
            self._serial.close()
            self._serial = None
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="device-bridge", daemon=True)
        self._thread.start()
        self._status(f"Listening on {self.endpoint}; serial {self.serial_port} at {self.baudrate} baud")
        return self

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client = self._listener.accept(timeout=0.1)
            except TimeoutError:
                continue
            except OSError:
                return
            self._client = client
            self._status(f"Client connected from {client.address[0]}:{client.address[1]}")
            try:
                self._serve_client(client)
            except (OSError, TransportError) as exc:
                if not self._stop.is_set():
                    self._status(f"Connection ended: {exc}")
            finally:
                client.close()
                self._client = None
                if not self._stop.is_set():
                    self._status("Client disconnected; waiting for another client")

    def _serve_client(self, client: TcpDeviceConnection) -> None:
        client_done = threading.Event()

        def serial_to_client() -> None:
            assert self._serial is not None
            while not self._stop.is_set() and not client_done.is_set():
                try:
                    data = self._serial.read(self.read_size)
                    if not data:
                        continue
                    self._trace("RX", data)
                    client.write_bytes(data)
                except (OSError, TransportError) as exc:
                    if not self._stop.is_set():
                        self._status(f"Serial receive stopped: {exc}")
                    client_done.set()
                    client.close()
                    return

        receiver = threading.Thread(target=serial_to_client, name="device-bridge-rx", daemon=True)
        receiver.start()
        try:
            assert self._serial is not None
            while not self._stop.is_set() and not client_done.is_set():
                data = client.read_bytes(self.read_size)
                if not data:
                    return
                self._trace("TX", data)
                self._serial.write(data)
                self._serial.flush()
        finally:
            client_done.set()
            receiver.join(timeout=max(0.5, self.serial_timeout + 0.1))

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            self._client.close()
        if self._listener is not None:
            self._listener.close()
        if self._serial is not None:
            self._serial.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._client = None
        self._listener = None
        self._serial = None
        self._thread = None
        self._status(f"Bridge stopped; TX {self.tx_bytes} bytes, RX {self.rx_bytes} bytes")

    def __enter__(self) -> DeviceBridge:  # noqa: PYI034 -- Python 3.10 support
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def wait_until_stopped(_bridge: DeviceBridge) -> None:
    try:
        while True:
            time.sleep(0.25)
    except KeyboardInterrupt:
        return
