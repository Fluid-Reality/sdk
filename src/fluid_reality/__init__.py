"""Fluid Reality Python SDK."""

from .boards import (
    Board,
    BluetoothBoard,
    ConfigurableNetworkBoard,
    EthernetBoard,
    Lansing,
    NetworkBoard,
    Rockford,
    RockfordConfig,
    WifiBoard,
    WifiNetwork,
)
from .bluetooth import (
    BluetoothDevice,
    BluetoothTransport,
    discover_bluetooth_boards,
    discover_bluetooth_boards_async,
    is_bluetooth_endpoint,
)
from .boards.board import (
    ActuatorDetection,
    ActuatorState,
    Diagnosis,
    FirmwareUpdateResult,
    LansingConfig,
    LansingVersion,
    ManualOutput,
)
from .errors import (
    ErrorInfo,
    FirmwareError,
    FluidRealityError,
    ProtocolError,
    TransportError,
)
from .connection import (
    ConnectionProfile,
    load_connection_file,
    open_board_from_connection_file,
)
from .listener import TcpDeviceConnection, TcpDeviceListener
from .transport import is_virtual_port, list_ports

__all__ = [
    "ActuatorDetection",
    "ActuatorState",
    "Board",
    "BluetoothBoard",
    "BluetoothDevice",
    "BluetoothTransport",
    "ConfigurableNetworkBoard",
    "ConnectionProfile",
    "Diagnosis",
    "EthernetBoard",
    "ErrorInfo",
    "FirmwareError",
    "FluidRealityError",
    "FirmwareUpdateResult",
    "Lansing",
    "LansingConfig",
    "LansingVersion",
    "ManualOutput",
    "NetworkBoard",
    "Rockford",
    "RockfordConfig",
    "WifiNetwork",
    "ProtocolError",
    "TcpDeviceConnection",
    "TcpDeviceListener",
    "TransportError",
    "WifiBoard",
    "discover_bluetooth_boards",
    "discover_bluetooth_boards_async",
    "is_bluetooth_endpoint",
    "is_virtual_port",
    "list_ports",
    "load_connection_file",
    "open_board_from_connection_file",
]

__version__ = "0.2.4"
