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
    detection_changed = Signal(int, object)
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
            if not self._ready_fingers(config):
                raise RuntimeError("No mapped actuators are Ready")
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
            if self._disconnect.is_set():
                return
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
        ready_fingers = self._ready_fingers(config)
        unique = tuple(sorted({config.actuators[finger] for finger in ready_fingers}))
        channels: dict[int, _Channel] | None = None
        previous_safety: bool | None = None
        try:
            if self._stop.is_set() or self._disconnect.is_set():
                return
            if not unique:
                raise RuntimeError("No mapped actuators are Ready; demo not started")
            self.phase_changed.emit("POWERING ON")
            board.power_on()
            self._check_voltage(board)
            if self._stop.is_set():
                return

            if config.pattern in ("Slow Wave", "Fast Wave"):
                self._run_wave(board, config, ready_fingers)
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

    def _ready_fingers(self, config: DemoConfig) -> tuple[int, ...]:
        return tuple(
            finger
            for finger, actuator in enumerate(config.actuators)
            if self._actuator_states.get(actuator) is ActuatorState.READY
        )

    def _detect_actuators_at_startup(self, board) -> None:
        try:
            self.phase_changed.emit("POWERING ON")
            board.power_on()
            self._check_voltage(board)
            self._actuator_states = {}
            for actuator in range(board.actuator_count):
                if self._disconnect.is_set():
                    return
                self.phase_changed.emit(f"CHECKING ACTUATOR {actuator}")
                self.detection_changed.emit(actuator, "Detecting")
                try:
                    state = board.detect(actuator)
                except Exception:
                    self._actuator_states[actuator] = ActuatorState.ERROR
                    self.detection_changed.emit(actuator, ActuatorState.ERROR)
                    raise
                self._actuator_states[actuator] = state
                self.detection_changed.emit(actuator, state)
        finally:
            self.phase_changed.emit("POWERING OFF")
            board.power_off()

    def _run_wave(
        self, board, config: DemoConfig, ready_fingers: tuple[int, ...]
    ) -> None:
        wave_fingers: list[int] = []
        seen_actuators: set[int] = set()
        for finger in ready_fingers:
            actuator = config.actuators[finger]
            if actuator not in seen_actuators:
                wave_fingers.append(finger)
                seen_actuators.add(actuator)
        hold_s = 1.0 if config.pattern == "Slow Wave" else 0.25
        step_s = hold_s * 0.75
        active_until: dict[int, float] = {}
        discharging_until: dict[int, float] = {}
        driven_actuators: set[int] = set()
        last_values: dict[int, int] = {}
        next_start = time.monotonic()
        position = 0
        try:
            while not self._stop.is_set() and not self._disconnect.is_set():
                now = time.monotonic()
                for actuator, deadline in tuple(discharging_until.items()):
                    if now >= deadline:
                        del discharging_until[actuator]
                for actuator, deadline in tuple(active_until.items()):
                    if now >= deadline:
                        board.set_actuator(actuator, 0)
                        del active_until[actuator]
                        # Firmware discharges for approximately the preceding
                        # active time. Keep this estimate out of the serial
                        # command path; verify actual discharge on shutdown.
                        discharging_until[actuator] = time.monotonic() + hold_s

                now = time.monotonic()
                if now >= next_start:
                    finger = wave_fingers[position]
                    actuator = config.actuators[finger]
                    self.phase_changed.emit(
                        f"{config.pattern.upper()} / {FINGER_NAMES[finger].upper()}"
                    )
                    # Repeated mappings cannot overlap independently on one
                    # physical channel. Keep its current pulse bounded and
                    # wait for discharge before retriggering it.
                    if actuator not in active_until and actuator not in discharging_until:
                        board.set_actuator(actuator, 255)
                        active_until[actuator] = time.monotonic() + hold_s
                        driven_actuators.add(actuator)
                    position = (position + 1) % len(wave_fingers)
                    next_start = time.monotonic() + step_s

                values = {
                    **dict.fromkeys(discharging_until, -255),
                    **dict.fromkeys(active_until, 255),
                }
                if values != last_values:
                    self.values_changed.emit(values)
                    last_values = values

                next_event = min(
                    (next_start, *active_until.values(), *discharging_until.values())
                )
                self._stop.wait(max(0.0, next_event - time.monotonic()))
        finally:
            zero_error: Exception | None = None
            for actuator in tuple(active_until):
                try:
                    board.set_actuator(actuator, 0)
                except Exception as exc:
                    zero_error = zero_error or exc
            discharge_deadlines = {
                actuator: time.monotonic() + 15.0 for actuator in driven_actuators
            }
            try:
                while discharge_deadlines:
                    self.phase_changed.emit("DISCHARGING / WAVE OUTPUTS")
                    self.values_changed.emit(
                        self._poll_wave_discharge(board, discharge_deadlines)
                    )
                    if discharge_deadlines:
                        time.sleep(0.05)
            finally:
                self.values_changed.emit({})
            if zero_error is not None:
                raise zero_error

    @staticmethod
    def _poll_wave_discharge(board, deadlines: dict[int, float]) -> dict[int, int]:
        status = board.status()
        remaining = status["discharge_ms_left"]
        outputs = status.get("manual_outputs", {})
        values: dict[int, int] = {}
        for actuator, deadline in tuple(deadlines.items()):
            if remaining[actuator] <= 0:
                del deadlines[actuator]
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Actuator {actuator} did not finish discharging")
            if actuator in outputs:
                positive, negative = outputs[actuator]
                values[actuator] = positive - negative
            else:
                # Older status implementations may omit OUT_VALUES.
                values[actuator] = -255
        return values

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
