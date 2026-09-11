"""Shared Fluid Reality board command and actuator implementation."""

from __future__ import annotations

import hashlib
import math
import struct
import time
import zlib
from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from logging import Logger
from typing import Literal

from ..protocol import LineTransport, ProtocolError, Response
from ..transport import SerialTransport
from .base import TransportBoard
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
    PRESENT = "Present"
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
    initial_forward_ma: float | None = None
    initial_delta_ma: float | None = None


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


@dataclass(frozen=True)
class FirmwareUpdateResult:
    path: Path
    size: int
    sha256: str


class Board(TransportBoard):
    """Common command and actuator functionality for Fluid Reality boards."""

    actuator_count = 0
    default_timeout_s = 45.0
    min_output = 0
    max_output = 255
    not_connected_delta_ma = 0.1
    error_delta_ma = 3.0
    initial_detection_error_delta_ma = 10.0
    initial_detection_duration_s = 0.25
    conditioned_detection_duration_s = 2.0
    initialization_stages_v = (25.0, 50.0, 100.0, 200.0)
    initialization_stage_duration_s = 30.0
    initialization_phase_interval_s = 0.5
    direct_top_bottom_output = False

    @classmethod
    def from_connection_file(cls, path: str | Path, **overrides):
        """Open this board using a Fluid Reality YAML connection profile."""

        from ..connection import open_board_from_connection_file

        return open_board_from_connection_file(cls, path, **overrides)

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
            if port.lower().startswith("ble://"):
                from ..bluetooth import BluetoothTransport

                transport = BluetoothTransport(
                    port,
                    timeout=timeout,
                    **serial_kwargs,
                )
            else:
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
        self.power_connection_supported: bool | None = None

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

    def update_firmware(
        self,
        path: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        should_abort: Callable[[], bool] | None = None,
    ) -> FirmwareUpdateResult:
        """Install an ESP32 application image through USB serial or TCP/TLS.

        The board writes framed chunks to its inactive OTA slot. Each frame is
        protected by CRC-32 and the board verifies the complete SHA-256 before
        selecting the image for the next boot.
        """

        image_path = Path(path).expanduser().resolve()
        if not image_path.is_file():
            raise FileNotFoundError(f"Firmware image not found: {image_path}")
        endpoint = str(getattr(self.transport, "endpoint", ""))
        if endpoint.lower().startswith("ble://") or "bluetooth" in type(self.transport).__name__.lower():
            raise ValueError("Firmware update is available over USB serial or TCP/TLS, not Bluetooth.")

        image_size = image_path.stat().st_size
        if image_size <= 0:
            raise ValueError("Firmware image is empty.")
        digest = hashlib.sha256()
        with image_path.open("rb") as image:
            for chunk in iter(lambda: image.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()

        ready = self.raw_command("FWU", "BEGIN", image_size, sha256)[0]
        frame_size = int(ready.fields.get("FRAME", "1024"))
        if frame_size <= 0 or frame_size > 4096:
            raise ProtocolError(f"Invalid firmware-update frame size: {frame_size}")

        written = 0
        sequence = 0
        if progress is not None:
            progress(0, image_size)
        try:
            with image_path.open("rb") as image:
                while chunk := image.read(frame_size):
                    if should_abort is not None and should_abort():
                        abort_header = struct.pack("<IH", 0xFFFFFFFF, 0)
                        self.transport.write_bytes(abort_header)
                        self.protocol.read_result()
                        raise RuntimeError("Firmware update cancelled.")
                    header = struct.pack("<IH", sequence, len(chunk))
                    crc = zlib.crc32(header + chunk) & 0xFFFFFFFF
                    self.transport.write_bytes(header + chunk + struct.pack("<I", crc))
                    response = self.protocol.read_result()[0]
                    acknowledged = int(response.fields.get("SEQ", "-1"))
                    board_written = int(response.fields.get("WRITTEN", "-1"))
                    if acknowledged != sequence or board_written != written + len(chunk):
                        raise ProtocolError(
                            f"Invalid firmware-update acknowledgement: {response.raw!r}"
                        )
                    written = board_written
                    sequence += 1
                    if progress is not None:
                        progress(written, image_size)
            completed = self.raw_command("FWU", "END")[0]
            if completed.fields.get("STATE") != "VERIFIED":
                raise ProtocolError(f"Firmware was not verified: {completed.raw!r}")
        except Exception:
            if written < image_size:
                try:
                    self.raw_command("FWU", "ABORT")
                except Exception:
                    pass
            raise
        return FirmwareUpdateResult(path=image_path, size=image_size, sha256=sha256)

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

    def capabilities(self) -> dict[str, str]:
        """Return the feature flags reported by the firmware ``CAP`` command."""

        return self.raw_command("CAP")[0].fields

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
        if self.power_connection_supported is False and state is not None:
            return "NONE"
        self.debug("connect_power", requested_state=state)
        response = self.raw_command("PSC", *self._optional_bool_param(state))[0]
        result = response.values[0] if response.values else response.payload
        if state is None:
            self.power_connection_supported = result.strip().upper() != "NONE"
        return result

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

    def manual_output_current(
        self,
        actuator: int,
        top: int,
        bottom: int,
        measurement_ms: int,
    ) -> float:
        """Set Rockford TOP/BOTTOM output and measure current over an interval."""
        self._validate_actuator(actuator)
        self._validate_output(top)
        if not isinstance(bottom, int) or isinstance(bottom, bool) or bottom not in (0, 1):
            raise ValueError("bottom must be 0 or 1")
        if measurement_ms < 1:
            raise ValueError("measurement_ms must be >= 1")

        self.debug(
            "manual_output_current.request",
            actuator=actuator,
            top=top,
            bottom=bottom,
            measurement_ms=measurement_ms,
        )
        response = self.raw_command(
            "OUC", actuator, top, bottom, measurement_ms
        )[0]
        try:
            current_ma = float(response.fields["CUR"])
            reported_time_ms = int(response.fields["TIME"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(f"Invalid OUC response: {response.raw}") from exc
        if reported_time_ms != measurement_ms:
            raise ProtocolError(
                "OUC measurement interval mismatch: "
                f"requested {measurement_ms} ms, received {reported_time_ms} ms"
            )
        self.debug(
            "manual_output_current.done",
            actuator=actuator,
            current_ma=current_ma,
            measurement_ms=reported_time_ms,
        )
        return current_ma

    def _initialization_output_current(
        self,
        actuator: int,
        output_value: int,
        phase: str,
        measurement_ms: int,
    ) -> float:
        if self.direct_top_bottom_output:
            if phase == "positive":
                top, bottom = output_value, 0
            elif phase == "negative":
                top, bottom = self.max_output - output_value, 1
            else:
                top, bottom = 0, 0
            return self.manual_output_current(
                actuator, top, bottom, measurement_ms
            )
        self._set_initialization_output(actuator, output_value, phase)
        return self.current()

    def _set_initialization_output(
        self, actuator: int, output_value: int, phase: str
    ) -> None:
        if phase == "positive":
            self.set_manual_output(actuator, output_value, 0)
        elif phase == "negative":
            self.set_manual_output(actuator, 0, output_value)
        else:
            self.set_manual_output(actuator, 0, 0)

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

    def detect_actuator_firmware(self, actuator: int) -> ActuatorDetection:
        """Run the firmware's initial detection stage for one actuator."""

        self._validate_actuator(actuator)
        response = self.raw_command("DT0", actuator)[0]
        return self._detection_from_firmware_response(response, actuator)

    def detect_actuator_firmware_conditioned(
        self, actuator: int
    ) -> ActuatorDetection:
        """Run the firmware's conditioned detection stage after a DT0 result."""

        self._validate_actuator(actuator)
        response = self.raw_command("DT1", actuator)[0]
        return self._detection_from_firmware_response(response, actuator)

    def detect_all_firmware(
        self,
        progress_callback: Callable[[ActuatorDetection], None] | None = None,
    ) -> tuple[ActuatorDetection, ...]:
        """Run firmware batch detection and publish each result as it arrives."""

        self.debug("detect_firmware.start", actuator_count=self.actuator_count)
        self.transport.write_line("DT0")
        baseline_response = self.protocol.read_result(ok_lines=1)[0]
        if "BASE" not in baseline_response.fields:
            raise ProtocolError("DT0 batch response did not begin with BASE")
        try:
            baseline_ma = float(baseline_response.fields["BASE"])
        except ValueError as exc:
            raise ProtocolError("DT0 BASE must be a current in mA") from exc

        detections: list[ActuatorDetection] = []
        for expected_actuator in range(self.actuator_count):
            response = self.protocol.read_result(ok_lines=1)[0]
            detection = self._detection_from_firmware_response(
                response, expected_actuator, expected_baseline_ma=baseline_ma
            )
            detections.append(detection)
            if progress_callback is not None:
                progress_callback(detection)

        final_detections = list(detections)
        for detection in detections:
            if detection.state is not ActuatorState.PRESENT:
                continue
            conditioned = self.detect_actuator_firmware_conditioned(
                detection.actuator
            )
            final_detections[detection.actuator] = conditioned
            if progress_callback is not None:
                progress_callback(conditioned)
        self.debug("detect_firmware.done", detections=tuple(final_detections))
        return tuple(final_detections)

    def detect_actuator(
        self,
        actuator: int,
        *,
        baseline_ma: float | None = None,
    ) -> ActuatorDetection:
        """Detect one actuator, optionally reusing a previously measured baseline."""

        self._validate_actuator(actuator)
        self.debug(
            "detect.start",
            actuator=actuator,
            initial_duration_s=self.initial_detection_duration_s,
            conditioned_duration_s=self.conditioned_detection_duration_s,
        )

        previous_safety = self.safety()
        initial_forward_ma = 0.0
        conditioned_forward_ma: float | None = None
        try:
            self.safety(False)
            # OUT resets both normal activation and discharge state. Zero every
            # actuator so the target is the only energized channel in both stages.
            for other_actuator in range(self.actuator_count):
                self.set_manual_output(other_actuator, 0, 0)
            if baseline_ma is None:
                baseline_ma = self.current()

            # Keep the same positive electrode energized continuously across both
            # measurements. Do not send ACT 0, which would start reverse discharge.
            self.set_manual_output(actuator, self.max_output, 0)
            time.sleep(self.initial_detection_duration_s)
            initial_forward_ma = self.current()
            initial_increase_ma = initial_forward_ma - baseline_ma
            initial_delta_ma = abs(initial_increase_ma)
            self.debug(
                "detect.initial",
                actuator=actuator,
                baseline_ma=baseline_ma,
                forward_ma=initial_forward_ma,
                delta_ma=initial_delta_ma,
            )

            if (
                initial_increase_ma >= self.not_connected_delta_ma
                and initial_increase_ma <= self.initial_detection_error_delta_ma
            ):
                time.sleep(self.conditioned_detection_duration_s)
                conditioned_forward_ma = self.current()
        finally:
            try:
                self.set_manual_output(actuator, 0, 0)
            finally:
                self.safety(previous_safety)
                self.debug("detect.safety_restored", actuator=actuator, enabled=previous_safety)

        if baseline_ma is None:  # Defensive: the measurement above always assigns it.
            raise RuntimeError("Detection baseline was not measured")
        initial_delta_ma = round(abs(initial_forward_ma - baseline_ma), 6)
        if conditioned_forward_ma is None:
            final_forward_ma = initial_forward_ma
            final_delta_ma = initial_delta_ma
            state = (
                ActuatorState.NOT_CONNECTED
                if initial_forward_ma <= baseline_ma
                or initial_delta_ma < self.not_connected_delta_ma
                else ActuatorState.ERROR
            )
        else:
            final_forward_ma = conditioned_forward_ma
            final_delta_ma = round(abs(conditioned_forward_ma - baseline_ma), 6)
            if (
                final_forward_ma <= baseline_ma
                or final_delta_ma < self.not_connected_delta_ma
            ):
                state = ActuatorState.NOT_CONNECTED
            elif final_delta_ma < self.error_delta_ma:
                state = ActuatorState.READY
            else:
                state = ActuatorState.ERROR

        detection = ActuatorDetection(
            actuator=actuator,
            state=state,
            baseline_ma=baseline_ma,
            forward_ma=final_forward_ma,
            discharge_ma=0.0,
            delta_ma=final_delta_ma,
            initial_forward_ma=initial_forward_ma,
            initial_delta_ma=initial_delta_ma,
        )
        self._actuator_states[actuator] = state
        self._actuator_detections[actuator] = detection
        self.debug("detect.done", detection=detection)
        return detection

    def initialize(
        self,
        actuator: int,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
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
        progress_callback: Callable[[dict[str, object]], None] | None = None,
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
        try:
            self.safety(False)
            measurement_ms = max(1, round(phase_interval_s * 1000))
            baseline_current_ma = self._initialization_output_current(
                actuator, 0, "off", measurement_ms
            )
            if progress_callback is not None:
                progress_callback(
                    {
                        "actuator": actuator,
                        "elapsed_s": 0.0,
                        "total_s": total_duration_s,
                        "stage_index": 0,
                        "stage_count": len(stages),
                        "stage_voltage": 0.0,
                        "sent_voltage": 0.0,
                        "phase": "baseline",
                        "phase_interval_s": phase_interval_s,
                        "output_value": 0,
                    }
                )
            start = time.monotonic()
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
                    if stage_elapsed_s >= stage_duration_s:
                        break

                    if now >= next_phase:
                        if phase % 2 == 0:
                            phase_name = "positive"
                            sent_voltage = target_voltage
                        else:
                            phase_name = "negative"
                            sent_voltage = -target_voltage
                        current_ma = self._initialization_output_current(
                            actuator, output_value, phase_name, measurement_ms
                        )
                        sent_elapsed_s = min(
                            time.monotonic() - start, total_duration_s
                        )
                        self.debug(
                            "initialize.phase",
                            actuator=actuator,
                            stage_index=stage_index,
                            phase=phase_name,
                            output_value=output_value,
                        )
                        if progress_callback is not None:
                            progress = {
                                "actuator": actuator,
                                "elapsed_s": sent_elapsed_s,
                                "total_s": total_duration_s,
                                "stage_index": stage_index,
                                "stage_count": len(stages),
                                "stage_voltage": target_voltage,
                                "sent_voltage": sent_voltage,
                                "phase": phase_name,
                                "phase_interval_s": phase_interval_s,
                                "output_value": output_value,
                            }
                            if phase_name == "positive":
                                progress.update(
                                    {
                                        "baseline_current_ma": baseline_current_ma,
                                        "current_ma": current_ma,
                                        "delta_ma": current_ma - baseline_current_ma,
                                    }
                                )
                            progress_callback(progress)
                        phase += 1
                        next_phase = now + phase_interval_s
                    time.sleep(0.05)
            self._initialization_output_current(
                actuator, 0, "off", measurement_ms
            )
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
                    "sent_voltage": 0.0,
                    "phase": "off",
                    "phase_interval_s": phase_interval_s,
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

    @staticmethod
    def _validate_positive_threshold(value: float, name: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be greater than zero")
        return parsed

    def detection_current_limit_ma(self, value: float | None = None) -> float:
        if value is None:
            return float(self.config("DET_MIN"))
        parsed = self._validate_positive_threshold(value, "detection current limit")
        self.config("DET_MIN", parsed)
        self.not_connected_delta_ma = parsed
        return parsed

    def dt0_error_threshold_ma(self, value: float | None = None) -> float:
        if value is None:
            return float(self.config("DT0_ERR"))
        parsed = self._validate_positive_threshold(value, "DT0 error threshold")
        self.config("DT0_ERR", parsed)
        self.initial_detection_error_delta_ma = parsed
        return parsed

    def dt1_error_threshold_ma(self, value: float | None = None) -> float:
        if value is None:
            return float(self.config("DT1_ERR"))
        parsed = self._validate_positive_threshold(value, "DT1 error threshold")
        self.config("DT1_ERR", parsed)
        self.error_delta_ma = parsed
        return parsed

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
        responses = [self.protocol.command("STS", ok_lines=1)[0]]
        for _ in range(15):
            if responses[-1].payload.startswith("DISCHARGE_MS_LEFT>"):
                break
            responses.append(self.protocol.read_result(ok_lines=1)[0])
        else:
            raise ProtocolError("STS response did not end with DISCHARGE_MS_LEFT")

        sections = {
            response.payload.split(">", 1)[0]: response
            for response in responses
            if ">" in response.payload
        }
        required_sections = {
            "PSU", "ACT_VALUES", "OUT_VALUES", "ACT_STATES", "ACTIVE_MS",
            "TOTAL_MS", "DISCHARGE_MS_LEFT",
        }
        missing_sections = required_sections.difference(sections)
        if missing_sections:
            received = " | ".join(response.raw for response in responses)
            raise ProtocolError(
                f"STS response missing sections {', '.join(sorted(missing_sections))}; "
                f"received: {received}"
            )

        summary = sections["PSU"].fields
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
        detection_limit = self.not_connected_delta_ma
        if "DET_MIN" in summary:
            try:
                detection_limit = float(summary["DET_MIN"])
            except ValueError as exc:
                raise ProtocolError("STS DET_MIN must be a number in mA") from exc
            if not math.isfinite(detection_limit) or detection_limit <= 0:
                raise ProtocolError("STS DET_MIN must be greater than zero")
            self.not_connected_delta_ma = detection_limit
        if "DT0_ERR" in summary:
            self.initial_detection_error_delta_ma = float(summary["DT0_ERR"])
        if "DT1_ERR" in summary:
            self.error_delta_ma = float(summary["DT1_ERR"])
        actuator_values = self._int_tuple(
            sections["ACT_VALUES"].payload.removeprefix("ACT_VALUES>")
        )
        actuator_count = len(actuator_values)
        if actuator_count < 1:
            raise ProtocolError("STS ACT_VALUES did not contain any actuators")

        actuator_states = self._int_tuple(
            sections["ACT_STATES"].payload.removeprefix("ACT_STATES>")
        )
        active_ms = self._int_tuple(
            sections["ACTIVE_MS"].payload.removeprefix("ACTIVE_MS>")
        )
        total_ms = self._int_tuple(
            sections["TOTAL_MS"].payload.removeprefix("TOTAL_MS>")
        )
        discharge_ms_left = self._int_tuple(
            sections["DISCHARGE_MS_LEFT"].payload.removeprefix("DISCHARGE_MS_LEFT>")
        )
        balance_response = sections.get("BALANCE_MS")
        balance_ms = (
            self._int_tuple(balance_response.payload.removeprefix("BALANCE_MS>"))
            if balance_response is not None else (0,) * actuator_count
        )
        arrays = {
            "ACT_STATES": actuator_states, "ACTIVE_MS": active_ms,
            "BALANCE_MS": balance_ms, "TOTAL_MS": total_ms,
            "DISCHARGE_MS_LEFT": discharge_ms_left,
        }
        mismatched = [name for name, values in arrays.items() if len(values) != actuator_count]
        if mismatched:
            raise ProtocolError("STS actuator-array length mismatch for " + ", ".join(mismatched))

        output_fields = self._fields_from_payload(
            sections["OUT_VALUES"].payload.removeprefix("OUT_VALUES>")
        )
        try:
            manual_outputs = {
                actuator: (
                    int(output_fields[f"A{actuator}P"]),
                    int(output_fields[f"A{actuator}N"]),
                )
                for actuator in range(actuator_count)
            }
        except KeyError as exc:
            raise ProtocolError(f"STS OUT_VALUES missing field {exc.args[0]}") from exc

        self.actuator_count = actuator_count
        if len(self._actuator_states) != actuator_count:
            previous = self._actuator_states[:actuator_count]
            self._actuator_states = previous + [ActuatorState.UNKNOWN] * (
                actuator_count - len(previous)
            )
            self._actuator_detections = {
                actuator: detection
                for actuator, detection in self._actuator_detections.items()
                if actuator < actuator_count
            }

        status = {
            "actuator_count": actuator_count,
            "psu": summary["PSU"],
            "psc": (
                "NONE"
                if self.power_connection_supported is False
                else summary["PSC"]
            ),
            "voltage": float(summary["VLT"]),
            "current": float(summary["CUR"]),
            "config": {
                "max_active_ms": int(summary["CFG_MAX"]),
                "discharge_ms": int(summary["CFG_DIS"]),
                "safe": summary["SAFE"],
                "debug": summary["DEBUG"],
                **(
                    {
                        "detection_current_limit_ma": detection_limit,
                        "dt0_error_threshold_ma": self.initial_detection_error_delta_ma,
                        "dt1_error_threshold_ma": self.error_delta_ma,
                    }
                    if "DET_MIN" in summary
                    else {}
                ),
            },
            "stream": summary["STREAM"],
            "detection_current_limit_ma": detection_limit,
            "actuator_values": actuator_values,
            "manual_outputs": manual_outputs,
            "actuator_states": actuator_states,
            "active_ms": active_ms,
            "balance_ms": balance_ms,
            "total_ms": total_ms,
            "discharge_ms_left": discharge_ms_left,
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

    def _detection_from_firmware_response(
        self,
        response: Response,
        expected_actuator: int,
        *,
        expected_baseline_ma: float | None = None,
    ) -> ActuatorDetection:
        required = {"ACT", "BASE", "FWD", "DELTA", "STATE"}
        missing = required.difference(response.fields)
        if missing:
            raise ProtocolError(
                "DT0 response missing fields " + ", ".join(sorted(missing))
            )
        try:
            actuator = int(response.fields["ACT"])
            baseline_ma = float(response.fields["BASE"])
            forward_ma = float(response.fields["FWD"])
            delta_ma = float(response.fields["DELTA"])
        except ValueError as exc:
            raise ProtocolError("DT0 response contains an invalid number") from exc
        if actuator != expected_actuator:
            raise ProtocolError(
                f"DT0 returned actuator {actuator}; expected {expected_actuator}"
            )
        if expected_baseline_ma is not None and not math.isclose(
            baseline_ma, expected_baseline_ma, abs_tol=0.011
        ):
            raise ProtocolError("DT0 actuator result does not match the shared baseline")

        state_name = response.fields["STATE"].upper()
        states = {
            "PRESENT": ActuatorState.PRESENT,
            "READY": ActuatorState.READY,
            "NOT_CONNECTED": ActuatorState.NOT_CONNECTED,
            "ERROR": ActuatorState.ERROR,
        }
        if state_name not in states:
            raise ProtocolError(f"DT0 returned unknown state {state_name!r}")
        detection = ActuatorDetection(
            actuator=actuator,
            state=states[state_name],
            baseline_ma=baseline_ma,
            forward_ma=forward_ma,
            discharge_ma=0.0,
            delta_ma=delta_ma,
            initial_forward_ma=forward_ma,
            initial_delta_ma=delta_ma,
        )
        self._actuator_states[actuator] = detection.state
        self._actuator_detections[actuator] = detection
        return detection

    def _detection_from_diagnosis(self, diagnosis: Diagnosis) -> ActuatorDetection:
        delta_ma = round(abs(diagnosis.forward_ma - diagnosis.baseline_ma), 6)
        if diagnosis.forward_ma <= diagnosis.baseline_ma:
            state = ActuatorState.NOT_CONNECTED
        elif delta_ma < self.not_connected_delta_ma:
            state = ActuatorState.NOT_CONNECTED
        elif delta_ma > self.error_delta_ma:
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

    @classmethod
    def _voltage_to_output(cls, target_voltage: float, supply_voltage: float) -> int:
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(1, min(cls.max_output, int(cls.max_output * ratio)))

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

    def _validate_actuator(self, actuator: int) -> None:
        if not isinstance(actuator, int) or not 0 <= actuator < self.actuator_count:
            raise ValueError(f"actuator must be 0..{self.actuator_count - 1}")

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
