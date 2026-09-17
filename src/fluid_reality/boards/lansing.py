"""Lansing hardware profile."""

from .board import (
    ActuatorDetection,
    ActuatorState,
    Board,
    Diagnosis,
    LansingConfig,
    LansingVersion,
    ManualOutput,
)


class Lansing(Board):
    """Twenty-four-channel Lansing board."""

    actuator_count = 24
    not_connected_delta_ma = 0.10


__all__ = [
    "ActuatorDetection",
    "ActuatorState",
    "Diagnosis",
    "Lansing",
    "LansingConfig",
    "LansingVersion",
    "ManualOutput",
]
