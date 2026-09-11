"""Raw TCP listener primitives for simulated Fluid Reality devices."""

from __future__ import annotations

import socket
import ssl
import threading
from types import TracebackType

from .errors import TransportError


class TcpDeviceConnection:
    """One raw byte-stream connection accepted by :class:`TcpDeviceListener`."""

    def __init__(self, connection: socket.socket, address: tuple[str, int]) -> None:
        self._socket = connection
        self.address = address
        self._write_lock = threading.Lock()
        self._closed = False

    def read_bytes(self, maximum: int = 4096) -> bytes:
        """Read up to ``maximum`` bytes; return ``b''`` after peer disconnect."""
        if maximum <= 0:
            raise ValueError("maximum must be greater than zero")
        try:
            return self._socket.recv(maximum)
        except OSError as exc:
            if self._closed:
                return b""
            raise TransportError(f"Could not read from TCP device connection: {exc}") from exc

    def write_bytes(self, data: bytes) -> None:
        """Write every byte using TCP ``sendall`` semantics."""
        try:
            with self._write_lock:
                self._socket.sendall(data)
        except OSError as exc:
            raise TransportError(f"Could not write to TCP device connection: {exc}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()

    def __enter__(self) -> TcpDeviceConnection:  # noqa: PYI034 -- Python 3.10 has no typing.Self
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class TcpDeviceListener:
    """Synchronous TCP/TLS listener for raw simulated-device byte streams.

    The listener performs no decoding, line splitting, buffering, or framing.
    Device protocol implementations receive exactly the bytes carried by TCP.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 49765,
        *,
        backlog: int = 1,
        tls_certfile: str | None = None,
        tls_keyfile: str | None = None,
        tls_key_password: str | None = None,
        tls_handshake_timeout: float = 5.0,
    ) -> None:
        if (tls_certfile is None) != (tls_keyfile is None):
            raise ValueError("TLS certificate and private key must be provided together")
        if tls_handshake_timeout <= 0:
            raise ValueError("TLS handshake timeout must be greater than zero")
        self.host = host
        self.port = port
        self.backlog = backlog
        self.tls_certfile = tls_certfile
        self.tls_keyfile = tls_keyfile
        self.tls_key_password = tls_key_password
        self.tls_handshake_timeout = tls_handshake_timeout
        self._socket: socket.socket | None = None
        self._tls_context: ssl.SSLContext | None = None

    def start(self) -> TcpDeviceListener:
        if self._socket is not None:
            raise RuntimeError("TCP device listener is already running")
        tls_context: ssl.SSLContext | None = None
        if self.tls_certfile is not None and self.tls_keyfile is not None:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                tls_context.load_cert_chain(
                    self.tls_certfile,
                    self.tls_keyfile,
                    self.tls_key_password,
                )
            except (OSError, ssl.SSLError) as exc:
                raise TransportError(f"Could not load TLS server credentials: {exc}") from exc

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(self.backlog)
        except OSError:
            listener.close()
            raise
        self._socket = listener
        self._tls_context = tls_context
        return self

    @property
    def address(self) -> tuple[str, int]:
        if self._socket is None:
            raise RuntimeError("TCP device listener is not running")
        host, port = self._socket.getsockname()[:2]
        return str(host), int(port)

    @property
    def endpoint(self) -> str:
        host, port = self.address
        scheme = "tls" if self._tls_context is not None else "tcp"
        return f"{scheme}://{host}:{port}"

    def accept(self, *, timeout: float | None = None) -> TcpDeviceConnection:
        """Accept one client, raising ``TimeoutError`` if ``timeout`` expires."""
        listener = self._socket
        if listener is None:
            raise RuntimeError("TCP device listener is not running")
        listener.settimeout(timeout)
        try:
            connection, address = listener.accept()
        except TimeoutError as exc:
            raise TimeoutError("Timed out waiting for a TCP device client") from exc
        if self._tls_context is not None:
            raw_connection = connection
            try:
                raw_connection.settimeout(self.tls_handshake_timeout)
                connection = self._tls_context.wrap_socket(
                    raw_connection,
                    server_side=True,
                )
                connection.settimeout(None)
            except (OSError, ssl.SSLError) as exc:
                raw_connection.close()
                raise TransportError(f"TLS handshake failed: {exc}") from exc
        return TcpDeviceConnection(connection, (str(address[0]), int(address[1])))

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._tls_context = None

    def __enter__(self) -> TcpDeviceListener:  # noqa: PYI034 -- Python 3.10 has no typing.Self
        return self.start()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
