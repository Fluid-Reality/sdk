"""Live plots for the selected actuator (pyqtgraph)."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .reporting import voltage_color
from .sequence import reading_is_first, reading_labels
from .ui_style import GRID, INK_2, MUTED, NEGATIVE, POSITIVE
from .widgets import legend_key

pg.setConfigOptions(antialias=True, background="w", foreground=INK_2)

WINDOWS = (("All", None), ("Last 2 h", 7200.0), ("Last 30 min", 1800.0), ("Last 5 min", 300.0), ("Last 1 min", 60.0))


def format_clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class TimeAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):  # type: ignore[override]
        return [format_clock(value) for value in values]


def _style_plot(plot: pg.PlotItem, y_label: str) -> None:
    plot.showGrid(x=False, y=True, alpha=0.18)
    plot.setMenuEnabled(False)
    plot.hideButtons()
    font = QFont()
    font.setPointSize(8)
    for name in ("left", "bottom"):
        axis = plot.getAxis(name)
        axis.setPen(pg.mkPen(GRID))
        axis.setTextPen(pg.mkPen(MUTED))
        axis.setTickFont(font)
    plot.getAxis("left").setWidth(48)
    plot.setLabel("left", y_label, color=MUTED, size="8pt")


class LivePlots(QWidget):
    """Drive voltage, current deltas and per-run conditioning for one actuator."""

    def __init__(self) -> None:
        super().__init__()
        self._model: Any = None
        self._dirty = True
        self._follow = True
        self._window: float | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        title = QLabel("Live data")
        title.setObjectName("ChartTitle")
        controls.addWidget(title)
        controls.addStretch()
        self.include_low = QCheckBox("Show low/negative-phase readings")
        self.include_low.toggled.connect(self.mark_dirty)
        controls.addWidget(self.include_low)
        self.window_combo = QComboBox()
        for label, _ in WINDOWS:
            self.window_combo.addItem(label)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        controls.addWidget(self.window_combo)
        self.reset_button = QPushButton("Follow live")
        self.reset_button.setObjectName("SmallSecondary")
        self.reset_button.clicked.connect(self._resume_follow)
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)

        self.voltage_widget = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.voltage_plot = self.voltage_widget.getPlotItem()
        _style_plot(self.voltage_plot, "Drive (V)")
        self.voltage_widget.setMinimumHeight(150)
        self.pos_curve = pg.PlotDataItem(stepMode="center", fillLevel=0, brush=pg.mkBrush(QColor(POSITIVE).lighter(125)),
                                         pen=pg.mkPen(POSITIVE, width=1.2))
        self.neg_curve = pg.PlotDataItem(stepMode="center", fillLevel=0, brush=pg.mkBrush(QColor(NEGATIVE).lighter(118)),
                                         pen=pg.mkPen(NEGATIVE, width=1.2))
        self.voltage_plot.addItem(self.pos_curve)
        self.voltage_plot.addItem(self.neg_curve)
        self.now_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("#ee2c24", width=1.5))
        self.voltage_plot.addItem(self.now_line)
        self.voltage_plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen(MUTED, width=1)))
        self.voltage_plot.getViewBox().sigRangeChangedManually.connect(self._manual_range)

        self.delta_widget = pg.PlotWidget(axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.delta_plot = self.delta_widget.getPlotItem()
        _style_plot(self.delta_plot, "Current Δ (mA)")
        self.delta_widget.setMinimumHeight(170)
        self.delta_plot.setXLink(self.voltage_plot)
        self.end_scatter = pg.ScatterPlotItem(size=8, pen=pg.mkPen("w", width=1.2), hoverable=True,
                                              hoverSize=11, tip=self._tip)
        self.start_scatter = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(None), hoverable=True, hoverSize=10, tip=self._tip)
        self.low_scatter = pg.ScatterPlotItem(size=6, brush=pg.mkBrush(None), pen=pg.mkPen(NEGATIVE, width=1.2),
                                              hoverable=True, tip=self._tip)
        self.delta_plot.addItem(self.low_scatter)
        self.delta_plot.addItem(self.start_scatter)
        self.delta_plot.addItem(self.end_scatter)
        self.delta_plot.getViewBox().sigRangeChangedManually.connect(self._manual_range)

        self.legend_row = QHBoxLayout()
        self.legend_row.setSpacing(14)
        legend_host = QWidget()
        legend_host.setLayout(self.legend_row)

        self.runs_widget = pg.PlotWidget()
        self.runs_plot = self.runs_widget.getPlotItem()
        _style_plot(self.runs_plot, "Δ (mA)")
        self.runs_plot.setLabel("bottom", "Run", color=MUTED, size="8pt")
        self.runs_widget.setMinimumHeight(140)
        self.run_bars = pg.BarGraphItem(x=[], height=[], width=0.72)
        self.runs_plot.addItem(self.run_bars)

        for heading, widget in (("Drive voltage", self.voltage_widget),
                                ("Current delta readings", self.delta_widget)):
            label = QLabel(heading)
            label.setObjectName("ChartTitle")
            layout.addWidget(label)
            if widget is self.delta_widget:
                self.delta_title = label
                layout.addWidget(legend_host)
            layout.addWidget(widget, 3)
        runs_label = QLabel("Conditioning by run")
        runs_label.setObjectName("ChartTitle")
        layout.addWidget(runs_label)
        layout.addWidget(self.runs_widget, 2)

    # ------------------------------------------------------------ interaction
    def _tip(self, x: float, y: float, data: Any) -> str:
        if isinstance(data, dict):
            return data.get("tip", "")
        return f"{format_clock(x)}  Δ {y:.3f} mA"

    def _window_changed(self, index: int) -> None:
        self._window = WINDOWS[index][1]
        self._follow = True
        self.mark_dirty()

    def _manual_range(self, *_args: Any) -> None:
        self._follow = False

    def _resume_follow(self) -> None:
        self._follow = True
        self.mark_dirty()

    def mark_dirty(self, *_args: Any) -> None:
        self._dirty = True

    # ------------------------------------------------------------------ data
    def set_model(self, model: Any) -> None:
        self._model = model
        self._follow = True
        self._rebuild_legend()
        self._dirty = True
        self.refresh()

    def _rebuild_legend(self) -> None:
        while self.legend_row.count():
            item = self.legend_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        model = self._model
        voltages: Sequence[float] = model.config.voltages_v if model is not None and model.config else ()
        for voltage in voltages:
            self.legend_row.addWidget(legend_key(f"{voltage:g} V", voltage_color(voltage, voltages)))
        if voltages:
            title, filled, hollow = reading_labels(model.config)
            self.delta_title.setText(title)
            self.legend_row.addWidget(legend_key(filled, INK_2))
            self.legend_row.addWidget(legend_key(hollow, INK_2, hollow=True))
        self.legend_row.addStretch()

    def refresh(self, force: bool = False) -> None:
        model = self._model
        if model is None:
            for item in (self.pos_curve, self.neg_curve):
                item.setData([], [])
            for scatter in (self.end_scatter, self.start_scatter, self.low_scatter):
                scatter.setData([])
            self.run_bars.setOpts(x=[], height=[])
            return
        now = max(1.0, model.now_t)
        if model.data_version != getattr(self, "_drawn_version", None) or force or self._dirty:
            self._drawn_version = model.data_version
            self._draw_voltage(model, now)
            self._draw_deltas(model)
            self._draw_runs(model)
        else:
            self._extend_voltage(model, now)
        self.now_line.setPos(model.now_t)
        if self._follow:
            if self._window is None:
                self.voltage_plot.setXRange(0, now * 1.02 + 1, padding=0)
            else:
                self.voltage_plot.setXRange(max(0.0, now - self._window), now + self._window * 0.03, padding=0)
            self._autoscale_delta(model)
        self._dirty = False

    def _voltage_arrays(self, model: Any, now: float) -> tuple[np.ndarray, np.ndarray]:
        steps = model.voltage_steps
        if not steps:
            return np.array([0.0, now]), np.array([0.0])
        x = np.fromiter((t for t, _ in steps), float, len(steps))
        y = np.fromiter((v for _, v in steps), float, len(steps))
        end = max(now, float(x[-1]))
        return np.append(x, end), y

    def _draw_voltage(self, model: Any, now: float) -> None:
        x, y = self._voltage_arrays(model, now)
        self.pos_curve.setData(x, np.clip(y, 0, None))
        self.neg_curve.setData(x, np.clip(y, None, 0))
        limit = max(50.0, max((abs(v) for v in model.config.voltages_v), default=50.0) * 1.12) if model.config else 250.0
        self.voltage_plot.setYRange(-limit, limit, padding=0)

    def _extend_voltage(self, model: Any, now: float) -> None:
        x, y = self._voltage_arrays(model, now)
        self.pos_curve.setData(x, np.clip(y, 0, None))
        self.neg_curve.setData(x, np.clip(y, None, 0))

    def _draw_deltas(self, model: Any) -> None:
        voltages = model.config.voltages_v if model.config else ()
        ends, starts, lows = [], [], []
        for m in model.measurements:
            tip = (f"{format_clock(m.t_s)} · run {m.run_id} · {m.voltage_v:g} V · high {m.high_time_s:g} s\n"
                   f"{m.edge} of {m.phase_kind} {'' if m.cycle is None else f'(cycle {m.cycle})'} @ {m.applied_v:+.0f} V\n"
                   f"Δ {m.delta_ma:.3f} mA  (I {m.current_ma:.3f}, base {m.baseline_ma:.3f})")
            spot = {"pos": (m.t_s, m.delta_ma), "data": {"tip": tip}}
            if m.target_v <= 0:
                lows.append(spot)
                continue
            color = voltage_color(m.voltage_v, voltages)
            if not reading_is_first(model.config, m):
                spot["brush"] = pg.mkBrush(color)
                ends.append(spot)
            else:
                spot["pen"] = pg.mkPen(color, width=1.5)
                starts.append(spot)
        self.end_scatter.setData(ends)
        self.start_scatter.setData(starts)
        self.low_scatter.setData(lows if self.include_low.isChecked() else [])

    def _autoscale_delta(self, model: Any) -> None:
        values = [m.delta_ma for m in model.measurements
                  if self.include_low.isChecked() or m.target_v > 0]
        if not values:
            self.delta_plot.setYRange(0, 1, padding=0)
            return
        low = min(0.0, min(values))
        high = max(values)
        span = max(0.2, high - low)
        self.delta_plot.setYRange(low - 0.05 * span, high + 0.12 * span, padding=0)

    def _draw_runs(self, model: Any) -> None:
        runs = model.run_means()
        if not runs:
            self.run_bars.setOpts(x=[], height=[])
            return
        voltages = model.config.voltages_v
        xs = [run_id for run_id, _, _ in runs]
        heights = [value for _, value, _ in runs]
        brushes = [pg.mkBrush(voltage_color(voltage, voltages)) for _, _, voltage in runs]
        self.run_bars.setOpts(x=xs, height=heights, width=0.72, brushes=brushes, pens=[pg.mkPen(None)] * len(xs))
        total = max(len(model.plan_runs), max(xs))
        self.runs_plot.setXRange(0.4, total + 0.6, padding=0)
        self.runs_plot.setYRange(0, max(0.2, max(heights) * 1.15), padding=0)
