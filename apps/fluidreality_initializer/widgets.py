"""Reusable widgets: pills, metric tiles, sparkline, actuator tiles, board panels."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .ui_style import ACCENT, GRID, LINE, MUTED, NEGATIVE, POSITIVE, STATUS

APPS_ROOT = Path(__file__).resolve().parent.parent
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from fluidreality_dashboard.toggle import LabeledToggle  # noqa: E402  (shared dashboard widget)

CHANNELS = 5


def refresh_style(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Pill(QLabel):
    def __init__(self, text: str = "", kind: str = "idle") -> None:
        super().__init__(text)
        self.setObjectName("Pill")
        self.setAlignment(Qt.AlignCenter)
        self.set(text, kind)

    def set(self, text: str, kind: str) -> None:
        fg, bg, border = STATUS.get(kind, STATUS["idle"])
        self.setText(text)
        self.setStyleSheet(
            f"QLabel#Pill {{ color: {fg}; background: {bg}; border: 1px solid {border}; "
            "border-radius: 7px; padding: 2px 7px; font-size: 11px; font-weight: 700; }"
        )


class MetricTile(QFrame):
    def __init__(self, title: str, value: str = "-", sub: str = "") -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(1)
        self.title = QLabel(title.upper())
        self.title.setObjectName("MetricTitle")
        self.value = QLabel(value)
        self.value.setObjectName("MetricValue")
        self.sub = QLabel(sub)
        self.sub.setObjectName("MetricSub")
        layout.addWidget(self.title)
        layout.addWidget(self.value)
        layout.addWidget(self.sub)

    def set(self, value: str, sub: str | None = None) -> None:
        self.value.setText(value)
        if sub is not None:
            self.sub.setText(sub)


class Sparkline(QWidget):
    """Tiny trend line of recent end-of-high current deltas."""

    def __init__(self) -> None:
        super().__init__()
        self.values: list[float] = []
        self.setMinimumHeight(26)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:
        return QSize(140, 26)

    def set_values(self, values: Sequence[float]) -> None:
        self.values = list(values)[-80:]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 4, -6, -4)
        painter.setPen(QPen(QColor(GRID), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if len(self.values) < 2:
            return
        low, high = min(self.values), max(self.values)
        if high - low < 1e-9:
            high, low = high + 0.5, low - 0.5
        step = rect.width() / (len(self.values) - 1)
        path = QPainterPath()
        for index, value in enumerate(self.values):
            point = QPointF(rect.left() + index * step, rect.bottom() - (value - low) / (high - low) * rect.height())
            if index == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(QColor(ACCENT), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(path)
        painter.setPen(QPen(QColor("#ffffff"), 1.5))
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(path.currentPosition(), 3.2, 3.2)


class PolarityBar(QWidget):
    """Centred bar showing the applied voltage: blue to the right, red to the left."""

    def __init__(self) -> None:
        super().__init__()
        self.value = 0.0
        self.full_scale = 250.0
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_value(self, volts: float, full_scale: float | None = None) -> None:
        self.value = volts
        if full_scale:
            self.full_scale = max(1.0, full_scale)
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0, 1, 0, -1)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#eef1f6"))
        painter.drawRoundedRect(rect, 3, 3)
        mid = rect.center().x()
        fraction = max(-1.0, min(1.0, self.value / self.full_scale))
        if abs(fraction) > 0.002:
            width = abs(fraction) * rect.width() / 2
            bar = QRectF(mid, rect.top(), width, rect.height()) if fraction > 0 else QRectF(mid - width, rect.top(), width, rect.height())
            painter.setBrush(QColor(POSITIVE if fraction > 0 else NEGATIVE))
            painter.drawRoundedRect(bar, 3, 3)
        painter.setPen(QPen(QColor(MUTED), 1))
        painter.drawLine(QPointF(mid, rect.top() - 1), QPointF(mid, rect.bottom() + 1))


class ActuatorTile(QFrame):
    clicked = Signal(int)
    double_clicked = Signal(int)

    def __init__(self, channel: int) -> None:
        super().__init__()
        self.channel = channel
        self.setObjectName("ActuatorTile")
        self.setProperty("state", "idle")
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 10)
        layout.setSpacing(4)
        top = QHBoxLayout()
        self.channel_label = QLabel(f"CH {channel}")
        self.channel_label.setObjectName("TileChannel")
        self.pill = Pill("Idle", "idle")
        top.addWidget(self.channel_label)
        top.addStretch()
        top.addWidget(self.pill)
        self.id_label = QLabel("Not configured")
        self.id_label.setObjectName("TileId")
        self.voltage_label = QLabel("0 V")
        self.voltage_label.setObjectName("TileVoltage")
        self.polarity = PolarityBar()
        self.detail = QLabel(" ")
        self.detail.setObjectName("TileDetail")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(30)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress_text = QLabel(" ")
        self.progress_text.setObjectName("TileMuted")
        self.spark = Sparkline()
        self.spark_caption = QLabel(" ")
        self.spark_caption.setObjectName("TileMuted")
        for widget in (self.id_label, self.voltage_label, self.polarity, self.detail, self.progress,
                       self.progress_text, self.spark, self.spark_caption):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addLayout(top)
        layout.addWidget(self.id_label)
        layout.addWidget(self.voltage_label)
        layout.addWidget(self.polarity)
        layout.addWidget(self.detail)
        layout.addWidget(self.progress)
        layout.addWidget(self.progress_text)
        layout.addWidget(self.spark)
        layout.addWidget(self.spark_caption)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.channel)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        self.double_clicked.emit(self.channel)
        super().mouseDoubleClickEvent(event)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        refresh_style(self)

    def render(self, view: dict[str, Any]) -> None:
        state = view.get("state", "idle")
        if self.property("state") != state:
            self.setProperty("state", state)
            refresh_style(self)
        self.pill.set(view.get("pill", "Idle"), view.get("pill_kind", state))
        self.id_label.setText(view.get("actuator_id") or "Not configured")
        self.voltage_label.setText(view.get("voltage_text", "0 V"))
        self.polarity.set_value(view.get("applied_v", 0.0), view.get("full_scale_v"))
        self.detail.setText(view.get("detail", " ") or " ")
        self.progress.setValue(round(1000 * view.get("progress", 0.0)))
        self.progress_text.setText(view.get("progress_text", " ") or " ")
        self.spark.set_values(view.get("spark", []))
        self.spark_caption.setText(view.get("spark_caption", " ") or " ")


class BoardPanel(QFrame):
    power_toggled = Signal(str, bool)
    detect_requested = Signal(str)
    start_all_requested = Signal(str)
    stop_all_requested = Signal(str)
    remove_requested = Signal(str)
    tile_clicked = Signal(str, int)
    tile_double_clicked = Signal(str, int)

    def __init__(self, key: str, label: str, endpoint: str) -> None:
        super().__init__()
        self.key = key
        self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(label)
        self.title.setObjectName("BoardTitle")
        self.detail = QLabel(f"{endpoint} · connecting…")
        self.detail.setObjectName("BoardDetail")
        titles.addWidget(self.title)
        titles.addWidget(self.detail)
        header.addLayout(titles, 1)
        self.status_pill = Pill("Connecting", "detecting")
        header.addWidget(self.status_pill)
        self.supply = QLabel("— V")
        self.supply.setObjectName("BoardTitle")
        self.supply.setToolTip("Measured high-voltage supply")
        header.addWidget(self.supply)
        self.power = LabeledToggle("Power")
        self.power.setEnabled(False)
        self.power.toggled.connect(lambda checked: self.power_toggled.emit(self.key, checked))
        header.addWidget(self.power)
        layout.addLayout(header)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.detect_button = QPushButton("Detect actuators")
        self.detect_button.setObjectName("SmallSecondary")
        self.detect_button.setToolTip("Run firmware detection (DT0/DT1) on channels 0-4")
        self.detect_button.clicked.connect(lambda: self.detect_requested.emit(self.key))
        self.start_all_button = QPushButton("Start all configured")
        self.start_all_button.setObjectName("SmallButton")
        self.start_all_button.setToolTip("Start every configured, idle actuator on this board")
        self.start_all_button.clicked.connect(lambda: self.start_all_requested.emit(self.key))
        self.stop_all_button = QPushButton("Stop all")
        self.stop_all_button.setObjectName("DangerButton")
        self.stop_all_button.setStyleSheet("padding: 4px 9px; font-size: 11px;")
        self.stop_all_button.clicked.connect(lambda: self.stop_all_requested.emit(self.key))
        self.remove_button = QPushButton("Disconnect")
        self.remove_button.setObjectName("SmallSecondary")
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.key))
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.start_all_button)
        buttons.addWidget(self.stop_all_button)
        buttons.addStretch()
        buttons.addWidget(self.remove_button)
        layout.addLayout(buttons)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles: list[ActuatorTile] = []
        for channel in range(CHANNELS):
            tile = ActuatorTile(channel)
            tile.clicked.connect(lambda ch, key=key: self.tile_clicked.emit(key, ch))
            tile.double_clicked.connect(lambda ch, key=key: self.tile_double_clicked.emit(key, ch))
            grid.addWidget(tile, 0, channel)
            self.tiles.append(tile)
        layout.addLayout(grid)
        self.set_controls(connected=False, psu_on=False, active=False, any_configured=False, any_running=False)

    def set_controls(self, *, connected: bool, psu_on: bool, active: bool, any_configured: bool, any_running: bool) -> None:
        self.power.setEnabled(connected)
        self.power.blockSignals(True)
        self.power.setChecked(psu_on)
        self.power.blockSignals(False)
        self.detect_button.setEnabled(connected and psu_on and not active)
        self.start_all_button.setEnabled(connected and psu_on and any_configured)
        self.stop_all_button.setEnabled(any_running)
        self.remove_button.setEnabled(not active)

    def set_status(self, text: str, kind: str, detail: str | None = None) -> None:
        self.status_pill.set(text, kind)
        if detail is not None:
            self.detail.setText(detail)

    def set_supply(self, volts: float | None) -> None:
        self.supply.setText("— V" if not volts else f"{volts:.1f} V")


class Divider(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Divider")


def legend_key(text: str, color: str, *, hollow: bool = False) -> QWidget:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(5)
    swatch = QLabel()
    swatch.setFixedSize(10, 10)
    if hollow:
        swatch.setStyleSheet(f"border: 1.5px solid {color}; border-radius: 5px; background: transparent;")
    else:
        swatch.setStyleSheet(f"background: {color}; border-radius: 5px;")
    label = QLabel(text)
    label.setObjectName("LegendText")
    row.addWidget(swatch)
    row.addWidget(label)
    return holder


__all__ = [
    "ActuatorTile", "BoardPanel", "CHANNELS", "Divider", "LINE", "MetricTile", "Pill",
    "PolarityBar", "Sparkline", "legend_key", "refresh_style",
]
