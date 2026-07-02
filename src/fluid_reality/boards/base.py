"""Shared board-wrapper foundation."""

from __future__ import annotations

from logging import Logger
from typing import Callable, Mapping

from ..debug import DebugDestination, DebugOut
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
        self._debug_out = DebugOut()
        self.protocol = TextProtocol(
            transport,
            debug_callback=debug_callback,
            debug_logger=debug_logger,
            log_debug_messages=log_debug_messages,
            error_info=error_info,
            debug_out=self._debug_out,
        )

    @property
    def debug_lines(self) -> tuple[str, ...]:
        return tuple(self.protocol.debug_lines)

    def set_debug_out(self, destination: DebugDestination) -> None:
        self._debug_out.set_destination(destination)

    def debug(self, event: str, **fields: object) -> None:
        self._debug_out.emit(self.__class__.__name__, event, **fields)

    def close(self) -> None:
        try:
            self.debug("close")
            self.transport.close()
        finally:
            self._debug_out.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
