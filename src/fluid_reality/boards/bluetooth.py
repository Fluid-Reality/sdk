"""Composable Bluetooth capability for Fluid Reality boards."""

from __future__ import annotations

from .board import Board


class BluetoothBoard(Board):
    """A board exposing reusable ``BLT`` Bluetooth configuration commands."""

    bluetooth_capability = True

    def bluetooth_status(self) -> dict[str, str]:
        return self.raw_command("BLT", "STATUS")[0].fields

    def set_bluetooth_enabled(self, enabled: bool) -> dict[str, str]:
        return self.raw_command("BLT", "ON" if enabled else "OFF")[0].fields

    def set_bluetooth_name(self, name: str) -> dict[str, str]:
        """Persist the suffix of the firmware-enforced ``FR-`` name."""

        suffix = str(name).strip()
        if (
            not suffix
            or len(suffix) > 28
            or not suffix.isascii()
            or suffix.upper().startswith("FR-")
            or not all(
                character.isalnum() or character in "-_" for character in suffix
            )
        ):
            raise ValueError(
                "Bluetooth name suffix must be 1-28 ASCII letters, digits, "
                "hyphens, or underscores"
            )
        return self.raw_command("BLT", "NAME", suffix)[0].fields

    def set_bluetooth_security(self, enabled: bool) -> dict[str, str]:
        return self.raw_command("BLT", "SEC", "ON" if enabled else "OFF")[0].fields

    def clear_bluetooth_bonds(self) -> dict[str, str]:
        return self.raw_command("BLT", "BONDS", "CLEAR")[0].fields


__all__ = ["BluetoothBoard"]
