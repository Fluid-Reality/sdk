"""Exceptions raised by the Fluid Reality SDK."""

from __future__ import annotations

from dataclasses import dataclass, field


class FluidRealityError(Exception):
    """Base exception for all SDK errors."""


class TransportError(FluidRealityError):
    """Raised when the serial transport cannot be opened, read, or written."""


class ProtocolError(FluidRealityError):
    """Raised when a device response does not match the expected protocol."""


@dataclass(frozen=True)
class ErrorInfo:
    """Human-readable details for a stable firmware error code."""

    meaning: str
    common_cause: str
    recovery: str


@dataclass
class FirmwareError(FluidRealityError):
    """Raised when firmware returns an ``ER:`` response."""

    code: str
    raw: str
    fields: dict[str, str] = field(default_factory=dict)
    info: ErrorInfo | None = None

    def __str__(self) -> str:
        message = self.code
        if self.info is not None:
            message = f"{message}: {self.info.meaning}"
        if self.fields:
            fields = ",".join(f"{key}>{value}" for key, value in self.fields.items())
            message = f"{message} ({fields})"
        return message
