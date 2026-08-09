"""Serial transport implementation."""

from __future__ import annotations

import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

from .errors import TransportError

VIRTUAL_PORTS_ENV = "FLUID_REALITY_VIRTUAL_PORTS"


def _parse_tcp_endpoint(endpoint: str, *, context: str) -> None:
    try:
        parsed = urlparse(endpoint)
        valid_address = parsed.scheme.lower() == "tcp" and parsed.hostname is not None and parsed.port is not None
    except ValueError as exc:
        raise TransportError(f"Invalid {context} {endpoint!r}; expected tcp://host:port") from exc
    if not valid_address:
        raise TransportError(f"Invalid {context} {endpoint!r}; expected tcp://host:port")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise TransportError(
            f"Invalid {context} {endpoint!r}; paths, parameters, queries, and fragments are not supported"
        )


def _endpoint_aliases() -> dict[str, tuple[str, str]]:
    """Return case-insensitive port aliases as alias -> (display name, endpoint)."""
    value = os.environ.get(VIRTUAL_PORTS_ENV, "").strip()
    aliases: dict[str, tuple[str, str]] = {}
    if not value:
        return aliases
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise TransportError(
                f"Invalid {VIRTUAL_PORTS_ENV} entry {entry!r}; expected PORT=tcp://host:port"
            )
        alias, endpoint = (part.strip() for part in entry.split("=", 1))
        if not alias or not endpoint:
            raise TransportError(
                f"Invalid {VIRTUAL_PORTS_ENV} entry {entry!r}; expected PORT=tcp://host:port"
            )
        _parse_tcp_endpoint(endpoint, context=f"endpoint for alias {alias!r}")
        aliases[alias.casefold()] = (alias, endpoint)
    return aliases


def is_virtual_port(port: str) -> bool:
    """Return whether ``port`` is a direct TCP endpoint or configured alias."""
    return port.lower().startswith("tcp://") or port.casefold() in _endpoint_aliases()


def list_ports() -> list[str]:
    """Return OS serial ports plus configured TCP endpoint aliases."""

    try:
        from serial.tools import list_ports as serial_list_ports
    except ImportError as exc:  # pragma: no cover - dependency metadata covers this.
        raise TransportError("pyserial is required to list serial ports") from exc

    ports = [item.device for item in serial_list_ports.comports()]
    existing = {port.casefold() for port in ports}
    for key, (alias, _endpoint) in _endpoint_aliases().items():
        if key not in existing:
            ports.append(alias)
            existing.add(key)
    return ports


class _SocketBackend:
    """Raw TCP byte stream with the subset of pyserial used by the SDK."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float,
        write_timeout: float | None,
    ) -> None:
        _parse_tcp_endpoint(endpoint, context="TCP endpoint")
        parsed = urlparse(endpoint)

        self.timeout = timeout
        self.write_timeout = write_timeout
        self._receive_buffer = bytearray()
        try:
            self._socket = socket.create_connection(
                (parsed.hostname, parsed.port),
                timeout=timeout,
            )
            self._socket.settimeout(timeout)
        except (OSError, ValueError) as exc:
            raise TransportError(
                f"Could not connect to redirected transport {endpoint!r}: {exc}"
            ) from exc

    def write(self, data: bytes) -> None:
        original_timeout = self._socket.gettimeout()
        try:
            self._socket.settimeout(self.write_timeout)
            self._socket.sendall(data)
        finally:
            self._socket.settimeout(original_timeout)

    def flush(self) -> None:
        # TCP sendall() has already handed every byte to the operating system.
        return None

    def readline(self) -> bytes:
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            newline = self._receive_buffer.find(b"\n")
            if newline >= 0:
                end = newline + 1
                line = bytes(self._receive_buffer[:end])
                del self._receive_buffer[:end]
                return line

            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return b""
            self._socket.settimeout(remaining)
            try:
                chunk = self._socket.recv(4096)
            except TimeoutError:
                return b""
            if not chunk:
                raise ConnectionError("redirected transport closed the connection")
            self._receive_buffer.extend(chunk)

    def reset_input_buffer(self) -> None:
        self._receive_buffer.clear()
        original_timeout = self._socket.gettimeout()
        try:
            self._socket.setblocking(False)
            while self._socket.recv(4096):
                pass
        except BlockingIOError:
            pass
        finally:
            self._socket.settimeout(original_timeout)

    def close(self) -> None:
        self._socket.close()


class SerialTransport:
    """Line-oriented serial transport with selective virtual-port routing.

    ``FLUID_REALITY_VIRTUAL_PORTS`` maps selected port aliases to TCP endpoints, for
    example ``COM66=tcp://127.0.0.1:8765``. Only an exact alias selection is
    redirected; every other port continues through the physical serial layer.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        write_timeout: float | None = 1.0,
        **serial_kwargs: Any,
    ) -> None:
        aliases = _endpoint_aliases()
        selected_endpoint = (
            port if port.lower().startswith("tcp://")
            else aliases.get(port.casefold(), ("", None))[1]
        )
        self.port = port
        self.endpoint = selected_endpoint or port
        self.redirected = selected_endpoint is not None

        if selected_endpoint is not None:
            self._serial = _SocketBackend(
                selected_endpoint,
                timeout=timeout,
                write_timeout=write_timeout,
            )
        else:
            try:
                import serial
            except ImportError as exc:  # pragma: no cover - dependency metadata covers this.
                raise TransportError("pyserial is required to use SerialTransport") from exc

            try:
                self._serial = serial.Serial(
                    port=port,
                    baudrate=baudrate,
                    timeout=timeout,
                    write_timeout=write_timeout,
                    **serial_kwargs,
                )
            except Exception as exc:  # pragma: no cover - hardware dependent.
                raise TransportError(f"Could not open serial port {port!r}: {exc}") from exc

    def write_line(self, line: str) -> None:
        self.write_bytes(f"{line}\n".encode("ascii"))

    def read_line(self) -> str:
        try:
            data = self._serial.readline()
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not read from transport: {exc}") from exc
        if not data:
            raise TransportError("Timed out waiting for firmware response")
        return data.decode("ascii", errors="replace").strip("\r\n")

    def write_bytes(self, data: bytes) -> None:
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not write to transport: {exc}") from exc

    def reset_input_buffer(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not reset transport input buffer: {exc}") from exc

    def drain_lines(self, *, timeout: float = 0.05, max_lines: int = 50) -> tuple[str, ...]:
        lines: list[str] = []
        original_timeout = self._serial.timeout
        try:
            self._serial.timeout = timeout
            for _ in range(max_lines):
                data = self._serial.readline()
                if not data:
                    break
                lines.append(data.decode("ascii", errors="replace").strip("\r\n"))
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not drain transport input buffer: {exc}") from exc
        finally:
            self._serial.timeout = original_timeout
        return tuple(lines)

    def wait_for_quiet(self, *, quiet_s: float = 0.05) -> None:
        time.sleep(quiet_s)

    def close(self) -> None:
        self._serial.close()
