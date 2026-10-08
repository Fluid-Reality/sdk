"""In-process simulated Rockford boards for demo mode and tests.

Wraps the SDK simulator's protocol engine
(``apps/fluidreality_simulator/simulator.py``) in a line transport, so the app
talks to it through the real :class:`fluid_reality.Rockford` class. Commands
that block on real firmware (``OUC``, ``CUR``, ``VLT ms``, ``DT0``/``DT1``)
sleep for the same time here, divided by the demo clock speed.
"""

from __future__ import annotations

import collections
import sys
import time
from dataclasses import replace
from pathlib import Path

from fluid_reality import Rockford, TransportError

APPS_ROOT = Path(__file__).resolve().parent.parent
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from fluidreality_simulator.simulator import (  # noqa: E402
    FluidRealityDeviceSimulator,
    SimulatedActuator,
    SimulatorConfig,
)

DT0_S = 1.25
DT1_S = 2.5
STS_S = 1.0


def _actuator(name: str, start_ma: float, floor_ma: float, noise_ma: float = 0.015) -> SimulatedActuator:
    return SimulatedActuator(
        guid=f"demo-{name}",
        name=name,
        max_starting_current_ma=start_ma,
        offline_current_increase_ma_s=0.002,
        running_current_decrease_ma_v_s=0.00025,
        min_running_current_ma=floor_ma,
        current_noise_ma=noise_ma,
    )


DEMO_BOARDS: dict[str, SimulatorConfig] = {
    "Demo bench A": SimulatorConfig(
        name="Demo bench A",
        psu_voltage_v=212.0,
        psu_voltage_noise_v=0.8,
        psu_base_current_ma=0.18,
        psu_base_current_noise_ma=0.01,
        board_type="rockford",
        actuators={
            0: _actuator("A0", 3.6, 0.55),
            1: _actuator("A1", 2.2, 0.45),
            2: _actuator("A2", 4.8, 0.9, 0.03),
            4: _actuator("A4", 1.6, 0.4),
        },
    ),
    "Demo bench B": SimulatorConfig(
        name="Demo bench B",
        psu_voltage_v=208.0,
        psu_voltage_noise_v=0.8,
        psu_base_current_ma=0.22,
        psu_base_current_noise_ma=0.01,
        board_type="rockford",
        actuators={
            0: _actuator("B0", 2.9, 0.5),
            1: _actuator("B1", 3.3, 0.6),
        },
    ),
}


class SimulatedRockfordTransport:
    """Line transport backed by the SDK's simulator engine."""

    def __init__(self, config: SimulatorConfig, *, speed: float = 1.0, name: str = "demo") -> None:
        self.endpoint = f"sim://{name}"
        self.speed = max(0.01, float(speed))
        self._lines: collections.deque[str] = collections.deque()
        self._partial = bytearray()
        self.engine = FluidRealityDeviceSimulator(
            self._receive,
            config,
            board_type="rockford",
            diagnosis_delay_s=0.0,
            random_seed=None,
        )
        self.closed = False

    def _receive(self, data: bytes) -> None:
        self._partial.extend(data)
        while b"\n" in self._partial:
            line, _, rest = bytes(self._partial).partition(b"\n")
            self._partial = bytearray(rest)
            self._lines.append(line.decode("ascii").rstrip("\r"))

    def _blocking_time_s(self, line: str) -> float:
        parts = line.replace(",", " ").split()
        if not parts:
            return 0.0
        command = parts[0].upper()
        if command == "OUC" and len(parts) == 5:
            return int(parts[4]) / 1000.0
        if command == "CUR":
            return 0.5
        if command == "VLT" and len(parts) == 2:
            return int(parts[1]) / 1000.0
        if command == "DT0":
            return DT0_S if len(parts) == 2 else DT0_S * 4
        if command == "DT1":
            return DT1_S
        if command == "STS":
            return STS_S
        return 0.0

    def write_line(self, line: str) -> None:
        if self.closed:
            raise TransportError("Simulated board is closed")
        delay = self._blocking_time_s(line)
        parts = line.split()
        if parts and parts[0].upper() == "VLT" and len(parts) == 2:
            # The engine itself sleeps for VLT ms; avoid sleeping twice.
            line = "VLT"
        if delay:
            time.sleep(delay / self.speed)
        self.engine.feed((line + "\n").encode("ascii"))

    def read_line(self) -> str:
        if not self._lines:
            raise TransportError("Timed out waiting for firmware response")
        return self._lines.popleft()

    def write_bytes(self, data: bytes) -> None:
        self.engine.feed(data)

    def close(self) -> None:
        self.closed = True


def demo_board_factory(name: str, *, speed: float = 1.0):
    """Factory for a demo board; actuator physics are sped up with the clock."""

    base = DEMO_BOARDS[name]
    config = replace(
        base,
        actuators={
            channel: replace(
                profile,
                running_current_decrease_ma_v_s=profile.running_current_decrease_ma_v_s * speed,
                offline_current_increase_ma_s=profile.offline_current_increase_ma_s * speed,
            )
            for channel, profile in base.actuators.items()
        },
    )

    def factory() -> Rockford:
        return Rockford(transport=SimulatedRockfordTransport(config, speed=speed, name=name))

    return factory
