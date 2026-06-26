"""Serial transport implementation."""

from __future__ import annotations

import time
from typing import Any

from .errors import TransportError


class SerialTransport:
    """Line-oriented wrapper around pyserial."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 115200,
        timeout: float = 1.0,
        write_timeout: float | None = 1.0,
        **serial_kwargs: Any,
    ) -> None:
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
            raise TransportError(f"Could not read from serial port: {exc}") from exc
        if not data:
            raise TransportError("Timed out waiting for firmware response")
        return data.decode("ascii", errors="replace").strip("\r\n")

    def write_bytes(self, data: bytes) -> None:
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not write to serial port: {exc}") from exc

    def reset_input_buffer(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception as exc:  # pragma: no cover - hardware dependent.
            raise TransportError(f"Could not reset serial input buffer: {exc}") from exc

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
            raise TransportError(f"Could not drain serial input buffer: {exc}") from exc
        finally:
            self._serial.timeout = original_timeout
        return tuple(lines)

    def wait_for_quiet(self, *, quiet_s: float = 0.05) -> None:
        time.sleep(quiet_s)

    def close(self) -> None:
        self._serial.close()
