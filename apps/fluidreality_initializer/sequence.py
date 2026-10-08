"""Initialization sequence model.

An initialization sweeps every combination of drive voltage, high time and
repeat, exactly like ``infinidaq/amplifier_sequence_run.py``. Each run is::

    [pause @ 0 V | pre-hold @ -V]  ->  num_cycles x ([+V for high_time] [low for high_time])  ->  [pause @ 0 V]

where *low* is ``-V`` (``low_mode="negative"``) or ``0 V`` (``low_mode="zero"``).
The plan is a flat list of :class:`Phase` objects that the board engine walks
through for one actuator.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

CONFIG_FORMAT = "fluid-reality-initialization"
CONFIG_VERSION = 1
MAX_DRIVE_V = 250.0

PHASE_PAUSE = "pause"
PHASE_PRE_HOLD = "pre_hold"
PHASE_HIGH = "high"
PHASE_LOW = "low"
PHASE_RECOVERY = "recovery"

PHASE_LABELS = {
    PHASE_PAUSE: "Baseline pause",
    PHASE_PRE_HOLD: "Pre-hold",
    PHASE_HIGH: "High",
    PHASE_LOW: "Low",
    PHASE_RECOVERY: "Recovery pause",
}


@dataclass(frozen=True)
class InitializationConfig:
    """Everything that defines one actuator's initialization."""

    name: str = "Standard initialization"
    voltages_v: tuple[float, ...] = (50.0, 100.0, 150.0, 200.0)
    high_times_s: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0)
    repeats: int = 3
    num_cycles: int = 10
    pause_time_s: float = 5.0
    low_mode: str = "negative"  # "negative" (low = -V) or "zero" (low = 0 V)
    pre_hold: bool = False  # hold -V for high_time instead of the leading pause
    measure_window_ms: int = 500  # OUC window for each start/end reading
    baseline_window_ms: int = 250  # OUC window for the all-off baseline
    measure_zero_phases: bool = False  # also read current at 0 V phase edges
    pre_check: bool = True  # firmware DT0/DT1 detection before the sweep
    post_check: bool = True  # firmware DT0/DT1 detection after the sweep
    pass_max_delta_ma: float | None = None  # optional acceptance on final delta

    def __post_init__(self) -> None:
        object.__setattr__(self, "voltages_v", tuple(float(v) for v in self.voltages_v))
        object.__setattr__(self, "high_times_s", tuple(float(v) for v in self.high_times_s))
        # Accept whole-number floats from JSON (e.g. 5.0) for the integer fields.
        for name in ("repeats", "num_cycles", "measure_window_ms", "baseline_window_ms"):
            value = getattr(self, name)
            if isinstance(value, float) and value.is_integer():
                object.__setattr__(self, name, int(value))

    # ------------------------------------------------------------------ checks
    def validate(self) -> list[str]:
        """Return human-readable problems; an empty list means valid."""

        problems: list[str] = []
        for name in ("repeats", "num_cycles", "measure_window_ms", "baseline_window_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                problems.append(f"{name} must be a whole number.")
        if problems:
            return problems
        for name in ("pause_time_s",):
            if not isinstance(getattr(self, name), (int, float)) or not math.isfinite(getattr(self, name)):
                problems.append(f"{name} must be a number.")
        if self.pass_max_delta_ma is not None and not isinstance(self.pass_max_delta_ma, (int, float)):
            problems.append("pass_max_delta_ma must be a number.")
        if problems:
            return problems
        if not self.voltages_v:
            problems.append("Add at least one drive voltage.")
        for voltage in self.voltages_v:
            if not math.isfinite(voltage) or voltage <= 0 or voltage > MAX_DRIVE_V:
                problems.append(f"Voltage {voltage:g} V must be between 0 and {MAX_DRIVE_V:g} V.")
        if not self.high_times_s:
            problems.append("Add at least one high time.")
        for high_time in self.high_times_s:
            if not math.isfinite(high_time) or high_time <= 0:
                problems.append(f"High time {high_time:g} s must be greater than zero.")
        if self.repeats < 1:
            problems.append("Repeats must be at least 1.")
        if self.num_cycles < 1:
            problems.append("Cycles must be at least 1.")
        if self.pause_time_s < 0:
            problems.append("Pause time cannot be negative.")
        if self.low_mode not in {"negative", "zero"}:
            problems.append("Low mode must be 'negative' or 'zero'.")
        if not 20 <= self.measure_window_ms <= 5000:
            problems.append("Measurement window must be 20-5000 ms.")
        if not 20 <= self.baseline_window_ms <= 5000:
            problems.append("Baseline window must be 20-5000 ms.")
        if self.pass_max_delta_ma is not None and self.pass_max_delta_ma <= 0:
            problems.append("Pass threshold must be greater than zero.")
        return problems

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["voltages_v"] = list(self.voltages_v)
        data["high_times_s"] = list(self.high_times_s)
        return {"format": CONFIG_FORMAT, "version": CONFIG_VERSION, **data}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InitializationConfig":
        """Load our own format or an infinidaq ``amplifier_sequence_config``."""

        if data.get("format") == CONFIG_FORMAT:
            known = {f for f in cls.__dataclass_fields__}
            values = {key: value for key, value in data.items() if key in known}
            return cls(**values)
        # infinidaq amplifier_sequence_config*.json
        if "voltages" in data or "high_times" in data:
            return cls(
                name=str(data.get("actuator_name") or data.get("name") or "Imported sequence"),
                voltages_v=tuple(float(v) for v in data.get("voltages", [])),
                high_times_s=tuple(float(v) for v in data.get("high_times", [])),
                repeats=int(data.get("repeats", 1)),
                num_cycles=int(data.get("num_cycles", 5)),
                pause_time_s=float(data.get("pause_time", 5.0)),
                low_mode=str(data.get("low_mode", "zero")),
                pre_hold=bool(data.get("pre_hold", False)),
            )
        raise ValueError("Unrecognised initialization file (expected a Fluid Reality or infinidaq sequence config).")

    @classmethod
    def load(cls, path: str | Path) -> "InitializationConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    def with_changes(self, **changes: Any) -> "InitializationConfig":
        return replace(self, **changes)


@dataclass(frozen=True)
class RunSpec:
    run_id: int  # 1-based
    voltage_v: float
    high_time_s: float
    low_v: float
    repeat: int
    num_cycles: int
    pre_hold_v: float | None


@dataclass(frozen=True)
class Phase:
    index: int  # 0-based position in the plan
    run: RunSpec
    kind: str
    target_v: float  # signed drive voltage
    duration_s: float
    cycle: int | None = None  # 1-based for high/low phases

    @property
    def label(self) -> str:
        text = PHASE_LABELS.get(self.kind, self.kind)
        if self.cycle is not None:
            text += f" {self.cycle}/{self.run.num_cycles}"
        return text


@dataclass
class SequencePlan:
    config: InitializationConfig
    runs: list[RunSpec] = field(default_factory=list)
    phases: list[Phase] = field(default_factory=list)

    @property
    def drive_duration_s(self) -> float:
        return sum(phase.duration_s for phase in self.phases)

    def measured(self, phase: Phase | None) -> bool:
        if phase is None:
            return False
        return phase.target_v != 0 or self.config.measure_zero_phases

    def measurement_event_count(self) -> int:
        """Boundary events that need a board pause (start of plan to end)."""

        count = 0
        previous: Phase | None = None
        for phase in [*self.phases, None]:
            if self.measured(previous) or self.measured(phase):
                count += 1
            previous = phase
        return count

    def measurement_count(self) -> int:
        return 2 * sum(1 for phase in self.phases if self.measured(phase))

    def event_overhead_s(self) -> float:
        config = self.config
        per_event = (2 * config.measure_window_ms + config.baseline_window_ms) / 1000.0 + 0.08
        return per_event * self.measurement_event_count()

    def estimated_duration_s(self) -> float:
        checks = (3.5 if self.config.pre_check else 0.0) + (3.5 if self.config.post_check else 0.0)
        return self.drive_duration_s + self.event_overhead_s() + checks

    def volt_seconds(self) -> tuple[float, float]:
        """(forward V·s, reverse V·s) the plan applies, both positive numbers."""

        forward = sum(p.target_v * p.duration_s for p in self.phases if p.target_v > 0)
        reverse = sum(-p.target_v * p.duration_s for p in self.phases if p.target_v < 0)
        return forward, reverse


def build_plan(config: InitializationConfig) -> SequencePlan:
    problems = config.validate()
    if problems:
        raise ValueError(" ".join(problems))
    plan = SequencePlan(config=config)
    run_id = 0
    for voltage in config.voltages_v:
        for high_time in config.high_times_s:
            for repeat in range(1, config.repeats + 1):
                run_id += 1
                low_v = -voltage if config.low_mode == "negative" else 0.0
                run = RunSpec(
                    run_id=run_id,
                    voltage_v=voltage,
                    high_time_s=high_time,
                    low_v=low_v,
                    repeat=repeat,
                    num_cycles=config.num_cycles,
                    pre_hold_v=-voltage if config.pre_hold else None,
                )
                plan.runs.append(run)
                _append_run_phases(plan, run, config.pause_time_s)
    return plan


def _append_run_phases(plan: SequencePlan, run: RunSpec, pause_time_s: float) -> None:
    def add(kind: str, target_v: float, duration_s: float, cycle: int | None = None) -> None:
        if duration_s <= 0:
            return
        plan.phases.append(
            Phase(
                index=len(plan.phases),
                run=run,
                kind=kind,
                target_v=float(target_v),
                duration_s=float(duration_s),
                cycle=cycle,
            )
        )

    if run.pre_hold_v is not None:
        add(PHASE_PRE_HOLD, run.pre_hold_v, run.high_time_s)
    else:
        add(PHASE_PAUSE, 0.0, pause_time_s)
    for cycle in range(1, run.num_cycles + 1):
        add(PHASE_HIGH, run.voltage_v, run.high_time_s, cycle)
        add(PHASE_LOW, run.low_v, run.high_time_s, cycle)
    add(PHASE_RECOVERY, 0.0, pause_time_s)


def parse_number_list(text: str) -> tuple[float, ...]:
    """Parse ``"50, 100 150"`` into floats; raises ``ValueError`` on junk."""

    items = [item for item in text.replace(";", ",").replace(" ", ",").split(",") if item.strip()]
    return tuple(float(item) for item in items)


def format_number_list(values: Iterable[float]) -> str:
    return ", ".join(f"{value:g}" for value in values)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


PRESETS_DIR = Path(__file__).resolve().parent / "presets"


def load_presets() -> dict[str, InitializationConfig]:
    presets: dict[str, InitializationConfig] = {}
    if PRESETS_DIR.is_dir():
        for path in sorted(PRESETS_DIR.glob("*.json")):
            try:
                config = InitializationConfig.load(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            presets[config.name] = config
    if not presets:
        presets["Standard initialization"] = InitializationConfig()
    return presets
