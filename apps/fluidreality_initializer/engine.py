"""Board engine: runs independent actuator initializations on Rockford boards.

Design
======
* One :class:`BoardSession` thread owns one board connection. Every command to
  that board is issued from that thread, so board control is strictly
  serialized ("control blocking"): nothing else can talk to the board while a
  measurement is in progress.
* Each actuator on the board has its own :class:`ActuatorProcess` (config,
  plan, clock, data, report). Processes never share state; a failure in one
  only stops that one. Only a board-level fault (lost connection, supply off,
  another client taking control) stops every process on the board.
* Drive uses the same firmware path as the SDK dashboard's tools: safety off,
  ``OUT`` to set an output and ``OUC`` to set an output and measure current.
  Positive volts -> ``TOP=value, BOTTOM=0``; negative -> ``TOP=255-value,
  BOTTOM=1``; ``value = round(255 * |V| / supply)``.
* Current is only measured at phase boundaries. A boundary event pauses every
  other process on the board (outputs to 0 V, clocks frozen), then takes
  ``OUC`` at the end of the old phase, an all-off baseline, and ``OUC`` at the
  start of the new phase. The measured windows count as drive time of their
  phases, so each phase receives its configured exposure.
* A 1 s ``VLT`` poll doubles as the keep-alive for the firmware's 10 s
  control-lease watchdog and as supply telemetry.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fluid_reality import ActuatorState, FirmwareError, ProtocolError, Rockford, TransportError

from .sequence import PHASE_HIGH, InitializationConfig, Phase, SequencePlan, build_plan

MAX_CHANNELS = 5
MIN_SUPPLY_V = 20.0
KEEPALIVE_INTERVAL_S = 1.0
SNAPSHOT_INTERVAL_S = 0.25
REPORT_AUTOSAVE_S = 120.0

# Process states
IDLE = "idle"
CHECKING = "checking"
RUNNING = "running"
PAUSED = "paused"
COMPLETED = "completed"
STOPPED = "stopped"
FAILED = "failed"
ACTIVE_STATES = {CHECKING, RUNNING, PAUSED}
FINAL_STATES = {COMPLETED, STOPPED, FAILED}


class BoardFault(RuntimeError):
    """A failure that affects every process on the board."""


class StartRejected(ValueError):
    """A start request refused before any process was created (only logged)."""


class Clock:
    """Monotonic clock with an optional speed factor (demo mode only)."""

    def __init__(self, speed: float = 1.0) -> None:
        self.speed = float(speed)
        self._origin = time.monotonic()

    def now(self) -> float:
        return (time.monotonic() - self._origin) * self.speed

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds / self.speed)


def utc_now() -> str:
    """Local time with UTC offset (ISO 8601), e.g. 2026-10-08T14:03:11-05:00."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Measurement:
    t_s: float  # seconds since the process started (wall, includes pauses)
    timestamp: str
    run_id: int
    phase_index: int
    phase_kind: str
    cycle: int | None
    edge: str  # "start" or "end"
    voltage_v: float  # run drive voltage (|V| of the high phase)
    high_time_s: float
    repeat: int
    target_v: float  # signed target for this phase
    applied_v: float  # signed voltage actually applied (DAC-quantised)
    current_ma: float
    baseline_ma: float
    delta_ma: float  # current - baseline
    window_ms: int
    supply_v: float


@dataclass
class PhaseRecord:
    index: int
    run_id: int
    kind: str
    cycle: int | None
    voltage_v: float
    high_time_s: float
    repeat: int
    target_v: float
    applied_v: float = 0.0
    started_t_s: float = 0.0
    ended_t_s: float | None = None
    drive_s: float = 0.0  # time this phase's output was applied
    interrupted_s: float = 0.0  # time frozen while another actuator was measured
    start_delta_ma: float | None = None
    end_delta_ma: float | None = None
    start_current_ma: float | None = None
    end_current_ma: float | None = None
    completed: bool = False  # False when the phase was cut short by stop/fail


@dataclass(frozen=True)
class CheckResult:
    stage: str  # "pre", "post" or "detect"
    timestamp: str
    state: str
    baseline_ma: float
    forward_ma: float
    delta_ma: float
    initial_delta_ma: float | None


@dataclass
class Event:
    kind: str
    board: str
    channel: int | None = None
    data: Any = None


@dataclass
class ActuatorProcess:
    """One actuator's independent initialization."""

    board_key: str
    channel: int
    actuator_id: str
    config: InitializationConfig
    plan: SequencePlan
    notes: str = ""
    operator: str = ""
    status: str = IDLE
    status_detail: str = ""
    phase_pos: int = -1
    phase_elapsed_s: float = 0.0
    drive_elapsed_s: float = 0.0
    interrupted_s: float = 0.0
    interruptions: int = 0
    measuring: bool = False
    board_paused: bool = False
    output_value: int = 0  # signed DAC value, -255..255
    applied_v: float = 0.0
    started_clock: float | None = None
    ended_clock: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    last_tick: float | None = None
    vt_forward_vs: float = 0.0
    vt_reverse_vs: float = 0.0
    events_done: int = 0
    measurements: list[Measurement] = field(default_factory=list)
    phase_records: list[PhaseRecord] = field(default_factory=list)
    voltage_steps: list[tuple[float, float]] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    log: list[tuple[float, str, str]] = field(default_factory=list)
    report_paths: dict[str, str] = field(default_factory=dict)
    clamped: bool = False
    pause_started: float = 0.0

    # ---------------------------------------------------------------- helpers
    @property
    def phase(self) -> Phase | None:
        if 0 <= self.phase_pos < len(self.plan.phases):
            return self.plan.phases[self.phase_pos]
        return None

    @property
    def next_phase(self) -> Phase | None:
        index = self.phase_pos + 1
        if 0 <= index < len(self.plan.phases):
            return self.plan.phases[index]
        return None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATES

    @property
    def accruing(self) -> bool:
        return self.status == RUNNING and not self.board_paused and not self.measuring

    def window_s(self, phase: Phase | None, configured_ms: int) -> float:
        if phase is None:
            return 0.0
        return max(0.02, min(configured_ms / 1000.0, phase.duration_s / 2.0))

    def end_window_s(self) -> float:
        phase = self.phase
        if phase is None or not self.plan.measure_end(phase):
            return 0.0
        return self.window_s(phase, self.config.measure_window_ms)

    def boundary_due(self) -> bool:
        phase = self.phase
        if phase is None or self.status != RUNNING:
            return False
        return self.phase_elapsed_s >= phase.duration_s - self.end_window_s() - 1e-9

    def remaining_drive_s(self) -> float:
        phase = self.phase
        if phase is None:
            return 0.0 if self.phase_pos >= len(self.plan.phases) else self.plan.drive_duration_s
        future = sum(p.duration_s for p in self.plan.phases[self.phase_pos + 1:])
        return max(0.0, phase.duration_s - self.phase_elapsed_s) + future

    def eta_s(self) -> float:
        if self.status in FINAL_STATES:
            return 0.0
        total_events = max(1, self.plan.measurement_event_count())
        remaining_events = max(0, total_events - self.events_done)
        overhead = self.plan.event_overhead_s() * remaining_events / total_events
        estimate = self.remaining_drive_s() + overhead
        if self.config.post_check:
            estimate += 3.5
        # Interruptions by neighbours stretch the schedule; use the observed rate.
        if self.drive_elapsed_s > 30:
            estimate *= 1.0 + self.interrupted_s / self.drive_elapsed_s
        return estimate

    def progress(self) -> float:
        total = self.plan.drive_duration_s
        if total <= 0:
            return 0.0
        if self.status == COMPLETED:
            return 1.0
        return min(1.0, max(0.0, 1.0 - self.remaining_drive_s() / total))

    def wall_t(self, clock_now: float) -> float:
        if self.started_clock is None:
            return 0.0
        end = self.ended_clock if self.ended_clock is not None else clock_now
        return max(0.0, end - self.started_clock)

    def last_delta(self, edge: str | None = None, kind: str | None = None) -> float | None:
        for measurement in reversed(self.measurements):
            if (edge is None or measurement.edge == edge) and (kind is None or measurement.phase_kind == kind):
                return measurement.delta_ma
        return None

    def last_high_end_delta(self) -> float | None:
        return self.last_delta("end", PHASE_HIGH)

    def snapshot(self, clock_now: float, supply_v: float) -> dict[str, Any]:
        phase = self.phase
        finished_plan = self.phase_pos >= len(self.plan.phases)
        run = phase.run if phase else (self.plan.runs[-1] if finished_plan and self.plan.runs else None)
        return {
            "board": self.board_key,
            "channel": self.channel,
            "actuator_id": self.actuator_id,
            "status": self.status,
            "status_detail": self.status_detail,
            "measuring": self.measuring,
            "board_paused": self.board_paused,
            "phase_pos": self.phase_pos,
            "phase_count": len(self.plan.phases),
            "phase_label": phase.label if phase else ("Finished" if finished_plan else ""),
            "phase_kind": phase.kind if phase else "",
            "phase_elapsed_s": self.phase_elapsed_s,
            "phase_duration_s": phase.duration_s if phase else 0.0,
            "run_id": run.run_id if run else 0,
            "run_count": len(self.plan.runs),
            "run_voltage_v": run.voltage_v if run else 0.0,
            "run_high_time_s": run.high_time_s if run else 0.0,
            "run_repeat": run.repeat if run else 0,
            "target_v": phase.target_v if phase else 0.0,
            "applied_v": self.applied_v,
            "wall_t_s": self.wall_t(clock_now),
            "drive_elapsed_s": self.drive_elapsed_s,
            "interrupted_s": self.interrupted_s,
            "eta_s": self.eta_s(),
            "progress": self.progress(),
            "vt_forward_vs": self.vt_forward_vs,
            "vt_reverse_vs": self.vt_reverse_vs,
            "measurement_count": len(self.measurements),
            "last_end_delta_ma": self.last_delta("end"),
            "last_start_delta_ma": self.last_delta("start"),
            "last_high_end_delta_ma": self.last_high_end_delta(),
            "supply_v": supply_v,
            "config_name": self.config.name,
            "report_paths": dict(self.report_paths),
            "started_at": self.started_at,
        }


BoardFactory = Callable[[], Rockford]

SUPPLY_OUTLIER_FRACTION = 0.15
SUPPLY_BAD_READINGS = 3


class BoardSession(threading.Thread):
    """Owns one Rockford board and schedules its actuator processes.

    Safety rules this class enforces:

    * Every channel that cannot be confirmed at 0 V escalates to a board
      fault; a board fault zeroes all channels first (each independently) and
      powers the supply off if any zero fails, then does the bookkeeping.
    * Firmware safety is turned off only while a process is active and is
      always restored to ON when the board is idle again.
    * The DAC scale uses the higher of the smoothed and latest supply
      readings, so a supply estimation error can only lower the drive voltage.
    """

    def __init__(
        self,
        key: str,
        label: str,
        endpoint: str,
        board_factory: BoardFactory,
        events: "queue.Queue[Event]",
        *,
        output_dir: Path,
        clock: Clock | None = None,
        reporter_factory: Callable[..., Any] | None = None,
    ) -> None:
        # Not a daemon: interpreter exit waits for the safe shutdown to finish.
        super().__init__(name=f"board-{key}", daemon=False)
        self.key = key
        self.label = label
        self.endpoint = endpoint
        self._board_factory = board_factory
        self.events = events
        self.output_dir = Path(output_dir)
        self.clock = clock or Clock()
        self._reporter_factory = reporter_factory
        self._commands: "queue.Queue[tuple[str, tuple[Any, ...]]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"reports-{key}")
        self.board: Rockford | None = None
        self.info: dict[str, Any] = {"label": label, "endpoint": endpoint}
        self.processes: dict[int, ActuatorProcess] = {}
        self._reporters: dict[int, Any] = {}
        self.supply_v = 0.0  # smoothed estimate (reported)
        self._last_supply_reading = 0.0
        self._supply_outliers = 0
        self._supply_low_count = 0
        self.psu_on = False
        self.manual_mode = False  # we turned firmware safety off
        self.connected = False
        self.faulted = False
        self.link_dead = False
        self._shutting_down = False
        self._last_keepalive = 0.0
        self._last_snapshot = 0.0
        self._last_autosave = 0.0
        self._last_safety_retry = 0.0
        self.detections: dict[int, CheckResult] = {}

    # ================================================================ public API
    def submit(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def power(self, enabled: bool) -> None:
        self.submit("power", bool(enabled))

    def detect(self, channels: list[int]) -> None:
        self.submit("detect", list(channels))

    def start_process(
        self,
        channel: int,
        actuator_id: str,
        config: InitializationConfig,
        notes: str = "",
        operator: str = "",
    ) -> None:
        self.submit("start", channel, actuator_id, config, notes, operator)

    def stop_process(self, channel: int, reason: str = "Stopped by operator") -> None:
        self.submit("stop", channel, reason)

    def pause_process(self, channel: int) -> None:
        self.submit("pause", channel)

    def resume_process(self, channel: int) -> None:
        self.submit("resume", channel)

    def stop_all(self, reason: str = "Stopped by operator") -> None:
        self.submit("stop_all", reason)

    def shutdown(self) -> None:
        self.submit("shutdown")
        self._stop_event.set()

    def has_active_processes(self) -> bool:
        return any(p.active for p in list(self.processes.values()))

    # =============================================================== thread body
    def run(self) -> None:
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001 - surface every connection failure
            self._emit("board_error", data=f"Connection failed: {exc}")
            self._emit("board_state", data=self._board_state())
            self._close()
            self._io.shutdown(wait=True)
            return
        try:
            while not self._stop_event.is_set():
                try:
                    self._process_commands()
                    if self._stop_event.is_set():
                        break
                    self._service()
                except Exception as exc:  # noqa: BLE001
                    if self._is_link_failure(exc):
                        self.link_dead = True
                    if isinstance(exc, BoardFault):
                        message = str(exc)
                    elif isinstance(exc, FirmwareError) and self._is_board_level(exc):
                        message = f"Firmware rejected command: {exc}"
                    elif self._is_board_exception(exc):
                        message = f"Communication lost: {exc}"
                    else:
                        # Unexpected bug outside any process context: fail safe.
                        traceback.print_exc()
                        message = f"Internal error: {exc!r}"
                    self._handle_board_fault(message)
                time.sleep(0.005)
        finally:
            self._safe_shutdown()
            self._close()
            self._io.shutdown(wait=True)

    # ============================================================ connection
    def _connect(self) -> None:
        self._emit("board_busy", data="Connecting")
        board = self._board_factory()
        self.board = board
        if hasattr(board.transport, "drain_lines"):
            try:
                board.force_text_mode()
            except Exception:  # noqa: BLE001 - best effort, as the dashboard does
                pass
        version = board.firmware_version()
        if version.firmware.strip().lower() != "rockford":
            raise RuntimeError(f"{version.firmware} firmware detected; this app drives Rockford boards.")
        capabilities = board.capabilities()
        if capabilities.get("OUC") != "1":
            raise RuntimeError("This firmware does not support OUC output-and-current commands.")
        self.info.update(
            firmware=f"{version.firmware} {version.version}",
            protocol=version.protocol,
            capabilities=capabilities,
        )
        try:
            self.info["bluetooth_name"] = board.bluetooth_status().get("NAME", "")
        except Exception:  # noqa: BLE001 - identity is optional
            self.info["bluetooth_name"] = ""
        safe_on_connect = True
        try:
            config = board.read_config()
            self.info["vt_limit_vs"] = config.vt_limit_vs
            safe_on_connect = config.safe
        except Exception:  # noqa: BLE001
            pass
        self.info["safety_on_connect"] = safe_on_connect
        board.connect_power()  # learn whether PSC exists, as the dashboard does
        self.psu_on = board.is_psu_on()
        if self.psu_on:
            self._reset_supply(board.voltage(50))
        self.connected = True
        self._last_keepalive = time.monotonic()
        self._emit("board_busy", data="")
        self._emit("board_state", data=self._board_state())
        self._log("ok", f"Connected to {self.info['firmware']} on {self.endpoint}")
        if not safe_on_connect:
            self._log("warn", "Firmware safety was OFF when connecting; it will be turned ON whenever the board is idle.")
            self.manual_mode = True  # so the idle restore turns it back on

    def _close(self) -> None:
        if self.board is not None:
            try:
                self.board.close()
            except Exception:  # noqa: BLE001
                pass
        self.board = None
        self.connected = False
        self._emit("board_state", data=self._board_state())
        self._emit("board_closed")

    def _board_state(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "faulted": self.faulted,
            "psu_on": self.psu_on,
            "supply_v": self.supply_v,
            "info": dict(self.info),
            "active": [p.channel for p in self.processes.values() if p.active],
        }

    # ============================================================== commands
    def _process_commands(self) -> None:
        while True:
            try:
                command, args = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "shutdown":
                self._stop_event.set()
                return
            channel = int(args[0]) if command in {"start", "stop", "pause", "resume"} else None
            try:
                if command == "power":
                    self._set_power(bool(args[0]))
                elif command == "detect":
                    self._detect(list(args[0]))
                elif command == "start":
                    self._start(*args)
                elif command == "stop":
                    self._finish_process(int(args[0]), STOPPED, str(args[1]))
                elif command == "stop_all":
                    for active in [p.channel for p in self.processes.values() if p.active]:
                        self._finish_process(active, STOPPED, str(args[0]))
                elif command == "pause":
                    self._user_pause(int(args[0]))
                elif command == "resume":
                    self._user_resume(int(args[0]))
            except StartRejected as exc:
                self._log("error", f"Channel {channel}: {exc}")
            except Exception as exc:  # noqa: BLE001
                if self._is_board_exception(exc):
                    raise
                self._fail_or_log(channel, exc)

    def _fail_or_log(self, channel: int | None, exc: Exception) -> None:
        """A non-board error: fail only the affected process (or just log it)."""

        process = self.processes.get(channel) if channel is not None else None
        if process is not None and process.active:
            detail = f"Firmware error: {exc}" if isinstance(exc, FirmwareError) else f"{exc}"
            self._finish_process(process.channel, FAILED, detail)
        else:
            prefix = f"Channel {channel}: " if channel is not None else ""
            self._log("error", f"{prefix}{exc}")

    def _require_board(self) -> Rockford:
        if self.board is None or not self.connected:
            raise BoardFault("Board is not connected")
        return self.board

    def _set_power(self, enabled: bool) -> None:
        board = self._require_board()
        if not enabled:
            for channel in [p.channel for p in self.processes.values() if p.active]:
                self._finish_process(channel, STOPPED, "Power turned off")
            self._restore_safety_if_idle()
            board.power_off()
            self.psu_on = False
            self.supply_v = 0.0
            self._last_supply_reading = 0.0
            self._log("ok", "Power OFF")
        else:
            self._emit("board_busy", data="Powering on")
            board.power_on()
            self.psu_on = True
            voltage = self._wait_for_settled_supply(board)
            self._emit("board_busy", data="")
            if voltage < MIN_SUPPLY_V:
                self._log("warn", f"Power ON but supply reads only {voltage:.1f} V")
            else:
                self._log("ok", f"Power ON - supply {voltage:.1f} V")
        self._emit("board_state", data=self._board_state())

    def _wait_for_settled_supply(self, board: Rockford, timeout_s: float = 15.0) -> float:
        """Poll until 50 ms readings stay within 1% of each other for 1 s."""

        deadline = time.monotonic() + timeout_s
        readings: list[float] = []
        voltage = 0.0
        while time.monotonic() < deadline and not self._stop_event.is_set():
            voltage = board.voltage(50)
            self._last_keepalive = time.monotonic()
            readings = (readings + [voltage])[-5:]  # 5 readings, 0.25 s apart = 1 s
            if (len(readings) == 5 and min(readings) >= MIN_SUPPLY_V
                    and max(readings) - min(readings) <= 0.01 * max(readings)):
                break
            time.sleep(0.25)
        self._reset_supply(max(readings) if readings else voltage)
        return self.supply_v

    def _detect(self, channels: list[int]) -> None:
        self._require_board()
        if self.has_active_processes():
            raise RuntimeError("Detection runs only while no initialization is active on this board.")
        if not self.psu_on:
            raise RuntimeError("Turn power on before detecting actuators.")
        self._emit("board_busy", data="Detecting actuators")
        try:
            for channel in channels:
                if self._stop_event.is_set():
                    break
                self._validate_channel(channel)
                self._emit("detection", channel, {"state": "Detecting"})
                result = self._firmware_check(channel, "detect")
                self.detections[channel] = result
                self._emit("detection", channel, {"state": result.state, "result": result})
                self._last_keepalive = time.monotonic()
        finally:
            self._emit("board_busy", data="")
        self._log("ok", "Detection complete: " + ", ".join(
            f"{ch}={self.detections[ch].state}" for ch in channels if ch in self.detections
        ))

    @staticmethod
    def _validate_channel(channel: int) -> None:
        if not 0 <= int(channel) < MAX_CHANNELS:
            raise ValueError(f"Channel must be 0-{MAX_CHANNELS - 1}")

    # ======================================================== process control
    def _start(
        self,
        channel: int,
        actuator_id: str,
        config: InitializationConfig,
        notes: str,
        operator: str,
    ) -> None:
        board = self._require_board()
        if not 0 <= int(channel) < MAX_CHANNELS:
            raise StartRejected(f"Channel must be 0-{MAX_CHANNELS - 1}")
        existing = self.processes.get(channel)
        if existing is not None and existing.active:
            raise StartRejected(f"Channel {channel} is already running.")
        if not actuator_id.strip():
            raise StartRejected("Enter the actuator ID before starting.")
        if not self.psu_on:
            raise StartRejected("Turn power on before starting.")
        try:
            plan = build_plan(config)
        except (ValueError, TypeError) as exc:
            raise StartRejected(f"Invalid sequence: {exc}") from exc
        reading = board.voltage(50)
        if reading < MIN_SUPPLY_V:
            raise StartRejected(f"Supply reads {reading:.1f} V; it must be above {MIN_SUPPLY_V:g} V to start.")
        self._observe_supply(reading)
        process = ActuatorProcess(
            board_key=self.key,
            channel=channel,
            actuator_id=actuator_id.strip(),
            config=config,
            plan=plan,
            notes=notes,
            operator=operator,
        )
        self.processes[channel] = process
        process.status = CHECKING
        process.started_clock = self.clock.now()
        process.started_at = utc_now()
        try:
            self._enter_manual_mode()
            self._open_reporter(process)
            self._plog(process, "info", (
                f"Starting '{config.name}' for {process.actuator_id}: {len(plan.runs)} runs, "
                f"{len(plan.phases)} phases"
            ))
            self._emit_snapshot(process)
            if config.pre_check:
                result = self._run_check_paused(process, "pre")
                if result.state == ActuatorState.NOT_CONNECTED.value:
                    self._finish_process(channel, FAILED, "Pre-check: actuator not connected")
                    return
                if result.state == ActuatorState.ERROR.value:
                    self._plog(process, "warn", "Pre-check reports Error; initialization continues to condition it.")
            process.status = RUNNING
            self._boundary_event(process, old=None, new=plan.phases[0])
        except Exception as exc:  # noqa: BLE001
            if self._is_board_exception(exc):
                raise
            self._finish_process(channel, FAILED, f"Could not start: {exc}")
            return
        self._emit("board_state", data=self._board_state())

    def _user_pause(self, channel: int) -> None:
        process = self.processes.get(channel)
        if process is None or process.status != RUNNING:
            return
        self._tick(self.clock.now())
        process.status = PAUSED
        self._zero_channel(channel)
        self._note_output(process, 0)
        self._plog(process, "info", "Paused by operator (output 0 V)")
        self._emit_snapshot(process)

    def _user_resume(self, channel: int) -> None:
        process = self.processes.get(channel)
        if process is None or process.status != PAUSED:
            return
        phase = process.phase
        if phase is not None:
            self._write_output(process, self._dac_value(phase.target_v))
        process.status = RUNNING
        process.last_tick = self.clock.now()
        self._plog(process, "info", "Resumed")
        self._emit_snapshot(process)

    def _finish_process(self, channel: int, status: str, detail: str) -> None:
        """End a process. Raises BoardFault if its output cannot be confirmed at 0 V."""

        process = self.processes.get(channel)
        if process is None or process.status in FINAL_STATES:
            return
        if process.status == RUNNING:
            self._tick(self.clock.now())
        if not self.faulted and not self._shutting_down and self.board is not None and self.connected:
            self._zero_channel(channel)  # BoardFault -> every channel zeroed, PSU off
        self._note_output(process, 0)
        interrupted = self._close_open_phase(process, completed=False)
        if interrupted is not None:
            self._persist_phase(process, interrupted)
        process.status = status
        process.status_detail = detail
        process.measuring = False
        process.board_paused = False
        process.ended_clock = self.clock.now()
        process.finished_at = utc_now()
        level = {"completed": "ok", "stopped": "warn"}.get(status, "error")
        self._plog(process, level, f"{status.title()}: {detail}")
        self._finalize_report(process)
        self._emit_snapshot(process)
        self._emit("process_finished", channel, {"status": status, "detail": detail})
        if not self._shutting_down:
            self._restore_safety_if_idle()
        self._emit("board_state", data=self._board_state())

    def _complete(self, process: ActuatorProcess) -> None:
        if process.config.post_check:
            process.status = CHECKING
            self._emit_snapshot(process)
            try:
                self._run_check_paused(process, "post")
            except Exception as exc:  # noqa: BLE001
                if self._is_board_exception(exc):
                    raise
                self._plog(process, "error", f"Post-check failed: {exc}")
        self._finish_process(process.channel, COMPLETED, self._verdict(process))

    def _verdict(self, process: ActuatorProcess) -> str:
        post = next((c for c in reversed(process.checks) if c.stage == "post"), None)
        parts = ["Initialization complete"]
        if post is not None:
            parts.append(f"post-check {post.state}")
        threshold = process.config.pass_max_delta_ma
        if threshold is not None:
            final = process.last_high_end_delta()
            if final is not None:
                parts.append("PASS" if abs(final) <= threshold else "FAIL")
        return " - ".join(parts)

    # ============================================================== servicing
    def _service(self) -> None:
        if self.board is None:
            return
        self._tick(self.clock.now())
        due = [p for p in self.processes.values() if p.boundary_due()]
        due.sort(key=lambda p: p.phase_elapsed_s - (p.phase.duration_s if p.phase else 0), reverse=True)
        for process in due:
            if self._stop_event.is_set():
                return
            if not process.boundary_due():
                continue
            self._advance(process)
            # Let Stop/Pause/Shutdown requests in between long boundary work.
            self._process_commands()
        real_now = time.monotonic()
        if real_now - self._last_keepalive >= KEEPALIVE_INTERVAL_S:
            self._keepalive()
        if self.manual_mode and not self.has_active_processes() and real_now - self._last_safety_retry >= 2.0:
            self._last_safety_retry = real_now
            self._restore_safety_if_idle()
        if real_now - self._last_snapshot >= SNAPSHOT_INTERVAL_S:
            self._last_snapshot = real_now
            for process in self.processes.values():
                if process.active:
                    self._emit_snapshot(process)
            self._emit("board_state", data=self._board_state())
        if real_now - self._last_autosave >= REPORT_AUTOSAVE_S:
            self._last_autosave = real_now
            for process in self.processes.values():
                if process.active:
                    self._autosave_report(process)

    def _keepalive(self) -> None:
        board = self._require_board()
        self._last_keepalive = time.monotonic()
        reading = board.voltage()
        if self.psu_on:
            self._update_supply(reading)
            self._rescale_outputs()
        self._emit("telemetry", data={"supply_v": self.supply_v, "reading_v": reading})

    # ------------------------------------------------------------ supply model
    def _reset_supply(self, reading: float) -> None:
        self.supply_v = max(0.0, reading)
        self._last_supply_reading = self.supply_v
        self._supply_outliers = 0
        self._supply_low_count = 0

    def _observe_supply(self, reading: float) -> None:
        """Take a reading that may only raise the drive scale (used at start)."""

        if self.supply_v <= 0:
            self._reset_supply(reading)
        elif reading > self._last_supply_reading:
            self._last_supply_reading = reading

    def _update_supply(self, reading: float) -> None:
        """Smooth supply readings. Higher readings are adopted at once (they
        lower the drive); isolated low readings are ignored (they would raise it)."""

        if reading < MIN_SUPPLY_V:
            self._supply_low_count += 1
            if self.has_active_processes() and self._supply_low_count >= SUPPLY_BAD_READINGS:
                raise BoardFault(f"Supply voltage dropped to {reading:.1f} V")
            return
        self._supply_low_count = 0
        if self.supply_v <= 0:
            self._reset_supply(reading)
            return
        if reading > self.supply_v * (1 + SUPPLY_OUTLIER_FRACTION):
            self._reset_supply(reading)  # supply rose: adopt immediately
            return
        if reading < self.supply_v * (1 - SUPPLY_OUTLIER_FRACTION):
            self._supply_outliers += 1
            if self._supply_outliers >= SUPPLY_BAD_READINGS:
                self._reset_supply(reading)  # a persistent drop: adopt it
            return
        self._supply_outliers = 0
        self._last_supply_reading = reading
        self.supply_v = 0.8 * self.supply_v + 0.2 * reading

    def _rescale_outputs(self) -> None:
        """Re-issue running outputs whose DAC value drifted with the supply."""

        for process in list(self.processes.values()):
            phase = process.phase
            if (process.status != RUNNING or process.board_paused or process.measuring
                    or phase is None or process.output_value == 0):
                continue
            desired = self._dac_value(phase.target_v)
            if abs(desired - process.output_value) >= 2:
                self._write_output(process, desired)

    def _scale_supply_v(self) -> float:
        """Supply used for V->DAC: the higher estimate, so errors only lower drive."""

        return max(self.supply_v, self._last_supply_reading)

    # ------------------------------------------------------------- clocking
    def _tick(self, now: float) -> None:
        for process in self.processes.values():
            if process.last_tick is None:
                continue
            dt = now - process.last_tick
            process.last_tick = now
            if dt <= 0 or not process.accruing:
                continue
            self._accrue(process, dt)

    def _accrue(self, process: ActuatorProcess, dt: float) -> None:
        process.phase_elapsed_s += dt
        process.drive_elapsed_s += dt
        if process.applied_v > 0:
            process.vt_forward_vs += process.applied_v * dt
        elif process.applied_v < 0:
            process.vt_reverse_vs += -process.applied_v * dt
        if process.phase_records and process.phase_records[-1].ended_t_s is None:
            process.phase_records[-1].drive_s += dt

    def _advance(self, process: ActuatorProcess) -> None:
        old = process.phase
        new = process.next_phase
        try:
            self._boundary_event(process, old=old, new=new)
        except Exception as exc:  # noqa: BLE001
            if self._is_board_exception(exc):
                raise
            self._fail_or_log(process.channel, exc)
            return
        if new is None:
            self._complete(process)

    # ===================================================== boundary measurement
    def _boundary_event(self, process: ActuatorProcess, *, old: Phase | None, new: Phase | None) -> None:
        plan = process.plan
        measure_end = plan.measure_end(old)
        measure_start = plan.measure_start(new)
        if not measure_end and not measure_start and old is not None:  # always baseline at start
            self._tick(self.clock.now())
            closed = self._close_open_phase(process, completed=True)
            if closed is not None:
                self._persist_phase(process, closed)
            self._begin_phase(process, new)
            self._write_output(process, self._dac_value(new.target_v) if new is not None else 0)
            process.last_tick = self.clock.now()
            return

        board = self._require_board()
        config = process.config
        paused: list[ActuatorProcess] = []
        end_reading: tuple[float, float, int] | None = None
        start_reading: tuple[float, float, int] | None = None
        closed: PhaseRecord | None = None
        try:
            self._tick(self.clock.now())
            self._pause_others(process, paused)
            # The measured channel stayed driven while neighbours were zeroed.
            self._tick(self.clock.now())
            process.measuring = True
            self._emit_snapshot(process)
            if measure_end and old is not None:
                # Measure over exactly the time left in the phase.
                window_s = min(
                    process.window_s(old, config.measure_window_ms),
                    max(0.02, old.duration_s - process.phase_elapsed_s),
                )
                current, elapsed = self._ouc(board, process, process.output_value, window_s)
                self._accrue(process, elapsed)
                end_reading = (current, process.applied_v, round(window_s * 1000))
            closed = self._close_open_phase(process, completed=True)

            baseline_s = max(0.02, config.baseline_window_ms / 1000.0)
            baseline, _ = self._ouc(board, process, 0, baseline_s)

            self._begin_phase(process, new)
            if new is not None:
                value = self._dac_value(new.target_v)
                if measure_start:
                    window_s = process.window_s(new, config.measure_window_ms)
                    current, elapsed = self._ouc(board, process, value, window_s)
                    self._accrue(process, elapsed)
                    start_reading = (current, process.applied_v, round(window_s * 1000))
                else:
                    self._write_output(process, value)
        except Exception as exc:
            process.measuring = False
            process.last_tick = self.clock.now()
            if self._is_board_exception(exc):
                # Leave neighbours at 0 V; the fault handler stops everything.
                for other in paused:
                    other.board_paused = False
                    other.last_tick = self.clock.now()
                raise
            if closed is not None:
                self._persist_phase(process, closed)
            self._resume_others(paused)  # a problem with this actuator only
            raise
        process.measuring = False
        process.last_tick = self.clock.now()  # resume time below counts as drive
        process.events_done += 1
        try:
            if end_reading is not None and old is not None:
                current, applied, window_ms = end_reading
                self._record_measurement(process, old, "end", current, baseline, applied, window_ms)
                if closed is not None:
                    closed.end_current_ma = current
                    closed.end_delta_ma = current - baseline
            if closed is not None:
                self._persist_phase(process, closed)
            if start_reading is not None and new is not None:
                current, applied, window_ms = start_reading
                self._record_measurement(process, new, "start", current, baseline, applied, window_ms)
                record = process.phase_records[-1]
                record.start_current_ma = current
                record.start_delta_ma = current - baseline
        finally:
            self._resume_others(paused)

    def _close_open_phase(self, process: ActuatorProcess, *, completed: bool) -> PhaseRecord | None:
        if process.phase_records and process.phase_records[-1].ended_t_s is None:
            record = process.phase_records[-1]
            record.ended_t_s = process.wall_t(self.clock.now())
            record.completed = completed
            return record
        return None

    def _persist_phase(self, process: ActuatorProcess, record: PhaseRecord) -> None:
        reporter = self._reporters.get(process.channel)
        if reporter is not None:
            self._submit_io(reporter.append_phase, replace_record(record))
        self._emit("phase_completed", process.channel, record)

    def _begin_phase(self, process: ActuatorProcess, phase: Phase | None) -> None:
        if phase is None:
            process.phase_pos = len(process.plan.phases)
            process.phase_elapsed_s = 0.0
            return
        process.phase_pos = phase.index
        process.phase_elapsed_s = 0.0
        run = phase.run
        process.phase_records.append(
            PhaseRecord(
                index=phase.index,
                run_id=run.run_id,
                kind=phase.kind,
                cycle=phase.cycle,
                voltage_v=run.voltage_v,
                high_time_s=run.high_time_s,
                repeat=run.repeat,
                target_v=phase.target_v,
                applied_v=self._applied_from_value(self._dac_value(phase.target_v)),
                started_t_s=process.wall_t(self.clock.now()),
            )
        )
        self._emit("phase_started", process.channel, {
            "index": phase.index, "kind": phase.kind, "run_id": run.run_id,
            "target_v": phase.target_v, "label": phase.label,
        })

    def _record_measurement(
        self,
        process: ActuatorProcess,
        phase: Phase,
        edge: str,
        current: float,
        baseline: float,
        applied: float,
        window_ms: int,
    ) -> None:
        run = phase.run
        measurement = Measurement(
            t_s=process.wall_t(self.clock.now()),
            timestamp=utc_now(),
            run_id=run.run_id,
            phase_index=phase.index,
            phase_kind=phase.kind,
            cycle=phase.cycle,
            edge=edge,
            voltage_v=run.voltage_v,
            high_time_s=run.high_time_s,
            repeat=run.repeat,
            target_v=phase.target_v,
            applied_v=applied,
            current_ma=current,
            baseline_ma=baseline,
            delta_ma=current - baseline,
            window_ms=window_ms,
            supply_v=self.supply_v,
        )
        process.measurements.append(measurement)
        reporter = self._reporters.get(process.channel)
        if reporter is not None:
            self._submit_io(reporter.append_measurement, measurement)
        self._emit("measurement", process.channel, measurement)

    def _pause_others(self, measured: ActuatorProcess, paused: list[ActuatorProcess]) -> None:
        """Hold every other running process at 0 V with its clock frozen.

        Each process is recorded in ``paused`` before its output is touched, so
        a caller can always undo the bookkeeping. A channel that cannot be set
        to 0 V raises BoardFault (it might still be energized).
        """

        for process in list(self.processes.values()):
            if process is measured or process.status not in {RUNNING, CHECKING} or process.board_paused:
                continue
            process.board_paused = True
            process.interruptions += 1
            process.pause_started = self.clock.now()
            paused.append(process)
            if process.output_value != 0:
                self._zero_channel(process.channel)
            self._emit_snapshot(process)

    def _resume_others(self, paused: list[ActuatorProcess]) -> None:
        """Restore paused neighbours; a failure fails only that neighbour."""

        failures: list[tuple[ActuatorProcess, Exception]] = []
        for process in paused:
            now = self.clock.now()
            frozen = max(0.0, now - process.pause_started)
            process.interrupted_s += frozen
            if process.phase_records:
                process.phase_records[-1].interrupted_s += frozen
            process.board_paused = False
            process.last_tick = now
            if process.status in {RUNNING, CHECKING} and process.output_value != 0:
                try:
                    self._set_hardware_output(process.channel, process.output_value)
                except Exception as exc:  # noqa: BLE001
                    if self._is_board_exception(exc):
                        raise BoardFault(f"Could not restore channel {process.channel}: {exc}") from exc
                    failures.append((process, exc))
            self._emit_snapshot(process)
        for process, exc in failures:
            self._finish_process(process.channel, FAILED, f"Could not restore output after a neighbour's reading: {exc}")

    def _run_check_paused(self, process: ActuatorProcess, stage: str) -> CheckResult:
        """Firmware DT0/DT1 check; DT0 zeroes every output, so pause the others."""

        paused: list[ActuatorProcess] = []
        try:
            self._tick(self.clock.now())
            self._pause_others(process, paused)
            process.measuring = True
            self._emit_snapshot(process)
            self._write_output(process, 0)
            result = self._firmware_check(process.channel, stage)
        except Exception as exc:
            process.measuring = False
            process.last_tick = self.clock.now()
            if self._is_board_exception(exc):
                for other in paused:
                    other.board_paused = False
                    other.last_tick = self.clock.now()
                raise
            self._resume_others(paused)
            raise
        process.measuring = False
        process.last_tick = self.clock.now()
        process.checks.append(result)
        self._plog(process, "info", (
            f"{stage.title()}-check: {result.state} (delta {result.delta_ma:.2f} mA, "
            f"baseline {result.baseline_ma:.2f} mA)"
        ))
        self._emit("check", process.channel, result)
        self._resume_others(paused)
        return result

    def _firmware_check(self, channel: int, stage: str) -> CheckResult:
        board = self._require_board()
        detection = board.detect_actuator_firmware(channel)
        self._last_keepalive = time.monotonic()
        if detection.state is ActuatorState.PRESENT:
            detection = board.detect_actuator_firmware_conditioned(channel)
            self._last_keepalive = time.monotonic()
        return CheckResult(
            stage=stage,
            timestamp=utc_now(),
            state=detection.state.value,
            baseline_ma=detection.baseline_ma,
            forward_ma=detection.forward_ma,
            delta_ma=detection.delta_ma,
            initial_delta_ma=detection.initial_delta_ma,
        )

    # ============================================================== hardware
    def _dac_value(self, target_v: float) -> int:
        """Signed DAC value for a target voltage, scaled by the measured supply."""

        if target_v == 0:
            return 0
        supply = self._scale_supply_v()
        if supply < MIN_SUPPLY_V:
            raise BoardFault(f"Supply estimate {supply:.1f} V is too low to scale the drive voltage")
        magnitude = min(255, max(1, round(255.0 * abs(target_v) / supply)))
        return magnitude if target_v > 0 else -magnitude

    def _applied_from_value(self, value: int) -> float:
        return value / 255.0 * self.supply_v

    @staticmethod
    def _top_bottom(value: int) -> tuple[int, int]:
        if value > 0:
            return value, 0
        if value < 0:
            return 255 - (-value), 1
        return 0, 0

    def _set_hardware_output(self, channel: int, value: int) -> None:
        board = self._require_board()
        if value > 0:
            board.set_manual_output(channel, value, 0)
        elif value < 0:
            board.set_manual_output(channel, 0, -value)
        else:
            board.set_manual_output(channel, 0, 0)

    def _zero_channel(self, channel: int) -> None:
        """Set a channel to 0 V; BoardFault if it cannot be confirmed.

        A firmware rejection is retried once; a transport failure is not (the
        link is gone, and the firmware watchdog de-energizes the board).
        """

        last: Exception | None = None
        for _attempt in range(2):
            try:
                self._set_hardware_output(channel, 0)
                return
            except FirmwareError as exc:
                last = exc
            except Exception as exc:  # noqa: BLE001
                if self._is_link_failure(exc):
                    self.link_dead = True
                raise BoardFault(f"Could not set channel {channel} to 0 V: {exc}") from exc
        raise BoardFault(f"Could not set channel {channel} to 0 V: {last}")

    @staticmethod
    def _is_link_failure(exc: BaseException) -> bool:
        return isinstance(exc, (TransportError, OSError, TimeoutError))

    def _write_output(self, process: ActuatorProcess, value: int) -> None:
        self._set_hardware_output(process.channel, value)
        self._note_output(process, value)

    def _note_output(self, process: ActuatorProcess, value: int) -> None:
        phase = process.phase
        if phase is not None and value != 0 and abs(value) == 255:
            if abs(phase.target_v) > self.supply_v + 0.5 and not process.clamped:
                process.clamped = True
                self._plog(process, "warn", (
                    f"{abs(phase.target_v):g} V exceeds the {self.supply_v:.0f} V supply; "
                    "driving at full supply instead."
                ))
        process.output_value = value
        applied = self._applied_from_value(value)
        if applied != process.applied_v or not process.voltage_steps:
            process.applied_v = applied
            point = (process.wall_t(self.clock.now()), applied)
            process.voltage_steps.append(point)
            self._emit("voltage", process.channel, point)

    def _ouc(self, board: Rockford, process: ActuatorProcess, value: int, window_s: float) -> tuple[float, float]:
        """Set the process output with OUC and measure; returns (mA, clock seconds)."""

        top, bottom = self._top_bottom(value)
        window_ms = max(1, round(window_s * 1000))
        self._note_output(process, value)
        started = self.clock.now()
        current = board.manual_output_current(process.channel, top, bottom, window_ms)
        elapsed = self.clock.now() - started
        self._last_keepalive = time.monotonic()
        return current, elapsed

    def _enter_manual_mode(self) -> None:
        board = self._require_board()
        self.manual_mode = True
        if board.safety():
            board.safety(False)

    def _restore_safety_if_idle(self) -> None:
        """Turn firmware safety back ON once no process is active."""

        if not self.manual_mode or self.has_active_processes():
            return
        if self.board is None or not self.connected or self.link_dead:
            return
        try:
            self.board.safety(True)
            self.manual_mode = False
        except Exception as exc:  # noqa: BLE001 - retried from _service
            self._log("error", f"Could not restore firmware safety (will retry): {exc}")

    @staticmethod
    def _is_board_level(exc: FirmwareError) -> bool:
        code = exc.code.upper()
        return "BUSY" in code or "PSU" in code or "SAFETY" in code

    def _is_board_exception(self, exc: BaseException) -> bool:
        if isinstance(exc, BoardFault):
            return True
        if isinstance(exc, FirmwareError):
            return self._is_board_level(exc)
        return isinstance(exc, (TransportError, ProtocolError, OSError, TimeoutError))

    def _emergency_zero_all(self) -> bool:
        """Zero every channel independently; power off if any zero fails."""

        board = self.board
        if board is None or self.link_dead:
            return False
        ok = True
        for channel in range(MAX_CHANNELS):
            try:
                board.set_manual_output(channel, 0, 0)
            except Exception as exc:  # noqa: BLE001 - keep going with the other channels
                ok = False
                if self._is_link_failure(exc):
                    self.link_dead = True
                    self._log("error", "Board link lost; the firmware watchdog turns outputs off within 10 s.")
                    return False
        if not ok:
            try:
                board.power_off()
                self.psu_on = False
                self._log("warn", "Could not confirm every output at 0 V; supply powered off.")
            except Exception:  # noqa: BLE001
                self._log("error", "Could not confirm outputs at 0 V and could not power off the supply.")
        return ok

    def _handle_board_fault(self, message: str) -> None:
        if self.faulted:
            return
        self.faulted = True
        self._log("error", f"Board fault: {message}")
        # 1) Hardware first: every output to 0 V (power off on doubt), safety on.
        if self.manual_mode or self.has_active_processes():
            if self._emergency_zero_all():
                try:
                    if self.board is not None:
                        self.board.safety(True)
                        self.manual_mode = False
                except Exception:  # noqa: BLE001
                    pass
        # 2) Then bookkeeping and reports.
        for process in list(self.processes.values()):
            if process.active:
                self._finish_process(process.channel, FAILED, f"Board fault: {message}")
        self._emit("board_error", data=message)
        self._emit("board_state", data=self._board_state())
        self._stop_event.set()

    def _safe_shutdown(self) -> None:
        self._shutting_down = True
        active = [p for p in self.processes.values() if p.active]
        if self.board is not None and not self.faulted and (active or self.manual_mode):
            zero_ok = self._emergency_zero_all()
            for process in active:
                try:
                    self._finish_process(process.channel, STOPPED, "Application closed")
                except Exception:  # noqa: BLE001
                    pass
            if zero_ok:
                try:
                    self.board.safety(True)
                    self.manual_mode = False
                except Exception:  # noqa: BLE001
                    pass
        else:
            for process in active:
                try:
                    self._finish_process(process.channel, STOPPED, "Application closed")
                except Exception:  # noqa: BLE001
                    pass

    # ============================================================== reporting
    def _submit_io(self, function: Callable[..., Any], *args: Any) -> Future | None:
        try:
            future = self._io.submit(function, *args)
        except RuntimeError:  # executor already shut down
            return None
        future.add_done_callback(self._io_done)
        return future

    def _io_done(self, future: Future) -> None:
        exc = future.exception()
        if exc is not None:
            self._log("error", f"Report write failed: {exc}")

    def _open_reporter(self, process: ActuatorProcess) -> None:
        if self._reporter_factory is None:
            return
        try:
            self._reporters[process.channel] = self._reporter_factory(self.output_dir, process, dict(self.info))
        except Exception as exc:  # noqa: BLE001 - reporting never stops the hardware
            self._plog(process, "error", f"Could not create report folder: {exc}")

    def _autosave_report(self, process: ActuatorProcess) -> None:
        reporter = self._reporters.get(process.channel)
        if reporter is None:
            return
        try:
            text = reporter.render_progress(process, dict(self.info))
        except Exception as exc:  # noqa: BLE001
            self._plog(process, "error", f"Autosave failed: {exc}")
            return
        self._submit_io(reporter.write_progress_text, text)

    def _finalize_report(self, process: ActuatorProcess) -> None:
        reporter = self._reporters.pop(process.channel, None)
        if reporter is None:
            return
        info = dict(self.info)

        def job() -> None:
            try:
                paths = reporter.finalize(process, info)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._log("error", f"Report generation for {process.actuator_id} failed: {exc}")
                return
            process.report_paths = paths
            self._emit("report", process.channel, {**paths, "started_at": process.started_at,
                                                    "actuator_id": process.actuator_id})

        self._submit_io(job)

    # ================================================================ events
    def _emit(self, kind: str, channel: int | None = None, data: Any = None) -> None:
        self.events.put(Event(kind=kind, board=self.key, channel=channel, data=data))

    def _emit_snapshot(self, process: ActuatorProcess) -> None:
        self._emit("process", process.channel, process.snapshot(self.clock.now(), self.supply_v))

    def _log(self, level: str, text: str) -> None:
        self._emit("log", data={"level": level, "text": text, "time": time.strftime("%H:%M:%S")})

    def _plog(self, process: ActuatorProcess, level: str, text: str) -> None:
        process.log.append((process.wall_t(self.clock.now()), level, text))
        self._emit("log", process.channel, {
            "level": level,
            "text": f"ch{process.channel} {process.actuator_id}: {text}",
            "time": time.strftime("%H:%M:%S"),
        })


def replace_record(record: PhaseRecord) -> PhaseRecord:
    """Copy a phase record for the report thread (the original keeps changing)."""

    return PhaseRecord(**{name: getattr(record, name) for name in record.__dataclass_fields__})
