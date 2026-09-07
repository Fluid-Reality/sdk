"""Raw, bidirectional TCP-to-serial device bridge."""

from __future__ import annotations

import threading
import time
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fluid_reality import (
    ConfigurableNetworkBoard,
    TcpDeviceConnection,
    TcpDeviceListener,
    TransportError,
)

try:
    from .bridge_network import BridgeNetProtocol, BridgeNetworkSettings
except ImportError:  # Direct execution through device_bridge.py.
    from bridge_network import BridgeNetProtocol, BridgeNetworkSettings


class DeviceBridgeBoard(ConfigurableNetworkBoard):
    """Connection-only SDK profile for a device exposed by Device Bridge.

    The bridge computer owns the TCP/TLS endpoint. Consequently this profile
    implements ``NET`` against the bridge process instead of the serial board.
    """

    network_interface_capability = "HOST"

    def configure_tcp(
        self,
        *,
        enabled: bool | None = None,
        port: int | None = None,
        bind: str | None = None,
    ) -> dict[str, str]:
        """Atomically update bridge TCP settings before its listener restarts."""

        state = "KEEP" if enabled is None else ("ON" if enabled else "OFF")
        selected_port: object = "KEEP" if port is None else port
        selected_bind = "KEEP" if bind is None else str(bind).upper()
        return self.raw_command(
            "NET", "TCP", "SET", state, selected_port, selected_bind
        )[0].fields


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
    """Forward a TCP/TLS client byte stream to a physical serial device."""

    def __init__(
        self,
        serial_port: str,
        *,
        baudrate: int = 250000,
        host: str | None = None,
        port: int | None = None,
        serial_timeout: float = 0.1,
        write_timeout: float = 1.0,
        read_size: int = 4096,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        network_token: str | None = None,
        clear_network_token: bool = False,
        tls_certfile: str | None = None,
        tls_keyfile: str | None = None,
        tls_key_password: str | None = None,
        config_file: str | None = None,
        trace: Callable[[TraceEvent], None] | None = None,
        status: Callable[[str], None] | None = None,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if read_size <= 0:
            raise ValueError("read_size must be greater than zero")
        if clear_network_token and network_token is not None:
            raise ValueError("network_token and clear_network_token are mutually exclusive")
        if network_token is not None:
            if not network_token or not network_token.isascii() or any(
                character.isspace() for character in network_token
            ):
                raise ValueError("network_token must be non-empty ASCII without whitespace")
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.config_file = Path(config_file).expanduser() if config_file else None
        settings = (
            BridgeNetworkSettings.load(self.config_file)
            if self.config_file is not None
            else BridgeNetworkSettings()
        )
        self._settings_dirty = any(
            value is not None
            for value in (
                host, port, network_token, tls_certfile, tls_keyfile,
                tls_key_password, True if clear_network_token else None,
            )
        )
        if host is not None:
            settings.host = host
            settings.bind_interface = "ANY" if host in {"0.0.0.0", "::"} else "HOST"
            settings.tcp_enabled = True
        if port is not None:
            settings.port = port
            settings.tcp_enabled = True
        if clear_network_token:
            settings.network_token = None
        elif network_token is not None:
            settings.network_token = network_token
        if tls_certfile is not None:
            settings.tls_certfile = tls_certfile
        if tls_keyfile is not None:
            settings.tls_keyfile = tls_keyfile
        if tls_key_password is not None:
            settings.tls_key_password = tls_key_password
        if tls_certfile is not None or tls_keyfile is not None:
            settings.tls_enabled = bool(tls_certfile and tls_keyfile)
        self.network_settings = settings
        self._net_protocol = BridgeNetProtocol(settings, self.config_file)
        self._sync_network_settings()
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
        self._restart_listener_requested = False
        self.tx_bytes = 0
        self.rx_bytes = 0

    def _sync_network_settings(self) -> None:
        settings = self.network_settings
        self.host = settings.host
        self.port = settings.port
        self.network_token = settings.network_token
        self.tls_certfile = settings.tls_certfile if settings.tls_enabled else None
        self.tls_keyfile = settings.tls_keyfile if settings.tls_enabled else None
        self.tls_key_password = settings.tls_key_password

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

    def open_board(
        self,
        *,
        timeout: float = 45.0,
        network_token: str | None = None,
        tls_ca_file: str | None = None,
        tls_fingerprint: str | None = None,
        tls_server_hostname: str | None = None,
    ) -> DeviceBridgeBoard:
        """Open an SDK board proxy through this running TCP/TLS bridge."""

        if self._listener is None:
            raise RuntimeError("Device bridge is not running")
        token = self.network_token if network_token is None else network_token
        return DeviceBridgeBoard(
            self.endpoint,
            timeout=timeout,
            network_token=token,
            tls_ca_file=tls_ca_file,
            tls_fingerprint=tls_fingerprint,
            tls_server_hostname=tls_server_hostname,
        )

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
            self._start_listener()
        except BaseException:
            self._serial.close()
            self._serial = None
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="device-bridge", daemon=True)
        self._thread.start()
        self._status(f"Listening on {self.endpoint}; serial {self.serial_port} at {self.baudrate} baud")
        return self

    def _start_listener(self) -> None:
        self._sync_network_settings()
        if not self.network_settings.tcp_enabled:
            self._listener = None
            return
        self._listener = TcpDeviceListener(
            self.host,
            self.port,
            tls_certfile=self.tls_certfile,
            tls_keyfile=self.tls_keyfile,
            tls_key_password=self.tls_key_password,
        ).start()
        # Preserve an OS-selected ephemeral port for the lifetime of this run.
        if self.port == 0:
            self.port = self._listener.address[1]
            self.network_settings.port = self.port
        if self.config_file is not None and (
            self._settings_dirty or not self.config_file.exists()
        ):
            self._net_protocol.save()
            self._settings_dirty = False

    def _restart_listener(self) -> None:
        old_listener = self._listener
        self._listener = None
        if old_listener is not None:
            old_listener.close()
        self._sync_network_settings()
        if self.network_settings.tcp_enabled and not self._stop.is_set():
            self._start_listener()
            self._status(f"Network listener restarted on {self.endpoint}")
        elif not self.network_settings.tcp_enabled:
            self._status("Network listener disabled by NET TCP OFF")

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self._listener is None:
                self._stop.wait(0.1)
                continue
            try:
                client = self._listener.accept(timeout=0.1)
            except TimeoutError:
                continue
            except TransportError as exc:
                if not self._stop.is_set():
                    self._status(str(exc))
                continue
            except OSError:
                return
            self._client = client
            self._status(f"Client connected from {client.address[0]}:{client.address[1]}")
            try:
                if not self._authenticate_client(client):
                    continue
                self._serve_client(client)
            except (OSError, TransportError) as exc:
                if not self._stop.is_set():
                    self._status(f"Connection ended: {exc}")
            finally:
                client.close()
                self._client = None
                if self._restart_listener_requested and not self._stop.is_set():
                    self._restart_listener_requested = False
                    self._restart_listener()
                if not self._stop.is_set():
                    self._status("Client disconnected; waiting for another client")

    def _authenticate_client(self, client: TcpDeviceConnection) -> bool:
        """Consume SDK network authentication before forwarding device bytes."""

        if self.network_token is None:
            return True
        line = bytearray()
        while len(line) <= 512 and not self._stop.is_set():
            chunk = client.read_bytes(1)
            if not chunk:
                return False
            line.extend(chunk)
            if chunk == b"\n":
                break
        expected = f"NET AUTH {self.network_token}\n".encode("ascii")
        if not hmac.compare_digest(bytes(line), expected):
            client.write_bytes(b"ER:NET,OP>AUTH,REASON>DENIED\n")
            self._status(f"Client authentication failed for {client.address[0]}:{client.address[1]}")
            return False
        client.write_bytes(b"OK:NET,OP>AUTH\n")
        self._status(f"Client authenticated from {client.address[0]}:{client.address[1]}")
        return True

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
        command_buffer = bytearray()
        try:
            assert self._serial is not None
            while not self._stop.is_set() and not client_done.is_set():
                data = client.read_bytes(self.read_size)
                if not data:
                    return
                command_buffer.extend(data)
                while command_buffer:
                    newline = command_buffer.find(b"\n")
                    if newline >= 0:
                        line = bytes(command_buffer[: newline + 1])
                        del command_buffer[: newline + 1]
                        if line.upper().startswith((b"NET ", b"NET\r", b"NET\n")):
                            response, restart = self._net_protocol.handle(line)
                            self._sync_network_settings()
                            client.write_bytes(response)
                            self._status(
                                f"Processed locally: {line.decode('ascii', errors='replace').strip()}"
                            )
                            if restart:
                                self._restart_listener_requested = True
                                return
                        else:
                            self._forward_to_serial(line)
                        continue
                    upper = bytes(command_buffer).upper()
                    if len(command_buffer) < 4 and b"NET ".startswith(upper):
                        break
                    if upper.startswith(b"NET "):
                        if len(command_buffer) > 2048:
                            client.write_bytes(
                                b"ER:NET,OP>UNKNOWN,REASON>LINE_TOO_LONG\n"
                            )
                            command_buffer.clear()
                        break
                    self._forward_to_serial(bytes(command_buffer))
                    command_buffer.clear()
        finally:
            client_done.set()
            receiver.join(timeout=max(0.5, self.serial_timeout + 0.1))

    def _forward_to_serial(self, data: bytes) -> None:
        assert self._serial is not None
        self._trace("TX", data)
        self._serial.write(data)
        self._serial.flush()

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
