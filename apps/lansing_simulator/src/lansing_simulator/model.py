"""Stateful emulation of the Lansing 0.1 firmware and electrical behavior."""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .clock import Clock
from .config import SimulatorConfig, resolved_path
from .event_log import EventLogger, NullEventLogger
from .persistence import StateStore


class ActuatorState(IntEnum):
    IDLE = 0
    FORWARD_ACTIVE = 1
    DISCHARGING = 2


@dataclass
class Actuator:
    state: ActuatorState = ActuatorState.IDLE
    forward_value: int = 0
    positive: int = 0
    negative: int = 0
    active_start_ms: int | None = None
    total_runtime_ms: int = 0
    discharge_start_ms: int | None = None
    discharge_duration_ms: int = 0
    forward_delta_ma: float = 0.0
    discharge_delta_ma: float = 0.0
    conditioning_updated_ms: int = 0
    conditioning_last_logged_ma: float = 0.0


class LansingBoardModel:
    actuator_count = 24

    def __init__(
        self,
        config: SimulatorConfig,
        clock: Clock,
        logger: EventLogger | NullEventLogger,
    ) -> None:
        self.config = config
        self.clock = clock
        self.logger = logger
        self.rng = random.Random(config.simulation.random_seed)
        self.boot_ms = self.now_ms
        self.psu_on = False
        self.psu_on_start_ms: int | None = None
        self.psc_on = False
        self.safety = True
        self.debug = False
        self.binary_stream_mode = False
        self.max_active_ms = config.firmware.max_active_ms
        self.discharge_ms = config.firmware.discharge_ms
        self.actuators = [self._new_actuator(index) for index in range(self.actuator_count)]
        state_path = resolved_path(config, config.firmware.state_file)
        self.store = StateStore(state_path, config.firmware.persist_state)
        self._load_persistent_state()
        self._log(
            "simulator.session",
            firmware=config.firmware.version,
            protocol=config.firmware.protocol,
            random_seed=config.simulation.random_seed,
            config_hash=logger.config_hash,
        )

    @property
    def now_ms(self) -> int:
        return int(self.clock.monotonic() * 1000)

    def _new_actuator(self, index: int) -> Actuator:
        settings = self.config.actuator(index)
        return Actuator(
            forward_delta_ma=settings.forward_delta_ma_at_255,
            discharge_delta_ma=settings.discharge_delta_ma_at_255,
            conditioning_updated_ms=self.now_ms,
            conditioning_last_logged_ma=settings.forward_delta_ma_at_255,
        )

    def _log(self, event: str, **fields: Any) -> None:
        self.logger.emit(event, monotonic_ms=self.now_ms, **fields)

    def _load_persistent_state(self) -> None:
        state = self.store.load()
        self.max_active_ms = int(state.get("max_active_ms", self.max_active_ms))
        self.discharge_ms = int(state.get("discharge_ms", self.discharge_ms))
        runtimes = state.get("actuator_runtime_ms", [])
        forward_deltas = state.get("forward_delta_ma", [])
        discharge_deltas = state.get("discharge_delta_ma", [])
        for index, actuator in enumerate(self.actuators):
            if index < len(runtimes):
                actuator.total_runtime_ms = max(0, int(runtimes[index]))
            if index < len(forward_deltas):
                actuator.forward_delta_ma = max(0.0, float(forward_deltas[index]))
            if index < len(discharge_deltas):
                actuator.discharge_delta_ma = max(0.0, float(discharge_deltas[index]))

    def persist(self) -> None:
        self.store.save(
            {
                "schema_version": 1,
                "max_active_ms": self.max_active_ms,
                "discharge_ms": self.discharge_ms,
                "actuator_runtime_ms": [item.total_runtime_ms for item in self.actuators],
                "forward_delta_ma": [item.forward_delta_ma for item in self.actuators],
                "discharge_delta_ma": [item.discharge_delta_ma for item in self.actuators],
            }
        )
        self._log("persistence.write")

    def reboot(self) -> None:
        self.service()
        self.persist()
        self.psu_on = False
        self.psu_on_start_ms = None
        self.psc_on = False
        self.safety = True
        self.debug = False
        self.binary_stream_mode = False
        self.boot_ms = self.now_ms
        for actuator in self.actuators:
            actuator.state = ActuatorState.IDLE
            actuator.forward_value = 0
            actuator.positive = 0
            actuator.negative = 0
            actuator.active_start_ms = None
            actuator.discharge_start_ms = None
            actuator.discharge_duration_ms = 0
            actuator.conditioning_updated_ms = self.now_ms
        self._log("simulator.reboot")

    def service(self) -> list[str]:
        """Advance conditioning and automatic actuator transitions."""

        debug_lines: list[str] = []
        now = self.now_ms
        for index, actuator in enumerate(self.actuators):
            self._integrate_conditioning(index, now)
            if actuator.state == ActuatorState.FORWARD_ACTIVE and actuator.active_start_ms is not None:
                elapsed = now - actuator.active_start_ms
                if elapsed >= self.max_active_ms:
                    if self.debug:
                        debug_lines.append(
                            f"DBG:MAX_ACTIVE_REACHED,ACT>{index},ACTIVE_MS>{elapsed}"
                        )
                    self._finish_active(index, elapsed)
                    self._start_discharge(index, min(elapsed, self.discharge_ms))
            if actuator.state == ActuatorState.DISCHARGING and actuator.discharge_start_ms is not None:
                elapsed = now - actuator.discharge_start_ms
                if elapsed >= actuator.discharge_duration_ms:
                    if self.debug:
                        debug_lines.append(
                            f"DBG:DISCHARGE_TIME_REACHED,ACT>{index},ELAPSED_MS>{elapsed}"
                        )
                    self._stop_discharge(index)
                    if self.debug:
                        debug_lines.append(f"DBG:DISCHARGE_STOP,ACT>{index}")
        return debug_lines

    def _integrate_conditioning(self, index: int, now_ms: int) -> None:
        actuator = self.actuators[index]
        elapsed_s = max(0, now_ms - actuator.conditioning_updated_ms) / 1000.0
        actuator.conditioning_updated_ms = now_ms
        settings = self.config.actuator(index)
        conditioning = settings.conditioning
        if not settings.connected or not conditioning.enabled or elapsed_s <= 0:
            return
        positive, negative = self._effective_outputs(index)
        output = max(positive, negative)
        if output <= 0:
            return
        improvement = conditioning.improvement_ma_per_dose_second * elapsed_s * output / 255.0
        old_forward = actuator.forward_delta_ma
        old_discharge = actuator.discharge_delta_ma
        actuator.forward_delta_ma = max(
            conditioning.minimum_delta_ma, actuator.forward_delta_ma - improvement
        )
        actuator.discharge_delta_ma = max(
            conditioning.minimum_delta_ma, actuator.discharge_delta_ma - improvement
        )
        if abs(actuator.forward_delta_ma - actuator.conditioning_last_logged_ma) >= 0.01:
            actuator.conditioning_last_logged_ma = actuator.forward_delta_ma
            self._log(
                "conditioning.progress",
                actuator=index,
                dose_seconds=elapsed_s * output / 255.0,
                forward_delta_ma=actuator.forward_delta_ma,
                discharge_delta_ma=actuator.discharge_delta_ma,
            )

    def set_psu(self, enabled: bool) -> None:
        self.psu_on = enabled
        self.psu_on_start_ms = self.now_ms if enabled else None
        self._log("power.psu", state="ON" if enabled else "OFF")

    def set_psc(self, enabled: bool) -> bool:
        if enabled and not self.psu_on:
            return False
        self.psc_on = enabled
        self._log("power.psc", state="ON" if enabled else "OFF")
        return True

    def set_forward(self, index: int, value: int) -> bool:
        self.service()
        actuator = self.actuators[index]
        if actuator.state == ActuatorState.DISCHARGING:
            return False
        if value > 0:
            if actuator.active_start_ms is None:
                actuator.active_start_ms = self.now_ms
                self._log("actuator.active_start", actuator=index, value=value)
            actuator.state = ActuatorState.FORWARD_ACTIVE
            actuator.forward_value = value
            actuator.positive = value
            actuator.negative = 0
            actuator.discharge_start_ms = None
            actuator.discharge_duration_ms = 0
            self._log("actuator.output", actuator=index, positive=value, negative=0)
            return True
        if actuator.state == ActuatorState.FORWARD_ACTIVE and actuator.active_start_ms is not None:
            elapsed = max(0, self.now_ms - actuator.active_start_ms)
            self._finish_active(index, elapsed)
            self._start_discharge(index, min(elapsed, self.discharge_ms))
            self.service()
            return True
        actuator.state = ActuatorState.IDLE
        actuator.forward_value = 0
        actuator.positive = 0
        actuator.negative = 0
        return True

    def _finish_active(self, index: int, elapsed_ms: int) -> None:
        actuator = self.actuators[index]
        actuator.total_runtime_ms += elapsed_ms
        actuator.active_start_ms = None
        actuator.forward_value = 0
        self.persist()
        self._log(
            "actuator.active_finish",
            actuator=index,
            elapsed_ms=elapsed_ms,
            total_runtime_ms=actuator.total_runtime_ms,
        )

    def _start_discharge(self, index: int, duration_ms: int) -> None:
        actuator = self.actuators[index]
        actuator.state = ActuatorState.DISCHARGING
        actuator.positive = 0
        actuator.negative = 255
        actuator.discharge_start_ms = self.now_ms
        actuator.discharge_duration_ms = duration_ms
        self._log("actuator.discharge_start", actuator=index, duration_ms=duration_ms)

    def _stop_discharge(self, index: int) -> None:
        actuator = self.actuators[index]
        actuator.state = ActuatorState.IDLE
        actuator.positive = 0
        actuator.negative = 0
        actuator.discharge_start_ms = None
        actuator.discharge_duration_ms = 0
        self._log("actuator.discharge_stop", actuator=index)

    def set_manual_output(self, index: int, positive: int, negative: int) -> None:
        self.service()
        actuator = self.actuators[index]
        actuator.state = ActuatorState.IDLE
        actuator.forward_value = 0
        actuator.active_start_ms = None
        actuator.discharge_start_ms = None
        actuator.discharge_duration_ms = 0
        actuator.positive = positive
        actuator.negative = negative
        self._log("actuator.manual_output", actuator=index, positive=positive, negative=negative)

    def set_all_off(self) -> None:
        for index in range(self.actuator_count):
            self.set_forward(index, 0)

    def wait_for_idle(self, timeout_ms: int) -> bool:
        deadline = self.now_ms + timeout_ms
        while any(item.state != ActuatorState.IDLE for item in self.actuators):
            now = self.now_ms
            if now >= deadline:
                return False
            next_transition = deadline
            for item in self.actuators:
                if item.state == ActuatorState.DISCHARGING and item.discharge_start_ms is not None:
                    next_transition = min(
                        next_transition, item.discharge_start_ms + item.discharge_duration_ms
                    )
                elif item.state == ActuatorState.FORWARD_ACTIVE and item.active_start_ms is not None:
                    next_transition = min(next_transition, item.active_start_ms + self.max_active_ms)
            self.clock.sleep(max(0.001, (next_transition - now) / 1000.0))
            self.service()
        return True

    def runtime_ms(self, index: int) -> int:
        self.service()
        actuator = self.actuators[index]
        total = actuator.total_runtime_ms
        if actuator.active_start_ms is not None:
            total += max(0, self.now_ms - actuator.active_start_ms)
        return total

    def active_ms(self, index: int) -> int:
        actuator = self.actuators[index]
        if actuator.active_start_ms is None:
            return 0
        return max(0, self.now_ms - actuator.active_start_ms)

    def discharge_left_ms(self, index: int) -> int:
        actuator = self.actuators[index]
        if actuator.state != ActuatorState.DISCHARGING or actuator.discharge_start_ms is None:
            return 0
        elapsed = self.now_ms - actuator.discharge_start_ms
        return max(0, actuator.discharge_duration_ms - elapsed)

    def reset_runtimes(self) -> None:
        for actuator in self.actuators:
            actuator.total_runtime_ms = 0
            if actuator.active_start_ms is not None:
                actuator.active_start_ms = self.now_ms
        self.persist()
        self._log("runtime.reset_all")

    def _instant_current_ma(self) -> float:
        if not self.psu_on:
            return self.config.power.current_when_off_ma
        total = self.config.current.board_baseline_ma
        if self.psc_on:
            for index, actuator in enumerate(self.actuators):
                settings = self.config.actuator(index)
                if not settings.connected:
                    continue
                faults = settings.faults
                if self.rng.random() < faults.intermittent_disconnect_probability:
                    self._log("fault.actuator_disconnected", actuator=index)
                    continue
                positive, negative = self._effective_outputs(index)
                total += actuator.forward_delta_ma * positive / 255.0
                total += actuator.discharge_delta_ma * negative / 255.0
                total += faults.short_circuit_delta_ma_at_255 * max(positive, negative) / 255.0
                if positive or negative:
                    total += self.rng.gauss(0.0, settings.current_noise_stddev_ma)
        total += self.rng.gauss(0.0, self.config.current.board_noise_stddev_ma)
        return max(0.0, total)

    def _effective_outputs(self, index: int) -> tuple[int, int]:
        actuator = self.actuators[index]
        faults = self.config.actuator(index).faults
        positive = (
            actuator.positive if faults.stuck_positive_output < 0 else faults.stuck_positive_output
        )
        negative = (
            actuator.negative if faults.stuck_negative_output < 0 else faults.stuck_negative_output
        )
        return positive, negative

    def _instant_voltage_v(self) -> float:
        if not self.psu_on:
            return self.config.power.voltage_when_off_v
        ramp_ms = self.config.power.startup_delay_ms
        if ramp_ms > 0 and self.psu_on_start_ms is not None:
            fraction = min(1.0, max(0.0, (self.now_ms - self.psu_on_start_ms) / ramp_ms))
        else:
            fraction = 1.0
        value = self.config.power.voltage_v * fraction
        value += self.rng.gauss(0.0, self.config.power.voltage_noise_stddev_v)
        return max(0.0, value)

    def _quantize_current(self, current_ma: float) -> float:
        settings = self.config.current
        adc_max = (1 << settings.adc_bits) - 1
        ma_per_count = settings.adc_reference_v * settings.current_conversion_ma_per_v / adc_max
        return round(current_ma / ma_per_count) * ma_per_count

    def _quantize_voltage(self, voltage_v: float) -> float:
        settings = self.config.current
        adc_max = (1 << settings.adc_bits) - 1
        volts_per_count = (
            settings.adc_reference_v * settings.voltage_conversion_v_per_v / adc_max
        )
        return round(voltage_v / volts_per_count) * volts_per_count

    def measure_current(self, duration_ms: int) -> float:
        return self._measure(duration_ms, current=True)

    def measure_voltage(self, duration_ms: int) -> float:
        return self._measure(duration_ms, current=False)

    def _measure(self, duration_ms: int, *, current: bool) -> float:
        duration_ms = max(1, duration_ms)
        remaining = duration_ms
        weighted_sum = 0.0
        total_weight = 0
        while remaining > 0:
            step = min(10, remaining)
            self.service()
            raw = self._instant_current_ma() if current else self._instant_voltage_v()
            value = self._quantize_current(raw) if current else self._quantize_voltage(raw)
            weighted_sum += value * step
            total_weight += step
            self.clock.sleep(step / 1000.0)
            remaining -= step
        self.service()
        result = weighted_sum / total_weight
        self._log(
            "measurement.current" if current else "measurement.voltage",
            duration_ms=duration_ms,
            value=result,
        )
        return result

    def close(self) -> None:
        self.service()
        self.persist()
        self._log("simulator.close")

    def initialize_actuator(self, index: int) -> bool:
        if self.actuators[index].state == ActuatorState.DISCHARGING:
            return False
        self.actuators[index].total_runtime_ms = 0
        self.persist()
        for duration_ms in (500,) * 5 + (1000,) * 5:
            if not self.set_forward(index, 255):
                return False
            self.clock.sleep(duration_ms / 1000.0)
            self.service()
            if not self.set_forward(index, 0):
                return False
            discharge_timeout = self.actuators[index].discharge_duration_ms + 1000
            if not self.wait_for_idle(max(1000, discharge_timeout)):
                return False
            self.clock.sleep(duration_ms / 1000.0)
            self.service()
        self.persist()
        self._log("actuator.initialize", actuator=index)
        return True

    def diagnose(self, index: int) -> tuple[float, float, float] | None:
        self.set_all_off()
        if not self.wait_for_idle(self.max_active_ms + self.discharge_ms + 1000):
            return None
        baseline = self.measure_current(250)
        if not self.set_forward(index, 255):
            return None
        forward = self.measure_current(1000)
        if not self.set_forward(index, 0):
            return None
        discharge_time = max(1, self.actuators[index].discharge_duration_ms)
        discharge = self.measure_current(discharge_time)
        self.wait_for_idle(discharge_time + 100)
        self._log(
            "actuator.diagnose",
            actuator=index,
            baseline_ma=baseline,
            forward_ma=forward,
            discharge_ma=discharge,
        )
        return baseline, forward, discharge
