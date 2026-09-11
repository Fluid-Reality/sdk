"""Board wrappers provided by the Fluid Reality SDK."""

from .board import (
    ActuatorDetection,
    ActuatorState,
    Board,
    Diagnosis,
    FirmwareUpdateResult,
    LansingConfig,
    LansingVersion,
    ManualOutput,
)
from .bluetooth import BluetoothBoard
from .lansing import Lansing
from .network import (
    ConfigurableNetworkBoard,
    EthernetBoard,
    NetworkBoard,
    WifiBoard,
    WifiNetwork,
)
from .rockford import Rockford, RockfordConfig

__all__ = [
    "BluetoothBoard",
    "ActuatorDetection",
    "ActuatorState",
    "Board",
    "ConfigurableNetworkBoard",
    "Diagnosis",
    "EthernetBoard",
    "FirmwareUpdateResult",
    "Lansing",
    "LansingConfig",
    "LansingVersion",
    "ManualOutput",
    "NetworkBoard",
    "Rockford",
    "RockfordConfig",
    "WifiBoard",
    "WifiNetwork",
]
