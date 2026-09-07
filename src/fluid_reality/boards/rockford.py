"""Rockford hardware profile."""

from .bluetooth import BluetoothBoard
from .network import WifiBoard, WifiNetwork


class Rockford(WifiBoard, BluetoothBoard):
    """Eight-channel Rockford board with the shared network extension."""

    actuator_count = 8


__all__ = ["Rockford", "WifiNetwork"]
