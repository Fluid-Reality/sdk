"""Lansing board dashboard.

Run from the SDK root with:

    python apps/lansing_dashboard/app.py
"""

from __future__ import annotations

import html
import queue
import sys
import time
from pathlib import Path
from typing import Any

SDK_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = SDK_ROOT / "src"
APP_ROOT = Path(__file__).resolve().parent
LOGO_PATH = APP_ROOT / "assets" / "fluid_reality_logo_transparent.png"
SERIAL_TIMEOUT_S = 45.0
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fluid_reality import FirmwareError, Lansing


STATE_NAMES = {
    0: "Idle",
    1: "Forward",
    2: "Discharge",
}


def format_ms(ms: int | float | None) -> str:
    if ms is None:
        return "-"
    value = int(ms)
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    return f"{minutes / 60:.1f} h"


def state_class(state: int) -> str:
    if state == 1:
        return "forward"
    if state == 2:
        return "discharge"
    return "idle"


class BoardWorker(QThread):
    connected_changed = Signal(bool, str)
    status_ready = Signal(dict)
    diagnosis_ready = Signal(dict)
    initialization_progress = Signal(dict)
    recovery_ready = Signal(dict)
    recovery_progress = Signal(dict)
    health_ready = Signal(int, object)
    square_changed = Signal(bool, list, str)
    busy_changed = Signal(str)
    message = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._board: Lansing | None = None
        self._square_actuators: list[int] = []
        self._square_phase = "idle"
        self._next_square_step = 0.0
        self._last_square_warning = 0.0
        self._square_restore_debug: bool | None = None
        self._last_status = 0.0
        self._actuator_health: dict[int, dict[str, Any]] = {}

    def enqueue(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self._process_pending_commands()
            self._service_square_wave()
            self._poll_status_if_due()
            time.sleep(0.025)

        self._close_board()

    def _process_pending_commands(self) -> None:
        while True:
            try:
                command, args = self._commands.get_nowait()
            except queue.Empty:
                return

            try:
                if command == "connect":
                    self._connect(str(args[0]))
                elif command == "disconnect":
                    self._disconnect()
                elif command == "refresh":
                    self._emit_status()
                elif command == "psu":
                    self._require_board().power_supply(bool(args[0]))
                    self.message.emit(f"Power supply {'on' if args[0] else 'off'}", "ok")
                    self._emit_status()
                elif command == "psc":
                    self._require_board().connect_power(bool(args[0]))
                    self.message.emit(f"Power connection {'closed' if args[0] else 'open'}", "ok")
                    self._emit_status()
                elif command == "init":
                    self._initialize_actuator(int(args[0]))
                elif command == "diagnose":
                    self._diagnose_actuator(int(args[0]))
                elif command == "recover":
                    self._recover_actuator(int(args[0]), float(args[1]), int(args[2]))
                elif command == "detect_group":
                    self._detect_group(int(args[0]))
                elif command == "square_start":
                    self._start_square_wave(list(args[0]))
                elif command == "square_stop":
                    self._stop_square_wave()
                elif command == "all_off":
                    self._all_off()
                else:
                    self.message.emit(f"Unknown worker command: {command}", "error")
            except Exception as exc:
                self.busy_changed.emit("")
                self.message.emit(str(exc), "error")
                if command == "connect":
                    self.connected_changed.emit(False, "Disconnected")
                if self._board is not None and command in {"psu", "psc"}:
                    try:
                        self._emit_status()
                    except Exception:
                        pass

    def _connect(self, port: str) -> None:
        self._close_board()
        self.busy_changed.emit("Connecting")
        self._board = Lansing(port, timeout=SERIAL_TIMEOUT_S)
        self._board.force_text_mode()
        version = self._board.firmware_version()
        self.connected_changed.emit(True, f"{port} - {version.firmware} {version.version}")
        self.message.emit(f"Connected to {version.firmware} firmware {version.version}", "ok")
        self.busy_changed.emit("")
        self._emit_status()

    def _disconnect(self) -> None:
        self._stop_square_wave()
        self._close_board()
        self.connected_changed.emit(False, "Disconnected")
        self.message.emit("Disconnected", "info")

    def _close_board(self) -> None:
        if self._board is None:
            return
        try:
            for actuator in list(self._square_actuators):
                self._board.set_actuator(actuator, 0)
        except Exception:
            pass
        self._restore_square_debug()
        try:
            self._board.close()
        except Exception:
            pass
        self._board = None
        self._square_actuators = []
        self.square_changed.emit(False, [], "idle")

    def _require_board(self) -> Lansing:
        if self._board is None:
            raise RuntimeError("Connect to a Lansing board first.")
        return self._board

    def _emit_status(self) -> None:
        board = self._require_board()
        status = board.status()
        self.status_ready.emit(status)
        self._last_status = time.monotonic()

    def _poll_status_if_due(self) -> None:
        if self._board is None:
            return
        if time.monotonic() - self._last_status < 0.8:
            return
        try:
            self._emit_status()
        except Exception as exc:
            self.message.emit(f"Status refresh failed: {exc}", "error")
            self._last_status = time.monotonic()

    def _initialize_actuator(self, actuator: int) -> None:
        self._ensure_actuator_recoverable(actuator)
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
        stages = (25.0, 50.0, 100.0, 200.0)
        stage_duration_s = 30.0
        total_duration_s = stage_duration_s * len(stages)
        phase_interval_s = 0.5
        self.busy_changed.emit(f"Initializing actuator {actuator}")
        self.message.emit(
            f"Initializing actuator {actuator}: safety off, 1 Hz bipolar drive "
            "at +/-25 V, +/-50 V, +/-100 V, +/-200 V, then diagnose.",
            "warn",
        )
        supply_voltage = board.voltage()
        if supply_voltage <= 0:
            raise RuntimeError("Cannot initialize: measured PSU voltage is 0 V.")
        start = time.monotonic()
        next_report_second = -1
        try:
            board.safety(False)
            for stage_index, target_voltage in enumerate(stages, start=1):
                output_value = self._voltage_to_output(target_voltage, supply_voltage)
                stage_start = time.monotonic()
                next_phase = stage_start
                phase = 0
                self.message.emit(
                    f"Initialize actuator {actuator}: stage {stage_index}/4 "
                    f"+/-{target_voltage:.0f} V using raw {output_value}/255.",
                    "info",
                )
                while True:
                    now = time.monotonic()
                    stage_elapsed_s = now - stage_start
                    elapsed_s = now - start
                    if stage_elapsed_s >= stage_duration_s:
                        break

                    if now >= next_phase:
                        if phase % 2 == 0:
                            board.set_manual_output(actuator, output_value, 0)
                        else:
                            board.set_manual_output(actuator, 0, output_value)
                        phase += 1
                        next_phase = now + phase_interval_s

                    whole_second = int(elapsed_s)
                    if whole_second > next_report_second:
                        self.initialization_progress.emit(
                            {
                                "actuator": actuator,
                                "elapsed_s": min(elapsed_s, total_duration_s),
                                "total_s": total_duration_s,
                                "stage_index": stage_index,
                                "stage_count": len(stages),
                                "stage_voltage": target_voltage,
                                "output_value": output_value,
                            }
                        )
                        next_report_second = whole_second
                    time.sleep(0.05)
            board.set_manual_output(actuator, 0, 0)
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(True)

        self.initialization_progress.emit(
            {
                "actuator": actuator,
                "elapsed_s": total_duration_s,
                "total_s": total_duration_s,
                "stage_index": len(stages),
                "stage_count": len(stages),
                "stage_voltage": stages[-1],
                "output_value": self._voltage_to_output(stages[-1], supply_voltage),
            }
        )
        self.message.emit(f"Initialization drive complete for actuator {actuator}; diagnosing.", "info")
        result = board.diagnose_actuator(actuator)
        health = self._health_from_currents(
            result.baseline_ma,
            result.forward_ma,
            result.discharge_ma,
        )
        self._actuator_health[actuator] = health
        self.health_ready.emit(actuator // 8, {actuator: health})
        self.diagnosis_ready.emit(
            {
                "actuator": result.actuator,
                "baseline_ma": result.baseline_ma,
                "forward_ma": result.forward_ma,
                "discharge_ma": result.discharge_ma,
            }
        )
        self.message.emit(f"Actuator {actuator} initialized", "ok")
        self.message.emit(
            f"Actuator {actuator} classified as {health['state']} after initialization.",
            "ok" if health["state"] == "idle" else "warn",
        )
        self.busy_changed.emit("")
        self._emit_status()

    def _diagnose_actuator(self, actuator: int) -> None:
        self._ensure_actuator_diagnosable(actuator)
        board = self._require_board()
        self.busy_changed.emit(f"Diagnosing actuator {actuator}")
        self.message.emit(f"Diagnosing actuator {actuator}", "info")
        result = board.diagnose_actuator(actuator)
        health = self._health_from_currents(
            result.baseline_ma,
            result.forward_ma,
            result.discharge_ma,
        )
        self._actuator_health[actuator] = health
        self.health_ready.emit(actuator // 8, {actuator: health})
        self.diagnosis_ready.emit(
            {
                "actuator": result.actuator,
                "baseline_ma": result.baseline_ma,
                "forward_ma": result.forward_ma,
                "discharge_ma": result.discharge_ma,
            }
        )
        self.message.emit(f"Diagnosis complete for actuator {actuator}", "ok")
        self.message.emit(
            f"Actuator {actuator} classified as {health['state']} after diagnosis.",
            "ok" if health["state"] == "idle" else "warn",
        )
        self.busy_changed.emit("")
        self._emit_status()

    def _recover_actuator(self, actuator: int, target_voltage: float, duration_s: int) -> None:
        self._ensure_actuator_recoverable(actuator)
        if target_voltage <= 0:
            raise ValueError("Recovery voltage must be greater than 0.")
        if duration_s <= 0:
            raise ValueError("Recovery duration must be greater than 0.")
        board = self._require_board()
        supply_voltage = board.voltage()
        if supply_voltage <= 0:
            raise RuntimeError("Cannot recover: measured PSU voltage is 0 V.")
        output_value = self._voltage_to_output(target_voltage, supply_voltage)
        self.busy_changed.emit(f"Recovering actuator {actuator}")
        self.message.emit(
            f"Recovering actuator {actuator} at +/-{target_voltage:.1f} V "
            f"for {duration_s}s using raw {output_value}/255 from {supply_voltage:.1f} V PSU.",
            "warn",
        )
        duration_s_float = float(duration_s)
        phase_interval_s = 0.5
        baseline_ma = board.current()
        previous_safety = board.safety()
        samples: list[float] = []
        try:
            if previous_safety:
                board.safety(False)
            start = time.monotonic()
            next_phase = start
            next_report_second = 1
            phase = 0
            while True:
                now = time.monotonic()
                elapsed_s = now - start
                if elapsed_s >= duration_s_float:
                    break

                if now >= next_phase:
                    if phase % 2 == 0:
                        board.set_manual_output(actuator, output_value, 0)
                    else:
                        board.set_manual_output(actuator, 0, output_value)
                    phase += 1
                    next_phase = now + phase_interval_s

                current_ma = board.current()
                samples.append(current_ma)
                delta_ma = abs(current_ma - baseline_ma)
                whole_second = int(elapsed_s)
                if whole_second >= next_report_second:
                    progress = {
                        "actuator": actuator,
                        "elapsed_s": min(whole_second, duration_s),
                        "duration_s": duration_s,
                        "baseline_ma": baseline_ma,
                        "current_ma": current_ma,
                        "delta_ma": delta_ma,
                        "target_voltage": target_voltage,
                        "supply_voltage": supply_voltage,
                        "output_value": output_value,
                    }
                    self.recovery_progress.emit(progress)
                    self.message.emit(
                        "Recovery {actuator}: {elapsed_s}/{duration_s}s, "
                        "delta {delta_ma:.2f} mA.".format(**progress),
                        "info",
                    )
                    next_report_second = whole_second + 1
                time.sleep(0.1)
            board.set_manual_output(actuator, 0, 0)
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(previous_safety)

        recovery_ma = sum(samples) / len(samples) if samples else baseline_ma
        delta_ma = abs(recovery_ma - baseline_ma)
        result = {
            "actuator": actuator,
            "baseline_ma": baseline_ma,
            "recovery_ma": recovery_ma,
            "delta_ma": delta_ma,
            "duration_s": duration_s,
            "target_voltage": target_voltage,
            "supply_voltage": supply_voltage,
            "output_value": output_value,
        }
        self.recovery_ready.emit(result)
        self.message.emit(
            f"Recovery complete for actuator {actuator}: delta {delta_ma:.2f} mA.",
            "ok",
        )
        self.busy_changed.emit("")
        self._emit_status()

    def _detect_group(self, group: int) -> None:
        board = self._require_board()
        if not 0 <= group <= 2:
            raise ValueError("group must be 0, 1, or 2")
        status = board.status()
        self.status_ready.emit(status)
        self._last_status = time.monotonic()
        if str(status["psu"]).upper() != "ON" or str(status["psc"]).upper() != "ON":
            self.message.emit("Detection waits until PSU is on and output is connected.", "warn")
            return

        actuators = list(range(group * 8, group * 8 + 8))
        self.busy_changed.emit(f"Detecting group {group}")
        self.message.emit(
            f"Detection group {group}: actuators {actuators[0]}-{actuators[-1]}, "
            f"PSU {status['psu']}, output {status['psc']}, "
            f"voltage {float(status.get('voltage', 0.0)):.2f} V, "
            f"current {float(status.get('current', 0.0)):.2f} mA.",
            "info",
        )
        self.message.emit(
            "Detection thresholds: <0.10 mA not connected, >3.00 mA error, otherwise ready.",
            "info",
        )
        if self._square_actuators:
            self._stop_square_wave()

        self.message.emit(f"Detection group {group}: commanding all actuators off.", "info")
        for actuator in actuators:
            try:
                board.set_actuator(actuator, 0)
                self.message.emit(f"Detection actuator {actuator}: off command sent.", "info")
            except FirmwareError as exc:
                if exc.code != "ACT_FAILED":
                    raise
                self.message.emit(
                    f"Detection actuator {actuator}: off command refused during discharge lockout.",
                    "warn",
                )

        results: dict[int, dict[str, Any]] = {}
        for actuator in actuators:
            self.health_ready.emit(group, {actuator: {"state": "detecting"}})
            self.message.emit(f"Detection actuator {actuator}: running diagnostic.", "info")
            result = board.diagnose_actuator(actuator)
            entry = self._health_from_currents(
                result.baseline_ma,
                result.forward_ma,
                result.discharge_ma,
            )
            results[actuator] = entry
            self._actuator_health[actuator] = entry
            self.health_ready.emit(group, {actuator: entry})
            self.message.emit(
                f"Detection actuator {actuator}: baseline {result.baseline_ma:.2f} mA, "
                f"forward {result.forward_ma:.2f} mA, discharge {result.discharge_ma:.2f} mA, "
                f"delta {entry['delta_ma']:.2f} mA -> {'ready' if entry['state'] == 'idle' else entry['state']}.",
                "ok" if entry["state"] == "idle" else "warn",
            )

        self.health_ready.emit(group, results)
        self.busy_changed.emit("")
        connected = sum(1 for result in results.values() if result["state"] == "idle")
        errors = sum(1 for result in results.values() if result["state"] == "error")
        missing = sum(1 for result in results.values() if result["state"] == "disconnected")
        self.message.emit(
            f"Group {group} detection: {connected} ready, {missing} not connected, {errors} error.",
            "ok" if errors == 0 else "warn",
        )
        self._emit_status()

    def _start_square_wave(self, actuators: list[int]) -> None:
        board = self._require_board()
        unique = sorted({int(actuator) for actuator in actuators})
        if not unique:
            raise ValueError("Choose at least one actuator for square wave output.")
        for actuator in unique:
            Lansing._validate_actuator(actuator)
            self._ensure_actuator_available(actuator)
        self._square_restore_debug = board.firmware_debug()
        if not self._square_restore_debug:
            board.firmware_debug(True)
        board.flush_debug_lines()
        self._square_actuators = unique
        self._square_phase = "forward"
        self._next_square_step = 0.0
        self.square_changed.emit(True, unique, "arming")
        self.message.emit(
            "Square wave started for "
            + ", ".join(str(value) for value in unique)
            + "; waiting for firmware discharge debug before each reactivation",
            "ok",
        )

    def _stop_square_wave(self) -> None:
        if not self._square_actuators:
            self.square_changed.emit(False, [], "idle")
            return
        board = self._require_board()
        actuators = list(self._square_actuators)
        self._square_actuators = []
        self._square_phase = "idle"
        for actuator in actuators:
            try:
                board.set_actuator(actuator, 0)
            except FirmwareError as exc:
                if exc.code != "ACT_FAILED":
                    raise
        self._restore_square_debug()
        self.square_changed.emit(False, [], "idle")
        self.message.emit("Square wave stopped", "ok")
        self._emit_status()

    def _restore_square_debug(self) -> None:
        if self._board is None or self._square_restore_debug is None:
            self._square_restore_debug = None
            return
        try:
            self._board.firmware_debug(self._square_restore_debug)
        finally:
            self._square_restore_debug = None

    @staticmethod
    def _health_from_currents(
        baseline_ma: float,
        forward_ma: float,
        discharge_ma: float,
    ) -> dict[str, Any]:
        delta_ma = round(abs(forward_ma - baseline_ma), 6)
        if delta_ma < 0.1:
            state = "disconnected"
        elif delta_ma > 3.0:
            state = "error"
        else:
            state = "idle"
        return {
            "state": state,
            "baseline_ma": baseline_ma,
            "forward_ma": forward_ma,
            "discharge_ma": discharge_ma,
            "delta_ma": delta_ma,
        }

    def _ensure_actuator_available(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if not health:
            return
        state = health.get("state")
        if state == "idle":
            return
        if state == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")
        if state == "error":
            raise RuntimeError(
                f"Actuator {actuator} is in error state; current delta is "
                f"{health.get('delta_ma', 0):.2f} mA."
            )

    def _ensure_actuator_diagnosable(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if health and health.get("state") == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")

    def _ensure_actuator_recoverable(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if health and health.get("state") == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")

    @staticmethod
    def _voltage_to_output(target_voltage: float, supply_voltage: float) -> int:
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(1, min(Lansing.max_output, int(Lansing.max_output * ratio)))

    def _service_square_wave(self) -> None:
        if self._board is None or not self._square_actuators:
            return
        now = time.monotonic()
        if now < self._next_square_step:
            return

        try:
            if self._square_phase == "forward":
                for actuator in list(self._square_actuators):
                    self._board.set_actuator(actuator, 255)
                self.square_changed.emit(True, list(self._square_actuators), "full on")
                self._square_phase = "off"
                self._next_square_step = now + 1.0
                return

            if self._square_phase == "off":
                for actuator in list(self._square_actuators):
                    self._board.set_actuator(actuator, 0)
                self.square_changed.emit(True, list(self._square_actuators), "discharge")
                self._square_phase = "wait_debug"
                self._next_square_step = now + 1.0
                return

            if self._square_phase == "wait_debug":
                status = self._board.status()
                self.status_ready.emit(status)
                self._last_status = now
                debug_lines = self._board.flush_debug_lines()
                if self._debug_confirms_discharge_complete(debug_lines):
                    self.square_changed.emit(True, list(self._square_actuators), "ready")
                    self._square_phase = "forward"
                    self._next_square_step = 0.0
                    return
                if now - self._last_square_warning > 2.0:
                    self.message.emit(
                        "Square wave waiting for DBG:DISCHARGE_STOP before reactivation.",
                        "warn",
                    )
                    self._last_square_warning = now
                self._next_square_step = now + 0.1
                return

            self._square_phase = "forward"
            self._next_square_step = 0.0
        except FirmwareError as exc:
            if exc.code == "ACT_FAILED":
                if now - self._last_square_warning > 2.0:
                    self.message.emit(
                        "Square wave waiting for DBG:DISCHARGE_STOP before reactivation.",
                        "warn",
                    )
                    self._last_square_warning = now
                self._square_phase = "wait_debug"
                self._next_square_step = now + 0.1
                return
            self.message.emit(f"Square wave stopped: {exc}", "error")
            self._square_actuators = []
            self._square_phase = "idle"
            self._restore_square_debug()
            self.square_changed.emit(False, [], "idle")
            return
        except Exception as exc:
            self.message.emit(f"Square wave stopped: {exc}", "error")
            self._square_actuators = []
            self._square_phase = "idle"
            self._restore_square_debug()
            self.square_changed.emit(False, [], "idle")
            return

    def _debug_confirms_discharge_complete(self, debug_lines: tuple[str, ...]) -> bool:
        stopped: set[int] = set()
        for line in debug_lines:
            if not line.startswith("DBG:DISCHARGE_STOP,ACT>"):
                continue
            payload = line.removeprefix("DBG:DISCHARGE_STOP,ACT>")
            actuator_text = payload.split(",", 1)[0]
            try:
                stopped.add(int(actuator_text))
            except ValueError:
                continue
        return all(actuator in stopped for actuator in self._square_actuators)

    def _all_off(self) -> None:
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
            return
        board.all_actuators_off()
        self.message.emit("All actuators commanded off", "ok")
        self._emit_status()


class StatusPill(QLabel):
    def __init__(self, text: str = "Disconnected", kind: str = "neutral") -> None:
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setProperty("kind", kind)

    def set(self, text: str, kind: str) -> None:
        self.setText(text)
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "-", unit: str = "") -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")

        row = QHBoxLayout()
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.unit_label = QLabel(unit)
        self.unit_label.setObjectName("MetricUnit")
        row.addWidget(self.value_label)
        row.addWidget(self.unit_label, 0, Qt.AlignBottom)
        row.addStretch()

        layout.addWidget(title_label)
        layout.addLayout(row)

    def set_value(self, value: str, unit: str | None = None) -> None:
        self.value_label.setText(value)
        if unit is not None:
            self.unit_label.setText(unit)


class SwitchToggle(QAbstractButton):
    def __init__(self, *, on_color: str = "#ee2c24", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_color = QColor(on_color)
        self._off_color = QColor("#d9d9d9")
        self._thumb_color = QColor("#ffffff")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(74, 38)

    def sizeHint(self) -> QSize:
        return QSize(74, 38)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        margin = 2
        track = self.rect().adjusted(margin, margin, -margin, -margin)
        radius = track.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._on_color if self.isChecked() else self._off_color)
        painter.drawRoundedRect(track, radius, radius)

        thumb_diameter = track.height() - 6
        thumb_x = track.right() - thumb_diameter - 3 if self.isChecked() else track.left() + 3
        thumb_y = track.top() + 3
        painter.setBrush(self._thumb_color)
        painter.drawEllipse(thumb_x, thumb_y, thumb_diameter, thumb_diameter)


class ToggleMetricCard(QFrame):
    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        on_text: str,
        off_text: str,
        *,
        on_color: str = "#ee2c24",
    ) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self._on_text = on_text
        self._off_text = off_text

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")

        row = QHBoxLayout()
        self.value_label = QLabel(off_text)
        self.value_label.setObjectName("MetricValue")
        self.toggle = SwitchToggle(on_color=on_color)
        self.toggle.toggled.connect(self._on_toggled)
        row.addWidget(self.value_label)
        row.addStretch()
        row.addWidget(self.toggle)

        layout.addWidget(title_label)
        layout.addLayout(row)

    def _on_toggled(self, checked: bool) -> None:
        self.value_label.setText(self._on_text if checked else self._off_text)
        self.toggled.emit(checked)

    def set_state(self, checked: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(checked)
        self.toggle.update()
        self.toggle.blockSignals(False)
        self.value_label.setText(self._on_text if checked else self._off_text)


class ActuatorCard(QFrame):
    focused = Signal(int)

    def __init__(self, actuator: int) -> None:
        super().__init__()
        self.actuator = actuator
        self.setObjectName("ActuatorCard")
        self.setProperty("state", "na")
        self.setProperty("selected", False)
        self._health: dict[str, Any] | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(180)
        self.setFixedHeight(158)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        number = QLabel(f"{actuator:02d}")
        number.setObjectName("ActuatorNumber")
        self.state = StatusPill("N/A", "neutral")
        header.addWidget(number)
        header.addStretch()
        header.addWidget(self.state)

        self.value = QLabel("not detected")
        self.value.setObjectName("ActuatorValue")
        self.runtime = QLabel("connect and detect")
        self.runtime.setObjectName("ActuatorDetail")

        layout.addLayout(header)
        layout.addWidget(self.value)
        layout.addStretch()
        layout.addWidget(self.runtime)

    def mousePressEvent(self, event: Any) -> None:
        self.focused.emit(self.actuator)
        super().mousePressEvent(event)

    def is_selected(self) -> bool:
        return self.property("selected") is True

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_health(self, health: dict[str, Any] | None) -> None:
        self._health = health
        self.setCursor(Qt.PointingHandCursor if self.is_selectable() else Qt.ForbiddenCursor)
        self._apply_health_style()

    def reset_detection(self) -> None:
        self._health = None
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("state", "na")
        self.state.set("N/A", "neutral")
        self.value.setText("not detected")
        self.runtime.setText("connect and detect")
        self.style().unpolish(self)
        self.style().polish(self)

    def is_available(self) -> bool:
        return bool(self._health) and self._health.get("state") == "idle"

    def is_selectable(self) -> bool:
        return not self._health or self._health.get("state") not in {"disconnected", "detecting"}

    def _apply_health_style(self) -> bool:
        if not self._health:
            return False
        state = self._health.get("state")
        if state == "idle":
            self.setProperty("state", "idle")
            self.state.set("Ready", "ok")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"delta {delta:.2f} mA")
            self.runtime.setText(
                f"base {float(self._health.get('baseline_ma', 0.0)):.2f} / "
                f"fwd {float(self._health.get('forward_ma', 0.0)):.2f} mA"
            )
        elif state == "detecting":
            self.setProperty("state", "detecting")
            self.state.set("Detecting", "active")
            self.value.setText("checking...")
            self.runtime.setText("diagnostic running")
        elif state == "disconnected":
            self.setProperty("state", "disconnected")
            self.state.set("Not connected", "neutral")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"delta {delta:.2f} mA")
            self.runtime.setText(
                f"base {float(self._health.get('baseline_ma', 0.0)):.2f} / "
                f"fwd {float(self._health.get('forward_ma', 0.0)):.2f} mA"
            )
        else:
            self.setProperty("state", "health_error")
            self.state.set("Error", "warn")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"delta {delta:.2f} mA")
            self.runtime.setText(
                f"base {float(self._health.get('baseline_ma', 0.0)):.2f} / "
                f"fwd {float(self._health.get('forward_ma', 0.0)):.2f} mA"
            )
        self.style().unpolish(self)
        self.style().polish(self)
        return True

    def update_from_status(self, status: dict[str, Any]) -> None:
        if self._apply_health_style():
            return
        state_value = int(status["actuator_states"][self.actuator])
        if state_value == 0:
            self.reset_detection()
            return
        self.setProperty("state", state_class(state_value))
        self.style().unpolish(self)
        self.style().polish(self)

        label = STATE_NAMES.get(state_value, f"State {state_value}")
        pill_kind = "ok" if state_value == 0 else "active" if state_value == 1 else "warn"
        self.state.set(label, pill_kind)
        self.value.setText(f"value {status['actuator_values'][self.actuator]}")
        self.runtime.setText(f"runtime {format_ms(status['total_ms'][self.actuator])}")


class DashboardWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality Lansing Dashboard")
        self.resize(1480, 940)
        self._cards: list[ActuatorCard] = []
        self._connected = False
        self._status: dict[str, Any] | None = None
        self._selected_actuator = 0
        self._psu_on = False
        self._psc_on = False

        self.worker = BoardWorker()
        self.worker.connected_changed.connect(self._on_connected_changed)
        self.worker.status_ready.connect(self._on_status_ready)
        self.worker.diagnosis_ready.connect(self._on_diagnosis_ready)
        self.worker.initialization_progress.connect(self._on_initialization_progress)
        self.worker.recovery_ready.connect(self._on_recovery_ready)
        self.worker.recovery_progress.connect(self._on_recovery_progress)
        self.worker.health_ready.connect(self._on_health_ready)
        self.worker.square_changed.connect(self._on_square_changed)
        self.worker.busy_changed.connect(self._on_busy_changed)
        self.worker.message.connect(self._log)
        self.worker.start()

        self._build_ui()
        self._refresh_ports()

    def closeEvent(self, event: Any) -> None:
        self.worker.enqueue("disconnect")
        self.worker.requestInterruption()
        self.worker.wait(2000)
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        title_stack = QVBoxLayout()
        brand_row = QHBoxLayout()
        self.logo_label = QLabel()
        self.logo_label.setObjectName("Logo")
        if LOGO_PATH.exists():
            pixmap = QPixmap(str(LOGO_PATH))
            self.logo_label.setPixmap(pixmap.scaledToWidth(250, Qt.SmoothTransformation))
        else:
            self.logo_label.setText("Fluid Reality")
            self.logo_label.setObjectName("AppTitle")

        brand_text = QVBoxLayout()
        title = QLabel("Lansing Dashboard")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Board telemetry, actuator runtime, initialization, diagnostics, and square-wave drive.")
        subtitle.setObjectName("AppSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_row.addWidget(self.logo_label)
        brand_row.addSpacing(18)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        title_stack.addLayout(brand_row)

        self.connection_pill = StatusPill("Disconnected", "neutral")
        self.busy_pill = StatusPill("Ready", "neutral")
        title_row.addLayout(title_stack)
        title_row.addStretch()
        title_row.addWidget(self.busy_pill)
        title_row.addWidget(self.connection_pill)
        outer.addLayout(title_row)

        outer.addWidget(self._build_connection_bar())
        self.metrics_bar = self._build_metrics_bar()
        outer.addWidget(self.metrics_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.actuator_panel = self._build_actuator_panel()
        self.side_panel = self._build_side_panel()
        splitter.addWidget(self.actuator_panel)
        splitter.addWidget(self.side_panel)
        splitter.setSizes([900, 520])
        outer.addWidget(splitter, 1)

        self.setStyleSheet(APP_STYLES)
        self._set_board_controls_enabled(False)

    def _build_connection_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Port"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(150)
        self.refresh_ports_btn = QPushButton("Refresh")
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.refresh_status_btn = QPushButton("Refresh Status")

        self.refresh_ports_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn.clicked.connect(lambda: self.worker.enqueue("disconnect"))
        self.refresh_status_btn.clicked.connect(lambda: self.worker.enqueue("refresh"))
        self.disconnect_btn.setEnabled(False)
        self.refresh_status_btn.setEnabled(False)

        layout.addWidget(self.port_combo)
        layout.addWidget(self.refresh_ports_btn)
        layout.addWidget(self.connect_btn)
        layout.addWidget(self.disconnect_btn)
        layout.addStretch()
        layout.addWidget(self.refresh_status_btn)
        return bar

    def _build_metrics_bar(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.psu_card = ToggleMetricCard("Power Supply", "On", "Off", on_color="#ee2c24")
        self.psu_card.toggled.connect(lambda checked: self.worker.enqueue("psu", checked))
        self.psc_card = ToggleMetricCard(
            "Output Connection",
            "Connected",
            "Open",
            on_color="#0050bd",
        )
        self.psc_card.toggled.connect(lambda checked: self.worker.enqueue("psc", checked))
        self.voltage_card = MetricCard("Voltage", "-", "V")
        self.current_card = MetricCard("Current", "-", "mA")
        self.config_card = MetricCard("Timing", "-")

        for card in (self.psu_card, self.psc_card, self.voltage_card, self.current_card, self.config_card):
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            layout.addWidget(card)
        return row

    def _build_actuator_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        label = QLabel("Actuators")
        label.setObjectName("SectionTitle")
        self.group_combo = QComboBox()
        self.group_combo.addItems(["Group 0 (0-7)", "Group 1 (8-15)", "Group 2 (16-23)"])
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(QLabel("Group"))
        header.addWidget(self.group_combo)

        scroll = QScrollArea()
        scroll.setObjectName("ActuatorScroll")
        scroll.viewport().setObjectName("ActuatorViewport")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        grid_host = QWidget()
        grid_host.setObjectName("ActuatorGridHost")
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)

        for actuator in range(Lansing.actuator_count):
            card = ActuatorCard(actuator)
            card.focused.connect(self._select_actuator)
            self._cards.append(card)
            grid.addWidget(card, (actuator % 8) // 4, actuator % 4)

        scroll.setWidget(grid_host)
        layout.addLayout(header)
        layout.addWidget(self._build_board_controls_panel())
        layout.addWidget(scroll, 1)
        self._show_group(0)
        self._select_actuator(0)
        return panel

    def _build_board_controls_panel(self) -> QWidget:
        controls = QFrame()
        controls.setObjectName("ControlsPanel")
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setObjectName("ControlTabs")

        init_tab = QWidget()
        init_layout = QHBoxLayout(init_tab)
        init_layout.setContentsMargins(10, 10, 10, 10)
        init_layout.setSpacing(10)
        self.init_btn = QPushButton("Initialize")
        self.init_btn.clicked.connect(lambda: self.worker.enqueue("init", self._selected_actuator))
        self.init_progress = QProgressBar()
        self.init_progress.setRange(0, 120)
        self.init_progress.setValue(0)
        self.init_progress.setTextVisible(False)
        self.init_progress.setObjectName("InitProgress")
        self.init_elapsed_label = QLabel("Elapsed 0 / 120 s")
        self.init_elapsed_label.setObjectName("Diagnosis")
        init_layout.addWidget(self.init_btn)
        init_layout.addWidget(self.init_progress, 1)
        init_layout.addWidget(self.init_elapsed_label)
        init_layout.addStretch()

        diag_tab = QWidget()
        diag_layout = QHBoxLayout(diag_tab)
        diag_layout.setContentsMargins(10, 10, 10, 10)
        diag_layout.setSpacing(10)
        self.diag_btn = QPushButton("Diagnose")
        self.diag_btn.clicked.connect(lambda: self.worker.enqueue("diagnose", self._selected_actuator))
        self.diagnosis_label = QLabel("No diagnosis yet")
        self.diagnosis_label.setObjectName("Diagnosis")
        self.diagnosis_label.setWordWrap(True)
        diag_layout.addWidget(self.diag_btn)
        diag_layout.addWidget(self.diagnosis_label, 1)

        recover_tab = QWidget()
        recover_layout = QHBoxLayout(recover_tab)
        recover_layout.setContentsMargins(10, 10, 10, 10)
        recover_layout.setSpacing(10)
        self.recover_btn = QPushButton("Recover")
        self.recover_btn.clicked.connect(
            lambda: self.worker.enqueue(
                "recover",
                self._selected_actuator,
                self.recover_voltage_spin.value(),
                self.recover_duration_spin.value(),
            )
        )
        recover_layout.addWidget(QLabel("Voltage"))
        self.recover_voltage_spin = QDoubleSpinBox()
        self.recover_voltage_spin.setRange(1.0, 500.0)
        self.recover_voltage_spin.setDecimals(1)
        self.recover_voltage_spin.setSingleStep(5.0)
        self.recover_voltage_spin.setValue(50.0)
        self.recover_voltage_spin.setSuffix(" V")
        recover_layout.addWidget(self.recover_voltage_spin)
        recover_layout.addWidget(QLabel("Duration"))
        self.recover_duration_spin = QSpinBox()
        self.recover_duration_spin.setRange(1, 300)
        self.recover_duration_spin.setValue(60)
        self.recover_duration_spin.setSuffix(" s")
        recover_layout.addWidget(self.recover_duration_spin)
        recover_layout.addWidget(self.recover_btn)
        recover_layout.addStretch()

        wave_tab = QWidget()
        wave_layout = QHBoxLayout(wave_tab)
        wave_layout.setContentsMargins(10, 10, 10, 10)
        wave_layout.setSpacing(10)
        self.square_target_btn = QPushButton("Start Selected")
        square_stop = QPushButton("Stop")
        all_off = QPushButton("All Off")
        self.square_target_btn.clicked.connect(lambda: self.worker.enqueue("square_start", [self._selected_actuator]))
        square_stop.clicked.connect(lambda: self.worker.enqueue("square_stop"))
        all_off.clicked.connect(lambda: self.worker.enqueue("all_off"))
        self.square_pill = StatusPill("Stopped", "neutral")
        wave_layout.addWidget(self.square_target_btn)
        wave_layout.addWidget(square_stop)
        wave_layout.addWidget(all_off)
        wave_layout.addWidget(self.square_pill)
        wave_layout.addStretch()

        tabs.addTab(init_tab, "Initialize")
        tabs.addTab(diag_tab, "Diagnose")
        tabs.addTab(recover_tab, "Recover")
        tabs.addTab(wave_tab, "Square Wave")
        layout.addWidget(tabs)

        return controls

    def _build_side_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        log_title = QLabel("Event Log")
        log_title.setObjectName("SectionTitle")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)

        layout.addWidget(log_title)
        layout.addWidget(self.log, 1)
        return panel

    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText().strip()
        ports: list[str] = []
        try:
            from serial.tools import list_ports

            ports = [port.device for port in list_ports.comports()]
        except Exception:
            ports = []

        if current and current not in ports:
            ports.insert(0, current)
        if not ports:
            ports = ["COM4"]

        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if current:
            index = self.port_combo.findText(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
        self.port_combo.blockSignals(False)

    def _connect(self) -> None:
        port = self.port_combo.currentText().strip()
        if not port:
            QMessageBox.warning(self, "Missing Port", "Choose a serial port first.")
            return
        self.worker.enqueue("connect", port)

    def _on_connected_changed(self, connected: bool, detail: str) -> None:
        self._connected = connected
        self.connection_pill.set(detail, "ok" if connected else "neutral")
        self._set_board_controls_enabled(connected)
        self.disconnect_btn.setEnabled(connected)
        self.connect_btn.setEnabled(not connected)
        self.refresh_status_btn.setEnabled(connected)
        if not connected:
            self._psu_on = False
            self._psc_on = False
            self.psu_card.set_state(False)
            self.psc_card.set_state(False)
            self.voltage_card.set_value("-", "V")
            self.current_card.set_value("-", "mA")
            self.config_card.set_value("-", "")
            if hasattr(self, "init_progress"):
                self.init_progress.setValue(0)
                self.init_elapsed_label.setText("Elapsed 0 / 120 s")
            for card in self._cards:
                card.reset_detection()

    def _on_busy_changed(self, text: str) -> None:
        self.busy_pill.set(text or "Ready", "warn" if text else "neutral")

    def _set_board_controls_enabled(self, enabled: bool) -> None:
        if hasattr(self, "metrics_bar"):
            self.metrics_bar.setEnabled(enabled)
        if hasattr(self, "actuator_panel"):
            self.actuator_panel.setEnabled(enabled)
        if hasattr(self, "side_panel"):
            self.side_panel.setEnabled(enabled)
        if enabled:
            self._update_action_availability()
        elif hasattr(self, "init_btn"):
            self.init_btn.setEnabled(False)
            self.diag_btn.setEnabled(False)
            self.recover_btn.setEnabled(False)
            self.square_target_btn.setEnabled(False)

    def _on_status_ready(self, status: dict[str, Any]) -> None:
        self._status = status
        psu_on = str(status["psu"]).upper() == "ON"
        psc_on = str(status["psc"]).upper() == "ON"
        became_ready = (not self._psu_on or not self._psc_on) and psu_on and psc_on
        self._psu_on = psu_on
        self._psc_on = psc_on
        self.psu_card.set_state(psu_on)
        self.psc_card.set_state(psc_on)
        self.voltage_card.set_value(f"{float(status['voltage']):.2f}", "V")
        self.current_card.set_value(f"{float(status['current']):.2f}", "mA")
        config = status["config"]
        self.config_card.set_value(
            f"MAX {format_ms(config['max_active_ms'])} / DIS {format_ms(config['discharge_ms'])}",
            "",
        )
        for card in self._cards:
            card.update_from_status(status)
        if became_ready:
            self._request_group_detection()

    def _on_diagnosis_ready(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Actuator {actuator}: baseline {baseline_ma:.2f} mA, "
            "forward {forward_ma:.2f} mA, discharge {discharge_ma:.2f} mA".format(**result)
        )

    def _on_initialization_progress(self, result: dict[str, Any]) -> None:
        elapsed_s = int(result["elapsed_s"])
        total_s = int(result["total_s"])
        self.init_progress.setRange(0, total_s)
        self.init_progress.setValue(elapsed_s)
        self.init_elapsed_label.setText(
            "Elapsed {elapsed_s} / {total_s} s - stage {stage_index}/{stage_count}, "
            "+/-{stage_voltage:.0f} V".format(
                elapsed_s=elapsed_s,
                total_s=total_s,
                stage_index=int(result["stage_index"]),
                stage_count=int(result["stage_count"]),
                stage_voltage=float(result["stage_voltage"]),
            )
        )

    def _on_recovery_ready(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Recovery {actuator} done after {duration_s}s: baseline {baseline_ma:.2f} mA, "
            "manual avg {recovery_ma:.2f} mA, delta {delta_ma:.2f} mA "
            "at {target_voltage:.1f} V target from {supply_voltage:.1f} V PSU "
            "(raw {output_value}/255)".format(**result)
        )

    def _on_recovery_progress(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Recovery {actuator}: {elapsed_s}/{duration_s}s, baseline "
            "{baseline_ma:.2f} mA, current {current_ma:.2f} mA, "
            "delta {delta_ma:.2f} mA, raw {output_value}/255".format(**result)
        )

    def _on_health_ready(self, group: int, results: dict[int, dict[str, Any]]) -> None:
        for actuator, health in results.items():
            self._cards[actuator].set_health(health)
        has_detecting = any(health.get("state") == "detecting" for health in results.values())
        if not has_detecting and self._selected_actuator // 8 == group:
            self._select_first_available_in_group(group)
        self._update_action_availability()

    def _request_group_detection(self) -> None:
        if self._connected and self._psu_on and self._psc_on:
            self.worker.enqueue("detect_group", self.group_combo.currentIndex())

    def _on_square_changed(self, running: bool, actuators: list[int], phase: str) -> None:
        if running:
            text = f"{phase}: " + ", ".join(str(actuator) for actuator in actuators)
            self.square_pill.set(text, "active")
        else:
            self.square_pill.set("Stopped", "neutral")

    def _show_group(self, group: int) -> None:
        for card in self._cards:
            card.setVisible(card.actuator // 8 == group)

    def _on_group_changed(self, group: int) -> None:
        self._show_group(group)
        selected = self._selected_actuator
        if selected // 8 != group:
            self._select_actuator(group * 8)
        self._request_group_detection()

    def _select_actuator(self, actuator: int) -> None:
        actuator = int(actuator)
        group = actuator // 8
        if hasattr(self, "_cards") and self._cards and not self._cards[actuator].is_selectable():
            health = self._cards[actuator]._health or {}
            state = health.get("state", "blocked")
            self._log(f"Actuator {actuator} is {state}; actions are disabled.", "warn")
            return
        if hasattr(self, "group_combo") and self.group_combo.currentIndex() != group:
            self.group_combo.blockSignals(True)
            self.group_combo.setCurrentIndex(group)
            self.group_combo.blockSignals(False)
            self._show_group(group)
        self._selected_actuator = actuator
        if hasattr(self, "selected_actuator_label"):
            self.selected_actuator_label.setText(f"Actuator {actuator}")
        for card in self._cards:
            card.set_selected(card.actuator == actuator)
        self._update_action_availability()

    def _select_first_available_in_group(self, group: int) -> None:
        current = self._cards[self._selected_actuator]
        if current.actuator // 8 == group and current.is_selectable():
            return
        for actuator in range(group * 8, group * 8 + 8):
            if self._cards[actuator].is_selectable():
                self._select_actuator(actuator)
                return
        self._update_action_availability()

    def _update_action_availability(self) -> None:
        if not hasattr(self, "init_btn"):
            return
        card = self._cards[self._selected_actuator]
        health = card._health or {}
        state = health.get("state", "na")
        available = state == "idle"
        error = state == "error"
        connected = state in {"idle", "error"}
        self.init_btn.setEnabled(available or error)
        self.diag_btn.setEnabled(available or error)
        self.recover_btn.setEnabled(connected)
        self.square_target_btn.setEnabled(available)

    def _log(self, text: str, level: str = "info") -> None:
        color = {
            "ok": "#43b97f",
            "warn": "#d8a22c",
            "error": "#e65f5c",
            "info": "#8fa3bf",
        }.get(level, "#8fa3bf")
        timestamp = time.strftime("%H:%M:%S")
        safe_text = html.escape(text)
        self.log.append(f'<span style="color:{color}">[{timestamp}] {safe_text}</span>')


APP_STYLES = """
QWidget#Root {
    background: #f7f7fc;
    color: #1a1b1f;
    font-size: 13px;
}
QWidget:disabled {
    color: #7a8797;
}
QLabel:disabled {
    color: #7a8797;
}
QLabel#Logo {
    background: transparent;
}
QLabel#AppTitle {
    color: #1a1b1f;
    font-size: 28px;
    font-weight: 700;
}
QLabel#AppSubtitle {
    color: #5d6c7b;
    font-size: 13px;
}
QFrame#TopBar,
QFrame#Panel,
QFrame#MetricCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ControlsPanel {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QTabWidget#ControlTabs::pane {
    background: #ffffff;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    top: -1px;
}
QTabWidget#ControlTabs QTabBar::tab {
    background: #f3f4f7;
    color: #4f5f70;
    border: 1px solid #dcddeb;
    border-bottom-color: #dcddeb;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 7px 14px;
    margin-right: 4px;
    font-weight: 700;
}
QTabWidget#ControlTabs QTabBar::tab:selected {
    background: #0050bd;
    color: #ffffff;
    border-color: #0050bd;
}
QTabWidget#ControlTabs QTabBar::tab:hover:!selected {
    background: #eaf2ff;
    color: #1a1b1f;
}
QProgressBar#InitProgress {
    background: #eef1f6;
    border: 1px solid #d6dae3;
    border-radius: 7px;
    min-height: 16px;
}
QProgressBar#InitProgress::chunk {
    background: #0050bd;
    border-radius: 6px;
}
QFrame#Inset {
    background: #fafafa;
    border: 1px solid #dcddeb;
    border-radius: 8px;
}
QLabel#SectionTitle {
    color: #1a1b1f;
    font-size: 17px;
    font-weight: 700;
}
QLabel#ToolTitle {
    color: #1a1b1f;
    font-size: 13px;
    font-weight: 700;
}
QLabel#MetricTitle {
    color: #5d6c7b;
    font-size: 12px;
    text-transform: uppercase;
}
QLabel#MetricValue {
    color: #1a1b1f;
    font-size: 22px;
    font-weight: 700;
}
QLabel#MetricUnit {
    color: #5d6c7b;
    padding-bottom: 3px;
}
QFrame#ActuatorCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ActuatorCard[state="forward"] {
    background: #f0f7ff;
    border-color: #0050bd;
}
QFrame#ActuatorCard[state="discharge"] {
    background: #ffeff0;
    border-color: #ee2c24;
}
QFrame#ActuatorCard[state="disconnected"] {
    background: #f2f2f3;
    border-color: #c8c8c8;
}
QFrame#ActuatorCard[state="na"] {
    background: #fafafa;
    border-color: #dedfe3;
}
QFrame#ActuatorCard[state="detecting"] {
    background: #f0f7ff;
    border-color: #0050bd;
}
QFrame#ActuatorCard[state="health_error"] {
    background: #ffeff0;
    border-color: #ee2c24;
}
QFrame#ActuatorCard[selected="true"] {
    border: 2px solid #0050bd;
}
QLabel#ActuatorNumber {
    color: #1a1b1f;
    font-size: 20px;
    font-weight: 700;
}
QLabel#ActuatorValue {
    color: #1a1b1f;
    font-size: 16px;
    font-weight: 700;
}
QLabel#ActuatorDetail {
    color: #4f5f70;
    font-size: 13px;
    font-weight: 600;
}
QLabel#ActuatorDetail:disabled {
    color: #5f6f80;
}
QLabel#Diagnosis {
    color: #5d6c7b;
}
QLabel#SelectedActuatorBadge {
    color: #ffffff;
    background: #0050bd;
    border: 1px solid #0050bd;
    border-radius: 7px;
    padding: 9px 14px;
    font-weight: 700;
    min-width: 96px;
}
QLabel#SelectedActuatorBadge:disabled {
    color: #ffffff;
    background: #8fb6ec;
    border-color: #8fb6ec;
}
QLabel[kind="neutral"] {
    color: #1a1b1f;
    background: #f2f2f3;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="ok"] {
    color: #0d4d2c;
    background: #eaf8f1;
    border: 1px solid #69c695;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="active"] {
    color: #ffffff;
    background: #0050bd;
    border: 1px solid #0050bd;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="warn"] {
    color: #721012;
    background: #ffeff0;
    border: 1px solid #ff5a65;
    border-radius: 8px;
    padding: 4px 10px;
}
QPushButton {
    background: #1a1b1f;
    color: #ffffff;
    border: 1px solid #1a1b1f;
    border-radius: 7px;
    padding: 8px 11px;
    font-weight: 700;
}
QPushButton:hover {
    background: #ee2c24;
    border-color: #ee2c24;
}
QPushButton:pressed {
    background: #721012;
    border-color: #721012;
}
QPushButton:disabled {
    background: #e6e8ee;
    color: #7a8797;
    border-color: #d6dae3;
}
QComboBox,
QSpinBox,
QDoubleSpinBox {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 7px 9px;
}
QComboBox:disabled,
QSpinBox:disabled,
QDoubleSpinBox:disabled {
    background: #f3f4f7;
    color: #7a8797;
    border-color: #d6dae3;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 4px;
    selection-background-color: #eaf2ff;
    selection-color: #1a1b1f;
    outline: 0;
}
QTextEdit {
    background: #1a1b1f;
    color: #ffffff;
    border: 1px solid #26272c;
    border-radius: 8px;
    padding: 8px;
}
QScrollArea#ActuatorScroll,
QWidget#ActuatorViewport {
    background: #ffffff;
    border: 0;
}
QWidget#ActuatorGridHost {
    background: #ffffff;
}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    window = DashboardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
