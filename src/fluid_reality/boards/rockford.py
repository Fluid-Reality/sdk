"""Rockford hardware profile."""

from .board import ActuatorState
from .bluetooth import BluetoothBoard
from .network import WifiBoard, WifiNetwork


class Rockford(WifiBoard, BluetoothBoard):
    """Eight-channel Rockford board with the shared network extension."""

    actuator_count = 8
    direct_top_bottom_output = True

    def factory_reset(self) -> None:
        """Erase persistent board configuration and reboot (USB only)."""

        self.debug("factory_reset")
        self.raw_command("CFG", "FACTORY_RESET")
        self._actuator_states = [ActuatorState.UNKNOWN] * self.actuator_count
        self._actuator_detections.clear()


__all__ = ["Rockford", "WifiNetwork"]
