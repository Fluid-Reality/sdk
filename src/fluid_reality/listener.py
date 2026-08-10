"""Raw TCP listener primitives for simulated Fluid Reality devices."""

from __future__ import annotations

import socket
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
    """Synchronous loopback TCP listener for raw simulated-device byte streams.

    The listener performs no decoding, line splitting, buffering, or framing.
    Device protocol implementations receive exactly the bytes carried by TCP.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, backlog: int = 1) -> None:
        self.host = host
        self.port = port
        self.backlog = backlog
        self._socket: socket.socket | None = None

    def start(self) -> TcpDeviceListener:
        if self._socket is not None:
            raise RuntimeError("TCP device listener is already running")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(self.backlog)
        except OSError:
            listener.close()
            raise
        self._socket = listener
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
        return f"tcp://{host}:{port}"

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
        return TcpDeviceConnection(connection, (str(address[0]), int(address[1])))

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> TcpDeviceListener:  # noqa: PYI034 -- Python 3.10 has no typing.Self
        return self.start()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
