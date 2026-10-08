"""Per-actuator initialization reports.

Each actuator run gets its own folder containing:

* ``report.json``     - machine-readable report (stable schema, Fibery-ready
                         ``summary`` block of flat scalar fields)
* ``measurements.csv`` - every start/end current reading (appended live)
* ``phases.csv``       - every completed phase (appended live)
* ``report.html``      - self-contained visual report (inline SVG charts)
* ``config.json``      - the exact initialization configuration used
"""

from __future__ import annotations

import csv
import html
import json
import math
import platform
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Sequence

from .sequence import PHASE_HIGH, PHASE_LOW, format_duration

SCHEMA = "fluid-reality/actuator-initialization-report"
SCHEMA_VERSION = 1
APP_NAME = "fluidreality_initializer"
APP_VERSION = "0.1.0"

MEASUREMENT_FIELDS = (
    "t_s", "timestamp", "run_id", "phase_index", "phase_kind", "cycle", "edge",
    "voltage_v", "high_time_s", "repeat", "target_v", "applied_v", "current_ma",
    "baseline_ma", "delta_ma", "window_ms", "supply_v",
)
PHASE_FIELDS = (
    "index", "run_id", "kind", "cycle", "voltage_v", "high_time_s", "repeat",
    "target_v", "applied_v", "started_t_s", "ended_t_s", "drive_s", "interrupted_s",
    "start_current_ma", "start_delta_ma", "end_current_ma", "end_delta_ma", "completed",
)

# Ordinal blue ramp (light->dark) for drive-voltage levels; diverging pair for polarity.
VOLTAGE_RAMP = ("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b")
POSITIVE = "#2a78d6"
NEGATIVE = "#e34948"


def _safe(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip("-")
    return cleaned or "actuator"


def voltage_color(voltage: float, voltages: Sequence[float]) -> str:
    levels = sorted(set(voltages))
    if not levels:
        return VOLTAGE_RAMP[2]
    if len(levels) == 1:
        return VOLTAGE_RAMP[3]
    index = levels.index(voltage) if voltage in levels else 0
    position = index / (len(levels) - 1)
    return VOLTAGE_RAMP[min(len(VOLTAGE_RAMP) - 1, round(position * (len(VOLTAGE_RAMP) - 1)))]


class ReportWriter:
    """Creates the report folder and keeps its files current during a run."""

    def __init__(self, output_dir: Path, process: Any, board_info: dict[str, Any]) -> None:
        stamp = datetime.now()
        board = _safe(str(board_info.get("label", "board")))
        name = f"{_safe(process.actuator_id)}_{board}_ch{process.channel}_{stamp:%H%M%S}"
        self.folder = Path(output_dir) / f"{stamp:%Y-%m-%d}" / name
        self.folder.mkdir(parents=True, exist_ok=True)
        self.report_id = str(uuid.uuid4())
        self.measurements_path = self.folder / "measurements.csv"
        self.phases_path = self.folder / "phases.csv"
        self.json_path = self.folder / "report.json"
        self.html_path = self.folder / "report.html"
        (self.folder / "config.json").write_text(
            json.dumps(process.config.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        for path, fields in ((self.measurements_path, MEASUREMENT_FIELDS), (self.phases_path, PHASE_FIELDS)):
            with path.open("w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(fields)
        self.write_progress(process, board_info)

    def append_measurement(self, measurement: Any) -> None:
        row = asdict(measurement)
        with self.measurements_path.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([_cell(row[field]) for field in MEASUREMENT_FIELDS])

    def append_phase(self, record: Any) -> None:
        row = asdict(record)
        with self.phases_path.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([_cell(row[field]) for field in PHASE_FIELDS])

    def write_progress(self, process: Any, board_info: dict[str, Any]) -> None:
        self.write_progress_text(self.render_progress(process, board_info))

    def render_progress(self, process: Any, board_info: dict[str, Any]) -> str:
        """Build the in-progress report.json text (call on the control thread)."""

        report = build_report(process, board_info, report_id=self.report_id, files=self.files())
        return json.dumps(report, indent=2) + "\n"

    def write_progress_text(self, text: str) -> None:
        _atomic_write(self.json_path, text)

    def finalize(self, process: Any, board_info: dict[str, Any]) -> dict[str, str]:
        report = build_report(process, board_info, report_id=self.report_id, files=self.files())
        _atomic_write(self.json_path, json.dumps(report, indent=2) + "\n")
        _atomic_write(self.html_path, render_html(report, process))
        return {key: str(value) for key, value in self.files().items()} | {"folder": str(self.folder)}

    def files(self) -> dict[str, str]:
        return {
            "report_json": self.json_path.name,
            "report_html": self.html_path.name,
            "measurements_csv": self.measurements_path.name,
            "phases_csv": self.phases_path.name,
            "config_json": "config.json",
        }


def _cell(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.6g}"
    return "" if value is None else value


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# ---------------------------------------------------------------- analysis
def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def summarize_runs(process: Any) -> list[dict[str, Any]]:
    by_run: dict[int, list[Any]] = {}
    for measurement in process.measurements:
        by_run.setdefault(measurement.run_id, []).append(measurement)
    completed_phases = {r.index for r in process.phase_records if r.completed}
    runs = []
    for run in process.plan.runs:
        items = by_run.get(run.run_id, [])
        high_end = [m.delta_ma for m in items if m.phase_kind == PHASE_HIGH and m.edge == "end"]
        high_start = [m.delta_ma for m in items if m.phase_kind == PHASE_HIGH and m.edge == "start"]
        low_end = [m.delta_ma for m in items if m.phase_kind == PHASE_LOW and m.edge == "end"]
        run_phases = [p.index for p in process.plan.phases if p.run.run_id == run.run_id]
        runs.append({
            "run_id": run.run_id,
            "voltage_v": run.voltage_v,
            "high_time_s": run.high_time_s,
            "repeat": run.repeat,
            "low_v": run.low_v,
            "pre_hold_v": run.pre_hold_v,
            "completed": bool(run_phases) and all(index in completed_phases for index in run_phases),
            "high_end_delta_first_ma": _round(high_end[0]) if high_end else None,
            "high_end_delta_last_ma": _round(high_end[-1]) if high_end else None,
            "high_end_delta_mean_ma": _round(mean(high_end)) if high_end else None,
            "high_start_delta_mean_ma": _round(mean(high_start)) if high_start else None,
            "low_end_delta_mean_ma": _round(mean(low_end)) if low_end else None,
            "measurements": len(items),
        })
    return runs


def summarize_by_voltage(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for voltage in sorted({run["voltage_v"] for run in runs}):
        values = [r["high_end_delta_mean_ma"] for r in runs if r["voltage_v"] == voltage and r["high_end_delta_mean_ma"] is not None]
        first = values[0] if values else None
        last = values[-1] if values else None
        change = None
        if first not in (None, 0) and last is not None:
            change = (last - first) / abs(first) * 100.0
        result.append({
            "voltage_v": voltage,
            "runs_measured": len(values),
            "first_run_high_end_delta_ma": _round(first),
            "last_run_high_end_delta_ma": _round(last),
            "mean_high_end_delta_ma": _round(mean(values)) if values else None,
            "change_pct": _round(change, 1),
        })
    return result


def build_report(
    process: Any,
    board_info: dict[str, Any],
    *,
    report_id: str | None = None,
    files: dict[str, str] | None = None,
) -> dict[str, Any]:
    runs = summarize_runs(process)
    by_voltage = summarize_by_voltage(runs)
    checks = {c.stage: asdict(c) for c in process.checks}
    pre, post = checks.get("pre"), checks.get("post")
    max_voltage = max(process.config.voltages_v) if process.config.voltages_v else None
    max_v_row = next((row for row in by_voltage if row["voltage_v"] == max_voltage), None)
    final_delta = process.last_high_end_delta()
    threshold = process.config.pass_max_delta_ma
    verdict = None
    if process.status == "completed" and threshold is not None and final_delta is not None:
        verdict = "pass" if abs(final_delta) <= threshold else "fail"
    started = process.started_at
    finished = process.finished_at
    if process.ended_clock is not None and process.started_clock is not None:
        wall = process.ended_clock - process.started_clock
    else:  # still running: drive time plus time frozen for neighbours
        wall = process.drive_elapsed_s + process.interrupted_s
    runs_done = sum(1 for run in runs if run["completed"])
    summary = {
        "actuator_id": process.actuator_id,
        "status": process.status,
        "verdict": verdict,
        "sequence_name": process.config.name,
        "board_label": board_info.get("label"),
        "board_id": board_info.get("bluetooth_name") or board_info.get("endpoint"),
        "channel": process.channel,
        "operator": process.operator or None,
        "started_at": started,
        "finished_at": finished,
        "duration_h": _round(wall / 3600.0, 3) if wall else None,
        "runs_completed": runs_done,
        "runs_planned": len(runs),
        "pre_check_state": pre["state"] if pre else None,
        "pre_check_delta_ma": _round(pre["delta_ma"]) if pre else None,
        "post_check_state": post["state"] if post else None,
        "post_check_delta_ma": _round(post["delta_ma"]) if post else None,
        "max_voltage_v": max_voltage,
        "max_voltage_first_delta_ma": max_v_row["first_run_high_end_delta_ma"] if max_v_row else None,
        "max_voltage_last_delta_ma": max_v_row["last_run_high_end_delta_ma"] if max_v_row else None,
        "max_voltage_change_pct": max_v_row["change_pct"] if max_v_row else None,
        "final_high_end_delta_ma": _round(final_delta),
        "forward_exposure_vs": _round(process.vt_forward_vs, 1),
        "reverse_exposure_vs": _round(process.vt_reverse_vs, 1),
        "measurement_count": len(process.measurements),
        "notes": process.notes or None,
    }
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id or str(uuid.uuid4()),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summary,
        "status": process.status,
        "status_detail": process.status_detail,
        "actuator": {"id": process.actuator_id, "channel": process.channel, "notes": process.notes},
        "board": {
            "label": board_info.get("label"),
            "endpoint": board_info.get("endpoint"),
            "firmware": board_info.get("firmware"),
            "bluetooth_name": board_info.get("bluetooth_name"),
            "vt_limit_vs": board_info.get("vt_limit_vs"),
        },
        "operator": process.operator,
        "station": platform.node(),
        "software": {"app": APP_NAME, "version": APP_VERSION, "sdk_version": _sdk_version()},
        "timing": {
            "started_at": started,
            "finished_at": finished,
            "wall_duration_s": _round(wall, 1),
            "drive_time_s": _round(process.drive_elapsed_s, 1),
            "interrupted_s": _round(process.interrupted_s, 1),
            "interruptions": process.interruptions,
        },
        "config": process.config.to_dict(),
        "plan": {
            "runs": len(process.plan.runs),
            "phases": len(process.plan.phases),
            "phases_completed": sum(1 for r in process.phase_records if r.completed),
            "drive_duration_s": _round(process.plan.drive_duration_s, 1),
        },
        "checks": {"pre": pre, "post": post},
        "exposure": {
            "forward_vs": _round(process.vt_forward_vs, 1),
            "reverse_vs": _round(process.vt_reverse_vs, 1),
            "net_vs": _round(process.vt_forward_vs - process.vt_reverse_vs, 1),
        },
        "by_voltage": by_voltage,
        "runs": runs,
        "files": files or {},
        "log": [{"t_s": _round(t, 2), "level": level, "text": text} for t, level, text in process.log],
    }


def _sdk_version() -> str | None:
    try:
        import fluid_reality

        return getattr(fluid_reality, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------- HTML
def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if high <= low:
        high = low + 1
    span = high - low
    raw = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = min((m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw), default=raw)
    start = math.floor(low / step) * step
    ticks = []
    value = start
    while value <= high + step * 0.001:
        if value >= low - step * 0.001:
            ticks.append(round(value, 10))
        value += step
    return ticks


def _fmt_time(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


class _Svg:
    """Tiny SVG chart frame with linear axes."""

    def __init__(self, width: int, height: int, x_range: tuple[float, float], y_range: tuple[float, float],
                 *, left: int = 56, right: int = 18, top: int = 14, bottom: int = 34) -> None:
        self.w, self.h = width, height
        self.left, self.right, self.top, self.bottom = left, right, top, bottom
        self.x0, self.x1 = x_range
        self.y0, self.y1 = y_range
        if self.x1 <= self.x0:
            self.x1 = self.x0 + 1
        if self.y1 <= self.y0:
            self.y1 = self.y0 + 1
        self.parts: list[str] = []

    def sx(self, x: float) -> float:
        return self.left + (x - self.x0) / (self.x1 - self.x0) * (self.w - self.left - self.right)

    def sy(self, y: float) -> float:
        return self.top + (1 - (y - self.y0) / (self.y1 - self.y0)) * (self.h - self.top - self.bottom)

    def axes(self, *, x_ticks: Iterable[float], y_ticks: Iterable[float], x_fmt, y_fmt, y_label: str) -> None:
        for y in y_ticks:
            py = self.sy(y)
            self.parts.append(f'<line class="grid" x1="{self.left}" x2="{self.w - self.right}" y1="{py:.1f}" y2="{py:.1f}"/>')
            self.parts.append(f'<text class="tick" x="{self.left - 8}" y="{py + 4:.1f}" text-anchor="end">{html.escape(y_fmt(y))}</text>')
        for x in x_ticks:
            px = self.sx(x)
            self.parts.append(f'<text class="tick" x="{px:.1f}" y="{self.h - 12}" text-anchor="middle">{html.escape(x_fmt(x))}</text>')
        self.parts.append(f'<line class="axis" x1="{self.left}" x2="{self.w - self.right}" y1="{self.h - self.bottom}" y2="{self.h - self.bottom}"/>')
        self.parts.append(f'<text class="axis-label" x="12" y="{self.top + 4}" >{html.escape(y_label)}</text>')

    def render(self, label: str) -> str:
        body = "".join(self.parts)
        return (f'<svg viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{html.escape(label)}" '
                f'preserveAspectRatio="xMidYMid meet">{body}</svg>')


def _downsample_steps(points: list[tuple[float, float]], limit: int = 6000) -> list[tuple[float, float]]:
    if len(points) <= limit:
        return points
    stride = math.ceil(len(points) / limit)
    reduced = points[::stride]
    if reduced[-1] != points[-1]:
        reduced.append(points[-1])
    return reduced


def _voltage_chart(process: Any, end_t: float) -> str:
    steps = _downsample_steps(list(process.voltage_steps))
    if not steps:
        return '<p class="empty">No drive data.</p>'
    vmax = max(10.0, max(abs(v) for _, v in steps))
    limit = math.ceil(vmax / 50.0) * 50.0
    svg = _Svg(960, 220, (0, max(end_t, steps[-1][0], 1)), (-limit, limit))
    svg.axes(x_ticks=_nice_ticks(0, svg.x1, 8), y_ticks=_nice_ticks(-limit, limit, 4),
             x_fmt=_fmt_time, y_fmt=lambda v: f"{v:+.0f}" if v else "0", y_label="Applied voltage (V)")
    zero = svg.sy(0)
    pos_path, neg_path = [], []
    points = steps + [(max(end_t, steps[-1][0]), steps[-1][1])]
    for (t, v), (t_next, _) in zip(points, points[1:]):
        x0, x1 = svg.sx(t), svg.sx(t_next)
        if v > 0:
            pos_path.append(f"M{x0:.1f},{zero:.1f}V{svg.sy(v):.1f}H{x1:.1f}V{zero:.1f}")
        elif v < 0:
            neg_path.append(f"M{x0:.1f},{zero:.1f}V{svg.sy(v):.1f}H{x1:.1f}V{zero:.1f}")
    svg.parts.append(f'<path d="{"".join(pos_path)}" fill="{POSITIVE}" fill-opacity="0.55" stroke="none"/>')
    svg.parts.append(f'<path d="{"".join(neg_path)}" fill="{NEGATIVE}" fill-opacity="0.55" stroke="none"/>')
    svg.parts.append(f'<line class="zero" x1="{svg.left}" x2="{svg.w - svg.right}" y1="{zero:.1f}" y2="{zero:.1f}"/>')
    return svg.render("Applied drive voltage over time")


def _delta_chart(process: Any, end_t: float) -> str:
    items = [m for m in process.measurements if m.phase_kind == PHASE_HIGH or m.target_v > 0]
    if not items:
        return '<p class="empty">No positive-phase current readings yet.</p>'
    voltages = process.config.voltages_v
    values = [m.delta_ma for m in items]
    top = max(0.5, max(values) * 1.12)
    bottom = min(0.0, min(values) * 1.12)
    svg = _Svg(960, 260, (0, max(end_t, items[-1].t_s, 1)), (bottom, top))
    svg.axes(x_ticks=_nice_ticks(0, svg.x1, 8), y_ticks=_nice_ticks(bottom, top, 5),
             x_fmt=_fmt_time, y_fmt=lambda v: f"{v:.2g}", y_label="Current delta, positive phases (mA)")
    stride = max(1, math.ceil(len(items) / 4000))
    for m in items[::stride]:
        color = voltage_color(m.voltage_v, voltages)
        x, y = svg.sx(m.t_s), svg.sy(m.delta_ma)
        tip = html.escape(f"Run {m.run_id} · {m.voltage_v:g} V · high {m.high_time_s:g} s · {m.edge} of {m.phase_kind}"
                          f" {m.cycle or ''} · Δ {m.delta_ma:.3f} mA at {_fmt_time(m.t_s)}")
        if m.edge == "end":
            svg.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}" class="dot"><title>{tip}</title></circle>')
        else:
            svg.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="none" stroke="{color}" stroke-width="1.5" class="dot"><title>{tip}</title></circle>')
    return svg.render("Current delta at phase start and end")


def _run_chart(report: dict[str, Any], voltages: Sequence[float]) -> str:
    runs = [r for r in report["runs"] if r["high_end_delta_mean_ma"] is not None]
    if not runs:
        return '<p class="empty">No completed runs yet.</p>'
    top = max(0.5, max(r["high_end_delta_mean_ma"] for r in runs) * 1.15)
    svg = _Svg(960, 240, (0.5, len(report["runs"]) + 0.5), (0, top), bottom=40)
    count = len(report["runs"])
    stride = max(1, math.ceil(count / 14))
    svg.axes(x_ticks=[i for i in range(1, count + 1) if (i - 1) % stride == 0], y_ticks=_nice_ticks(0, top, 5),
             x_fmt=lambda v: f"{v:.0f}", y_fmt=lambda v: f"{v:.2g}", y_label="Mean end-of-high delta per run (mA)")
    band = (svg.w - svg.left - svg.right) / max(1, count)
    width = max(2.0, min(18.0, band - 2))
    for run in runs:
        x = svg.sx(run["run_id"]) - width / 2
        y = svg.sy(run["high_end_delta_mean_ma"])
        height = svg.sy(0) - y
        color = voltage_color(run["voltage_v"], voltages)
        tip = html.escape(f"Run {run['run_id']}: {run['voltage_v']:g} V, high {run['high_time_s']:g} s, repeat {run['repeat']}"
                          f" - mean Δ {run['high_end_delta_mean_ma']:.3f} mA")
        radius = min(4.0, width / 2, height)
        svg.parts.append(
            f'<path d="M{x:.1f},{y + height:.1f}V{y + radius:.1f}Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f}'
            f'H{x + width - radius:.1f}Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f}V{y + height:.1f}Z" '
            f'fill="{color}" class="bar"><title>{tip}</title></path>'
        )
    return svg.render("Mean end-of-high current delta per run")


def _heatmap(report: dict[str, Any], config: Any) -> str:
    grid: dict[tuple[float, float], list[float]] = {}
    for run in report["runs"]:
        if run["high_end_delta_mean_ma"] is not None:
            grid.setdefault((run["voltage_v"], run["high_time_s"]), []).append(run["high_end_delta_mean_ma"])
    if not grid:
        return '<p class="empty">No data yet.</p>'
    values = [mean(v) for v in grid.values()]
    low, high = min(values), max(values)
    ramp = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
    head = "".join(f"<th>{t:g} s</th>" for t in config.high_times_s)
    rows = []
    for voltage in config.voltages_v:
        cells = []
        for high_time in config.high_times_s:
            data = grid.get((voltage, high_time))
            if not data:
                cells.append('<td class="na">-</td>')
                continue
            value = mean(data)
            position = 0 if high == low else (value - low) / (high - low)
            color = ramp[round(position * (len(ramp) - 1))]
            ink = "#ffffff" if position > 0.45 else "#0b0b0b"
            cells.append(f'<td style="background:{color};color:{ink}" title="{voltage:g} V, {high_time:g} s: {value:.3f} mA">{value:.2f}</td>')
        rows.append(f"<tr><th>{voltage:g} V</th>{''.join(cells)}</tr>")
    return f'<table class="heat"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def render_html(report: dict[str, Any], process: Any) -> str:
    summary = report["summary"]
    config = process.config
    end_t = report["timing"]["wall_duration_s"] or 0
    status = report["status"]
    status_class = {"completed": "good", "stopped": "warn", "failed": "bad"}.get(status, "neutral")
    checks = report["checks"]

    def tile(label: str, value: str, sub: str = "") -> str:
        return f'<div class="tile"><div class="tile-label">{html.escape(label)}</div><div class="tile-value">{html.escape(value)}</div><div class="tile-sub">{html.escape(sub)}</div></div>'

    def fmt(value: Any, unit: str = "", digits: int = 3) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.{digits}g} {unit}".strip()
        return f"{value} {unit}".strip()

    def check_text(stage: str) -> tuple[str, str]:
        check = checks.get(stage)
        if not check:
            return "-", "not run"
        return check["state"], f"Δ {check['delta_ma']:.2f} mA"

    pre_state, pre_sub = check_text("pre")
    post_state, post_sub = check_text("post")
    change = summary["max_voltage_change_pct"]
    tiles = "".join([
        tile("Duration", format_duration(end_t) if end_t else "-", f"{summary['runs_completed']} of {summary['runs_planned']} runs"),
        tile("Pre-check", pre_state, pre_sub),
        tile("Post-check", post_state, post_sub),
        tile(f"Δ at {fmt(summary['max_voltage_v'], 'V')}", fmt(summary["max_voltage_last_delta_ma"], "mA"),
             f"first run {fmt(summary['max_voltage_first_delta_ma'], 'mA')}" + (f" · {change:+.0f}%" if change is not None else "")),
        tile("Exposure", f"{(summary['forward_exposure_vs'] or 0) / 1000:.1f} kV·s",
             f"reverse {(summary['reverse_exposure_vs'] or 0) / 1000:.1f} kV·s"),
        tile("Readings", str(summary["measurement_count"]), f"{report['timing']['interruptions']} pauses for neighbours"),
    ])
    legend = "".join(
        f'<span class="key"><i style="background:{voltage_color(v, config.voltages_v)}"></i>{v:g} V</span>'
        for v in config.voltages_v
    )
    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:+.1f}%"

    by_voltage_rows = "".join(
        f"<tr><td>{row['voltage_v']:g} V</td><td>{row['runs_measured']}</td><td>{fmt(row['first_run_high_end_delta_ma'])}</td>"
        f"<td>{fmt(row['last_run_high_end_delta_ma'])}</td><td>{fmt(row['mean_high_end_delta_ma'])}</td>"
        f"<td>{pct(row['change_pct'])}</td></tr>"
        for row in report["by_voltage"]
    )
    run_rows = "".join(
        f"<tr><td>{r['run_id']}</td><td>{r['voltage_v']:g}</td><td>{r['high_time_s']:g}</td><td>{r['repeat']}</td>"
        f"<td>{fmt(r['high_start_delta_mean_ma'])}</td><td>{fmt(r['high_end_delta_first_ma'])}</td>"
        f"<td>{fmt(r['high_end_delta_last_ma'])}</td><td>{fmt(r['low_end_delta_mean_ma'])}</td>"
        f"<td>{'✓' if r['completed'] else ''}</td></tr>"
        for r in report["runs"]
    )
    log_rows = "".join(
        f'<tr class="lv-{html.escape(item["level"])}"><td>{_fmt_time(item["t_s"] or 0)}</td><td>{html.escape(item["text"])}</td></tr>'
        for item in report["log"]
    )
    title = f"Initialization report · {process.actuator_id}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#f7f7fc;--card:#ffffff;--ink:#1a1b1f;--ink2:#52514e;--muted:#7a8797;--line:#e3e5ec;--grid:#eceef3;
--good:#0d6b3c;--good-bg:#e6f4ec;--warn:#8a5a00;--warn-bg:#fff4dc;--bad:#b42318;--bad-bg:#fdecea;}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#141416;--card:#1d1e21;--ink:#f2f2f4;--ink2:#c3c2b7;--muted:#9aa3ad;
--line:#2c2e33;--grid:#2a2c31;--good:#6fd39b;--good-bg:#15301f;--warn:#f0c060;--warn-bg:#33290f;--bad:#ff8a80;--bad-bg:#3a1714;}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:28px 20px 48px}}
header{{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:flex-end;justify-content:space-between;margin-bottom:18px}}
h1{{font-size:26px;margin:0}} h2{{font-size:16px;margin:0 0 10px}} .sub{{color:var(--ink2)}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px}}
.pill.good{{color:var(--good);background:var(--good-bg)}} .pill.warn{{color:var(--warn);background:var(--warn-bg)}}
.pill.bad{{color:var(--bad);background:var(--bad-bg)}} .pill.neutral{{color:var(--ink2);background:var(--line)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.tile-label{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.tile-value{{font-size:22px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}} .tile-sub{{color:var(--ink2);font-size:12px}}
svg{{width:100%;height:auto;display:block}} .grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--line);stroke-width:1}}
.zero{{stroke:var(--muted);stroke-width:1}} .tick,.axis-label{{fill:var(--muted);font-size:11px}} .dot:hover,.bar:hover{{opacity:.75}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;color:var(--ink2);font-size:12px;margin:2px 0 8px}}
.key i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}}
.key .hollow{{background:none;border:1.5px solid var(--ink2)}} .key .filled{{background:var(--ink2)}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}} th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}}
th:first-child,td:first-child{{text-align:left}} thead th{{color:var(--muted);font-weight:600;font-size:12px}}
.heat td,.heat thead th{{text-align:center}} .heat td{{border:2px solid var(--card);border-radius:6px}} .heat td.na{{color:var(--muted)}}
.scroll{{max-height:420px;overflow:auto}} .empty{{color:var(--muted)}}
.lv-error td{{color:var(--bad)}} .lv-warn td{{color:var(--warn)}} details summary{{cursor:pointer;font-weight:600}}
dl{{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}} dt{{color:var(--muted)}} dd{{margin:0}}
</style></head><body><main>
<header><div><div class="sub">Fluid Reality · Actuator initialization</div><h1>{html.escape(process.actuator_id)}</h1>
<div class="sub">{html.escape(config.name)} · {html.escape(str(summary['board_label']))} channel {process.channel}</div></div>
<div style="text-align:right"><span class="pill {status_class}">{html.escape(status.title())}</span>
<div class="sub" style="margin-top:6px">{html.escape(report['status_detail'] or '')}</div></div></header>
<section class="tiles">{tiles}</section>
<section class="card"><h2>Drive voltage</h2><div class="legend"><span class="key"><i style="background:{POSITIVE}"></i>Positive</span><span class="key"><i style="background:{NEGATIVE}"></i>Negative</span></div>{_voltage_chart(process, end_t)}</section>
<section class="card"><h2>Current delta at phase start and end</h2><div class="legend">{legend}<span class="key"><i class="filled"></i>End of phase</span><span class="key"><i class="hollow"></i>Start of phase</span></div>{_delta_chart(process, end_t)}</section>
<section class="card"><h2>Conditioning by run</h2><div class="legend">{legend}</div>{_run_chart(report, config.voltages_v)}</section>
<section class="card"><h2>Mean end-of-high delta (mA) by voltage and high time</h2>{_heatmap(report, config)}</section>
<section class="card"><h2>By voltage</h2><table><thead><tr><th>Voltage</th><th>Runs</th><th>First run Δ (mA)</th><th>Last run Δ (mA)</th><th>Mean Δ (mA)</th><th>Change</th></tr></thead><tbody>{by_voltage_rows}</tbody></table></section>
<section class="card"><h2>Runs</h2><div class="scroll"><table><thead><tr><th>Run</th><th>V</th><th>High (s)</th><th>Repeat</th><th>Start Δ</th><th>First end Δ</th><th>Last end Δ</th><th>Low end Δ</th><th>Done</th></tr></thead><tbody>{run_rows}</tbody></table></div></section>
<section class="card"><h2>Details</h2><dl>
<dt>Report ID</dt><dd>{html.escape(report['report_id'])}</dd><dt>Started</dt><dd>{html.escape(str(summary['started_at']))}</dd>
<dt>Finished</dt><dd>{html.escape(str(summary['finished_at']))}</dd><dt>Operator</dt><dd>{html.escape(str(summary['operator'] or '-'))}</dd>
<dt>Board</dt><dd>{html.escape(str(report['board']['label']))} · {html.escape(str(report['board']['firmware']))} · {html.escape(str(report['board']['bluetooth_name'] or report['board']['endpoint']))}</dd>
<dt>Sequence</dt><dd>voltages {', '.join(f'{v:g}' for v in config.voltages_v)} V · high times {', '.join(f'{v:g}' for v in config.high_times_s)} s · {config.repeats} repeats · {config.num_cycles} cycles · pause {config.pause_time_s:g} s · low {config.low_mode}{' · pre-hold' if config.pre_hold else ''}</dd>
<dt>Measurement</dt><dd>{config.measure_window_ms} ms reading windows · {config.baseline_window_ms} ms baseline · other actuators on the board held at 0 V while measuring</dd>
<dt>Notes</dt><dd>{html.escape(process.notes or '-')}</dd></dl></section>
<section class="card"><details><summary>Event log ({len(report['log'])})</summary><div class="scroll"><table><tbody>{log_rows}</tbody></table></div></details></section>
</main></body></html>
"""
