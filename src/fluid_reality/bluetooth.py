"""Bluetooth Low Energy discovery and line transport for Fluid Reality boards."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, TypeVar
from urllib.parse import unquote

from .errors import TransportError

BLE_UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
BLE_UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
BLE_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
BLE_ENDPOINT_PREFIX = "ble://"
_T = TypeVar("_T")


def is_bluetooth_endpoint(endpoint: str) -> bool:
    return str(endpoint).lower().startswith(BLE_ENDPOINT_PREFIX)


def bluetooth_identifier(endpoint: str) -> str:
    value = str(endpoint).strip()
    if not is_bluetooth_endpoint(value):
        raise ValueError("Bluetooth endpoint must start with ble://")
    identifier = unquote(value[len(BLE_ENDPOINT_PREFIX) :]).strip().strip("/")
    if not identifier:
        raise ValueError("Bluetooth endpoint requires a device name, address, or ID")
    return identifier


@dataclass(frozen=True)
class BluetoothDevice:
    """One discoverable Fluid Reality BLE peripheral."""

    identifier: str
    name: str
    address: str
    rssi: int | None = None

    @property
    def endpoint(self) -> str:
        return f"{BLE_ENDPOINT_PREFIX}{self.identifier}"


def _load_bleak() -> tuple[type[Any], type[Any]]:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise TransportError(
            "Bluetooth support requires the optional dependency; "
            "install it with 'pip install fluid-reality[bluetooth]'"
        ) from exc
    return BleakClient, BleakScanner


async def discover_bluetooth_boards_async(
    *, timeout: float = 5.0
) -> tuple[BluetoothDevice, ...]:
    """Discover identifiable Fluid Reality BLE peripherals.

    Some platforms report anonymous advertisements for every nearby BLE device.
    Never turn those into generic board entries: a compatible board must expose
    a Fluid Reality firmware name, while the UART UUID is supporting evidence.
    """

    _client, scanner_type = _load_bleak()
    found = await scanner_type.discover(timeout=timeout, return_adv=True)
    devices_by_address: dict[str, BluetoothDevice] = {}
    for device, advertisement in found.values():
        name = (advertisement.local_name or device.name or "").strip()
        if not name.lower().startswith(
            ("rockford", "lansing", "fluid reality", "fluid-reality")
        ):
            continue
        address = str(device.address)
        # The firmware's default name contains its hardware-derived device ID and
        # remains portable. Addresses are platform-specific handles on some OSes.
        candidate = BluetoothDevice(
            identifier=name,
            name=name,
            address=address,
            rssi=getattr(advertisement, "rssi", None),
        )
        existing = devices_by_address.get(address.casefold())
        if existing is None or (candidate.rssi or -999) > (existing.rssi or -999):
            devices_by_address[address.casefold()] = candidate
    devices = list(devices_by_address.values())
    devices.sort(key=lambda item: (-(item.rssi or -999), item.name.casefold()))
    return tuple(devices)


def _run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine from synchronous code, including inside an active loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: list[_T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - defensive thread bridge.
            error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def discover_bluetooth_boards(*, timeout: float = 5.0) -> tuple[BluetoothDevice, ...]:
    """Synchronous BLE discovery helper."""

    return _run_async(discover_bluetooth_boards_async(timeout=timeout))


class BluetoothTransport:
    """Synchronous ``LineTransport`` implemented over Nordic-UART-compatible BLE."""

    redirected = True

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 5.0,
        write_timeout: float | None = 5.0,
        access_token: str | None = None,
        bluetooth_token: str | None = None,
        network_token: str | None = None,
        pair: bool = False,
        acquire_control: bool = True,
        client_factory: Callable[..., Any] | None = None,
        **_unused: Any,
    ) -> None:
        self.endpoint = endpoint
        self.port = endpoint
        self.identifier = bluetooth_identifier(endpoint)
        self.timeout = timeout
        self.write_timeout = write_timeout
        self._receive_buffer = bytearray()
        self._condition = threading.Condition()
        self._closed = False
        self._disconnect_error: str | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        try:
            self._submit(
                self._connect(client_factory=client_factory, pair=pair),
                timeout=max(timeout, 10.0),
            )
            token = bluetooth_token
            if token is None:
                token = access_token if access_token is not None else network_token
            if token:
                self.authenticate(token)
            if acquire_control:
                self.acquire_control()
        except Exception:
            self.close()
            raise

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, operation: Awaitable[_T], *, timeout: float | None = None) -> _T:
        if self._closed:
            raise TransportError("Bluetooth transport is closed")
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        try:
            return future.result(timeout=timeout if timeout is not None else self.timeout)
        except Exception as exc:
            future.cancel()
            if isinstance(exc, TransportError):
                raise
            if isinstance(exc, TimeoutError):
                raise TransportError("Bluetooth operation timed out") from exc
            raise TransportError(f"Bluetooth operation failed: {exc}") from exc

    async def _connect(
        self, *, client_factory: Callable[..., Any] | None, pair: bool
    ) -> None:
        bleak_client, scanner_type = _load_bleak()
        target: Any = self.identifier
        # Names are convenient in connection files but Bleak connects most
        # reliably with the discovered platform device object.
        if not any(character in self.identifier for character in (":", "{")):
            found = await scanner_type.find_device_by_filter(
                lambda device, advertisement: (
                    (advertisement.local_name or device.name or "").casefold()
                    == self.identifier.casefold()
                    or str(device.address).casefold() == self.identifier.casefold()
                ),
                timeout=self.timeout,
            )
            if found is None:
                raise TransportError(f"Bluetooth board {self.identifier!r} was not found")
            target = found
        factory = client_factory or bleak_client
        self._client = factory(target, disconnected_callback=self._on_disconnect)
        await self._client.connect()
        if pair:
            pair_method = getattr(self._client, "pair", None)
            if pair_method is not None:
                await pair_method()
        await self._client.start_notify(BLE_UART_TX_UUID, self._on_notification)

    def _on_notification(self, _characteristic: Any, data: bytearray) -> None:
        with self._condition:
            self._receive_buffer.extend(data)
            self._condition.notify_all()

    def _on_disconnect(self, _client: Any) -> None:
        with self._condition:
            if not self._closed:
                self._disconnect_error = "Bluetooth board disconnected"
            self._condition.notify_all()

    async def _write(self, data: bytes, *, response: bool) -> None:
        # Twenty-byte chunks work with the default ATT MTU. Firmware performs
        # newline framing, so commands remain valid when split across writes.
        for offset in range(0, len(data), 20):
            await self._client.write_gatt_char(
                BLE_UART_RX_UUID, data[offset : offset + 20], response=response
            )

    def write_line(self, line: str) -> None:
        data = f"{line}\n".encode("ascii")
        self._submit(self._write(data, response=True), timeout=self.write_timeout)

    def write_bytes(self, data: bytes) -> None:
        if not data:
            return
        # Raw stream updates favor low latency; the firmware's control watchdog
        # provides the safety boundary if the link disappears.
        self._submit(
            self._write(bytes(data), response=False), timeout=self.write_timeout
        )

    def read_line(self) -> str:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        with self._condition:
            while True:
                newline = self._receive_buffer.find(b"\n")
                if newline >= 0:
                    raw = bytes(self._receive_buffer[:newline])
                    del self._receive_buffer[: newline + 1]
                    return raw.rstrip(b"\r").decode("ascii", errors="replace")
                if self._disconnect_error:
                    raise TransportError(self._disconnect_error)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TransportError("Timed out waiting for Bluetooth response")
                self._condition.wait(remaining)

    def reset_input_buffer(self) -> None:
        with self._condition:
            self._receive_buffer.clear()

    def drain_lines(self, *, timeout: float = 0.15, max_lines: int = 50) -> tuple[str, ...]:
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(lines) < max_lines:
                newline = self._receive_buffer.find(b"\n")
                if newline < 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                    continue
                raw = bytes(self._receive_buffer[:newline])
                del self._receive_buffer[: newline + 1]
                lines.append(raw.rstrip(b"\r").decode("ascii", errors="replace"))
        return tuple(lines)

    def wait_for_quiet(self, *, quiet_s: float = 0.05) -> None:
        time.sleep(quiet_s)

    def authenticate(self, token: str) -> None:
        self.write_line(f"AUT {token}")
        response = self.read_line()
        if response != "OK:AUT":
            if response.startswith("ER:AUT,REASON>FAILED"):
                raise TransportError("Bluetooth access token was rejected")
            raise TransportError(f"Bluetooth authentication failed: {response}")

    def acquire_control(self) -> None:
        self.write_line("CTL ACQ")
        response = self.read_line()
        if response.startswith("ER:AUT,REASON>REQUIRED"):
            raise TransportError("Bluetooth board requires an access token")
        if response != "OK:CTL,OWNER>BLE":
            raise TransportError(f"Could not acquire Bluetooth control: {response}")
        self._control_acquired = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            client = getattr(self, "_client", None)
            if client is not None and getattr(client, "is_connected", False):
                if getattr(self, "_control_acquired", False):
                    try:
                        self.write_line("CTL REL")
                        self.read_line()
                    except Exception:
                        pass
                future = asyncio.run_coroutine_threadsafe(client.disconnect(), self._loop)
                try:
                    future.result(timeout=2.0)
                except Exception:
                    pass
        finally:
            self._closed = True
            with self._condition:
                self._condition.notify_all()
            self._loop.call_soon_threadsafe(self._loop.stop)
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=2.0)


__all__ = [
    "BLE_UART_RX_UUID",
    "BLE_UART_SERVICE_UUID",
    "BLE_UART_TX_UUID",
    "BluetoothDevice",
    "BluetoothTransport",
    "bluetooth_identifier",
    "discover_bluetooth_boards",
    "discover_bluetooth_boards_async",
    "is_bluetooth_endpoint",
]
