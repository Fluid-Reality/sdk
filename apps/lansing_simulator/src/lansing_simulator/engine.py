"""Byte-oriented firmware protocol engine."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .clock import Clock, ScaledClock
from .config import SimulatorConfig
from .event_log import EventLogger, NullEventLogger
from .model import ActuatorState, LansingBoardModel

_SEPARATORS = re.compile(r"[ \t,]+")
_INTEGER = re.compile(r"[+-]?\d+\Z")


class LansingSimulator:
    """Emulate Lansing firmware 0.1 at its raw byte boundary."""

    def __init__(
        self,
        config: SimulatorConfig | None = None,
        *,
        clock: Clock | None = None,
        logger: EventLogger | NullEventLogger | None = None,
    ) -> None:
        self.config = config or SimulatorConfig()
        self.clock = clock or ScaledClock(self.config.simulation.time_scale)
        self.logger = logger or EventLogger(self.config)
        self.board = LansingBoardModel(self.config, self.clock, self.logger)
        self._line_buffer = bytearray()
        self._stream_waiting_for_value = False
        self._stream_actuator = 0
        self._deferred_input = bytearray()

    def startup_bytes(self) -> bytes:
        return self._emit_lines(["OK:READY"], event="protocol.startup")

    def feed_bytes(self, data: bytes) -> bytes:
        """Consume host bytes and return bytes the simulated firmware transmitted."""

        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if self.config.logging.include_raw_bytes:
            self._log("protocol.rx", data_hex=data.hex(), size=len(data))
        pending = self._deferred_input + data
        self._deferred_input.clear()
        output: list[str] = self.board.service()
        index = 0
        while index < len(pending):
            incoming = pending[index]
            index += 1
            if self.board.binary_stream_mode:
                if not self._stream_waiting_for_value:
                    if incoming == 255:
                        self.board.binary_stream_mode = False
                        if self.board.debug:
                            output.append("DBG:STREAM_MODE,MODE>TEXT")
                        # Firmware returns immediately; bytes already received remain unread.
                        remainder = pending[index:]
                        if (
                            not self.config.simulation.strict_firmware_0_1
                            and remainder[:1] == b"\x00"
                        ):
                            remainder = remainder[1:]
                        self._deferred_input.extend(remainder)
                        break
                    self._stream_actuator = incoming
                    self._stream_waiting_for_value = True
                    if self.board.debug:
                        output.append(f"DBG:STREAM_ACT,ACT>{incoming}")
                    continue
                if (
                    self._stream_actuator < self.board.actuator_count
                    and self.board.psu_on
                    and self.board.psc_on
                ):
                    self.board.set_forward(self._stream_actuator, incoming)
                    if self.board.debug:
                        output.append(
                            f"DBG:STREAM_VALUE,ACT>{self._stream_actuator},VALUE>{incoming}"
                        )
                elif self.board.debug:
                    output.append(
                        f"DBG:STREAM_REJECT,ACT>{self._stream_actuator},VALUE>{incoming},"
                        f"PSU>{'ON' if self.board.psu_on else 'OFF'},"
                        f"PSC>{'ON' if self.board.psc_on else 'OFF'}"
                    )
                self._stream_waiting_for_value = False
                output.extend(self.board.service())
                continue

            if incoming == 13:
                continue
            if incoming == 10:
                if self._line_buffer:
                    line = self._line_buffer.decode("ascii", errors="replace")
                    self._line_buffer.clear()
                    output.extend(self._execute_line(line))
                continue
            if len(self._line_buffer) >= 95:
                self._line_buffer.clear()
                if self.board.debug:
                    output.append("DBG:SERIAL_REJECT,REASON>LINE_TOO_LONG")
                output.append("ER:LINE_TOO_LONG")
                continue
            self._line_buffer.append(incoming)
        return self._emit_lines(output, event="protocol.tx")

    def tick(self) -> bytes:
        """Service time-dependent state without receiving a host command."""

        return self._emit_lines(self.board.service(), event="protocol.background")

    def close(self) -> None:
        """Flush persistent simulator state."""

        self.board.close()

    def __enter__(self) -> "LansingSimulator":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def execute_text(self, line: str) -> tuple[str, ...]:
        """Convenience harness for tests and local interactive use."""

        output = self.feed_bytes(line.encode("ascii") + b"\n")
        return tuple(item for item in output.decode("ascii").splitlines() if item)

    def _execute_line(self, line: str) -> list[str]:
        parsed = self._parse_line(line)
        if parsed is None:
            lines = []
            if self.board.debug:
                lines.append(f"DBG:PARSE_REJECT,LINE>{line}")
            lines.append("ER:BAD_COMMAND")
            return lines
        command, params, numbers = parsed
        self._log("protocol.command", command=command, params=params)
        lines: list[str] = []
        if self.board.debug:
            rendered = ",".join(f"PARAM{index}>{value}" for index, value in enumerate(params))
            lines.append(f"DBG:COMMAND>{command}" + (f",{rendered}" if rendered else ""))
        lines.extend(self._handle(command, params, numbers))
        return lines

    @staticmethod
    def _parse_line(line: str) -> tuple[str, list[str], list[int | None]] | None:
        tokens = [token for token in _SEPARATORS.split(line.strip(" \t,")) if token]
        if not tokens or len(tokens[0]) != 3 or len(tokens) - 1 > 8:
            return None
        if any(len(token) > 15 for token in tokens[1:]):
            return None
        command = tokens[0].upper()
        params = tokens[1:]
        numbers = [int(value) if _INTEGER.fullmatch(value) else None for value in params]
        return command, params, numbers

    def _handle(self, command: str, params: list[str], numbers: list[int | None]) -> list[str]:
        if command == "VER":
            if params:
                return ["ER:VER_PARAM_COUNT"]
            fw = self.config.firmware
            return [f"OK:FW>{fw.name},VERSION>{fw.version},PROTO>{fw.protocol}"]
        if command == "STR":
            if params:
                return ["ER:STR_PARAM_COUNT"]
            self.board.binary_stream_mode = True
            lines = ["DBG:STREAM_MODE,MODE>BIN"] if self.board.debug else []
            return lines + ["OK:STR"]
        if command == "RBT":
            if params:
                return ["ER:RBT_PARAM_COUNT"]
            self.clock.sleep(0.05)
            self.board.reboot()
            self._stream_waiting_for_value = False
            self._line_buffer.clear()
            return ["OK:RBT", "OK:READY"]
        if command == "PSU":
            if len(params) > 1:
                return ["ER:PSU_PARAM_COUNT"]
            if not params:
                return [f"OK:{'ON' if self.board.psu_on else 'OFF'}"]
            state = self._bool_param(params[0])
            if state is None:
                return ["ER:PSU_PARAM_VALUE"]
            self.board.set_psu(state)
            return [f"OK:PSU_{'ON' if state else 'OFF'}"]
        if command == "PSC":
            if len(params) > 1:
                return ["ER:PSC_PARAM_COUNT"]
            if not params:
                return [f"OK:{'ON' if self.board.psc_on else 'OFF'}"]
            state = self._bool_param(params[0])
            if state is None:
                return ["ER:PSC_PARAM_VALUE"]
            if state and not self.board.set_psc(True):
                return ["ER:PSC_PSU_OFF"]
            if not state:
                self.board.set_psc(False)
            return [f"OK:PSC_{'ON' if state else 'OFF'}"]
        if command == "CUR":
            if params:
                return ["ER:CUR_PARAM_COUNT"]
            return [f"OK:{self.board.measure_current(250):.2f}"]
        if command == "VLT":
            if len(params) > 1:
                return ["ER:VLT_PARAM_COUNT"]
            duration = 1
            if params:
                if numbers[0] is None or numbers[0] < 1:
                    return ["ER:VLT_PARAM_VALUE"]
                duration = numbers[0]
            return [f"OK:{self.board.measure_voltage(duration):.2f}"]
        if command == "DIA":
            error, actuator = self._one_actuator(params, numbers, "DIA")
            if error:
                return [error]
            if not self.board.psu_on:
                return ["ER:DIA_PSU_OFF"]
            if not self.board.psc_on:
                return ["ER:DIA_PSU_DISCONNECTED"]
            result = self.board.diagnose(actuator)
            if result is None:
                return ["ER:DIA_FAILED"]
            baseline, forward, discharge = result
            return [
                f"OK:ACT>{actuator},BASE>{baseline:.2f},FWD>{forward:.2f},DIS>{discharge:.2f}"
            ]
        if command == "INI":
            error, actuator = self._one_actuator(params, numbers, "INI")
            if error:
                return [error]
            if not self.board.psu_on:
                return ["ER:INI_PSU_OFF"]
            if not self.board.psc_on:
                return ["ER:INI_PSU_DISCONNECTED"]
            return ["OK:INI" if self.board.initialize_actuator(actuator) else "ER:INI_FAILED"]
        if command == "ACT":
            return self._handle_act(params, numbers)
        if command == "OUT":
            return self._handle_out(params, numbers)
        if command == "TIM":
            if len(params) > 1:
                return ["ER:TIM_PARAM_COUNT"]
            if not params:
                values = ",".join(str(self.board.runtime_ms(index)) for index in range(24))
                return [f"OK:{values}"]
            if numbers[0] is None or not 0 <= numbers[0] < 24:
                return ["ER:TIM_ACTUATOR"]
            actuator = numbers[0]
            return [f"OK:{actuator},{self.board.runtime_ms(actuator)}"]
        if command == "RST":
            if params:
                return ["ER:RST_PARAM_COUNT"]
            self.board.reset_runtimes()
            return ["OK:RST"]
        if command == "CFG":
            return self._handle_cfg(params, numbers)
        if command == "STS":
            if params:
                return ["ER:STS_PARAM_COUNT"]
            return self._status_lines()
        return [f"ER:UNKNOWN_COMMAND>{command}"]

    def _handle_act(self, params: list[str], numbers: list[int | None]) -> list[str]:
        if len(params) > 2:
            return ["ER:ACT_PARAM_COUNT"]
        if not params:
            return ["OK:" + ",".join(str(item.forward_value) for item in self.board.actuators)]
        if numbers[0] is None or not 0 <= numbers[0] < 24:
            return ["ER:ACT_ACTUATOR"]
        actuator = numbers[0]
        if len(params) == 1:
            return [f"OK:{actuator},{self.board.actuators[actuator].forward_value}"]
        if numbers[1] is None or not 0 <= numbers[1] <= 255:
            return ["ER:ACT_VALUE"]
        if not self.board.psu_on:
            return ["ER:ACT_PSU_OFF"]
        if not self.board.psc_on:
            return ["ER:ACT_PSU_DISCONNECTED"]
        if not self.board.set_forward(actuator, numbers[1]):
            return ["ER:ACT_FAILED"]
        return ["OK:ACT"]

    def _handle_out(self, params: list[str], numbers: list[int | None]) -> list[str]:
        if len(params) not in {0, 1, 3}:
            return ["ER:OUT_PARAM_COUNT"]
        if not params:
            fields: list[str] = []
            for index, item in enumerate(self.board.actuators):
                fields.extend((f"A{index}P>{item.positive}", f"A{index}N>{item.negative}"))
            return ["OK:" + ",".join(fields)]
        if numbers[0] is None or not 0 <= numbers[0] < 24:
            return ["ER:OUT_ACTUATOR"]
        actuator = numbers[0]
        if len(params) == 1:
            item = self.board.actuators[actuator]
            return [f"OK:ACT>{actuator},POS>{item.positive},NEG>{item.negative}"]
        if numbers[1] is None or not 0 <= numbers[1] <= 255:
            return ["ER:OUT_POS_VALUE"]
        if numbers[2] is None or not 0 <= numbers[2] <= 255:
            return ["ER:OUT_NEG_VALUE"]
        if self.board.safety:
            return ["ER:OUT_SAFETY_ON"]
        self.board.set_manual_output(actuator, numbers[1], numbers[2])
        return ["OK:OUT"]

    def _handle_cfg(self, params: list[str], numbers: list[int | None]) -> list[str]:
        if len(params) > 2:
            return ["ER:CFG_PARAM_COUNT"]
        if not params:
            return [
                f"OK:MAX>{self.board.max_active_ms},DIS>{self.board.discharge_ms},"
                f"SAFE>{'ON' if self.board.safety else 'OFF'},"
                f"DEBUG>{'ON' if self.board.debug else 'OFF'}"
            ]
        key = params[0].upper()
        if key not in {"MAX", "DIS", "SAFE", "DEBUG"}:
            return ["ER:CFG_KEY"]
        if len(params) == 1:
            values = {
                "MAX": str(self.board.max_active_ms),
                "DIS": str(self.board.discharge_ms),
                "SAFE": "ON" if self.board.safety else "OFF",
                "DEBUG": "ON" if self.board.debug else "OFF",
            }
            return [f"OK:{key}>{values[key]}"]
        if key in {"MAX", "DIS"}:
            if numbers[1] is None or numbers[1] < 0:
                return ["ER:CFG_VALUE"]
            if key == "MAX":
                self.board.max_active_ms = numbers[1]
            else:
                self.board.discharge_ms = numbers[1]
            self.board.persist()
            return [f"OK:CFG_{key}"]
        state = self._bool_param(params[1])
        if state is None:
            return ["ER:CFG_VALUE"]
        if key == "SAFE":
            self.board.safety = state
            return ["OK:CFG_SAFE"]
        lines: list[str] = []
        if state:
            self.board.debug = True
            lines.append("DBG:DEBUG_ON")
        else:
            if self.board.debug:
                lines.append("DBG:DEBUG_OFF")
            self.board.debug = False
        lines.append("OK:CFG_DEBUG")
        return lines

    def _status_lines(self) -> list[str]:
        voltage = self.board.measure_voltage(250)
        current = self.board.measure_current(250)
        self.board.service()
        summary = (
            f"OK:PSU>{'ON' if self.board.psu_on else 'OFF'},"
            f"PSC>{'ON' if self.board.psc_on else 'OFF'},VLT>{voltage:.2f},CUR>{current:.2f},"
            f"CFG_MAX>{self.board.max_active_ms},CFG_DIS>{self.board.discharge_ms},"
            f"SAFE>{'ON' if self.board.safety else 'OFF'},"
            f"DEBUG>{'ON' if self.board.debug else 'OFF'},"
            f"STREAM>{'BIN' if self.board.binary_stream_mode else 'TEXT'}"
        )
        act_values = "OK:ACT_VALUES>" + ",".join(
            str(item.forward_value) for item in self.board.actuators
        )
        output_fields: list[str] = []
        for index, item in enumerate(self.board.actuators):
            output_fields.extend((f"A{index}P>{item.positive}", f"A{index}N>{item.negative}"))
        outputs = "OK:OUT_VALUES>" + ",".join(output_fields)
        states = "OK:ACT_STATES>" + ",".join(
            str(int(item.state)) for item in self.board.actuators
        )
        active = "OK:ACTIVE_MS>" + ",".join(
            str(self.board.active_ms(index)) for index in range(24)
        )
        total = "OK:TOTAL_MS>" + ",".join(
            str(self.board.runtime_ms(index)) for index in range(24)
        )
        discharge = "OK:DISCHARGE_MS_LEFT>" + ",".join(
            str(self.board.discharge_left_ms(index)) for index in range(24)
        )
        return [summary, act_values, outputs, states, active, total, discharge]

    @staticmethod
    def _bool_param(value: str) -> bool | None:
        upper = value.upper()
        if upper in {"ON", "1"}:
            return True
        if upper in {"OFF", "0"}:
            return False
        return None

    @staticmethod
    def _one_actuator(
        params: list[str], numbers: list[int | None], command: str
    ) -> tuple[str | None, int]:
        if len(params) != 1:
            return f"ER:{command}_PARAM_COUNT", 0
        if numbers[0] is None or not 0 <= numbers[0] < 24:
            return f"ER:{command}_ACTUATOR", 0
        return None, numbers[0]

    def _emit_lines(self, lines: Iterable[str], *, event: str) -> bytes:
        rendered = list(lines)
        if not rendered:
            return b""
        if self.config.faults.response_delay_ms:
            self.clock.sleep(self.config.faults.response_delay_ms / 1000.0)
        if self.board.rng.random() < self.config.faults.drop_response_probability:
            self._log("fault.response_dropped", lines=rendered)
            return b""
        payload = "".join(line + "\n" for line in rendered).encode("ascii", errors="replace")
        if self.board.rng.random() < self.config.faults.corrupt_response_probability and payload:
            payload = bytes([payload[0] ^ 0x01]) + payload[1:]
            self._log("fault.response_corrupted")
        fields = {"lines": rendered}
        if self.config.logging.include_raw_bytes:
            fields["data_hex"] = payload.hex()
        self._log(event, **fields)
        return payload

    def _log(self, event: str, **fields: object) -> None:
        self.logger.emit(event, monotonic_ms=self.board.now_ms, **fields)


def load_script(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return [line.rstrip("\r\n") for line in handle]
