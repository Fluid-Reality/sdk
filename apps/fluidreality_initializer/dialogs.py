"""Dialogs: configure an actuator's initialization, and add a board."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .plots import TimeAxis
from .sequence import (
    MEASURE_EVERY_PHASE,
    MEASUREMENT_MODES,
    InitializationConfig,
    build_plan,
    format_duration,
    format_number_list,
    parse_number_list,
)
from .ui_style import MUTED, NEGATIVE, POSITIVE
from .widgets import MetricTile, refresh_style


def _form_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("FormLabel")
    return label


class SequenceDialog(QDialog):
    """Configure one actuator: identity plus the initialization sequence."""

    def __init__(
        self,
        *,
        board_label: str,
        channel: int,
        actuator_id: str,
        notes: str,
        config: InitializationConfig,
        presets: dict[str, InitializationConfig],
        can_start: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self.setWindowTitle(f"Configure {board_label} · channel {channel}")
        self.setMinimumSize(980, 640)
        self.presets = presets
        self.start_requested = False
        self._can_start = can_start
        self._config = config
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(12)
        title = QLabel(f"Channel {channel} initialization")
        title.setObjectName("DialogTitle")
        subtitle = QLabel(
            f"{board_label} · each actuator runs its own sequence independently. Current is read only at the "
            "start and end of each phase; while it is read, the other actuators on this board are held at 0 V."
        )
        subtitle.setObjectName("DialogSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(18)
        root.addLayout(body, 1)

        # ---------------------------------------------------------- left column
        left = QVBoxLayout()
        left.setSpacing(10)
        body.addLayout(left, 5)

        identity = QGroupBox("Actuator")
        identity_form = QFormLayout(identity)
        identity_form.setLabelAlignment(Qt.AlignLeft)
        self.actuator_id = QLineEdit(actuator_id)
        self.actuator_id.setPlaceholderText("Serial / part ID, e.g. M4.2-0017 (required)")
        self.notes = QLineEdit(notes)
        self.notes.setPlaceholderText("Optional notes for the report")
        identity_form.addRow(_form_label("Actuator ID"), self.actuator_id)
        identity_form.addRow(_form_label("Notes"), self.notes)
        left.addWidget(identity)

        sequence = QGroupBox("Sequence")
        grid = QGridLayout(sequence)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(presets))
        self.preset_combo.addItem("Custom")
        self.preset_combo.currentTextChanged.connect(self._preset_selected)
        load_button = QPushButton("Load…")
        load_button.setObjectName("SmallSecondary")
        load_button.setToolTip("Load a saved sequence or an infinidaq amplifier_sequence_config JSON")
        load_button.clicked.connect(self._load_file)
        save_button = QPushButton("Save as…")
        save_button.setObjectName("SmallSecondary")
        save_button.clicked.connect(self._save_file)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(load_button)
        preset_row.addWidget(save_button)
        grid.addWidget(_form_label("Preset"), 0, 0)
        grid.addLayout(preset_row, 0, 1, 1, 3)

        self.voltages = QLineEdit()
        self.voltages.setPlaceholderText("e.g. 50, 100, 150, 200")
        self.high_times = QLineEdit()
        self.high_times.setPlaceholderText("e.g. 0.5, 1, 2, 5, 10, 20, 50")
        self.repeats = QSpinBox()
        self.repeats.setRange(1, 100)
        self.cycles = QSpinBox()
        self.cycles.setRange(1, 1000)
        self.pause = QDoubleSpinBox()
        self.pause.setRange(0, 3600)
        self.pause.setDecimals(1)
        self.pause.setSuffix(" s")
        self.low_mode = QComboBox()
        self.low_mode.addItem("−V (negative, balanced)", "negative")
        self.low_mode.addItem("0 V", "zero")
        self.pre_hold = QCheckBox("Pre-hold at −V for one high time instead of the leading pause")
        grid.addWidget(_form_label("Voltages (V)"), 1, 0)
        grid.addWidget(self.voltages, 1, 1, 1, 3)
        grid.addWidget(_form_label("High times (s)"), 2, 0)
        grid.addWidget(self.high_times, 2, 1, 1, 3)
        grid.addWidget(_form_label("Repeats"), 3, 0)
        grid.addWidget(self.repeats, 3, 1)
        grid.addWidget(_form_label("Cycles per run"), 3, 2)
        grid.addWidget(self.cycles, 3, 3)
        grid.addWidget(_form_label("Pause"), 4, 0)
        grid.addWidget(self.pause, 4, 1)
        grid.addWidget(_form_label("Low phase"), 4, 2)
        grid.addWidget(self.low_mode, 4, 3)
        grid.addWidget(self.pre_hold, 5, 0, 1, 4)
        left.addWidget(sequence)

        measurement = QGroupBox("Measurement and checks")
        mgrid = QGridLayout(measurement)
        mgrid.setHorizontalSpacing(10)
        self.window_ms = QSpinBox()
        self.window_ms.setRange(20, 5000)
        self.window_ms.setSuffix(" ms")
        self.baseline_ms = QSpinBox()
        self.baseline_ms.setRange(20, 5000)
        self.baseline_ms.setSuffix(" ms")
        self.measurement_mode = QComboBox()
        for key, label in MEASUREMENT_MODES.items():
            self.measurement_mode.addItem(label, key)
        self.measurement_mode.setToolTip(
            "Each reading holds the other actuators on this board at 0 V for about a second.")
        self.measure_zero = QCheckBox("Also read at 0 V phase edges")
        self.pre_check = QCheckBox("Firmware check before (DT0/DT1)")
        self.post_check = QCheckBox("Firmware check after (DT0/DT1)")
        self.use_threshold = QCheckBox("Pass if final Δ ≤")
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.01, 50)
        self.threshold.setDecimals(2)
        self.threshold.setSuffix(" mA")
        self.use_threshold.toggled.connect(self.threshold.setEnabled)
        mgrid.addWidget(_form_label("Readings"), 0, 0)
        mgrid.addWidget(self.measurement_mode, 0, 1, 1, 3)
        mgrid.addWidget(_form_label("Reading window"), 1, 0)
        mgrid.addWidget(self.window_ms, 1, 1)
        mgrid.addWidget(_form_label("Baseline window"), 1, 2)
        mgrid.addWidget(self.baseline_ms, 1, 3)
        mgrid.addWidget(self.pre_check, 2, 0, 1, 2)
        mgrid.addWidget(self.post_check, 2, 2, 1, 2)
        mgrid.addWidget(self.measure_zero, 3, 0, 1, 2)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(self.use_threshold)
        threshold_row.addWidget(self.threshold)
        mgrid.addLayout(threshold_row, 3, 2, 1, 2)
        left.addWidget(measurement)
        left.addStretch()

        # --------------------------------------------------------- right column
        right = QVBoxLayout()
        right.setSpacing(10)
        body.addLayout(right, 4)
        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.tile_time = MetricTile("Estimated time")
        self.tile_runs = MetricTile("Runs")
        self.tile_readings = MetricTile("Current readings")
        self.tile_exposure = MetricTile("Exposure")
        tiles.addWidget(self.tile_time, 0, 0)
        tiles.addWidget(self.tile_runs, 0, 1)
        tiles.addWidget(self.tile_readings, 1, 0)
        tiles.addWidget(self.tile_exposure, 1, 1)
        right.addLayout(tiles)
        preview_title = QLabel("Sequence preview (drive time only)")
        preview_title.setObjectName("ChartTitle")
        right.addWidget(preview_title)
        self.preview = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.preview.setMenuEnabled(False)
        self.preview.hideButtons()
        self.preview.showGrid(x=False, y=True, alpha=0.18)
        self.preview.getAxis("left").setTextPen(pg.mkPen(MUTED))
        self.preview.getAxis("bottom").setTextPen(pg.mkPen(MUTED))
        self.preview_pos = pg.PlotDataItem(stepMode="center", fillLevel=0, brush=pg.mkBrush(POSITIVE), pen=pg.mkPen(POSITIVE, width=1))
        self.preview_neg = pg.PlotDataItem(stepMode="center", fillLevel=0, brush=pg.mkBrush(NEGATIVE), pen=pg.mkPen(NEGATIVE, width=1))
        self.preview.addItem(self.preview_pos)
        self.preview.addItem(self.preview_neg)
        self.preview.setMinimumHeight(200)
        right.addWidget(self.preview, 1)
        self.error = QLabel("")
        self.error.setObjectName("ErrorText")
        self.error.setWordWrap(True)
        self.error.hide()
        right.addWidget(self.error)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("SecondaryButton")
        self.save_button.clicked.connect(self._save)
        self.start_button = QPushButton("Save && start")
        self.start_button.setEnabled(can_start)
        self.start_button.setToolTip("" if can_start else "Connect the board and turn power on to start")
        self.start_button.clicked.connect(self._save_and_start)
        footer.addWidget(cancel)
        footer.addWidget(self.save_button)
        footer.addWidget(self.start_button)
        root.addLayout(footer)

        for widget in (self.voltages, self.high_times):
            widget.textChanged.connect(self._changed)
        for widget in (self.repeats, self.cycles, self.window_ms, self.baseline_ms):
            widget.valueChanged.connect(self._changed)
        self.pause.valueChanged.connect(self._changed)
        self.threshold.valueChanged.connect(self._changed)
        self.low_mode.currentIndexChanged.connect(self._changed)
        self.measurement_mode.currentIndexChanged.connect(self._changed)
        for widget in (self.pre_hold, self.measure_zero, self.pre_check, self.post_check, self.use_threshold):
            widget.toggled.connect(self._changed)

        self._apply_config(config)
        matching = next((name for name, preset in presets.items() if preset == config), None)
        self._loading = True
        self.preset_combo.setCurrentText(matching or "Custom")
        self._loading = False
        self._changed()

    # ----------------------------------------------------------------- values
    def _apply_config(self, config: InitializationConfig) -> None:
        self._loading = True
        self.voltages.setText(format_number_list(config.voltages_v))
        self.high_times.setText(format_number_list(config.high_times_s))
        self.repeats.setValue(config.repeats)
        self.cycles.setValue(config.num_cycles)
        self.pause.setValue(config.pause_time_s)
        self.low_mode.setCurrentIndex(0 if config.low_mode == "negative" else 1)
        self.pre_hold.setChecked(config.pre_hold)
        self.window_ms.setValue(config.measure_window_ms)
        self.baseline_ms.setValue(config.baseline_window_ms)
        self.measure_zero.setChecked(config.measure_zero_phases)
        index = self.measurement_mode.findData(config.measurement_mode)
        self.measurement_mode.setCurrentIndex(max(0, index))
        self.measure_zero.setEnabled(config.measurement_mode == MEASURE_EVERY_PHASE)
        self.pre_check.setChecked(config.pre_check)
        self.post_check.setChecked(config.post_check)
        self.use_threshold.setChecked(config.pass_max_delta_ma is not None)
        self.threshold.setValue(config.pass_max_delta_ma or 3.0)
        self.threshold.setEnabled(config.pass_max_delta_ma is not None)
        self._config_name = config.name
        self._loading = False

    def _read_config(self) -> InitializationConfig:
        def numbers(field: QLineEdit, name: str) -> tuple[float, ...]:
            try:
                values = parse_number_list(field.text())
            except ValueError:
                field.setProperty("invalid", "true")
                refresh_style(field)
                raise ValueError(f"{name} must be a comma-separated list of numbers.") from None
            field.setProperty("invalid", "false")
            refresh_style(field)
            return values

        name = self.preset_combo.currentText()
        if name == "Custom":
            name = self._config_name if self._config_name not in self.presets else "Custom initialization"
        return InitializationConfig(
            name=name,
            voltages_v=numbers(self.voltages, "Voltages"),
            high_times_s=numbers(self.high_times, "High times"),
            repeats=self.repeats.value(),
            num_cycles=self.cycles.value(),
            pause_time_s=self.pause.value(),
            low_mode=self.low_mode.currentData(),
            pre_hold=self.pre_hold.isChecked(),
            measure_window_ms=self.window_ms.value(),
            baseline_window_ms=self.baseline_ms.value(),
            measurement_mode=self.measurement_mode.currentData(),
            measure_zero_phases=self.measure_zero.isChecked(),
            pre_check=self.pre_check.isChecked(),
            post_check=self.post_check.isChecked(),
            pass_max_delta_ma=self.threshold.value() if self.use_threshold.isChecked() else None,
        )

    def _changed(self, *_args: Any) -> None:
        if self._loading:
            return
        if self.preset_combo.currentText() != "Custom":
            preset = self.presets.get(self.preset_combo.currentText())
            try:
                current = self._read_config()
            except ValueError:
                current = None
            if current is None or preset is None or current.with_changes(name=preset.name) != preset:
                self._loading = True
                self.preset_combo.setCurrentText("Custom")
                self._loading = False
        try:
            config = self._read_config()
            plan = build_plan(config)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            self.save_button.setEnabled(False)
            self.start_button.setEnabled(False)
            return
        self.error.hide()
        self.measure_zero.setEnabled(config.measurement_mode == MEASURE_EVERY_PHASE)
        self.save_button.setEnabled(True)
        self.start_button.setEnabled(self._can_start)
        forward, reverse = plan.volt_seconds()
        self.tile_time.set(format_duration(plan.estimated_duration_s()),
                           f"{format_duration(plan.drive_duration_s)} drive + readings")
        self.tile_runs.set(str(len(plan.runs)), f"{len(plan.phases)} phases")
        per_run = plan.measurement_count() / max(1, len(plan.runs))
        self.tile_readings.set(str(plan.measurement_count()),
                               f"{per_run:g} per run · {plan.measurement_event_count()} pauses of the others")
        self.tile_exposure.set(f"{forward / 1000:.1f} kV·s", f"forward · reverse {reverse / 1000:.1f} kV·s")
        self._draw_preview(plan)

    def _draw_preview(self, plan: Any) -> None:
        phases = plan.phases
        if len(phases) > 8000:
            phases = phases[:8000]
        x = np.zeros(len(phases) + 1)
        y = np.zeros(len(phases))
        t = 0.0
        for index, phase in enumerate(phases):
            x[index] = t
            y[index] = phase.target_v
            t += phase.duration_s
        x[-1] = t
        self.preview_pos.setData(x, np.clip(y, 0, None))
        self.preview_neg.setData(x, np.clip(y, None, 0))
        limit = max(50.0, max(abs(v) for v in plan.config.voltages_v) * 1.12)
        self.preview.setYRange(-limit, limit, padding=0)
        self.preview.setXRange(0, max(1.0, t), padding=0.01)

    def _preset_selected(self, name: str) -> None:
        if self._loading or name not in self.presets:
            return
        self._apply_config(self.presets[name])
        self._changed()

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load sequence", str(Path.home()), "JSON (*.json)")
        if not path:
            return
        try:
            config = InitializationConfig.load(path)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(self, "Could not load", str(exc))
            return
        self._apply_config(config)
        self._loading = True
        self.preset_combo.setCurrentText("Custom")
        self._loading = False
        self._config_name = config.name
        self._changed()

    def _save_file(self) -> None:
        try:
            config = self._read_config()
        except ValueError as exc:
            QMessageBox.critical(self, "Invalid sequence", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save sequence", str(Path.home() / "initialization.json"), "JSON (*.json)")
        if path:
            config.save(path)

    def _validated(self) -> bool:
        if not self.actuator_id.text().strip():
            self.actuator_id.setProperty("invalid", "true")
            refresh_style(self.actuator_id)
            self.actuator_id.setFocus()
            self.error.setText("Enter the actuator ID; it identifies the report.")
            self.error.show()
            return False
        try:
            self._config = self._read_config()
            build_plan(self._config)
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return False
        return True

    def _save(self) -> None:
        if self._validated():
            self.accept()

    def _save_and_start(self) -> None:
        if self._validated():
            self.start_requested = True
            self.accept()

    def result_config(self) -> InitializationConfig:
        return self._config


class AddBoardDialog(QDialog):
    """Pick a serial port, network endpoint or simulated board."""

    def __init__(self, *, default_label: str, demo_boards: list[str], used_endpoints: set[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self.setWindowTitle("Add board")
        self.setMinimumWidth(520)
        self.used_endpoints = used_endpoints
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)
        title = QLabel("Add a Rockford board")
        title.setObjectName("DialogTitle")
        hint = QLabel("Connect over USB serial. Each board runs up to five actuators (channels 0-4).")
        hint.setObjectName("DialogSubtitle")
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        form = QFormLayout()
        self.label = QLineEdit(default_label)
        port_row = QHBoxLayout()
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setToolTip("Serial port (e.g. COM17), or tcp://host:port")
        refresh = QPushButton("Refresh")
        refresh.setObjectName("SmallSecondary")
        refresh.clicked.connect(self._refresh_ports)
        port_row.addWidget(self.port, 1)
        port_row.addWidget(refresh)
        form.addRow(_form_label("Name"), self.label)
        form.addRow(_form_label("Connection"), port_row)
        layout.addLayout(form)
        self.port_detail = QLabel("")
        self.port_detail.setObjectName("Hint")
        self.port_detail.setWordWrap(True)
        layout.addWidget(self.port_detail)
        self.demo_boards = demo_boards
        self.port.currentIndexChanged.connect(self._show_detail)
        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Connect")
        ok.clicked.connect(self._accept)
        footer.addWidget(cancel)
        footer.addWidget(ok)
        layout.addLayout(footer)
        self._details: dict[str, str] = {}
        self._refresh_ports()

    def _refresh_ports(self) -> None:
        current = self.port.currentText()
        self.port.clear()
        self._details.clear()
        try:
            from serial.tools import list_ports

            ports = sorted(list_ports.comports(), key=lambda p: p.device)
        except Exception:  # noqa: BLE001
            ports = []
        for port in ports:
            if port.device in self.used_endpoints:
                continue
            self.port.addItem(port.device, port.device)
            self._details[port.device] = " · ".join(
                item for item in (port.description, port.manufacturer, port.serial_number) if item
            )
        for name in self.demo_boards:
            endpoint = f"sim://{name}"
            if endpoint in self.used_endpoints:
                continue
            self.port.addItem(f"{name} (simulated)", endpoint)
            self._details[endpoint] = "Simulated Rockford board running the SDK simulator in-process."
        if current:
            index = self.port.findText(current)
            if index >= 0:
                self.port.setCurrentIndex(index)
        self._show_detail()

    def _show_detail(self, *_args: Any) -> None:
        endpoint = self.endpoint()
        self.port_detail.setText(self._details.get(endpoint, ""))

    def endpoint(self) -> str:
        data = self.port.currentData()
        text = self.port.currentText().strip()
        if data and self.port.itemText(self.port.currentIndex()) == text:
            return str(data)
        return text

    def _accept(self) -> None:
        if not self.endpoint():
            QMessageBox.warning(self, "Add board", "Choose a serial port.")
            return
        if self.endpoint() in self.used_endpoints:
            QMessageBox.warning(self, "Add board", "That board is already connected.")
            return
        self.accept()
