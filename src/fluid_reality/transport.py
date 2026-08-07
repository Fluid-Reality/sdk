"""Serial transport implementation."""

from __future__ import annotations

import os
import socket
import time
from typing import Any
from urllib.parse import urlparse

from .errors import TransportError


TRANSPORT_OVERRIDE_ENV = "FLUID_REALITY_TRANSPORT"


def list_ports() -> list[str]:
    """Return available OS serial ports plus the configured TCP endpoint."""

    try:
        from serial.tools import list_ports as serial_list_ports
    except ImportError as exc:  # pragma: no cover - dependency metadata covers this.
        raise TransportError("pyserial is required to list serial ports") from exc

    ports = [item.device for item in serial_list_ports.comports()]
    override = os.environ.get(TRANSPORT_OVERRIDE_ENV)
    if override:
        parsed = urlparse(override)
        if parsed.scheme.lower() == "tcp" and override not in ports:
            ports.append(override)
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
        parsed = urlparse(endpoint)
        if parsed.scheme.lower() != "tcp" or parsed.hostname is None or parsed.port is None:
            raise TransportError(
                f"Invalid {TRANSPORT_OVERRIDE_ENV} value {endpoint!r}; "
                "expected tcp://host:port"
            )
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise TransportError(
                f"Invalid TCP transport endpoint {endpoint!r}; "
                "paths, parameters, queries, and fragments are not supported"
            )

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
            except socket.timeout:
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
    """Line-oriented transport with optional transparent TCP redirection.

    When ``FLUID_REALITY_TRANSPORT`` is set to ``tcp://host:port``, traffic is
    sent to that raw TCP byte stream instead of the serial port supplied by the
    application. Existing ``Lansing(port)`` callers therefore need no changes.
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
        override = os.environ.get(TRANSPORT_OVERRIDE_ENV)
        selected_endpoint = port if port.lower().startswith("tcp://") else override
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
