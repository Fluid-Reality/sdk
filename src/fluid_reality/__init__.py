"""Fluid Reality Python SDK."""

from .boards import Lansing
from .boards.lansing import (
    ActuatorDetection,
    ActuatorState,
    Diagnosis,
    LansingConfig,
    LansingVersion,
    ManualOutput,
)
from .errors import ErrorInfo, FirmwareError, FluidRealityError, ProtocolError, TransportError
from .transport import list_ports

__all__ = [
    "Diagnosis",
    "ActuatorDetection",
    "ActuatorState",
    "ErrorInfo",
    "FirmwareError",
    "FluidRealityError",
    "Lansing",
    "LansingConfig",
    "LansingVersion",
    "ManualOutput",
    "ProtocolError",
    "TransportError",
    "list_ports",
]

__version__ = "0.1.4"
