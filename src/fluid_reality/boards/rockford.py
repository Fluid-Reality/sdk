"""Rockford hardware profile."""

from dataclasses import dataclass

from .board import ActuatorState
from .bluetooth import BluetoothBoard
from .network import WifiBoard, WifiNetwork


@dataclass(frozen=True)
class RockfordConfig:
    """Rockford VT-budget and common firmware configuration."""

    vt_limit_vs: int
    vt_limit_modified: bool
    safe: bool
    debug: bool
    detection_current_limit_ma: float | None = None
    dt0_error_threshold_ma: float | None = None
    dt1_error_threshold_ma: float | None = None


class Rockford(WifiBoard, BluetoothBoard):
    """Eight-channel Rockford board with the shared network extension."""

    actuator_count = 8
    direct_top_bottom_output = True
    default_vt_limit_vs = 10_000
    max_vt_limit_vs = 4_294_967

    def vt_limit_vs(self, value: int | None = None) -> int:
        """Read or set the per-actuator voltage-time budget in volt-seconds."""

        if value is None:
            return int(self.raw_command("CFG", "VT_LIMIT")[0].fields["VT_LIMIT_VS"])
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("VT limit must be an integer number of V·s")
        if not 1 <= value <= self.max_vt_limit_vs:
            raise ValueError(
                f"VT limit must be between 1 and {self.max_vt_limit_vs} V·s"
            )
        response = self.raw_command("CFG", "VT_LIMIT", value)[0]
        return int(response.fields.get("VT_LIMIT_VS", value))

    def read_config(self) -> RockfordConfig:
        fields = self.raw_command("CFG")[0].fields
        return RockfordConfig(
            vt_limit_vs=int(fields["VT_LIMIT_VS"]),
            vt_limit_modified=fields.get("VT_MODIFIED", "NO").upper() == "YES",
            safe=fields["SAFE"].upper() in {"ON", "1", "TRUE", "YES"},
            debug=fields["DEBUG"].upper() in {"ON", "1", "TRUE", "YES"},
            detection_current_limit_ma=(
                float(fields["DET_MIN"]) if "DET_MIN" in fields else None
            ),
            dt0_error_threshold_ma=(
                float(fields["DT0_ERR"]) if "DT0_ERR" in fields else None
            ),
            dt1_error_threshold_ma=(
                float(fields["DT1_ERR"]) if "DT1_ERR" in fields else None
            ),
        )

    def factory_reset(self) -> None:
        """Erase persistent board configuration and reboot (USB only)."""

        self.debug("factory_reset")
        self.raw_command("CFG", "FACTORY_RESET")
        self._actuator_states = [ActuatorState.UNKNOWN] * self.actuator_count
        self._actuator_detections.clear()


__all__ = ["Rockford", "RockfordConfig", "WifiNetwork"]
