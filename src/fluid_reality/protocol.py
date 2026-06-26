"""Shared text protocol helpers for Fluid Reality boards."""

from __future__ import annotations

from dataclasses import dataclass, field
from logging import Logger
from typing import Callable, Iterable, Mapping, Protocol

from .errors import ErrorInfo, FirmwareError, ProtocolError


class LineTransport(Protocol):
    """Minimal transport contract used by board wrappers."""

    def write_line(self, line: str) -> None:
        """Write a newline-terminated text command."""

    def read_line(self) -> str:
        """Read one decoded response line without trailing newline characters."""

    def write_bytes(self, data: bytes) -> None:
        """Write raw binary bytes."""

    def close(self) -> None:
        """Close the transport."""


@dataclass(frozen=True)
class Response:
    """Parsed firmware response."""

    status: str
    raw: str
    payload: str
    values: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    @property
    def error_code(self) -> str:
        if self.status != "ER":
            raise ProtocolError("OK responses do not have an error code")
        if self.values:
            return self.values[0]
        if self.fields:
            return next(iter(self.fields))
        return self.payload


def parse_response_line(line: str) -> Response:
    """Parse one ``OK:`` or ``ER:`` line."""

    if line.startswith("OK:"):
        status = "OK"
        payload = line[3:]
    elif line.startswith("ER:"):
        status = "ER"
        payload = line[3:]
    else:
        raise ProtocolError(f"Unexpected response line: {line!r}")

    values: list[str] = []
    fields: dict[str, str] = {}
    if payload:
        for item in payload.split(","):
            if ">" in item:
                key, value = item.split(">", 1)
                fields[key] = value
            else:
                values.append(item)

    return Response(
        status=status,
        raw=line,
        payload=payload,
        values=tuple(values),
        fields=fields,
    )


class TextProtocol:
    """Command/response helper for the firmware text protocol."""

    def __init__(
        self,
        transport: LineTransport,
        *,
        debug_callback: Callable[[str], None] | None = None,
        debug_logger: Logger | None = None,
        log_debug_messages: bool = False,
        error_info: Mapping[str, ErrorInfo] | None = None,
    ) -> None:
        self.transport = transport
        self.debug_callback = debug_callback
        self.debug_logger = debug_logger
        self.log_debug_messages = log_debug_messages
        self.error_info = error_info or {}
        self.debug_lines: list[str] = []

    def command(self, command: str, *params: object, ok_lines: int = 1) -> list[Response]:
        line = self.format_command(command, params)
        self.transport.write_line(line)
        return self.read_result(ok_lines=ok_lines)

    def read_result(self, *, ok_lines: int = 1) -> list[Response]:
        responses: list[Response] = []
        while len(responses) < ok_lines:
            line = self.transport.read_line()
            if line.startswith("DBG:"):
                self.debug_lines.append(line)
                if self.log_debug_messages and self.debug_logger is not None:
                    self.debug_logger.debug(line)
                if self.debug_callback is not None:
                    self.debug_callback(line)
                continue

            response = parse_response_line(line)
            if response.status == "ER":
                code = response.error_code
                raise FirmwareError(
                    code=code,
                    raw=response.raw,
                    fields=response.fields,
                    info=self.error_info.get(code),
                )
            responses.append(response)
        return responses

    @staticmethod
    def format_command(command: str, params: Iterable[object]) -> str:
        command = command.strip().upper()
        if len(command) != 3:
            raise ProtocolError(f"Command must be exactly 3 characters: {command!r}")
        parts = [command]
        parts.extend(str(param) for param in params)
        return " ".join(parts)
