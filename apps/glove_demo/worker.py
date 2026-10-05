"""Board control and waveform scheduling for the Glove Demo."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from fluid_reality import ActuatorState, Board, Lansing, Rockford


FINGER_NAMES = ("Pinky", "Ring", "Middle", "Index", "Thumb")
PATTERNS = ("Pulse", "Snap", "Slow Wave", "Fast Wave", "Square Wave")
UPDATE_INTERVAL_S = 0.025
MAX_IMBALANCE_OUTPUT_SECONDS = 255 * 1.5


@dataclass(frozen=True)
class DemoConfig:
    pattern: str
    actuators: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.pattern not in PATTERNS:
            raise ValueError(f"Unknown demo pattern: {self.pattern}")
        if len(self.actuators) != 5 or any(not 0 <= number <= 4 for number in self.actuators):
            raise ValueError("Assign each finger an actuator number from 0 through 4")


def pattern_values(pattern: str, elapsed_s: float) -> tuple[float, ...]:
    """Signed fingertip levels for the three bipolar patterns.

    Pulse keeps the requested positive ramp and -255 amplitude while shortening
    the negative dwell to 0.25 s. Its final 0.25 s at zero balances the area
    under the waveform over each one-second cycle.
    """

    phase = max(0.0, elapsed_s) % 1.0
    if pattern == "Pulse":
        level = 510 * phase if phase < 0.5 else (-255 if phase < 0.75 else 0)
    elif pattern == "Snap":
        level = 255 - 510 * phase
    elif pattern == "Square Wave":
        level = 255 if phase < 0.5 else -255
    else:
        raise ValueError(f"{pattern} is not a bipolar pattern")
    return (level,) * 5


@dataclass
class _Channel:
    value: int
    changed_at: float
    area: float = 0.0


class BoardThread(QThread):
    """Own the board on one thread while accepting thread-safe stop requests."""

    connected = Signal(str, str)
    disconnected = Signal()
    failed = Signal(str)
    values_changed = Signal(object)
    running_changed = Signal(bool)
    phase_changed = Signal(str)

    def __init__(
        self,
        endpoint: str,
        board_type: type[Board] | None = None,
        options: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._board_type = board_type
        self._options = options or {}
        self._lock = threading.Lock()
        self._pending: DemoConfig | None = None
        self._running = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._disconnect = threading.Event()
        self._actuator_states: dict[int, ActuatorState] = {}

    def start_demo(self, config: DemoConfig) -> None:
        with self._lock:
            if self._running or self._pending is not None or self._disconnect.is_set():
                raise RuntimeError("A demo is already starting or running")
            self._pending = config
        self._stop.clear()
        self._wake.set()

    def stop_demo(self) -> None:
        self._stop.set()
        self._wake.set()

    def disconnect(self) -> None:
        self._disconnect.set()
        self._stop.set()
        self._wake.set()

    def _open_board(self) -> Board:
        if self._board_type is not None:
            board = self._board_type(self._endpoint, **self._options)
            try:
                board.force_text_mode()
            except Exception:
                board.close()
                raise
            return board

        # Probe the shared text protocol without assuming a hardware profile.
        # Rewrap the same transport after VER so the serial port is opened once.
        probe = Board(self._endpoint, **self._options)
        try:
            probe.force_text_mode()
            firmware = probe.version().get("FW", "").strip()
            board_type = {"lansing": Lansing, "rockford": Rockford}.get(
                firmware.casefold()
            )
            if board_type is None:
                raise RuntimeError(
                    f"Unsupported board firmware {firmware or '(missing FW in VER)'}"
                )
            return board_type(transport=probe.transport)
        except Exception:
            probe.close()
            raise

    def run(self) -> None:  # type: ignore[override]
        board = None
        try:
            self.phase_changed.emit("IDENTIFYING BOARD")
            board = self._open_board()
            self._detect_actuators_at_startup(board)
            self.connected.emit(self._endpoint, type(board).__name__)
            while not self._disconnect.is_set():
                with self._lock:
                    config = self._pending
                    self._pending = None
                    if config is not None:
                        self._running = True
                if config is None:
                    self._wake.wait(0.1)
                    self._wake.clear()
                    continue
                self.running_changed.emit(True)
                try:
                    self._run_config(board, config)
                finally:
                    self.values_changed.emit({})
                    with self._lock:
                        self._running = False
                    self.running_changed.emit(False)
                    self._stop.clear()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if board is not None:
                try:
                    board.power_off()
                except Exception:
                    pass
                try:
                    board.close()
                except Exception as exc:
                    self.failed.emit(f"Unable to close board connection: {exc}")
            self.disconnected.emit()

    def _run_config(self, board, config: DemoConfig) -> None:
        unique = tuple(sorted(set(config.actuators)))
        channels: dict[int, _Channel] | None = None
        previous_safety: bool | None = None
        try:
            if self._stop.is_set() or self._disconnect.is_set():
                return
            self.phase_changed.emit("POWERING ON")
            board.power_on()
            self._check_voltage(board)
            for actuator in unique:
                state = self._actuator_states.get(actuator, ActuatorState.UNKNOWN)
                if state is not ActuatorState.READY:
                    raise RuntimeError(f"Actuator {actuator} is {state.value}; demo not started")
            if self._stop.is_set():
                return

            if config.pattern in ("Slow Wave", "Fast Wave"):
                self._run_wave(board, config)
            else:
                previous_safety = board.safety()
                if previous_safety:
                    board.safety(False)
                now = time.monotonic()
                channels = {actuator: _Channel(0, now) for actuator in unique}
                for actuator in unique:
                    board.set_manual_output(actuator, 0, 0)
                self._run_bipolar(board, config, channels)
        finally:
            try:
                if channels is not None:
                    self._balance_and_zero(board, channels)
            finally:
                try:
                    if previous_safety is not None:
                        board.safety(previous_safety)
                finally:
                    self.phase_changed.emit("POWERING OFF")
                    board.power_off()

    @staticmethod
    def _check_voltage(board) -> None:
        deadline = time.monotonic() + 2.0
        while True:
            if board.voltage() > 0:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Supply voltage is 0 V; check the power adapter")
            time.sleep(0.1)

    def _detect_actuators_at_startup(self, board) -> None:
        try:
            self.phase_changed.emit("POWERING ON")
            board.power_on()
            self._check_voltage(board)
            self.phase_changed.emit("CHECKING ACTUATORS")
            states: dict[int, ActuatorState] = {}
            for actuator in range(board.actuator_count):
                if self._disconnect.is_set():
                    return
                states[actuator] = board.detect(actuator)
            self._actuator_states = states
        finally:
            self.phase_changed.emit("POWERING OFF")
            board.power_off()

    def _run_wave(self, board, config: DemoConfig) -> None:
        hold_s = 1.0 if config.pattern == "Slow Wave" else 0.25
        finger = 0
        while not self._stop.is_set():
            actuator = config.actuators[finger]
            self.phase_changed.emit(f"{config.pattern.upper()} / {FINGER_NAMES[finger].upper()}")
            driven = False
            try:
                board.set_actuator(actuator, 255)
                driven = True
                self.values_changed.emit({actuator: 255})
                self._stop.wait(hold_s)
            finally:
                if driven:
                    board.set_actuator(actuator, 0)
                    self._wait_for_discharge(board, actuator)
            finger = (finger + 1) % 5

    def _wait_for_discharge(self, board, actuator: int) -> None:
        self.phase_changed.emit(f"DISCHARGING / ACTUATOR {actuator}")
        deadline = time.monotonic() + 15.0
        while True:
            status = board.status()
            remaining_ms = status["discharge_ms_left"][actuator]
            if remaining_ms <= 0:
                self.values_changed.emit({})
                return
            outputs = status.get("manual_outputs", {})
            if actuator in outputs:
                positive, negative = outputs[actuator]
                value = positive - negative
            else:
                # Older status implementations may omit OUT_VALUES.
                value = -255
            self.values_changed.emit({actuator: value} if value else {})
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Actuator {actuator} did not finish discharging")
            time.sleep(0.05)

    @staticmethod
    def _set_manual(board, actuator: int, channel: _Channel, value: int) -> None:
        if value == channel.value:
            return
        board.set_manual_output(actuator, max(0, value), max(0, -value))
        now = time.monotonic()
        channel.area += channel.value * (now - channel.changed_at)
        channel.value = value
        channel.changed_at = now

    def _run_bipolar(self, board, config: DemoConfig, channels: dict[int, _Channel]) -> None:
        self.phase_changed.emit(f"RUNNING / {config.pattern.upper()}")
        started = time.monotonic()
        while not self._stop.is_set():
            elapsed = time.monotonic() - started
            finger_values = pattern_values(config.pattern, elapsed)
            # Every bipolar pattern drives the five positions equally. A
            # repeated mapping therefore receives one physical command.
            value = round(finger_values[0])
            for actuator, channel in channels.items():
                self._set_manual(board, actuator, channel, value)
                projected_area = channel.area + channel.value * (
                    time.monotonic() - channel.changed_at
                )
                if abs(projected_area) > MAX_IMBALANCE_OUTPUT_SECONDS:
                    raise RuntimeError("Manual-output exposure limit reached")
            self.values_changed.emit({actuator: value for actuator in channels})
            next_tick = started + (int(elapsed / UPDATE_INTERVAL_S) + 1) * UPDATE_INTERVAL_S
            self._stop.wait(max(0.0, next_tick - time.monotonic()))

    def _balance_and_zero(self, board, channels: dict[int, _Channel]) -> None:
        self.phase_changed.emit("BALANCING OUTPUT")
        first_error: Exception | None = None
        for actuator, channel in channels.items():
            try:
                # Force an explicit zero even when our last recorded value is
                # zero, including after a failed setup command.
                board.set_manual_output(actuator, 0, 0)
                now = time.monotonic()
                channel.area += channel.value * (now - channel.changed_at)
                channel.value = 0
                channel.changed_at = now
            except Exception as exc:
                first_error = first_error or exc
        self.values_changed.emit({})
        if first_error is not None:
            raise first_error

        try:
            # Two short passes absorb the extra exposure incurred while each
            # zero command is acknowledged.
            for _ in range(2):
                deadlines: list[tuple[float, int]] = []
                for actuator, channel in channels.items():
                    if abs(channel.area) < 255 * 0.01:
                        continue
                    compensation = -255 if channel.area > 0 else 255
                    duration = abs(channel.area) / 255
                    self._set_manual(board, actuator, channel, compensation)
                    deadlines.append((time.monotonic() + duration, actuator))
                if not deadlines:
                    break
                self.values_changed.emit(
                    {actuator: channel.value for actuator, channel in channels.items()}
                )
                for deadline, actuator in sorted(deadlines):
                    time.sleep(max(0.0, deadline - time.monotonic()))
                    self._set_manual(board, actuator, channels[actuator], 0)
        finally:
            # If compensation fails partway through, force every channel back
            # to zero before restoring safety and switching the PSU off.
            zero_error: Exception | None = None
            for actuator, channel in channels.items():
                try:
                    board.set_manual_output(actuator, 0, 0)
                    channel.value = 0
                except Exception as exc:
                    zero_error = zero_error or exc
            self.values_changed.emit({})
            if zero_error is not None:
                raise zero_error
