"""Lansing board wrapper."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from logging import Logger
from typing import Callable, Literal

from ..errors import FirmwareError
from ..protocol import LineTransport, ProtocolError, Response
from ..transport import SerialTransport
from .base import Board
from .lansing_errors import LANSING_ERROR_INFO

Boolish = bool | Literal["ON", "OFF", "1", "0", 1, 0]


@dataclass(frozen=True)
class Diagnosis:
    actuator: int
    baseline_ma: float
    forward_ma: float
    discharge_ma: float


class ActuatorState(str, Enum):
    UNKNOWN = "Unknown"
    READY = "Ready"
    ERROR = "Error"
    NOT_CONNECTED = "Not connected"


@dataclass(frozen=True)
class ActuatorDetection:
    actuator: int
    state: ActuatorState
    baseline_ma: float
    forward_ma: float
    discharge_ma: float
    delta_ma: float


@dataclass(frozen=True)
class ManualOutput:
    actuator: int
    positive: int
    negative: int


@dataclass(frozen=True)
class LansingVersion:
    firmware: str
    version: str
    protocol: str


@dataclass(frozen=True)
class LansingConfig:
    max_active_ms: int
    discharge_ms: int
    safe: bool
    debug: bool


class Lansing(Board):
    """Wrapper for the Lansing actuator controller firmware."""

    actuator_count = 24
    default_timeout_s = 45.0
    min_output = 0
    max_output = 255
    not_connected_delta_ma = 0.1
    error_delta_ma = 3.0
    initialization_stages_v = (25.0, 50.0, 100.0, 200.0)
    initialization_stage_duration_s = 30.0
    initialization_phase_interval_s = 0.5

    def __init__(
        self,
        port: str | None = None,
        *,
        baudrate: int = 250000,
        timeout: float = default_timeout_s,
        transport: LineTransport | None = None,
        debug_callback: Callable[[str], None] | None = None,
        debug_logger: Logger | None = None,
        log_debug_messages: bool = False,
        **serial_kwargs,
    ) -> None:
        if transport is None:
            if port is None:
                raise ValueError("Provide either a serial port or a transport")
            transport = SerialTransport(
                port,
                baudrate=baudrate,
                timeout=timeout,
                **serial_kwargs,
            )
        super().__init__(
            transport,
            debug_callback=debug_callback,
            debug_logger=debug_logger,
            log_debug_messages=log_debug_messages,
            error_info=LANSING_ERROR_INFO,
        )
        self._actuator_states = [ActuatorState.UNKNOWN] * self.actuator_count
        self._actuator_detections: dict[int, ActuatorDetection] = {}

    def raw_command(self, command: str, *params: object, ok_lines: int = 1) -> list[Response]:
        """Send a raw 3-letter firmware command."""

        self.debug("raw_command.start", command=command, params=params, ok_lines=ok_lines)
        try:
            responses = self.protocol.command(command, *params, ok_lines=ok_lines)
        except Exception as exc:
            self.debug("raw_command.error", command=command, error=exc)
            raise
        self.debug(
            "raw_command.done",
            command=command,
            responses=tuple(response.raw for response in responses),
        )
        return responses

    def drain_input(self) -> tuple[str, ...]:
        drain = getattr(self.transport, "drain_lines", None)
        if drain is None:
            return ()
        return drain()

    def reset_input_buffer(self) -> None:
        reset = getattr(self.transport, "reset_input_buffer", None)
        if reset is not None:
            reset()

    def force_text_mode(self) -> tuple[str, ...]:
        """Try to recover a clean text-command boundary.

        This is useful after an interrupted binary stream run. If the board is
        still in binary mode, the first byte exits stream mode. If it is already
        in text mode, the bytes are flushed with a newline and any resulting
        parser error is drained before the next command.
        """

        self.debug("force_text_mode.start")
        self.transport.write_bytes(bytes([255, 0]))
        self.transport.write_line("")
        wait = getattr(self.transport, "wait_for_quiet", None)
        if wait is not None:
            wait()
        lines = self.drain_input()
        self.debug("force_text_mode.done", drained=lines)
        return lines

    def version(self) -> dict[str, str]:
        return self.raw_command("VER")[0].fields

    def firmware_version(self) -> LansingVersion:
        fields = self.version()
        return LansingVersion(
            firmware=fields["FW"],
            version=fields["VERSION"],
            protocol=fields["PROTO"],
        )

    def power_supply(self, state: Boolish | None = None) -> str:
        self.debug("power_supply", requested_state=state)
        response = self.raw_command("PSU", *self._optional_bool_param(state))[0]
        return response.values[0] if response.values else response.payload

    def psu_on(self) -> None:
        self.power_supply(True)

    def psu_off(self) -> None:
        self.power_supply(False)

    def is_psu_on(self) -> bool:
        return self._state_to_bool(self.power_supply())

    def connect_power(self, state: Boolish | None = None) -> str:
        self.debug("connect_power", requested_state=state)
        response = self.raw_command("PSC", *self._optional_bool_param(state))[0]
        return response.values[0] if response.values else response.payload

    def psc_on(self) -> None:
        self.connect_power(True)

    def psc_off(self) -> None:
        self.connect_power(False)

    def is_power_connected(self) -> bool:
        return self._state_to_bool(self.connect_power())

    def voltage(self, measurement_ms: int | None = None) -> float:
        if measurement_ms is not None and measurement_ms < 1:
            raise ValueError("measurement_ms must be >= 1")
        params = () if measurement_ms is None else (measurement_ms,)
        value = float(self.raw_command("VLT", *params)[0].payload)
        self.debug("voltage", measurement_ms=measurement_ms, value_v=value)
        return value

    def current(self) -> float:
        value = float(self.raw_command("CUR")[0].payload)
        self.debug("current", value_ma=value)
        return value

    def set_actuator(self, actuator: int, value: int) -> None:
        self._validate_actuator(actuator)
        self._validate_output(value)
        state = self.actuator_state(actuator)
        self.debug("set_actuator.request", actuator=actuator, value=value, state=state.value)
        if state is not ActuatorState.READY:
            self.debug("set_actuator.blocked", actuator=actuator, value=value, state=state.value)
            raise RuntimeError(
                f"Actuator {actuator} is {state.value}; run detect({actuator}) "
                "and ensure it is Ready before setting output."
            )
        self.raw_command("ACT", actuator, value)
        self.debug("set_actuator.done", actuator=actuator, value=value)

    def get_actuator(self, actuator: int) -> int:
        self._validate_actuator(actuator)
        response = self.raw_command("ACT", actuator)[0]
        if len(response.values) >= 2:
            return int(response.values[1])
        return int(response.fields.get("VALUE", response.payload))

    def get_actuators(self) -> tuple[int, ...]:
        response = self.raw_command("ACT")[0]
        return tuple(int(value) for value in response.values)

    def all_actuators_off(self) -> None:
        self.debug("all_actuators_off.start")
        for actuator in range(self.actuator_count):
            self.raw_command("ACT", actuator, 0)
        self.debug("all_actuators_off.done")

    def manual_output(
        self,
        actuator: int,
        positive: int | None = None,
        negative: int | None = None,
    ) -> ManualOutput | None:
        self._validate_actuator(actuator)
        self.debug("manual_output.request", actuator=actuator, positive=positive, negative=negative)
        if positive is None and negative is None:
            response = self.raw_command("OUT", actuator)[0]
            output = ManualOutput(
                actuator=int(response.fields["ACT"]),
                positive=int(response.fields["POS"]),
                negative=int(response.fields["NEG"]),
            )
            self.debug("manual_output.read", output=output)
            return output
        if positive is None or negative is None:
            raise ValueError("Provide both positive and negative values")
        self._validate_output(positive)
        self._validate_output(negative)
        self.raw_command("OUT", actuator, positive, negative)
        self.debug("manual_output.write", actuator=actuator, positive=positive, negative=negative)
        return None

    def set_manual_output(self, actuator: int, positive: int, negative: int) -> None:
        self.manual_output(actuator, positive, negative)

    def get_manual_output(self, actuator: int) -> ManualOutput:
        output = self.manual_output(actuator)
        if output is None:
            raise RuntimeError("manual_output unexpectedly returned no output")
        return output

    def manual_outputs(self) -> dict[int, tuple[int, int]]:
        response = self.raw_command("OUT")[0]
        outputs: dict[int, tuple[int, int]] = {}
        for actuator in range(self.actuator_count):
            outputs[actuator] = (
                int(response.fields[f"A{actuator}P"]),
                int(response.fields[f"A{actuator}N"]),
            )
        return outputs

    def initialize_actuator(self, actuator: int) -> None:
        self._validate_actuator(actuator)
        self.debug("initialize_actuator.firmware.start", actuator=actuator)
        self.raw_command("INI", actuator)
        self.debug("initialize_actuator.firmware.done", actuator=actuator)

    def actuator_state(self, actuator: int) -> ActuatorState:
        self._validate_actuator(actuator)
        return self._actuator_states[actuator]

    @property
    def actuator_states(self) -> tuple[ActuatorState, ...]:
        return tuple(self._actuator_states)

    def last_detection(self, actuator: int) -> ActuatorDetection | None:
        self._validate_actuator(actuator)
        return self._actuator_detections.get(actuator)

    def classify_diagnosis(self, diagnosis: Diagnosis) -> ActuatorDetection:
        self.debug("classify_diagnosis", diagnosis=diagnosis)
        return self._record_detection(diagnosis)

    def detect(self, actuator: int) -> ActuatorState:
        detection = self.detect_actuator(actuator)
        return detection.state

    def detect_actuator(self, actuator: int) -> ActuatorDetection:
        self._validate_actuator(actuator)
        group_start = (actuator // 8) * 8
        self.debug(
            "detect.start",
            actuator=actuator,
            group_start=group_start,
            group_end=group_start + 7,
        )
        for group_actuator in range(group_start, group_start + 8):
            try:
                self.raw_command("ACT", group_actuator, 0)
                self.debug("detect.group_off", actuator=group_actuator)
            except FirmwareError as exc:
                if exc.code != "ACT_FAILED":
                    raise
                self.debug("detect.group_off.lockout", actuator=group_actuator, error=exc.code)

        diagnosis = self.diagnose_actuator(actuator)
        detection = self._record_detection(diagnosis)
        self.debug("detect.done", detection=detection)
        return detection

    def initialize(
        self,
        actuator: int,
        *,
        progress_callback: Callable[[dict[str, float | int]], None] | None = None,
    ) -> ActuatorState:
        detection = self.initialize_actuator_stateful(
            actuator,
            progress_callback=progress_callback,
        )
        return detection.state

    def initialize_actuator_stateful(
        self,
        actuator: int,
        *,
        progress_callback: Callable[[dict[str, float | int]], None] | None = None,
    ) -> ActuatorDetection:
        self._validate_actuator(actuator)
        state = self.actuator_state(actuator)
        self.debug("initialize.start", actuator=actuator, state=state.value)
        if state is ActuatorState.UNKNOWN:
            self.debug("initialize.blocked", actuator=actuator, state=state.value)
            raise RuntimeError(f"Actuator {actuator} is Unknown; run detect({actuator}) first.")
        if state is ActuatorState.NOT_CONNECTED:
            self.debug("initialize.blocked", actuator=actuator, state=state.value)
            raise RuntimeError(f"Actuator {actuator} is not connected.")

        supply_voltage = self.voltage()
        if supply_voltage <= 0:
            self.debug("initialize.blocked", actuator=actuator, supply_voltage=supply_voltage)
            raise RuntimeError("Cannot initialize: measured PSU voltage is 0 V.")

        stages = tuple(float(value) for value in self.initialization_stages_v)
        stage_duration_s = float(self.initialization_stage_duration_s)
        phase_interval_s = float(self.initialization_phase_interval_s)
        total_duration_s = stage_duration_s * len(stages)
        start = time.monotonic()
        next_report_second = -1

        try:
            self.safety(False)
            for stage_index, target_voltage in enumerate(stages, start=1):
                output_value = self._voltage_to_output(target_voltage, supply_voltage)
                self.debug(
                    "initialize.stage.start",
                    actuator=actuator,
                    stage_index=stage_index,
                    target_voltage=target_voltage,
                    output_value=output_value,
                )
                stage_start = time.monotonic()
                next_phase = stage_start
                phase = 0
                while True:
                    now = time.monotonic()
                    stage_elapsed_s = now - stage_start
                    elapsed_s = now - start
                    if stage_elapsed_s >= stage_duration_s:
                        break

                    if now >= next_phase:
                        if phase % 2 == 0:
                            self.set_manual_output(actuator, output_value, 0)
                            phase_name = "positive"
                        else:
                            self.set_manual_output(actuator, 0, output_value)
                            phase_name = "negative"
                        self.debug(
                            "initialize.phase",
                            actuator=actuator,
                            stage_index=stage_index,
                            phase=phase_name,
                            output_value=output_value,
                        )
                        phase += 1
                        next_phase = now + phase_interval_s

                    whole_second = int(elapsed_s)
                    if progress_callback is not None and whole_second > next_report_second:
                        progress_callback(
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
            self.set_manual_output(actuator, 0, 0)
        finally:
            try:
                self.set_manual_output(actuator, 0, 0)
            finally:
                self.safety(True)
                self.debug("initialize.safety_restored", actuator=actuator)

        if progress_callback is not None:
            progress_callback(
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

        diagnosis = self.diagnose_actuator(actuator)
        detection = self._record_detection(diagnosis)
        self.debug("initialize.done", detection=detection)
        return detection

    def diagnose_actuator(self, actuator: int) -> Diagnosis:
        self._validate_actuator(actuator)
        self.debug("diagnose.start", actuator=actuator)
        response = self.raw_command("DIA", actuator)[0]
        diagnosis = Diagnosis(
            actuator=int(response.fields["ACT"]),
            baseline_ma=float(response.fields["BASE"]),
            forward_ma=float(response.fields["FWD"]),
            discharge_ma=float(response.fields["DIS"]),
        )
        self.debug("diagnose.done", diagnosis=diagnosis)
        return diagnosis

    def runtime(self, actuator: int | None = None) -> int | tuple[int, ...]:
        if actuator is None:
            response = self.raw_command("TIM")[0]
            return tuple(int(value) for value in response.values)
        self._validate_actuator(actuator)
        response = self.raw_command("TIM", actuator)[0]
        if len(response.values) >= 2:
            return int(response.values[1])
        return int(response.payload)

    def reset_runtimes(self) -> None:
        self.debug("reset_runtimes")
        self.raw_command("RST")

    def reboot(self) -> None:
        self.debug("reboot")
        self.raw_command("RBT")

    def config(self, key: str, value: object | None = None) -> str:
        key = key.upper()
        self.debug("config", key=key, value=value)
        params = (key,) if value is None else (key, value)
        response = self.raw_command("CFG", *params)[0]
        if response.fields:
            return next(iter(response.fields.values()))
        return response.payload

    def max_active_time_ms(self, value: int | None = None) -> int:
        if value is not None and value < 0:
            raise ValueError("max active time must be >= 0")
        if value is None:
            return int(self.config("MAX"))
        self.config("MAX", value)
        return value

    def discharge_time_ms(self, value: int | None = None) -> int:
        if value is not None and value < 0:
            raise ValueError("discharge time must be >= 0")
        if value is None:
            return int(self.config("DIS"))
        self.config("DIS", value)
        return value

    def safety(self, enabled: Boolish | None = None) -> bool:
        if enabled is None:
            return self._state_to_bool(self.config("SAFE"))
        self.config("SAFE", self._bool_config_value(enabled))
        return self._state_to_bool(enabled)

    def firmware_debug(self, enabled: Boolish | None = None) -> bool:
        if enabled is None:
            return self._state_to_bool(self.config("DEBUG"))
        self.config("DEBUG", self._bool_config_value(enabled))
        return self._state_to_bool(enabled)

    def enable_firmware_debug(self) -> None:
        self.firmware_debug(True)

    def disable_firmware_debug(self) -> None:
        self.firmware_debug(False)

    def read_config(self) -> LansingConfig:
        return LansingConfig(
            max_active_ms=self.max_active_time_ms(),
            discharge_ms=self.discharge_time_ms(),
            safe=self.safety(),
            debug=self.firmware_debug(),
        )

    def status(self) -> dict[str, object]:
        self.debug("status.start")
        responses = self.raw_command("STS", ok_lines=7)
        summary = responses[0].fields
        required_summary_fields = {
            "PSU",
            "PSC",
            "VLT",
            "CUR",
            "CFG_MAX",
            "CFG_DIS",
            "SAFE",
            "DEBUG",
            "STREAM",
        }
        missing_summary_fields = required_summary_fields.difference(summary)
        if missing_summary_fields:
            raw_lines = " | ".join(response.raw for response in responses)
            missing = ", ".join(sorted(missing_summary_fields))
            raise ProtocolError(
                f"STS response missing fields {missing}; received: {raw_lines}"
            )
        output_fields = self._fields_from_payload(
            responses[2].payload.removeprefix("OUT_VALUES>")
        )
        status = {
            "psu": summary["PSU"],
            "psc": summary["PSC"],
            "voltage": float(summary["VLT"]),
            "current": float(summary["CUR"]),
            "config": {
                "max_active_ms": int(summary["CFG_MAX"]),
                "discharge_ms": int(summary["CFG_DIS"]),
                "safe": summary["SAFE"],
                "debug": summary["DEBUG"],
            },
            "stream": summary["STREAM"],
            "actuator_values": self._int_tuple(responses[1].payload.removeprefix("ACT_VALUES>")),
            "manual_outputs": self._manual_outputs_from_fields(output_fields),
            "actuator_states": self._int_tuple(responses[3].payload.removeprefix("ACT_STATES>")),
            "active_ms": self._int_tuple(responses[4].payload.removeprefix("ACTIVE_MS>")),
            "total_ms": self._int_tuple(responses[5].payload.removeprefix("TOTAL_MS>")),
            "discharge_ms_left": self._int_tuple(
                responses[6].payload.removeprefix("DISCHARGE_MS_LEFT>")
            ),
        }
        self.debug(
            "status.done",
            psu=status["psu"],
            psc=status["psc"],
            voltage=status["voltage"],
            current=status["current"],
            stream=status["stream"],
        )
        return status

    def enter_stream_mode(self) -> None:
        self.debug("stream.enter")
        self.raw_command("STR")

    def stream_actuator(self, actuator: int, value: int) -> None:
        self._validate_actuator(actuator)
        self._validate_output(value)
        self.debug("stream.actuator", actuator=actuator, value=value)
        self.transport.write_bytes(bytes([actuator, value]))

    def stream_values(self, values: dict[int, int]) -> None:
        for actuator, value in values.items():
            self.stream_actuator(actuator, value)

    def stream_sine(
        self,
        actuator: int,
        *,
        duration_s: float,
        frequency_hz: float,
        update_hz: float = 100.0,
        minimum: int = 1,
        maximum: int = 255,
        discharge_on_finish: bool = True,
        value_callback: Callable[[int, int, float], None] | None = None,
    ) -> float:
        self._validate_actuator(actuator)
        self._validate_output(minimum)
        self._validate_output(maximum)
        if minimum > maximum:
            raise ValueError("minimum must be <= maximum")
        if duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if frequency_hz < 0:
            raise ValueError("frequency_hz must be >= 0")
        if update_hz <= 0:
            raise ValueError("update_hz must be > 0")

        self.debug(
            "stream_sine.start",
            actuator=actuator,
            duration_s=duration_s,
            frequency_hz=frequency_hz,
            update_hz=update_hz,
            minimum=minimum,
            maximum=maximum,
        )
        interval_s = 1.0 / update_hz
        midpoint = (minimum + maximum) / 2.0
        amplitude = (maximum - minimum) / 2.0
        start = time.perf_counter()
        next_update = start
        updates = 0

        while True:
            now = time.perf_counter()
            elapsed = now - start
            if elapsed >= duration_s:
                break
            value = round(midpoint + amplitude * math.sin(2.0 * math.pi * frequency_hz * elapsed))
            output_value = int(value)
            self.stream_actuator(actuator, output_value)
            if value_callback is not None:
                value_callback(actuator, output_value, elapsed)
            updates += 1
            next_update += interval_s
            sleep_s = next_update - time.perf_counter()
            if sleep_s > 0:
                time.sleep(sleep_s)

        if discharge_on_finish:
            self.stream_actuator(actuator, 0)
            if value_callback is not None:
                value_callback(actuator, 0, time.perf_counter() - start)
            updates += 1

        actual_duration = time.perf_counter() - start
        rate = updates / actual_duration if actual_duration > 0 else 0.0
        self.debug("stream_sine.done", actuator=actuator, updates=updates, rate_hz=rate)
        return rate

    def exit_stream_mode(self) -> None:
        self.debug("stream.exit")
        self.transport.write_bytes(bytes([255, 0]))

    def flush_debug_lines(self) -> tuple[str, ...]:
        lines = tuple(self.protocol.debug_lines)
        self.protocol.debug_lines.clear()
        self.debug("firmware_debug.flush", lines=lines)
        return lines

    def _record_detection(self, diagnosis: Diagnosis) -> ActuatorDetection:
        detection = self._detection_from_diagnosis(diagnosis)
        self._actuator_states[diagnosis.actuator] = detection.state
        self._actuator_detections[diagnosis.actuator] = detection
        self.debug("actuator_state.updated", actuator=diagnosis.actuator, state=detection.state.value)
        return detection

    @classmethod
    def _detection_from_diagnosis(cls, diagnosis: Diagnosis) -> ActuatorDetection:
        delta_ma = round(abs(diagnosis.forward_ma - diagnosis.baseline_ma), 6)
        if delta_ma < cls.not_connected_delta_ma:
            state = ActuatorState.NOT_CONNECTED
        elif delta_ma > cls.error_delta_ma:
            state = ActuatorState.ERROR
        else:
            state = ActuatorState.READY
        return ActuatorDetection(
            actuator=diagnosis.actuator,
            state=state,
            baseline_ma=diagnosis.baseline_ma,
            forward_ma=diagnosis.forward_ma,
            discharge_ma=diagnosis.discharge_ma,
            delta_ma=delta_ma,
        )

    @staticmethod
    def _voltage_to_output(target_voltage: float, supply_voltage: float) -> int:
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(1, min(Lansing.max_output, int(Lansing.max_output * ratio)))

    @staticmethod
    def _optional_bool_param(state: Boolish | None) -> tuple[object, ...]:
        if state is None:
            return ()
        if state is True:
            return ("ON",)
        if state is False:
            return ("OFF",)
        return (state,)

    @staticmethod
    def _bool_config_value(state: Boolish | None) -> object | None:
        if state is None:
            return None
        if state is True:
            return "ON"
        if state is False:
            return "OFF"
        return state

    @staticmethod
    def _state_to_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().upper()
        if text in {"ON", "1"}:
            return True
        if text in {"OFF", "0"}:
            return False
        raise ValueError(f"Cannot convert state to bool: {value!r}")

    @classmethod
    def _validate_actuator(cls, actuator: int) -> None:
        if not isinstance(actuator, int) or not 0 <= actuator < cls.actuator_count:
            raise ValueError(f"actuator must be 0..{cls.actuator_count - 1}")

    @classmethod
    def _validate_output(cls, value: int) -> None:
        if not isinstance(value, int) or not cls.min_output <= value <= cls.max_output:
            raise ValueError(f"value must be {cls.min_output}..{cls.max_output}")

    @staticmethod
    def _int_tuple(payload: str) -> tuple[int, ...]:
        if not payload:
            return ()
        return tuple(int(value) for value in payload.split(","))

    @staticmethod
    def _fields_from_payload(payload: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for item in payload.split(","):
            if ">" in item:
                key, value = item.split(">", 1)
                fields[key] = value
        return fields

    @classmethod
    def _manual_outputs_from_fields(cls, fields: dict[str, str]) -> dict[int, tuple[int, int]]:
        outputs: dict[int, tuple[int, int]] = {}
        for actuator in range(cls.actuator_count):
            outputs[actuator] = (
                int(fields[f"A{actuator}P"]),
                int(fields[f"A{actuator}N"]),
            )
        return outputs
