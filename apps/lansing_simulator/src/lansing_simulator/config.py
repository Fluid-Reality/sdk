"""Configuration loading and validation for the Lansing simulator."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SerialConfig:
    """Reserved communication settings; no serial backend is selected yet."""

    port: str = ""
    baudrate: int = 250_000


@dataclass
class SimulationConfig:
    random_seed: int = 12345
    time_scale: float = 1.0
    strict_firmware_0_1: bool = True


@dataclass
class FirmwareConfig:
    name: str = "Lansing"
    version: str = "0.1"
    protocol: str = "0.1"
    actuator_count: int = 24
    max_active_ms: int = 5000
    discharge_ms: int = 2000
    persist_state: bool = True
    state_file: str = "lansing_simulator_state.json"


@dataclass
class PowerConfig:
    voltage_v: float = 220.0
    voltage_noise_stddev_v: float = 0.25
    voltage_when_off_v: float = 0.0
    current_when_off_ma: float = 0.0
    startup_delay_ms: int = 0


@dataclass
class CurrentConfig:
    board_baseline_ma: float = 1.33
    board_noise_stddev_ma: float = 0.02
    adc_bits: int = 12
    adc_reference_v: float = 3.3
    current_conversion_ma_per_v: float = 20.0
    voltage_conversion_v_per_v: float = 137.5


@dataclass
class ConditioningConfig:
    enabled: bool = True
    improvement_ma_per_dose_second: float = 0.0025
    minimum_delta_ma: float = 1.5


@dataclass
class ActuatorFaultConfig:
    """Electrical faults that can exist beneath firmware-visible output state."""

    stuck_positive_output: int = -1
    stuck_negative_output: int = -1
    short_circuit_delta_ma_at_255: float = 0.0
    intermittent_disconnect_probability: float = 0.0


@dataclass
class ActuatorConfig:
    connected: bool = True
    forward_delta_ma_at_255: float = 2.0
    discharge_delta_ma_at_255: float = 2.0
    current_noise_stddev_ma: float = 0.03
    conditioning: ConditioningConfig = field(default_factory=ConditioningConfig)
    faults: ActuatorFaultConfig = field(default_factory=ActuatorFaultConfig)


@dataclass
class LoggingConfig:
    enabled: bool = True
    file: str = "logs/lansing_simulator.jsonl"
    console: bool = False
    include_raw_bytes: bool = True
    max_bytes: int = 5_000_000
    backup_count: int = 3


@dataclass
class FaultConfig:
    response_delay_ms: int = 0
    drop_response_probability: float = 0.0
    corrupt_response_probability: float = 0.0


@dataclass
class SimulatorConfig:
    schema_version: int = 1
    serial: SerialConfig = field(default_factory=SerialConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    firmware: FirmwareConfig = field(default_factory=FirmwareConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    current: CurrentConfig = field(default_factory=CurrentConfig)
    actuator_defaults: ActuatorConfig = field(default_factory=ActuatorConfig)
    actuators: dict[int, ActuatorConfig] = field(default_factory=dict)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    faults: FaultConfig = field(default_factory=FaultConfig)
    source_path: Path | None = field(default=None, repr=False)

    def actuator(self, index: int) -> ActuatorConfig:
        return self.actuators.get(index, self.actuator_defaults)


def _merge_dataclass(cls: type[Any], defaults: Any, values: dict[str, Any]) -> Any:
    known = {field.name for field in dataclasses.fields(cls)}
    unknown = set(values) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} fields: {', '.join(sorted(unknown))}")
    data = {field.name: getattr(defaults, field.name) for field in dataclasses.fields(cls)}
    data.update(values)
    return cls(**data)


def _actuator_config(defaults: ActuatorConfig, values: dict[str, Any]) -> ActuatorConfig:
    values = dict(values)
    conditioning_values = values.pop("conditioning", {})
    fault_values = values.pop("faults", {})
    actuator = _merge_dataclass(ActuatorConfig, defaults, values)
    actuator.conditioning = _merge_dataclass(
        ConditioningConfig,
        dataclasses.replace(defaults.conditioning),
        conditioning_values,
    )
    actuator.faults = _merge_dataclass(
        ActuatorFaultConfig,
        dataclasses.replace(defaults.faults),
        fault_values,
    )
    return actuator


def load_config(path: str | Path) -> SimulatorConfig:
    """Load a TOML configuration and resolve output paths relative to it."""

    source = Path(path).resolve()
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    allowed = {
        "schema_version", "serial", "simulation", "firmware", "power", "current",
        "actuator_defaults", "actuators", "logging", "faults",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"Unknown top-level fields: {', '.join(sorted(unknown))}")

    config = SimulatorConfig()
    config.schema_version = int(raw.get("schema_version", 1))
    config.serial = _merge_dataclass(SerialConfig, config.serial, raw.get("serial", {}))
    config.simulation = _merge_dataclass(
        SimulationConfig, config.simulation, raw.get("simulation", {})
    )
    config.firmware = _merge_dataclass(FirmwareConfig, config.firmware, raw.get("firmware", {}))
    config.power = _merge_dataclass(PowerConfig, config.power, raw.get("power", {}))
    config.current = _merge_dataclass(CurrentConfig, config.current, raw.get("current", {}))
    config.actuator_defaults = _actuator_config(
        config.actuator_defaults, raw.get("actuator_defaults", {})
    )
    config.logging = _merge_dataclass(LoggingConfig, config.logging, raw.get("logging", {}))
    config.faults = _merge_dataclass(FaultConfig, config.faults, raw.get("faults", {}))
    config.actuators = {}
    for key, values in raw.get("actuators", {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Actuator key must be an integer, got {key!r}") from exc
        config.actuators[index] = _actuator_config(config.actuator_defaults, values)
    config.source_path = source
    validate_config(config)
    return config


def validate_config(config: SimulatorConfig) -> None:
    if config.schema_version != 1:
        raise ValueError(f"Unsupported schema_version {config.schema_version}")
    if config.firmware.actuator_count != 24:
        raise ValueError("Firmware 0.1 compatibility requires exactly 24 actuators")
    if config.simulation.time_scale <= 0:
        raise ValueError("simulation.time_scale must be greater than zero")
    if config.serial.baudrate <= 0:
        raise ValueError("serial.baudrate must be greater than zero")
    if config.firmware.max_active_ms < 0 or config.firmware.discharge_ms < 0:
        raise ValueError("firmware timing values cannot be negative")
    if config.power.startup_delay_ms < 0:
        raise ValueError("power.startup_delay_ms cannot be negative")
    for name in (
        "voltage_v", "voltage_noise_stddev_v", "voltage_when_off_v", "current_when_off_ma"
    ):
        if getattr(config.power, name) < 0:
            raise ValueError(f"power.{name} cannot be negative")
    if not 1 <= config.current.adc_bits <= 24:
        raise ValueError("current.adc_bits must be between 1 and 24")
    for name in (
        "board_baseline_ma", "board_noise_stddev_ma",
    ):
        if getattr(config.current, name) < 0:
            raise ValueError(f"current.{name} cannot be negative")
    for name in ("adc_reference_v", "current_conversion_ma_per_v", "voltage_conversion_v_per_v"):
        if getattr(config.current, name) <= 0:
            raise ValueError(f"current.{name} must be greater than zero")
    if config.faults.response_delay_ms < 0:
        raise ValueError("fault response_delay_ms cannot be negative")
    if not 0 <= config.faults.drop_response_probability <= 1:
        raise ValueError("fault drop_response_probability must be between 0 and 1")
    if not 0 <= config.faults.corrupt_response_probability <= 1:
        raise ValueError("fault corrupt_response_probability must be between 0 and 1")
    all_actuators = {-1: config.actuator_defaults, **config.actuators}
    for index, actuator in all_actuators.items():
        label = "default actuator" if index == -1 else f"Actuator {index}"
        if not 0 <= index < config.firmware.actuator_count:
            if index != -1:
                raise ValueError(f"Actuator index {index} is outside 0..23")
        if actuator.forward_delta_ma_at_255 < 0 or actuator.discharge_delta_ma_at_255 < 0:
            raise ValueError(f"{label} current deltas cannot be negative")
        if actuator.conditioning.minimum_delta_ma < 0:
            raise ValueError(f"{label} minimum delta cannot be negative")
        if actuator.conditioning.improvement_ma_per_dose_second < 0:
            raise ValueError(f"{label} conditioning improvement cannot be negative")
        if actuator.current_noise_stddev_ma < 0:
            raise ValueError(f"{label} current noise cannot be negative")
        for field_name in ("stuck_positive_output", "stuck_negative_output"):
            value = getattr(actuator.faults, field_name)
            if value != -1 and not 0 <= value <= 255:
                raise ValueError(f"{label} {field_name} must be -1 or 0..255")
        if actuator.faults.short_circuit_delta_ma_at_255 < 0:
            raise ValueError(f"{label} short-circuit delta cannot be negative")
        probability = actuator.faults.intermittent_disconnect_probability
        if not 0 <= probability <= 1:
            raise ValueError(f"{label} intermittent disconnect probability must be 0..1")


def resolved_path(config: SimulatorConfig, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or config.source_path is None:
        return path
    return config.source_path.parent / path
