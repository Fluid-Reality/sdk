"""Serial transport implementation."""

from __future__ import annotations

import hashlib
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlparse

from .errors import TransportError

def _parse_tcp_endpoint(endpoint: str, *, context: str) -> None:
    try:
        parsed = urlparse(endpoint)
        valid_address = parsed.scheme.lower() in {"tcp", "tls"} and parsed.hostname is not None and parsed.port is not None
    except ValueError as exc:
        raise TransportError(f"Invalid {context} {endpoint!r}; expected tcp://host:port or tls://host:port") from exc
    if not valid_address:
        raise TransportError(f"Invalid {context} {endpoint!r}; expected tcp://host:port or tls://host:port")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise TransportError(
            f"Invalid {context} {endpoint!r}; paths, parameters, queries, and fragments are not supported"
        )


def list_ports() -> list[str]:
    """Return physical serial ports reported by the operating system."""

    try:
        from serial.tools import list_ports as serial_list_ports
    except ImportError as exc:  # pragma: no cover - dependency metadata covers this.
        raise TransportError("pyserial is required to list serial ports") from exc

    return [item.device for item in serial_list_ports.comports()]


class _SocketBackend:
    """Raw TCP byte stream with the subset of pyserial used by the SDK."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float,
        write_timeout: float | None,
        connect_timeout: float | None = None,
        tls_ca_file: str | None = None,
        tls_ca_data: str | None = None,
        tls_fingerprint: str | None = None,
        tls_server_hostname: str | None = None,
        tls_check_hostname: bool = True,
        tls_verify_certificate: bool = True,
    ) -> None:
        _parse_tcp_endpoint(endpoint, context="TCP endpoint")
        parsed = urlparse(endpoint)

        self.timeout = timeout
        self.write_timeout = write_timeout
        self._receive_buffer = bytearray()
        raw_socket: socket.socket | None = None
        try:
            raw_socket = socket.create_connection(
                (parsed.hostname, parsed.port),
                timeout=timeout if connect_timeout is None else connect_timeout,
            )
            if parsed.scheme.lower() == "tls":
                fingerprint_text = (tls_fingerprint or "").strip().lower()
                normalized_fingerprint = fingerprint_text.replace(":", "").replace(" ", "")
                if fingerprint_text and (
                    len(normalized_fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in normalized_fingerprint)
                ):
                    raise ValueError(
                        "TLS certificate fingerprint must contain 64 SHA-256 hex digits"
                    )
                if not tls_verify_certificate or (
                    normalized_fingerprint and tls_ca_file is None and tls_ca_data is None
                ):
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                else:
                    context = ssl.create_default_context(cafile=tls_ca_file)
                    if tls_ca_data is not None:
                        context.load_verify_locations(cadata=tls_ca_data)
                    context.check_hostname = tls_check_hostname
                self._socket = context.wrap_socket(
                    raw_socket,
                    server_hostname=tls_server_hostname or parsed.hostname,
                )
                if normalized_fingerprint:
                    peer_certificate = self._socket.getpeercert(binary_form=True)
                    actual_fingerprint = hashlib.sha256(peer_certificate).hexdigest()
                    if actual_fingerprint != normalized_fingerprint:
                        self._socket.close()
                        raise ssl.SSLError("TLS certificate fingerprint does not match the board")
            else:
                self._socket = raw_socket
            self._socket.settimeout(timeout)
        except (OSError, ValueError) as exc:
            if raw_socket is not None:
                raw_socket.close()
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

    def authenticate(self, token: str) -> None:
        """Authenticate a physical Fluid Reality TCP endpoint."""

        self.write(f"NET AUTH {token}\n".encode("ascii"))
        response = self.readline().decode("ascii", errors="replace").strip("\r\n")
        if response != "OK:NET,OP>AUTH":
            self.close()
            raise TransportError("TCP firmware authentication failed")


class SerialTransport:
    """Line-oriented transport for serial, TCP, and TLS endpoints."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        connect_timeout: float | None = None,
        write_timeout: float | None = 1.0,
        network_token: str | None = None,
        tls_ca_file: str | None = None,
        tls_ca_data: str | None = None,
        tls_fingerprint: str | None = None,
        tls_server_hostname: str | None = None,
        tls_check_hostname: bool = True,
        tls_verify_certificate: bool = True,
        **serial_kwargs: Any,
    ) -> None:
        selected_endpoint = port if port.lower().startswith(("tcp://", "tls://")) else None
        self.port = port
        self.endpoint = selected_endpoint or port
        self.redirected = selected_endpoint is not None

        if selected_endpoint is not None:
            self._serial = _SocketBackend(
                selected_endpoint,
                timeout=timeout,
                connect_timeout=connect_timeout,
                write_timeout=write_timeout,
                tls_ca_file=tls_ca_file,
                tls_ca_data=tls_ca_data,
                tls_fingerprint=tls_fingerprint,
                tls_server_hostname=tls_server_hostname,
                tls_check_hostname=tls_check_hostname,
                tls_verify_certificate=tls_verify_certificate,
            )
            if network_token is not None:
                self._serial.authenticate(network_token)
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
        # A serial-to-network bridge may spend up to one serial polling interval
        # receiving the firmware's response before relaying it to this socket.
        # Keep the short direct-serial drain, but allow redirected connections
        # enough time to consume the expected text-mode synchronization error.
        drain_timeout = max(timeout, 0.25) if self.redirected else timeout
        try:
            self._serial.timeout = drain_timeout
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
