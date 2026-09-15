"""Visual configuration designer and launcher for the Rockford simulator."""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QSettings, Qt, Signal
from PySide6.QtGui import QFontDatabase, QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

APP_ROOT = Path(__file__).resolve().parent
APPS_ROOT = APP_ROOT.parent
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))
STANDARD_CONFIG_DIR = APP_ROOT / "standard_configs"
LOGO_PATH = APP_ROOT / "assets" / "fluid_reality_logo_transparent.png"
APP_ICON_PATH = APP_ROOT / "assets" / "fluid-reality-icon.png"
GROUP_COUNT = 1
ACTUATORS_PER_GROUP = 8

from rockford_simulator.toggle import LabeledToggle


@dataclass
class ActuatorConfig:
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))
    connected: bool = False
    name: str = ""
    max_starting_current_ma: float = 4.0
    offline_current_increase_ma_s: float = 0.05
    running_current_decrease_ma_v_s: float = 0.001
    min_running_current_ma: float = 0.5
    current_noise_ma: float = 0.02


@dataclass
class GroupConfig:
    enabled: bool = False
    actuators: list[ActuatorConfig] = field(
        default_factory=lambda: [ActuatorConfig() for _ in range(ACTUATORS_PER_GROUP)]
    )


@dataclass
class BoardConfig:
    name: str = ""
    psu_voltage_v: float = 210.0
    psu_voltage_noise_v: float = 0.25
    psu_base_current_ma: float = 1.0
    psu_base_current_noise_ma: float = 0.02
    groups: list[GroupConfig] = field(
        default_factory=lambda: [GroupConfig(enabled=i == 0) for i in range(GROUP_COUNT)]
    )


class ActuatorCard(QFrame):
    clicked = Signal(int)
    assignment_changed = Signal(int, str)

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index
        self.setObjectName("ActuatorCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(132)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 13)
        top = QHBoxLayout()
        self.number = QLabel(f"{index:02d}")
        self.number.setObjectName("ActuatorNumber")
        self.assignment = QComboBox()
        self.assignment.setMinimumWidth(145)
        self.assignment.currentTextChanged.connect(
            lambda value: self.assignment_changed.emit(self.index, value)
        )
        top.addWidget(self.number)
        top.addStretch()
        top.addWidget(self.assignment)
        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        layout.addLayout(top)
        layout.addStretch()
        layout.addWidget(self.detail)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def render(
        self,
        config: ActuatorConfig,
        enabled: bool,
        selected: bool,
        profile_names: list[str],
    ) -> None:
        self.setEnabled(enabled)
        self.setProperty("selected", selected)
        self.assignment.blockSignals(True)
        self.assignment.clear()
        self.assignment.addItem("Not connected")
        self.assignment.addItems(profile_names)
        self.assignment.setCurrentText(config.name if config.connected and config.name in profile_names else "Not connected")
        self.assignment.blockSignals(False)
        self.detail.setText(
            f"Start max {config.max_starting_current_ma:g} mA"
            if config.connected else ""
        )
        for widget in (self,):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class SimulatorLogWindow(QMainWindow):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Rockford Simulator Log")
        self.resize(900, 520)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        header = QHBoxLayout()
        header.addWidget(QLabel("Simulator log", objectName="SectionTitle"))
        header.addStretch()
        clear_btn = QPushButton("Clear")
        header.addWidget(clear_btn)
        self.log = QPlainTextEdit()
        self.log.setObjectName("SimulatorLog")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        clear_btn.clicked.connect(self.log.clear)
        layout.addLayout(header)
        layout.addWidget(self.log, 1)


class DesignerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality Board Simulator Designer")
        self.resize(1440, 900)
        self.config = BoardConfig()
        self.profiles: dict[str, ActuatorConfig] = {}
        self.standard_guids: set[str] = set()
        self.profile_key: str | None = None
        self.path: Path | None = None
        self.group_index = 0
        self.actuator_index = 0
        self.cards: list[ActuatorCard] = []
        self.group_buttons: list[QPushButton] = []
        self._loading = False
        self._dirty = False
        self.settings = QSettings("Fluid Reality", "Rockford Simulator Designer")
        self.log_window = SimulatorLogWindow(self)
        self.simulator_log = self.log_window.log
        self.simulator_process = QProcess(self)
        self.simulator_process.setProcessChannelMode(QProcess.MergedChannels)
        self.simulator_process.readyReadStandardOutput.connect(self._read_simulator_log)
        self.simulator_process.started.connect(self._simulator_started)
        self.simulator_process.finished.connect(self._simulator_finished)
        self.simulator_process.errorOccurred.connect(self._simulator_error)
        self._build_ui()
        self._load_standard_configs()
        recent = self.settings.value("recent_board_path", "", type=str)
        if recent and Path(recent).is_file():
            try:
                self._load_configuration_path(Path(recent))
            except (OSError, ValueError, TypeError):
                self.settings.remove("recent_board_path")
                self._load_controls()
        else:
            if recent:
                self.settings.remove("recent_board_path")
            self._load_controls()

    def _build_ui(self) -> None:
        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(15)
        outer.addLayout(self._header())
        outer.addWidget(self._toolbar())

        body = QHBoxLayout()
        body.setSpacing(14)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)
        left_layout.addWidget(self._board_panel())
        left_layout.addWidget(self._actuator_panel(), 1)
        body.addWidget(left, 7)
        body.addWidget(self._editor_panel(), 4)
        outer.addLayout(body, 1)
        self.setStyleSheet(STYLES)

    def _header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        logo = QLabel(objectName="Logo")
        if LOGO_PATH.exists():
            logo.setPixmap(QPixmap(str(LOGO_PATH)).scaledToWidth(225, Qt.SmoothTransformation))
        else:
            logo.setText("FLUID REALITY")
        titles = QVBoxLayout()
        title = QLabel("Board Simulator Designer", objectName="AppTitle")
        subtitle = QLabel("Build a software board configuration for hardware-free development.", objectName="AppSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        row.addWidget(logo)
        row.addSpacing(18)
        row.addLayout(titles)
        row.addStretch()
        phase = QLabel("DESIGN + SIMULATE")
        phase.setProperty("kind", "warn")
        row.addWidget(phase)
        return row

    def _toolbar(self) -> QFrame:
        bar = QFrame(objectName="TopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(15, 12, 15, 12)
        self.board_name = QLineEdit()
        self.board_name.setPlaceholderText("Configuration name")
        self.board_name.setMinimumWidth(240)
        self.board_name.textChanged.connect(self._update_model)
        self.board_profile = QComboBox()
        self.board_profile.addItem("Rockford (8 actuators)", "rockford")
        self.board_profile.setToolTip("Firmware protocol profile")
        self.board_profile.currentIndexChanged.connect(self._profile_type_changed)
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self._new)
        open_btn = QPushButton("Open configuration")
        open_btn.clicked.connect(self._open)
        save_btn = QPushButton("Save configuration")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save)
        self.run_simulator_btn = QPushButton("Run simulator")
        self.run_simulator_btn.setObjectName("RunButton")
        self.run_simulator_btn.clicked.connect(self._toggle_simulator)
        show_log_btn = QPushButton("Show simulator log")
        show_log_btn.clicked.connect(self._show_simulator_log)
        layout.addWidget(QLabel("Board"))
        layout.addWidget(self.board_name)
        layout.addWidget(self.board_profile)
        layout.addStretch()
        layout.addWidget(new_btn)
        layout.addWidget(open_btn)
        layout.addWidget(save_btn)
        layout.addWidget(show_log_btn)
        layout.addWidget(self.run_simulator_btn)
        return bar

    def _show_simulator_log(self) -> None:
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()

    def _spin(self, suffix: str, maximum: float, decimals: int = 3, profile: bool = False) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, maximum)
        spin.setDecimals(decimals)
        spin.setSuffix(f" {suffix}")
        spin.setSingleStep(0.01 if decimals else 1)
        spin.valueChanged.connect(self._profile_changed if profile else self._update_model)
        return spin

    def _field(self, layout: QGridLayout, row: int, title: str, note: str, widget: QWidget) -> None:
        label = QLabel(title, objectName="FieldLabel")
        hint = QLabel(note, objectName="Muted")
        layout.addWidget(label, row * 2, 0)
        layout.addWidget(widget, row * 2, 1)
        layout.addWidget(hint, row * 2 + 1, 0, 1, 2)

    def _board_panel(self) -> QFrame:
        panel = QFrame(objectName="Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(17, 15, 17, 15)
        header = QHBoxLayout()
        header.addWidget(QLabel("Power supply model", objectName="SectionTitle"))
        header.addStretch()
        header.addWidget(QLabel("Values used while the simulated PSU is on", objectName="Muted"))
        layout.addLayout(header)
        grid = QGridLayout()
        grid.setHorizontalSpacing(22)
        self.psu_voltage = self._spin("V", 1000)
        self.psu_voltage_noise = self._spin("V noise σ", 100)
        self.psu_current = self._spin("mA", 10000)
        self.psu_current_noise = self._spin("mA noise σ", 1000)
        fields = [
            ("Nominal voltage", "Steady PSU output", self.psu_voltage),
            ("Voltage noise", "Standard deviation", self.psu_voltage_noise),
            ("Base current use", "Board draw with no actuators", self.psu_current),
            ("Base current noise", "Standard deviation", self.psu_current_noise),
        ]
        for i, (title, hint, widget) in enumerate(fields):
            box = QVBoxLayout()
            box.addWidget(QLabel(title, objectName="FieldLabel"))
            box.addWidget(widget)
            box.addWidget(QLabel(hint, objectName="Muted"))
            grid.addLayout(box, 0, i)
        layout.addLayout(grid)
        return panel

    def _actuator_panel(self) -> QFrame:
        panel = QFrame(objectName="Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(17, 15, 17, 15)
        top = QHBoxLayout()
        top.addWidget(QLabel("Actuator groups", objectName="SectionTitle"))
        top.addStretch()
        self.group_enable = LabeledToggle("Enable group")
        self.group_enable.toggled.connect(self._toggle_group)
        top.addWidget(self.group_enable)
        layout.addLayout(top)
        tabs = QHBoxLayout()
        for group in range(GROUP_COUNT):
            btn = QPushButton(f"Group {group}")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, g=group: self._select_group(g))
            self.group_buttons.append(btn)
            tabs.addWidget(btn)
        tabs.addStretch()
        layout.addLayout(tabs)
        grid = QGridLayout()
        grid.setSpacing(10)
        for i in range(ACTUATORS_PER_GROUP):
            card = ActuatorCard(i)
            card.clicked.connect(self._select_actuator)
            card.assignment_changed.connect(self._assign_profile)
            self.cards.append(card)
            grid.addWidget(card, i // 4, i % 4)
        layout.addLayout(grid, 1)
        return panel

    def _editor_panel(self) -> QFrame:
        panel = QFrame(objectName="Panel")
        panel.setMinimumWidth(410)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 18, 20, 18)
        self.editor_kicker = QLabel("CONFIGURATION LIBRARY", objectName="Kicker")
        self.editor_title = QLabel("Actuator configurations", objectName="EditorTitle")
        layout.addWidget(self.editor_kicker)
        layout.addWidget(self.editor_title)
        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._select_profile)
        layout.addWidget(self.profile_combo)
        file_actions = QHBoxLayout()
        new_actuator_btn = QPushButton("Add")
        new_actuator_btn.clicked.connect(self._add_profile)
        load_actuator_btn = QPushButton("Load actuator")
        load_actuator_btn.clicked.connect(self._load_actuator)
        save_actuator_btn = QPushButton("Save actuator")
        save_actuator_btn.clicked.connect(self._save_actuator)
        remove_actuator_btn = QPushButton("Remove")
        remove_actuator_btn.clicked.connect(self._remove_profile)
        file_actions.addWidget(new_actuator_btn)
        file_actions.addWidget(load_actuator_btn)
        file_actions.addWidget(save_actuator_btn)
        file_actions.addWidget(remove_actuator_btn)
        layout.addLayout(file_actions)
        divider = QFrame(objectName="Divider")
        divider.setFrameShape(QFrame.HLine)
        layout.addWidget(divider)
        form = QGridLayout()
        self.actuator_name = QLineEdit()
        self.actuator_name.setPlaceholderText("Optional profile name")
        self.actuator_name.editingFinished.connect(self._profile_changed)
        self.start_current = self._spin("mA", 10000, profile=True)
        self.offline_increase = self._spin("mA/s", 1000, 4, profile=True)
        self.running_decrease = self._spin("mA/(V·s)", 1000, 5, profile=True)
        self.min_running = self._spin("mA", 10000, profile=True)
        self.current_noise = self._spin("mA noise σ", 1000, 4, profile=True)
        specs = [
            ("Profile name", "Used to identify the actuator file.", self.actuator_name),
            ("Maximum starting current", "Initial current ceiling when drive begins.", self.start_current),
            ("Offline current increase", "Current rise per second while offline.", self.offline_increase),
            ("Running current decrease", "Decrease per volt for each running second.", self.running_decrease),
            ("Minimum running current", "Lower bound while the actuator runs.", self.min_running),
            ("Current noise", "Standard deviation applied to current readings.", self.current_noise),
        ]
        for row, (title, hint, widget) in enumerate(specs):
            self._field(form, row, title, hint, widget)
        layout.addLayout(form)
        layout.addStretch()
        self.file_preview = QLabel(objectName="FilePreview")
        self.file_preview.setWordWrap(True)
        layout.addWidget(QLabel("CONFIGURATION IDENTITY", objectName="Kicker"))
        layout.addWidget(self.file_preview)
        return panel

    def _current(self) -> ActuatorConfig:
        if self.profile_key and self.profile_key in self.profiles:
            return self.profiles[self.profile_key]
        return ActuatorConfig()

    def _load_controls(self) -> None:
        self._loading = True
        self.board_name.setText(self.config.name)
        self.psu_voltage.setValue(self.config.psu_voltage_v)
        self.psu_voltage_noise.setValue(self.config.psu_voltage_noise_v)
        self.psu_current.setValue(self.config.psu_base_current_ma)
        self.psu_current_noise.setValue(self.config.psu_base_current_noise_ma)
        group = self.config.groups[self.group_index]
        self.group_enable.setChecked(group.enabled)
        for i, btn in enumerate(self.group_buttons):
            btn.setChecked(i == self.group_index)
            btn.setProperty("active", i == self.group_index)
            btn.style().unpolish(btn); btn.style().polish(btn)
        names = sorted(self.profiles, key=str.casefold)
        if self.profile_key not in self.profiles:
            self.profile_key = names[0] if names else None
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        if self.profile_key:
            self.profile_combo.setCurrentText(self.profile_key)
        self.profile_combo.blockSignals(False)
        current = self._current()
        self.actuator_name.setText(current.name)
        self.start_current.setValue(current.max_starting_current_ma)
        self.offline_increase.setValue(current.offline_current_increase_ma_s)
        self.running_decrease.setValue(current.running_current_decrease_ma_v_s)
        self.min_running.setValue(current.min_running_current_ma)
        self.current_noise.setValue(current.current_noise_ma)
        self.editor_kicker.setText("CONFIGURATION LIBRARY")
        self.editor_title.setText("Actuator configurations")
        self.file_preview.setText(
            f"GUID: {current.guid}\nSaved inside the Rockford board JSON"
            if self.profile_key else "No configuration selected"
        )
        for i, card in enumerate(self.cards):
            card.number.setText(f"{self.group_index * 8 + i:02d}")
            card.render(group.actuators[i], group.enabled, i == self.actuator_index, names)
        self._loading = False
        self._set_editor_enabled()

    def _set_editor_enabled(self) -> None:
        enabled = self.profile_key is not None
        for widget in (self.actuator_name, self.start_current, self.offline_increase,
                       self.running_decrease, self.min_running, self.current_noise):
            widget.setEnabled(enabled)

    def _update_model(self, *_: Any) -> None:
        if self._loading:
            return
        self._dirty = True
        self.config.name = self.board_name.text().strip()
        self.config.psu_voltage_v = self.psu_voltage.value()
        self.config.psu_voltage_noise_v = self.psu_voltage_noise.value()
        self.config.psu_base_current_ma = self.psu_current.value()
        self.config.psu_base_current_noise_ma = self.psu_current_noise.value()
        if not self.profile_key:
            return
        current = self._current()
        requested_name = self.actuator_name.text().strip() or self.profile_key
        renamed = requested_name != self.profile_key
        if renamed:
            if requested_name in self.profiles:
                return
            old_name = self.profile_key
            self.profiles[requested_name] = self.profiles.pop(old_name)
            self.profile_key = requested_name
            for group in self.config.groups:
                for actuator in group.actuators:
                    if actuator.connected and actuator.name == old_name:
                        actuator.name = requested_name
        current = self._current()
        current.connected = True
        current.name = self.profile_key
        current.max_starting_current_ma = self.start_current.value()
        current.offline_current_increase_ma_s = self.offline_increase.value()
        current.running_current_decrease_ma_v_s = self.running_decrease.value()
        current.min_running_current_ma = self.min_running.value()
        current.current_noise_ma = self.current_noise.value()
        for group in self.config.groups:
            for index, actuator in enumerate(group.actuators):
                if actuator.connected and actuator.name == self.profile_key:
                    group.actuators[index] = ActuatorConfig(**asdict(current))
        if renamed:
            self._load_controls()

    def _profile_changed(self, *_: Any) -> None:
        if self._loading or not self.profile_key:
            return
        before = asdict(self._current())
        self._update_model()
        current = self._current()
        if asdict(current) == before:
            return
        old_guid = current.guid
        current.guid = str(uuid.uuid4())
        self.standard_guids.discard(old_guid)
        for group in self.config.groups:
            for index, actuator in enumerate(group.actuators):
                if actuator.connected and actuator.name == self.profile_key:
                    group.actuators[index] = ActuatorConfig(**asdict(current))
        self._load_controls()

    def _select_group(self, group: int) -> None:
        self.group_index = group
        self.actuator_index = 0
        self._load_controls()

    def _select_actuator(self, index: int) -> None:
        if not self.config.groups[self.group_index].enabled:
            return
        self.actuator_index = index
        assigned = self.config.groups[self.group_index].actuators[index]
        if assigned.connected and assigned.name in self.profiles:
            self.profile_key = assigned.name
        self._load_controls()

    def _select_profile(self, name: str) -> None:
        if self._loading or not name:
            return
        self.profile_key = name
        self._load_controls()

    def _assign_profile(self, index: int, name: str) -> None:
        if self._loading:
            return
        if name == "Not connected" or name not in self.profiles:
            self.config.groups[self.group_index].actuators[index] = ActuatorConfig()
        else:
            profile = ActuatorConfig(**asdict(self.profiles[name]))
            profile.connected = True
            profile.name = name
            self.config.groups[self.group_index].actuators[index] = profile
            self.profile_key = name
        self.actuator_index = index
        self._dirty = True
        self._load_controls()

    def _toggle_group(self, enabled: bool) -> None:
        if self._loading:
            return
        self.config.groups[self.group_index].enabled = enabled
        self._dirty = True
        self._load_controls()

    def _new(self) -> None:
        self.config = BoardConfig()
        self.board_profile.setCurrentIndex(0)
        self._load_standard_configs()
        self.path = None
        self.group_index = self.actuator_index = 0
        self._load_controls()
        self._dirty = False

    def _profile_type_changed(self, _index: int) -> None:
        if not self._loading:
            self._dirty = True
        is_rockford = self.board_profile.currentData() == "rockford"
        for index, button in enumerate(self.group_buttons):
            button.setEnabled(not is_rockford or index == 0)
        if is_rockford and self.group_index != 0:
            self._select_group(0)

    def _load_standard_configs(self) -> None:
        self.profiles = {}
        self.standard_guids = set()
        STANDARD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(STANDARD_CONFIG_DIR.glob("*.json")):
            try:
                profile = self._read_actuator_file(path)
            except (OSError, ValueError, TypeError):
                continue
            name = profile.name.strip() or path.stem
            if name in self.profiles:
                continue
            profile.connected = True
            profile.name = name
            self.profiles[name] = profile
            self.standard_guids.add(profile.guid)
        self.profile_key = next(iter(self.profiles), None)

    @staticmethod
    def _actuator_from_dict(data: dict[str, Any]) -> ActuatorConfig:
        """Build an actuator while ignoring manifest-only metadata."""
        defaults = ActuatorConfig()
        return ActuatorConfig(
            guid=str(data.get("guid") or defaults.guid),
            # Connection is a board assignment, never actuator-profile data.
            connected=False,
            name=str(data.get("name", defaults.name)),
            max_starting_current_ma=float(data.get("max_starting_current_ma", defaults.max_starting_current_ma)),
            offline_current_increase_ma_s=float(data.get("offline_current_increase_ma_s", defaults.offline_current_increase_ma_s)),
            running_current_decrease_ma_v_s=float(data.get("running_current_decrease_ma_v_s", defaults.running_current_decrease_ma_v_s)),
            min_running_current_ma=float(data.get("min_running_current_ma", defaults.min_running_current_ma)),
            current_noise_ma=float(data.get("current_noise_ma", defaults.current_noise_ma)),
        )

    @classmethod
    def _from_dict(cls, data: dict[str, Any], manifest_dir: Path | None = None) -> BoardConfig:
        groups_data = data.get("groups", {})
        groups = []
        for group_index in range(GROUP_COUNT):
            if isinstance(groups_data, dict):
                g = groups_data.get(str(group_index))
                if g is None:
                    groups.append(GroupConfig(enabled=False))
                    continue
                enabled = bool(g.get("enabled", True))
                actuator_entries = g.get("actuators", {})
            else:
                if group_index >= len(groups_data):
                    groups.append(GroupConfig(enabled=False))
                    continue
                g = groups_data[group_index]
                enabled = bool(g.get("enabled", False))
                actuator_entries = g.get("actuators", [])

            actuators = [ActuatorConfig() for _ in range(ACTUATORS_PER_GROUP)]
            entries = actuator_entries.items() if isinstance(actuator_entries, dict) else enumerate(actuator_entries)
            for actuator_key, actuator_data in entries:
                actuator_index = int(actuator_key)
                if not 0 <= actuator_index < ACTUATORS_PER_GROUP:
                    continue
                if "configuration_guid" in actuator_data:
                    actuators[actuator_index] = ActuatorConfig(
                        guid=str(actuator_data["configuration_guid"]),
                        connected=True,
                    )
                    continue
                source = actuator_data
                relative_file = actuator_data.get("file")
                if manifest_dir is not None and relative_file:
                    actuator_path = (manifest_dir / str(relative_file)).resolve()
                    manifest_root = manifest_dir.resolve()
                    if manifest_root not in actuator_path.parents:
                        raise ValueError(f"Actuator file is outside the configuration folder: {relative_file}")
                    if not actuator_path.is_file():
                        raise ValueError(f"Actuator file does not exist: {relative_file}")
                    source = json.loads(actuator_path.read_text(encoding="utf-8"))
                    if not isinstance(source, dict):
                        raise ValueError(f"Actuator file must contain a JSON object: {relative_file}")
                profile = cls._actuator_from_dict(source)
                profile.connected = True
                actuators[actuator_index] = profile
            groups.append(GroupConfig(enabled, actuators))
        if not groups_data:
            groups[0].enabled = True
        return BoardConfig(
            name=data.get("name", ""),
            psu_voltage_v=float(data.get("psu_voltage_v", 210)),
            psu_voltage_noise_v=float(data.get("psu_voltage_noise_v", 0.25)),
            psu_base_current_ma=float(data.get("psu_base_current_ma", 1)),
            psu_base_current_noise_ma=float(data.get("psu_base_current_noise_ma", 0.02)),
            groups=groups[:GROUP_COUNT],
        )

    def _open(self) -> None:
        initial = self.path.parent if self.path else APP_ROOT / "sample_configs"
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Open Rockford configuration",
            str(initial),
            "Rockford configurations (*.json)",
        )
        if not filename:
            return
        try:
            self._load_configuration_path(Path(filename))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(self, "Could not open configuration", str(exc))

    def _load_configuration_path(self, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 3:
            raise ValueError("Unsupported Rockford simulator schema; expected schema_version 3")
        if data.get("kind") != "rockford-simulator-design":
            raise ValueError("Configuration kind must be 'rockford-simulator-design'")
        self.path = path.resolve()
        board_type = str(data.get("board_type", "rockford")).lower()
        if board_type != "rockford":
            raise ValueError("Rockford simulator configurations must use board_type 'rockford'")
        profile_index = self.board_profile.findData(board_type)
        self.board_profile.setCurrentIndex(max(0, profile_index))
        self.config = self._from_dict(data, self.path.parent)
        self._load_standard_configs()
        for profile_data in data.get("actuator_configurations", []):
            source = profile_data
            relative_file = profile_data.get("file")
            if relative_file:
                profile_path = (self.path.parent / str(relative_file)).resolve()
                if not profile_path.is_file():
                    raise ValueError(f"Actuator configuration file does not exist: {relative_file}")
                source = json.loads(profile_path.read_text(encoding="utf-8"))
            profile = self._actuator_from_dict(source)
            if profile.guid in self.standard_guids:
                continue
            name = profile.name.strip() or Path(str(relative_file or "Actuator configuration")).stem
            base_name = name
            counter = 2
            while name in self.profiles:
                name = f"{base_name} (board {counter})"
                counter += 1
            profile.connected = True
            profile.name = name
            self.profiles[name] = profile
        for group_index, group in enumerate(self.config.groups):
            for port, actuator in enumerate(group.actuators):
                if not actuator.connected:
                    continue
                resolved_name = next(
                    (name for name, profile in self.profiles.items() if profile.guid == actuator.guid),
                    None,
                )
                if resolved_name is None:
                    resolved_name = actuator.name.strip() or f"Actuator G{group_index} P{port}"
                    base_name = resolved_name
                    counter = 2
                    while resolved_name in self.profiles:
                        resolved_name = f"{base_name} (board {counter})"
                        counter += 1
                    actuator.name = resolved_name
                    self.profiles[resolved_name] = ActuatorConfig(**asdict(actuator))
                standard_or_saved = self.profiles[resolved_name]
                group.actuators[port] = ActuatorConfig(**asdict(standard_or_saved))
        self.profile_key = next(iter(self.profiles), None)
        self.group_index = self.actuator_index = 0
        self._load_controls()
        self._dirty = False
        self.settings.setValue("recent_board_path", str(self.path))

    @classmethod
    def _read_actuator_file(cls, path: Path) -> ActuatorConfig:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Actuator configuration must contain a JSON object.")
        return cls._actuator_from_dict(data)

    def _write_actuator_file(self, path: Path) -> None:
        self._update_model()
        payload = asdict(self._current())
        payload.pop("connected", None)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _actuator_dialog_folder(self) -> Path:
        return STANDARD_CONFIG_DIR

    def _load_actuator(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Load named actuator configuration",
            str(self._actuator_dialog_folder()),
            "Actuator configuration (*.json);;JSON (*.json)",
        )
        if not filename:
            return
        try:
            loaded = self._read_actuator_file(Path(filename))
            base_name = loaded.name.strip() or Path(filename).stem
            name = base_name
            counter = 2
            while name in self.profiles:
                name = f"{base_name} {counter}"
                counter += 1
            loaded.connected = True
            loaded.name = name
            self.profiles[name] = loaded
            self.profile_key = name
            self._dirty = True
            self._load_controls()
            self.file_preview.setText(f"GUID: {loaded.guid}\nLoaded from {Path(filename)}")
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.critical(self, "Could not load actuator", str(exc))

    def _save_actuator(self) -> None:
        if not self.profile_key:
            QMessageBox.information(self, "No actuator configuration", "Add or load a configuration first.")
            return
        default_name = f"{self.profile_key}.json"
        default_path = self._actuator_dialog_folder() / default_name
        filename, _ = QFileDialog.getSaveFileName(
            self,
            f"Save actuator configuration: {self.profile_key}",
            str(default_path),
            "Actuator configuration (*.json);;JSON (*.json)",
        )
        if not filename:
            return
        path = Path(filename)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            self._write_actuator_file(path)
            if path.parent.resolve() == STANDARD_CONFIG_DIR.resolve():
                self.standard_guids.add(self._current().guid)
            self.file_preview.setText(f"GUID: {self._current().guid}\nSaved to {path}")
        except OSError as exc:
            QMessageBox.critical(self, "Could not save actuator", str(exc))

    def _add_profile(self) -> None:
        base_name = "New actuator"
        name = base_name
        counter = 2
        while name in self.profiles:
            name = f"{base_name} {counter}"
            counter += 1
        profile = ActuatorConfig(connected=True, name=name)
        self.profiles[name] = profile
        self.profile_key = name
        self._dirty = True
        self._load_controls()
        self.actuator_name.setFocus()
        self.actuator_name.selectAll()

    def _remove_profile(self) -> None:
        if not self.profile_key:
            return
        removed = self.profile_key
        del self.profiles[removed]
        for group in self.config.groups:
            for index, actuator in enumerate(group.actuators):
                if actuator.connected and actuator.name == removed:
                    group.actuators[index] = ActuatorConfig()
        self.profile_key = next(iter(self.profiles), None)
        self._dirty = True
        self._load_controls()

    def _toggle_simulator(self) -> None:
        if self.simulator_process.state() != QProcess.NotRunning:
            self._append_simulator_log("Stopping simulator...\n")
            self.simulator_process.terminate()
            if not self.simulator_process.waitForFinished(1500):
                self.simulator_process.kill()
                self.simulator_process.waitForFinished(1000)
            return

        if self.path is None or self._dirty:
            response = QMessageBox.question(
                self,
                "Save before running",
                "The current simulation design must be saved before it can run.",
                QMessageBox.Save | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if response != QMessageBox.Save or not self._save():
                return

        assert self.path is not None
        self._show_simulator_log()
        self.simulator_log.clear()
        self._append_simulator_log(f"Starting simulator from {self.path}\n")
        self.simulator_process.setWorkingDirectory(str(APP_ROOT.parent.parent))
        self.simulator_process.start(
            sys.executable,
            [
                "-u", str(APP_ROOT / "simulator.py"), str(self.path),
                "--board", str(self.board_profile.currentData()),
                "--tcp", "127.0.0.1:49765",
            ],
        )

    def _read_simulator_log(self) -> None:
        data = bytes(self.simulator_process.readAllStandardOutput())
        if data:
            self._append_simulator_log(data.decode("utf-8", errors="replace"))

    def _append_simulator_log(self, text: str) -> None:
        self.simulator_log.moveCursor(QTextCursor.End)
        self.simulator_log.insertPlainText(text)
        self.simulator_log.ensureCursorVisible()

    def _simulator_started(self) -> None:
        self.run_simulator_btn.setText("Stop simulator")
        self.run_simulator_btn.setProperty("running", True)
        self.run_simulator_btn.style().unpolish(self.run_simulator_btn)
        self.run_simulator_btn.style().polish(self.run_simulator_btn)

    def _simulator_finished(self, exit_code: int, _exit_status: Any) -> None:
        self._read_simulator_log()
        self._append_simulator_log(f"\nSimulator stopped (exit code {exit_code}).\n")
        self.run_simulator_btn.setText("Run simulator")
        self.run_simulator_btn.setProperty("running", False)
        self.run_simulator_btn.style().unpolish(self.run_simulator_btn)
        self.run_simulator_btn.style().polish(self.run_simulator_btn)

    def _simulator_error(self, error: Any) -> None:
        self._append_simulator_log(f"Simulator process error: {error}\n")

    def closeEvent(self, event: Any) -> None:
        if self.simulator_process.state() != QProcess.NotRunning:
            self.simulator_process.terminate()
            if not self.simulator_process.waitForFinished(1500):
                self.simulator_process.kill()
                self.simulator_process.waitForFinished(1000)
        super().closeEvent(event)

    def _save(self) -> bool:
        self._update_model()
        initial = str(self.path if self.path else Path.cwd() / "rockford-board.json")
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Save Rockford board configuration",
            initial,
            "Rockford board (*.json);;JSON (*.json)",
        )
        if not filename:
            return False
        path = Path(filename)
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        try:
            previous_state: dict[str, Any] | None = None
            if path.is_file():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                    candidate = existing.get("simulation_state")
                    if isinstance(candidate, dict):
                        previous_state = candidate
                except (OSError, ValueError, TypeError):
                    pass
            payload = {
                "schema_version": 3,
                "kind": "rockford-simulator-design",
                "board_type": str(self.board_profile.currentData()),
                "name": self.config.name,
                "psu_voltage_v": self.config.psu_voltage_v,
                "psu_voltage_noise_v": self.config.psu_voltage_noise_v,
                "psu_base_current_ma": self.config.psu_base_current_ma,
                "psu_base_current_noise_ma": self.config.psu_base_current_noise_ma,
                "groups": {},
                "actuator_configurations": [],
            }
            if previous_state is not None:
                payload["simulation_state"] = previous_state
            used_guids = {
                actuator.guid
                for group in self.config.groups
                for actuator in group.actuators
                if actuator.connected
            }
            for name, profile in sorted(self.profiles.items()):
                if profile.guid not in used_guids:
                    continue
                profile_payload = asdict(profile)
                profile_payload.pop("connected", None)
                profile_payload["name"] = name
                payload["actuator_configurations"].append(profile_payload)
            for group_index, group in enumerate(self.config.groups):
                if not group.enabled:
                    continue
                populated = {
                    str(actuator_index): {"configuration_guid": actuator.guid}
                    for actuator_index, actuator in enumerate(group.actuators)
                    if actuator.connected
                }
                payload["groups"][str(group_index)] = {"actuators": populated}
            self.path = path
            self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            self.settings.setValue("recent_board_path", str(self.path.resolve()))
            self._dirty = False
            return True
        except OSError as exc:
            QMessageBox.critical(self, "Could not save configuration", str(exc))
            return False


STYLES = """
QWidget#Root { background: #f7f7fc; color: #1a1b1f; font-size: 13px; }
QLabel#Logo { background: transparent; }
QLabel#AppTitle { font-size: 27px; font-weight: 700; }
QLabel#AppSubtitle, QLabel#Muted { color: #637184; }
QLabel#SectionTitle { font-size: 17px; font-weight: 700; }
QLabel#EditorTitle { font-size: 25px; font-weight: 700; padding-bottom: 7px; }
QLabel#Kicker { color: #0050bd; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#FieldLabel, QLabel#CardName { font-weight: 700; }
QLabel#FilePreview { background: #f3f5f8; border: 1px solid #dce0e7; border-radius: 7px; padding: 10px; color: #4f5f70; }
QFrame#TopBar, QFrame#Panel { background: white; border: 1px solid #dedfe3; border-radius: 9px; }
QFrame#Divider { color: #e0e3e8; margin: 8px 0; }
QFrame#ActuatorCard { background: #fafafa; border: 1px solid #dedfe3; border-radius: 8px; }
QFrame#ActuatorCard:hover { border-color: #8eb6ed; background: #f5f9ff; }
QFrame#ActuatorCard[selected="true"] { border: 2px solid #0050bd; background: #f2f7ff; }
QLabel#ActuatorNumber { font-size: 19px; font-weight: 700; }
QLabel[kind="neutral"] { background: #f0f1f3; border: 1px solid #d9dce2; border-radius: 7px; padding: 4px 8px; }
QLabel[kind="active"] { color: #0d4d2c; background: #eaf8f1; border: 1px solid #69c695; border-radius: 7px; padding: 4px 8px; }
QLabel[kind="warn"] { color: #721012; background: #ffeff0; border: 1px solid #ff8c92; border-radius: 7px; padding: 7px 11px; font-weight: 700; }
QPushButton { background: #1a1b1f; color: white; border: 1px solid #1a1b1f; border-radius: 7px; padding: 8px 12px; font-weight: 700; }
QPushButton:hover { background: #ee2c24; border-color: #ee2c24; }
QPushButton[active="true"] { background: #0050bd; border-color: #0050bd; }
QPushButton#PrimaryButton { background: #0050bd; border-color: #0050bd; }
QPushButton#PrimaryButton:hover { background: #003f96; }
QPushButton#RunButton { background: #0d6b42; border-color: #0d6b42; }
QPushButton#RunButton:hover { background: #095333; border-color: #095333; }
QPushButton#RunButton[running="true"] { background: #b4232c; border-color: #b4232c; }
QPlainTextEdit#SimulatorLog { background: #17191d; color: #e8edf5; border: 1px solid #2c3038; border-radius: 8px; padding: 8px; font-family: Consolas, monospace; }
QLineEdit, QDoubleSpinBox, QSpinBox { background: white; border: 1px solid #c8cbd1; border-radius: 7px; padding: 7px 9px; min-height: 20px; }
QLineEdit:focus, QDoubleSpinBox:focus { border: 2px solid #0050bd; }
QWidget:disabled { color: #8c95a0; }
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    window = DesignerWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
