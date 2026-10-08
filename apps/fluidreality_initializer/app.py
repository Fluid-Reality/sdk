"""Fluid Reality Actuator Initialization.

Runs long, independent initialization sequences on up to five actuators per
Rockford board, across any number of boards, with live visualisation and a
report per actuator.

    python apps/fluidreality_initializer/app.py            # real hardware
    python apps/fluidreality_initializer/app.py --demo     # simulated boards
"""

from __future__ import annotations

import argparse
import atexit
import json
import queue
import sys
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
APPS_ROOT = APP_ROOT.parent
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDesktopServices, QFontDatabase, QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fluid_reality import Rockford  # noqa: E402
from fluidreality_initializer.dialogs import AddBoardDialog, SequenceDialog  # noqa: E402
from fluidreality_initializer.engine import (  # noqa: E402
    ACTIVE_STATES,
    BoardSession,
    Clock,
    Event,
)
from fluidreality_initializer.plots import LivePlots, format_clock  # noqa: E402
from fluidreality_initializer.reporting import ReportWriter  # noqa: E402
from fluidreality_initializer.sequence import (  # noqa: E402
    PHASE_HIGH,
    InitializationConfig,
    build_plan,
    format_duration,
    load_presets,
)
from fluidreality_initializer.ui_style import APP_STYLES  # noqa: E402
from fluidreality_initializer.widgets import CHANNELS, BoardPanel, MetricTile, Pill  # noqa: E402

ASSETS = APP_ROOT / "assets"
LOGO_PATH = ASSETS / "fluid_reality_logo_transparent.png"
ICON_PATH = ASSETS / "fluid-reality-icon.png"
DEFAULT_OUTPUT = Path.home() / "FluidReality" / "initialization_reports"
LOG_COLORS = {"ok": "#7ee2a8", "info": "#d7dae0", "warn": "#f5c76b", "error": "#ff8a80", "debug": "#8b95a1"}


class ChannelModel:
    """UI-side state for one actuator channel."""

    def __init__(self, board_key: str, channel: int) -> None:
        self.board_key = board_key
        self.channel = channel
        self.actuator_id = ""
        self.notes = ""
        self.config: InitializationConfig | None = None
        self.detection: str | None = None
        self.detection_delta: float | None = None
        self.snapshot: dict[str, Any] | None = None
        self.process_status: str | None = None
        self.status_detail = ""
        self.report_paths: dict[str, str] = {}
        self.plan_runs: list[Any] = []
        self.run_started_at: str | None = None  # started_at of the run shown
        self.previous_started_at: str | None = None
        self.reset_data()

    def reset_data(self) -> None:
        self.voltage_steps: list[tuple[float, float]] = []
        self.measurements: list[Any] = []
        self.run_sums: dict[int, list[float]] = {}
        self.now_t = 0.0
        self.data_version = 0

    def add_measurement(self, measurement: Any) -> None:
        self.measurements.append(measurement)
        if measurement.phase_kind == PHASE_HIGH and measurement.edge == "end":
            entry = self.run_sums.setdefault(measurement.run_id, [0.0, 0, measurement.voltage_v])
            entry[0] += measurement.delta_ma
            entry[1] += 1
        self.data_version += 1

    def add_voltage(self, point: tuple[float, float]) -> None:
        self.voltage_steps.append(point)
        self.data_version += 1

    def run_means(self) -> list[tuple[int, float, float]]:
        return [(run_id, total / count, voltage) for run_id, (total, count, voltage) in sorted(self.run_sums.items()) if count]

    def spark(self) -> list[float]:
        return [m.delta_ma for m in self.measurements if m.phase_kind == PHASE_HIGH and m.edge == "end"][-60:]

    @property
    def active(self) -> bool:
        return self.process_status in ACTIVE_STATES


class BoardModel:
    def __init__(self, key: str, label: str, endpoint: str, session: BoardSession, panel: BoardPanel) -> None:
        self.key = key
        self.label = label
        self.endpoint = endpoint
        self.session = session
        self.panel = panel
        self.connected = False
        self.faulted = False
        self.closed = False
        self.psu_on = False
        self.supply_v = 0.0
        self.busy = ""
        self.info: dict[str, Any] = {}
        self.channels = [ChannelModel(key, channel) for channel in range(CHANNELS)]


def tile_view(model: ChannelModel, board: BoardModel) -> dict[str, Any]:
    snap = model.snapshot if model.active or model.process_status else None
    view: dict[str, Any] = {"actuator_id": model.actuator_id, "full_scale_v": max(board.supply_v, 50.0)}
    status = model.process_status
    if status in ACTIVE_STATES and snap:
        if snap.get("measuring"):
            state, pill = "measuring", "Reading"
        elif snap.get("board_paused"):
            state, pill = "held", "Held 0 V"
        elif status == "paused":
            state, pill = "paused", "Paused"
        elif status == "checking":
            state, pill = "checking", "Checking"
        else:
            state, pill = "running", "Running"
        applied = snap.get("applied_v", 0.0)
        view.update(
            state=state, pill=pill, pill_kind=state, applied_v=applied,
            voltage_text=f"{applied:+.0f} V" if abs(applied) >= 0.5 else "0 V",
            detail=(f"Run {snap['run_id']}/{snap['run_count']} · {snap['run_voltage_v']:g} V · {snap['run_high_time_s']:g} s\n"
                    f"{snap['phase_label']} · {max(0.0, snap['phase_duration_s'] - snap['phase_elapsed_s']):.1f} s left"),
            progress=snap.get("progress", 0.0),
            progress_text=f"{snap.get('progress', 0) * 100:.0f}% · {format_duration(snap.get('eta_s', 0))} left",
        )
    elif status in ("completed", "stopped", "failed"):
        view.update(
            state=status, pill=status.title(), pill_kind=status, applied_v=0.0, voltage_text="0 V",
            detail=model.status_detail.replace("Initialization complete - ", "").replace(" - ", " · ")[:90] or " ",
            progress=snap.get("progress", 0.0) if snap else 0.0,
            progress_text="Report ready" if model.report_paths else " ",
        )
    else:
        detection = model.detection
        mapping = {
            "Ready": ("ready", "Ready"), "Error": ("error", "Error"), "Not connected": ("not_connected", "Not connected"),
            "Detecting": ("detecting", "Detecting"), "Present": ("detecting", "Present"),
        }
        state, pill = mapping.get(detection or "", ("configured", "Configured") if model.config else ("idle", "Idle"))
        detail = " "
        if model.config:
            plan = build_plan(model.config)
            detail = f"{model.config.name}\n{len(plan.runs)} runs · ~{format_duration(plan.estimated_duration_s())}"
        elif detection in ("Ready", "Error"):
            detail = f"Detected Δ {model.detection_delta:.2f} mA" if model.detection_delta is not None else " "
        view.update(state=state, pill=pill, pill_kind=state, applied_v=0.0,
                    voltage_text="—" if not board.psu_on else "0 V", detail=detail, progress=0.0, progress_text=" ")
    spark = model.spark()
    view["spark"] = spark
    view["spark_caption"] = f"end-of-high Δ {spark[-1]:.2f} mA" if spark else " "
    return view


class DetailPane(QFrame):
    """Everything about the selected actuator."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel("Select an actuator")
        self.title.setObjectName("DetailTitle")
        self.subtitle = QLabel("Click a channel tile; double-click to configure it.")
        self.subtitle.setObjectName("AppSubtitle")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.pill = Pill("Idle", "idle")
        header.addWidget(self.pill, 0, Qt.AlignTop)
        layout.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.configure_button = QPushButton("Configure…")
        self.configure_button.setObjectName("SecondaryButton")
        self.start_button = QPushButton("Start")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("SecondaryButton")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("DangerButton")
        self.report_button = QPushButton("Open report")
        self.report_button.setObjectName("SecondaryButton")
        self.folder_button = QPushButton("Report folder")
        self.folder_button.setObjectName("SecondaryButton")
        for button in (self.configure_button, self.start_button, self.pause_button, self.stop_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.report_button)
        actions.addWidget(self.folder_button)
        layout.addLayout(actions)

        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.tile_progress = MetricTile("Progress")
        self.tile_phase = MetricTile("Phase")
        self.tile_run = MetricTile("Run")
        self.tile_drive = MetricTile("Drive")
        self.tile_delta = MetricTile("End-of-high Δ")
        self.tile_pauses = MetricTile("Held for neighbours")
        for index, tile in enumerate((self.tile_progress, self.tile_phase, self.tile_run, self.tile_drive,
                                      self.tile_delta, self.tile_pauses)):
            tiles.addWidget(tile, index // 3, index % 3)
        layout.addLayout(tiles)
        self.progress = QProgressBar()
        self.progress.setObjectName("BigProgress")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.plots = LivePlots()
        layout.addWidget(self.plots, 1)

        self.configure_button.clicked.connect(lambda: window.configure_selected())
        self.start_button.clicked.connect(lambda: window.start_selected())
        self.pause_button.clicked.connect(lambda: window.toggle_pause_selected())
        self.stop_button.clicked.connect(lambda: window.stop_selected())
        self.report_button.clicked.connect(lambda: window.open_report_selected(html=True))
        self.folder_button.clicked.connect(lambda: window.open_report_selected(html=False))
        self.show_model(None, None)

    def show_model(self, model: ChannelModel | None, board: BoardModel | None) -> None:
        self.plots.set_model(model)
        self.refresh(model, board)

    def refresh(self, model: ChannelModel | None, board: BoardModel | None) -> None:
        enabled = model is not None and board is not None
        for button in (self.configure_button, self.start_button, self.pause_button, self.stop_button,
                       self.report_button, self.folder_button):
            button.setEnabled(False)
        if not enabled:
            self.title.setText("Select an actuator")
            self.subtitle.setText("Click a channel tile; double-click to configure it.")
            self.pill.set("Idle", "idle")
            for tile in (self.tile_progress, self.tile_phase, self.tile_run, self.tile_drive, self.tile_delta, self.tile_pauses):
                tile.set("-", " ")
            self.progress.setValue(0)
            return
        assert model is not None and board is not None
        view = tile_view(model, board)
        self.title.setText(model.actuator_id or f"Channel {model.channel}")
        config_name = model.config.name if model.config else "not configured"
        self.subtitle.setText(f"{board.label} · channel {model.channel} · {config_name}")
        self.pill.set(view["pill"], view["pill_kind"])
        ready_board = board.connected and not board.faulted and board.psu_on
        self.configure_button.setEnabled(not model.active)
        self.start_button.setEnabled(ready_board and not model.active)
        self.start_button.setText("Restart" if model.process_status in ("completed", "stopped", "failed") else "Start")
        self.pause_button.setEnabled(model.process_status in ("running", "paused"))
        self.pause_button.setText("Resume" if model.process_status == "paused" else "Pause")
        self.stop_button.setEnabled(model.active)
        self.report_button.setEnabled(bool(model.report_paths))
        self.folder_button.setEnabled(bool(model.report_paths))
        snap = model.snapshot
        if snap and model.process_status:
            self.tile_progress.set(f"{snap['progress'] * 100:.1f}%",
                                   f"{format_duration(snap['eta_s'])} left · {format_clock(snap['wall_t_s'])} elapsed")
            self.tile_phase.set(snap["phase_label"] or "-",
                                f"{snap['phase_elapsed_s']:.1f} / {snap['phase_duration_s']:g} s · {snap['phase_pos'] + 1}/{snap['phase_count']}")
            self.tile_run.set(f"{snap['run_id']} / {snap['run_count']}",
                              f"{snap['run_voltage_v']:g} V · high {snap['run_high_time_s']:g} s · repeat {snap['run_repeat']}")
            applied = snap["applied_v"]
            self.tile_drive.set(f"{applied:+.0f} V" if abs(applied) >= 0.5 else "0 V",
                                f"target {snap['target_v']:+g} V · supply {snap['supply_v']:.0f} V")
            last = snap.get("last_high_end_delta_ma")
            start = snap.get("last_start_delta_ma")
            self.tile_delta.set("-" if last is None else f"{last:.3f} mA",
                                f"{snap['measurement_count']} readings" + ("" if start is None else f" · last start {start:.3f}"))
            self.tile_pauses.set(format_clock(snap["interrupted_s"]),
                                 f"exposure +{snap['vt_forward_vs'] / 1000:.1f} / −{snap['vt_reverse_vs'] / 1000:.1f} kV·s")
            self.progress.setValue(round(snap["progress"] * 1000))
        else:
            for tile in (self.tile_progress, self.tile_phase, self.tile_run, self.tile_drive, self.tile_delta, self.tile_pauses):
                tile.set("-", " ")
            if model.config:
                plan = build_plan(model.config)
                self.tile_progress.set("0%", f"~{format_duration(plan.estimated_duration_s())} estimated")
                self.tile_run.set(f"0 / {len(plan.runs)}", f"{len(plan.phases)} phases")
            self.progress.setValue(0)
        if model.process_status in ("completed", "stopped", "failed"):
            self.subtitle.setText(f"{board.label} · channel {model.channel} · {model.status_detail}")


class MainWindow(QMainWindow):
    def __init__(self, *, demo: bool = False, demo_speed: float = 20.0) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality · Actuator Initialization")
        self.resize(1680, 1020)
        self.demo = demo
        self.demo_speed = demo_speed
        self.settings = QSettings("FluidReality", "ActuatorInitialization")
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.boards: dict[str, BoardModel] = {}
        self._board_counter = 0
        self.selected: tuple[str, int] | None = None
        self.presets = load_presets()
        self.output_dir = Path(str(self.settings.value("output_dir", str(DEFAULT_OUTPUT))))
        self._build_ui()
        self.event_timer = QTimer(self)
        self.event_timer.timeout.connect(self._drain_events)
        self.event_timer.start(80)
        self.plot_timer = QTimer(self)
        self.plot_timer.timeout.connect(self._refresh_selected)
        self.plot_timer.start(250)
        if demo:
            from fluidreality_initializer.demo import DEMO_BOARDS

            for name in DEMO_BOARDS:
                self.add_board(name, f"sim://{name}")

    # ------------------------------------------------------------------ layout
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel()
        pixmap = QPixmap(str(LOGO_PATH))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaledToHeight(44, Qt.SmoothTransformation))
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Actuator Initialization")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Independent per-actuator sequences on Rockford boards · live readings · per-actuator reports")
        subtitle.setObjectName("AppSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        if self.demo:
            header.addWidget(Pill(f"DEMO · simulated boards · {self.demo_speed:g}× speed", "held"))
        operator_label = QLabel("Operator")
        operator_label.setObjectName("FormLabel")
        self.operator = QLineEdit(str(self.settings.value("operator", "")))
        self.operator.setPlaceholderText("Name or initials")
        self.operator.setFixedWidth(170)
        self.operator.editingFinished.connect(lambda: self.settings.setValue("operator", self.operator.text()))
        self.output_button = QPushButton()
        self.output_button.setObjectName("SecondaryButton")
        self.output_button.setToolTip("Folder where per-actuator reports are written")
        self.output_button.clicked.connect(self._choose_output_dir)
        self._update_output_button()
        add_button = QPushButton("+ Add board")
        add_button.clicked.connect(self.prompt_add_board)
        header.addWidget(operator_label)
        header.addWidget(self.operator)
        header.addWidget(self.output_button)
        header.addWidget(add_button)
        layout.addLayout(header)

        vertical = QSplitter(Qt.Vertical)
        horizontal = QSplitter(Qt.Horizontal)
        self.board_scroll = QScrollArea()
        self.board_scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("ScrollHost")
        self.board_column = QVBoxLayout(host)
        self.board_column.setContentsMargins(0, 0, 6, 0)
        self.board_column.setSpacing(10)
        self.empty_hint = QLabel("No boards yet. Use “+ Add board” to connect a Rockford over USB.")
        self.empty_hint.setObjectName("AppSubtitle")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setMinimumHeight(120)
        self.board_column.addWidget(self.empty_hint)
        self.board_column.addStretch()
        self.board_scroll.setWidget(host)
        horizontal.addWidget(self.board_scroll)
        self.detail = DetailPane(self)
        horizontal.addWidget(self.detail)
        horizontal.setStretchFactor(0, 5)
        horizontal.setStretchFactor(1, 6)
        horizontal.setSizes([980, 680])
        vertical.addWidget(horizontal)

        log_panel = QFrame()
        log_panel.setObjectName("Panel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 8, 12, 10)
        log_header = QHBoxLayout()
        log_title = QLabel("Event log")
        log_title.setObjectName("SectionTitle")
        save_log = QPushButton("Save log")
        save_log.setObjectName("SmallSecondary")
        save_log.clicked.connect(self._save_log)
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(save_log)
        log_layout.addLayout(log_header)
        self.log_view = QTextEdit()
        self.log_view.setObjectName("EventLog")
        self.log_view.setReadOnly(True)
        log_layout.addWidget(self.log_view)
        vertical.addWidget(log_panel)
        vertical.setStretchFactor(0, 5)
        vertical.setStretchFactor(1, 1)
        vertical.setSizes([840, 150])
        layout.addWidget(vertical, 1)

    def _update_output_button(self) -> None:
        text = str(self.output_dir)
        if len(text) > 42:
            text = "…" + text[-41:]
        self.output_button.setText(f"Reports: {text}")

    def _choose_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Report folder", str(self.output_dir))
        if path:
            self.output_dir = Path(path)
            self.settings.setValue("output_dir", path)
            self._update_output_button()
            for board in self.boards.values():
                board.session.output_dir = self.output_dir

    # ------------------------------------------------------------------ boards
    def prompt_add_board(self) -> None:
        demo_boards: list[str] = []
        if self.demo:
            from fluidreality_initializer.demo import DEMO_BOARDS

            demo_boards = list(DEMO_BOARDS)
        dialog = AddBoardDialog(
            default_label=f"Board {self._board_counter + 1}",
            demo_boards=demo_boards,
            used_endpoints={board.endpoint for board in self.boards.values()},
            parent=self,
        )
        if dialog.exec():
            self.add_board(dialog.label.text().strip() or dialog.endpoint(), dialog.endpoint())

    def add_board(self, label: str, endpoint: str) -> None:
        self._board_counter += 1
        key = f"b{self._board_counter}"
        if endpoint.startswith("sim://"):
            from fluidreality_initializer.demo import demo_board_factory

            factory = demo_board_factory(endpoint[len("sim://"):], speed=self.demo_speed)
            clock = Clock(self.demo_speed)
        else:
            def factory(endpoint: str = endpoint) -> Rockford:
                return Rockford(endpoint, timeout=15.0)

            clock = Clock()
        session = BoardSession(key, label, endpoint, factory, self.events, output_dir=self.output_dir,
                               clock=clock, reporter_factory=ReportWriter)
        panel = BoardPanel(key, label, endpoint)
        panel.power_toggled.connect(self._power_toggled)
        panel.detect_requested.connect(self._detect)
        panel.start_all_requested.connect(self._start_all)
        panel.stop_all_requested.connect(self._stop_all)
        panel.remove_requested.connect(self._remove_board)
        panel.tile_clicked.connect(self.select)
        panel.tile_double_clicked.connect(self._configure)
        board = BoardModel(key, label, endpoint, session, panel)
        self.boards[key] = board
        self.empty_hint.hide()
        self.board_column.insertWidget(self.board_column.count() - 1, panel)
        self._refresh_board(board)
        session.start()
        self._log("info", f"Connecting to {label} ({endpoint})…")
        if self.selected is None:
            self.select(key, 0)

    def shutdown_all(self) -> None:
        """Stop every board thread: outputs to 0 V, safety on, partial reports."""

        for board in self.boards.values():
            board.session.shutdown()

    def _remove_board(self, key: str) -> None:
        board = self.boards.get(key)
        if board is None:
            return
        if any(channel.active for channel in board.channels):
            QMessageBox.warning(self, "Disconnect", "Stop the running initializations on this board first.")
            return
        board.session.shutdown()
        board.panel.setParent(None)
        board.panel.deleteLater()
        del self.boards[key]
        if self.selected and self.selected[0] == key:
            self.selected = None
            self.detail.show_model(None, None)
        if not self.boards:
            self.empty_hint.show()
        self._log("info", f"Disconnected {board.label}")

    def _power_toggled(self, key: str, enabled: bool) -> None:
        board = self.boards[key]
        if not enabled and any(channel.active for channel in board.channels):
            answer = QMessageBox.question(
                self, "Turn power off",
                "Initializations are running on this board. Turning power off stops them. Continue?",
            )
            if answer != QMessageBox.Yes:
                self._refresh_board(board)
                return
        board.session.power(enabled)

    def _detect(self, key: str) -> None:
        board = self.boards[key]
        board.session.detect(list(range(CHANNELS)))

    def _start_all(self, key: str) -> None:
        board = self.boards[key]
        started = 0
        for channel in board.channels:
            if channel.config and channel.actuator_id and channel.process_status is None:
                self._start(board, channel)
                started += 1
        if not started:
            QMessageBox.information(self, "Start all", "No configured, idle channels on this board. Double-click a tile to configure it.")

    def _stop_all(self, key: str) -> None:
        board = self.boards[key]
        if QMessageBox.question(self, "Stop all", f"Stop every running initialization on {board.label}?") == QMessageBox.Yes:
            board.session.stop_all()

    def _refresh_board(self, board: BoardModel) -> None:
        if board.closed and not board.connected:
            kind, text = ("failed", "Fault") if board.faulted else ("stopped", "Disconnected")
        elif not board.connected:
            kind, text = "detecting", "Connecting"
        elif board.busy:
            kind, text = "detecting", board.busy
        elif board.psu_on:
            kind, text = "ready", "Power on"
        else:
            kind, text = "idle", "Power off"
        detail = board.endpoint
        if board.info.get("firmware"):
            detail += f" · {board.info['firmware']}"
        if board.info.get("bluetooth_name"):
            detail += f" · {board.info['bluetooth_name']}"
        board.panel.set_status(text, kind, detail)
        board.panel.set_supply(board.supply_v if board.psu_on else None)
        board.panel.set_controls(
            connected=board.connected and not board.faulted,
            psu_on=board.psu_on,
            active=any(c.active for c in board.channels) or bool(board.busy),
            any_configured=any(c.config and c.actuator_id and c.process_status is None for c in board.channels),
            any_running=any(c.active for c in board.channels),
        )
        for channel in board.channels:
            board.panel.tiles[channel.channel].render(tile_view(channel, board))

    # --------------------------------------------------------------- selection
    def select(self, key: str, channel: int) -> None:
        if self.selected is not None and self.selected[0] in self.boards:
            old_board = self.boards[self.selected[0]]
            old_board.panel.tiles[self.selected[1]].set_selected(False)
        self.selected = (key, channel)
        board = self.boards[key]
        board.panel.tiles[channel].set_selected(True)
        self.detail.show_model(board.channels[channel], board)

    def _selected_models(self) -> tuple[BoardModel, ChannelModel] | None:
        if self.selected is None or self.selected[0] not in self.boards:
            return None
        board = self.boards[self.selected[0]]
        return board, board.channels[self.selected[1]]

    def _refresh_selected(self) -> None:
        selected = self._selected_models()
        if selected is None:
            return
        board, model = selected
        self.detail.refresh(model, board)
        self.detail.plots.refresh()

    # ------------------------------------------------------------ channel ops
    def _configure(self, key: str, channel: int) -> None:
        self.select(key, channel)
        self.configure_selected()

    def configure_selected(self, *, then_start: bool = False) -> bool:
        selected = self._selected_models()
        if selected is None:
            return False
        board, model = selected
        if model.active:
            return False
        config = model.config or self._last_config()
        dialog = SequenceDialog(
            board_label=board.label,
            channel=model.channel,
            actuator_id=model.actuator_id,
            notes=model.notes,
            config=config,
            presets=self.presets,
            can_start=board.connected and board.psu_on and not board.faulted,
            parent=self,
        )
        if not dialog.exec():
            return False
        model.actuator_id = dialog.actuator_id.text().strip()
        model.notes = dialog.notes.text().strip()
        model.config = dialog.result_config()
        if model.process_status in ("completed", "stopped", "failed"):
            model.process_status = None
            model.snapshot = None
            model.report_paths = {}
            model.reset_data()
        self.settings.setValue("last_config", json.dumps(model.config.to_dict()))
        self._refresh_board(board)
        self.detail.show_model(model, board)
        if dialog.start_requested or then_start:
            self._start(board, model)
        return True

    def _last_config(self) -> InitializationConfig:
        raw = self.settings.value("last_config")
        if raw:
            try:
                return InitializationConfig.from_dict(json.loads(str(raw)))
            except (ValueError, TypeError):
                pass
        return next(iter(self.presets.values()))

    def start_selected(self) -> None:
        selected = self._selected_models()
        if selected is None:
            return
        board, model = selected
        if model.active:
            return  # never restart a running channel (double-click safety)
        if not model.config or not model.actuator_id:
            self.configure_selected(then_start=True)
            return
        if model.process_status in ("completed", "stopped", "failed"):
            answer = QMessageBox.question(
                self, "Restart",
                f"Start a new initialization for {model.actuator_id}? A new report is created; the previous one is kept.",
            )
            if answer != QMessageBox.Yes:
                return
        self._start(board, model)

    def _start(self, board: BoardModel, model: ChannelModel) -> None:
        if model.config is None or model.active:
            return
        if model.detection == "Not connected":
            answer = QMessageBox.question(
                self, "Start", f"Channel {model.channel} was detected as not connected. Start anyway?",
            )
            if answer != QMessageBox.Yes:
                return
        model.reset_data()
        model.report_paths = {}
        model.previous_started_at = model.run_started_at
        model.run_started_at = None
        model.status_detail = ""
        model.plan_runs = build_plan(model.config).runs
        model.process_status = "checking"
        model.snapshot = None
        board.session.start_process(model.channel, model.actuator_id, model.config, model.notes, self.operator.text().strip())
        self._refresh_board(board)
        if self.selected == (board.key, model.channel):
            self.detail.show_model(model, board)

    def toggle_pause_selected(self) -> None:
        selected = self._selected_models()
        if selected is None:
            return
        board, model = selected
        if model.process_status == "paused":
            board.session.resume_process(model.channel)
        elif model.process_status == "running":
            board.session.pause_process(model.channel)

    def stop_selected(self) -> None:
        selected = self._selected_models()
        if selected is None:
            return
        board, model = selected
        answer = QMessageBox.question(
            self, "Stop", f"Stop the initialization of {model.actuator_id}? A partial report is written.",
        )
        if answer == QMessageBox.Yes:
            board.session.stop_process(model.channel)

    def open_report_selected(self, *, html: bool) -> None:
        selected = self._selected_models()
        if selected is None:
            return
        _board, model = selected
        folder = model.report_paths.get("folder")
        if not folder:
            return
        target = Path(folder) / model.report_paths.get("report_html", "report.html") if html else Path(folder)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ------------------------------------------------------------------ events
    def _drain_events(self) -> None:
        touched: set[str] = set()
        deadline = time.monotonic() + 0.05
        while time.monotonic() < deadline:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            board = self.boards.get(event.board)
            if board is None:
                continue
            touched.add(board.key)
            self._handle_event(board, event)
        for key in touched:
            if key in self.boards:
                self._refresh_board(self.boards[key])

    def _handle_event(self, board: BoardModel, event: Event) -> None:
        kind, data = event.kind, event.data
        channel = board.channels[event.channel] if event.channel is not None and event.channel < CHANNELS else None
        if kind == "board_state":
            board.connected = data["connected"]
            board.faulted = data["faulted"]
            board.psu_on = data["psu_on"]
            board.supply_v = data["supply_v"]
            board.info = data["info"]
        elif kind == "board_closed":
            board.closed = True
            board.connected = False
        elif kind == "board_busy":
            board.busy = data or ""
        elif kind == "telemetry":
            board.supply_v = data["supply_v"]
        elif kind == "board_error":
            self._log("error", f"{board.label}: {data}")
        elif kind == "log":
            prefix = board.label if event.channel is None else board.label
            self._log(data["level"], f"{prefix}: {data['text']}", data.get("time"))
        elif channel is None:
            return
        elif kind == "process":
            if data.get("started_at") and data.get("started_at") == channel.previous_started_at:
                return  # late snapshot from the previous run on this channel
            channel.snapshot = data
            channel.run_started_at = data.get("started_at")
            channel.process_status = data["status"]
            channel.status_detail = data.get("status_detail", "")
            channel.now_t = data["wall_t_s"]
        elif kind == "voltage":
            channel.add_voltage(data)
        elif kind == "measurement":
            channel.add_measurement(data)
        elif kind == "detection":
            channel.detection = data["state"]
            result = data.get("result")
            channel.detection_delta = result.delta_ma if result is not None else None
        elif kind == "check":
            channel.detection = data.state
            channel.detection_delta = data.delta_ma
        elif kind == "process_finished":
            channel.process_status = data["status"]
            channel.status_detail = data["detail"]
        elif kind == "report":
            self._log("ok", f"{board.label}: report for {data.get('actuator_id')} written to {data.get('folder')}")
            if data.get("started_at") == channel.run_started_at:
                channel.report_paths = data

    # --------------------------------------------------------------------- log
    def _log(self, level: str, text: str, stamp: str | None = None) -> None:
        color = LOG_COLORS.get(level, LOG_COLORS["info"])
        stamp = stamp or time.strftime("%H:%M:%S")
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log_view.append(f'<span style="color:#8b95a1">[{stamp}]</span> <span style="color:{color}">{safe}</span>')

    def _save_log(self) -> None:
        default = Path.home() / f"initialization-log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "Save event log", str(default), "Text (*.txt)")
        if path:
            Path(path).write_text(self.log_view.toPlainText() + "\n", encoding="utf-8")

    # ------------------------------------------------------------------- close
    def closeEvent(self, event: Any) -> None:
        running = [c for b in self.boards.values() for c in b.channels if c.active]
        if running:
            answer = QMessageBox.question(
                self, "Quit",
                f"{len(running)} initialization(s) are running. Quitting stops them, sets every output to 0 V and "
                "writes partial reports. Quit?",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.shutdown_all()
        for board in self.boards.values():
            board.session.join(timeout=20.0)
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="add two simulated Rockford boards")
    parser.add_argument("--demo-speed", type=float, default=20.0, help="time acceleration for simulated boards")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Actuator Initialization")
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(APP_STYLES)
    window = MainWindow(demo=args.demo, demo_speed=args.demo_speed)
    # Any exit path (not only closing the window) stops the board threads safely.
    app.aboutToQuit.connect(window.shutdown_all)
    atexit.register(window.shutdown_all)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
