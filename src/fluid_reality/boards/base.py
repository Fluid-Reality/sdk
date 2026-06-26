"""Shared board-wrapper foundation."""

from __future__ import annotations

from logging import Logger
from typing import Callable, Mapping

from ..errors import ErrorInfo
from ..protocol import LineTransport, TextProtocol


class Board:
    """Base class for command/response Fluid Reality boards."""

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
        self.protocol = TextProtocol(
            transport,
            debug_callback=debug_callback,
            debug_logger=debug_logger,
            log_debug_messages=log_debug_messages,
            error_info=error_info,
        )

    @property
    def debug_lines(self) -> tuple[str, ...]:
        return tuple(self.protocol.debug_lines)

    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
