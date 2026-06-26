"""Fluid Reality Python SDK."""

from .boards import Lansing
from .boards.lansing import Diagnosis, LansingConfig, LansingVersion, ManualOutput
from .errors import ErrorInfo, FirmwareError, FluidRealityError, ProtocolError, TransportError

__all__ = [
    "Diagnosis",
    "ErrorInfo",
    "FirmwareError",
    "FluidRealityError",
    "Lansing",
    "LansingConfig",
    "LansingVersion",
    "ManualOutput",
    "ProtocolError",
    "TransportError",
]

__version__ = "0.1.0"
