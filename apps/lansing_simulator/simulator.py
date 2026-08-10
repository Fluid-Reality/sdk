"""Raw-TCP Lansing firmware simulator.

The simulator intentionally talks through the same byte stream as the hardware.
Applications continue calling ``Lansing(port)`` and select a virtual-port alias
configured through ``FLUID_REALITY_VIRTUAL_PORTS``.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fluid_reality import TcpDeviceConnection, TcpDeviceListener, TransportError

ACTUATOR_COUNT = 24
LINE_LIMIT = 256
TEXT_ENCODING = "ascii"
COMMAND_TERMINATOR = b"\n"
RESPONSE_TERMINATOR = b"\r\n"
STANDARD_CONFIG_DIR = Path(__file__).resolve().parent / "standard_configs"
DEFAULT_DIAGNOSIS_DELAY_S = 3.0


@dataclass(frozen=True)
class SimulatedActuator:
    guid: str
    name: str
    max_starting_current_ma: float
    offline_current_increase_ma_s: float
    running_current_decrease_ma_v_s: float
    min_running_current_ma: float
    current_noise_ma: float


@dataclass(frozen=True)
class SimulatorConfig:
    name: str
    psu_voltage_v: float
    psu_voltage_noise_v: float
    psu_base_current_ma: float
    psu_base_current_noise_ma: float
    actuators: dict[int, SimulatedActuator]

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        standard_config_dir: Path = STANDARD_CONFIG_DIR,
    ) -> "SimulatorConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Lansing board configuration must contain a JSON object")
        if data.get("schema_version") != 3:
            raise ValueError("Unsupported Lansing simulator schema; expected schema_version 3")
        if data.get("kind") != "lansing-simulator-design":
            raise ValueError("Configuration kind must be 'lansing-simulator-design'")
        def parse_profile(item: dict[str, Any]) -> SimulatedActuator:
            profile = SimulatedActuator(
                guid=str(item["guid"]),
                name=str(item.get("name", "Actuator")),
                max_starting_current_ma=float(item.get("max_starting_current_ma", 4.0)),
                offline_current_increase_ma_s=float(item.get("offline_current_increase_ma_s", 0.05)),
                running_current_decrease_ma_v_s=float(item.get("running_current_decrease_ma_v_s", 0.001)),
                min_running_current_ma=float(item.get("min_running_current_ma", 0.5)),
                current_noise_ma=float(item.get("current_noise_ma", 0.02)),
            )
            if profile.min_running_current_ma > profile.max_starting_current_ma:
                raise ValueError(
                    f"Actuator {profile.name!r} minimum running current exceeds maximum starting current"
                )
            return profile
        profiles = {
            str(item["guid"]): parse_profile(item)
            for item in data.get("actuator_configurations", [])
        }
        # Standards are loaded last and therefore win when a GUID is duplicated.
        if standard_config_dir.is_dir():
            for standard_path in sorted(standard_config_dir.glob("*.json")):
                item = json.loads(standard_path.read_text(encoding="utf-8"))
                profile = parse_profile(item)
                profiles[profile.guid] = profile
        assigned: dict[int, SimulatedActuator] = {}
        groups = data.get("groups", {})
        if isinstance(groups, dict):
            for group_key, group in groups.items():
                group_index = int(group_key)
                if not 0 <= group_index < 3:
                    raise ValueError(f"Group key must be 0, 1, or 2; received {group_key!r}")
                if not isinstance(group, dict):
                    raise ValueError(f"Group {group_key} must contain a JSON object")
                for port_key, assignment in group.get("actuators", {}).items():
                    port_index = int(port_key)
                    if not 0 <= port_index < 8:
                        raise ValueError(f"Actuator port key must be 0 through 7; received {port_key!r}")
                    actuator_index = group_index * 8 + port_index
                    guid = str(assignment["configuration_guid"])
                    if guid not in profiles:
                        raise ValueError(f"Actuator {actuator_index} references unknown GUID {guid}")
                    assigned[actuator_index] = profiles[guid]
        return cls(
            name=str(data.get("name", path.stem)),
            psu_voltage_v=float(data.get("psu_voltage_v", 200.0)),
            psu_voltage_noise_v=float(data.get("psu_voltage_noise_v", 0.0)),
            psu_base_current_ma=float(data.get("psu_base_current_ma", 1.0)),
            psu_base_current_noise_ma=float(data.get("psu_base_current_noise_ma", 0.0)),
            actuators=assigned,
        )


class LansingDeviceSimulator:
    """Connection-independent state and Lansing byte-protocol engine."""

    def __init__(
        self,
        write_bytes: Callable[[bytes], None],
        config: SimulatorConfig,
        *,
        response_delay_s: float = 0.0,
        diagnosis_delay_s: float = DEFAULT_DIAGNOSIS_DELAY_S,
        random_seed: int | None = 0,
        log: Callable[[str], None] | None = None,
        state_changed: Callable[["LansingDeviceSimulator"], None] | None = None,
    ) -> None:
        self._write_bytes = write_bytes
        self.config = config
        self.response_delay_s = response_delay_s
        self.diagnosis_delay_s = diagnosis_delay_s
        self._random = random.Random(random_seed)
        self._log = log
        self._state_changed = state_changed
        self._stream_mode = False
        self._stream_packet = bytearray()
        self._line = bytearray()
        self.psu = False
        self.psc = False
        self.safe = True
        self.debug = False
        self.max_active_ms = 5000
        self.discharge_ms = 2000
        self.values = [0] * ACTUATOR_COUNT
        self.manual = [(0, 0)] * ACTUATOR_COUNT
        self.total_ms = [0] * ACTUATOR_COUNT
        self._active_since: list[float | None] = [None] * ACTUATOR_COUNT
        self._actuator_current_ma = {
            index: profile.max_starting_current_ma
            for index, profile in config.actuators.items()
        }
        self._current_updated_at = time.monotonic()

    def feed(self, data: bytes) -> None:
        """Consume any TCP chunk size while preserving protocol boundaries."""
        for value in data:
            self._receive_byte(value)

    def _receive_byte(self, value: int) -> None:
        if self._stream_mode:
            self._stream_packet.append(value)
            if len(self._stream_packet) == 2:
                actuator, output = self._stream_packet
                self._stream_packet.clear()
                if self._log is not None:
                    self._log(f"RX: [{actuator}, {output}]")
                if actuator == 255:
                    self._stream_mode = False
                elif 0 <= actuator < ACTUATOR_COUNT:
                    self._set_value(actuator, output)
                if self._state_changed is not None:
                    self._state_changed(self)
            return
        if value == 10:
            raw = bytes(self._line).rstrip(b"\r")
            self._line.clear()
            self._handle_raw_line(raw)
            return
        self._line.append(value)
        if len(self._line) > LINE_LIMIT:
            self._line.clear()
            self._write_line("ER:LINE_TOO_LONG")

    def _handle_raw_line(self, raw: bytes) -> None:
        try:
            text = raw.decode(TEXT_ENCODING, errors="strict").strip()
        except UnicodeDecodeError:
            if self._log is not None:
                self._log(f"RX: {raw!r}")
            self._write_line("ER:BAD_COMMAND")
            return
        if self._log is not None:
            self._log(f"RX: {text}")
        if not text:
            self._write_line("ER:BAD_COMMAND")
            return
        parts = [part for part in re.split(r"[\s,]+", text) if part]
        command, params = parts[0].upper(), parts[1:]
        if len(command) != 3:
            self._write_line("ER:BAD_COMMAND")
            return
        response = self._dispatch(command, params)
        if response is None:
            return
        lines = response if isinstance(response, list) else [response]
        for line in lines:
            self._write_line(line)
        if self._state_changed is not None:
            self._state_changed(self)

    def snapshot(self) -> dict[str, Any]:
        self._update_actuator_currents()
        return {
            "psu": self.psu,
            "psc": self.psc,
            "actuator_activation": {
                str(index): {
                    "value": self.values[index],
                    "positive": self.manual[index][0],
                    "negative": self.manual[index][1],
                }
                for index in self.config.actuators
            },
            "actuator_current_ma": {
                str(index): self._actuator_current_ma[index]
                for index in self.config.actuators
            },
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.psu = bool(state.get("psu", False))
        self.psc = bool(state.get("psc", False)) and self.psu
        activations = state.get("actuator_activation", {})
        currents = state.get("actuator_current_ma", {})
        now = time.monotonic()
        for index, profile in self.config.actuators.items():
            item = activations.get(str(index), {})
            value = max(0, min(255, int(item.get("value", 0))))
            positive = max(0, min(255, int(item.get("positive", value))))
            negative = max(0, min(255, int(item.get("negative", 0))))
            self.values[index] = value
            self.manual[index] = (positive, negative)
            restored_current = float(currents.get(str(index), profile.max_starting_current_ma))
            self._actuator_current_ma[index] = max(
                profile.min_running_current_ma,
                min(profile.max_starting_current_ma, restored_current),
            )
            if value:
                self._active_since[index] = now
        self._current_updated_at = now

    def _dispatch(self, command: str, params: list[str]) -> str | list[str] | None:
        handlers = {
            "VER": self._version, "PSU": lambda p: self._switch("PSU", p),
            "PSC": lambda p: self._switch("PSC", p), "VLT": self._voltage,
            "CUR": self._current, "ACT": self._actuator, "OUT": self._output,
            "DIA": self._diagnose, "INI": self._initialize, "TIM": self._runtime,
            "RST": self._reset_runtimes, "RBT": self._reboot, "CFG": self._config,
            "STS": self._status, "STR": self._stream,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"ER:UNKNOWN_COMMAND>{command}"
        return handler(params)

    @staticmethod
    def _parse_bool(value: str) -> bool | None:
        if value.upper() in {"ON", "1"}:
            return True
        if value.upper() in {"OFF", "0"}:
            return False
        return None

    @staticmethod
    def _parse_int(value: str, low: int, high: int) -> int | None:
        try:
            result = int(value)
        except ValueError:
            return None
        return result if low <= result <= high else None

    def _version(self, params: list[str]) -> str:
        return "ER:VER_PARAM_COUNT" if params else "OK:FW>Lansing,VERSION>0.1,PROTO>0.1"

    def _switch(self, kind: str, params: list[str]) -> str:
        if len(params) > 1:
            return f"ER:{kind}_PARAM_COUNT"
        if params:
            state = self._parse_bool(params[0])
            if state is None:
                return f"ER:{kind}_PARAM_VALUE"
            if kind == "PSC" and state and not self.psu:
                return "ER:PSC_PSU_OFF"
            setattr(self, kind.lower(), state)
            if kind == "PSU" and not state:
                self.psc = False
            return f"OK:{kind}_{'ON' if state else 'OFF'}"
        return f"OK:{'ON' if getattr(self, kind.lower()) else 'OFF'}"

    def _voltage(self, params: list[str]) -> str:
        if len(params) > 1:
            return "ER:VLT_PARAM_COUNT"
        if params:
            measurement_ms = self._parse_int(params[0], 1, 2**31 - 1)
            if measurement_ms is None:
                return "ER:VLT_PARAM_VALUE"
            time.sleep(measurement_ms / 1000.0)
        return f"OK:{self._voltage_value():.3f}"

    def _voltage_value(self) -> float:
        value = self.config.psu_voltage_v if self.psu else 0.0
        value += self._random.gauss(0, self.config.psu_voltage_noise_v) if self.psu else 0
        return max(0.0, value)

    def _current_value(self) -> float:
        self._update_actuator_currents()
        value = self.config.psu_base_current_ma if self.psu else 0.0
        if self.psu:
            value += self._random.gauss(0, self.config.psu_base_current_noise_ma)
        for index, outputs in enumerate(self.manual):
            profile = self.config.actuators.get(index)
            positive, negative = outputs
            activation = max(positive, negative)
            if self.psu and profile and activation:
                ratio = activation / 255.0
                electrode_current = (
                    self._actuator_current_ma[index] if positive >= negative
                    else profile.min_running_current_ma
                )
                value += electrode_current * ratio
                value += self._random.gauss(0, profile.current_noise_ma)
        return max(0.0, value)

    def _update_actuator_currents(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        elapsed_s = max(0.0, now - self._current_updated_at)
        self._current_updated_at = now
        if not elapsed_s:
            return
        for index, profile in self.config.actuators.items():
            # Only positive/forward drive improves the actuator. The negative
            # electrode is the firmware's discharge path and counts as offline
            # recovery even though it can draw discharge current.
            activation = self.manual[index][0] if self.psu and self.psc else 0
            self._evolve_actuator_current(index, activation / 255.0, elapsed_s)

    def _evolve_actuator_current(self, index: int, activation: float, elapsed_s: float) -> None:
        profile = self.config.actuators.get(index)
        if profile is None or elapsed_s <= 0:
            return
        current = self._actuator_current_ma[index]
        if activation > 0:
            decrease = (
                profile.running_current_decrease_ma_v_s
                * self.config.psu_voltage_v
                * activation
                * elapsed_s
            )
            current = max(profile.min_running_current_ma, current - decrease)
        else:
            current = min(
                profile.max_starting_current_ma,
                current + profile.offline_current_increase_ma_s * elapsed_s,
            )
        self._actuator_current_ma[index] = current

    def _current(self, params: list[str]) -> str:
        if params:
            return "ER:CUR_PARAM_COUNT"
        value = self._current_value()
        if self._log is not None:
            actuator_values = ";".join(
                f"A{index}->{self._actuator_current_ma[index]:.3f}"
                for index in sorted(self.config.actuators)
            )
            self._log(f"LOG: {actuator_values}")
        return f"OK:{value:.3f}"

    def _actuator(self, params: list[str]) -> str:
        if len(params) > 2:
            return "ER:ACT_PARAM_COUNT"
        if not params:
            return "OK:" + ",".join(map(str, self.values))
        actuator = self._parse_int(params[0], 0, ACTUATOR_COUNT - 1)
        if actuator is None:
            return "ER:ACT_ACTUATOR"
        if len(params) == 1:
            return f"OK:{actuator},{self.values[actuator]}"
        output = self._parse_int(params[1], 0, 255)
        if output is None:
            return "ER:ACT_VALUE"
        if not self.psu:
            return "ER:ACT_PSU_OFF"
        if not self.psc:
            return "ER:ACT_PSU_DISCONNECTED"
        self._set_value(actuator, output)
        return "OK:ACT"

    def _set_value(self, actuator: int, output: int) -> None:
        now = time.monotonic()
        self._update_actuator_currents(now)
        started = self._active_since[actuator]
        if output and started is None:
            self._active_since[actuator] = now
        elif not output and started is not None:
            self.total_ms[actuator] += round((now - started) * 1000)
            self._active_since[actuator] = None
        self.values[actuator] = output
        self.manual[actuator] = (output, 0)

    def _output(self, params: list[str]) -> str:
        if len(params) not in {0, 1, 3}:
            return "ER:OUT_PARAM_COUNT"
        if not params:
            fields = ",".join(f"A{i}P>{p},A{i}N>{n}" for i, (p, n) in enumerate(self.manual))
            return f"OK:{fields}"
        actuator = self._parse_int(params[0], 0, ACTUATOR_COUNT - 1)
        if actuator is None:
            return "ER:OUT_ACTUATOR"
        if len(params) == 1:
            positive, negative = self.manual[actuator]
            return f"OK:ACT>{actuator},POS>{positive},NEG>{negative}"
        positive = self._parse_int(params[1], 0, 255)
        negative = self._parse_int(params[2], 0, 255)
        if positive is None:
            return "ER:OUT_POS_VALUE"
        if negative is None:
            return "ER:OUT_NEG_VALUE"
        if self.safe:
            return "ER:OUT_SAFETY_ON"
        now = time.monotonic()
        self._update_actuator_currents(now)
        self.values[actuator] = 0
        self._active_since[actuator] = None
        self.manual[actuator] = (positive, negative)
        return "OK:OUT"

    def _diagnose(self, params: list[str]) -> str:
        if len(params) != 1:
            return "ER:DIA_PARAM_COUNT"
        actuator = self._parse_int(params[0], 0, ACTUATOR_COUNT - 1)
        if actuator is None:
            return "ER:DIA_ACTUATOR"
        if not self.psu:
            return "ER:DIA_PSU_OFF"
        if not self.psc:
            return "ER:DIA_PSU_DISCONNECTED"
        # Real firmware performs baseline, forward-drive, and discharge-current
        # measurements before replying, so DIA is intentionally blocking.
        deadline = time.monotonic() + self.diagnosis_delay_s
        while (remaining := deadline - time.monotonic()) > 0:
            time.sleep(remaining)
        self._update_actuator_currents(deadline)
        self._evolve_actuator_current(actuator, 1.0, 1.0)
        self._evolve_actuator_current(actuator, 0.0, max(0.0, self.diagnosis_delay_s - 1.0))
        base = self.config.psu_base_current_ma
        profile = self.config.actuators.get(actuator)
        actuator_current = self._actuator_current_ma.get(actuator, 0.0)
        forward = base if profile is None else base + actuator_current
        discharge = base if profile is None else base + profile.min_running_current_ma
        return f"OK:ACT>{actuator},BASE>{base:.3f},FWD>{forward:.3f},DIS>{discharge:.3f}"

    def _initialize(self, params: list[str]) -> str:
        if len(params) != 1:
            return "ER:INI_PARAM_COUNT"
        actuator = self._parse_int(params[0], 0, ACTUATOR_COUNT - 1)
        if actuator is None:
            return "ER:INI_ACTUATOR"
        if not self.psu:
            return "ER:INI_PSU_OFF"
        if not self.psc:
            return "ER:INI_PSU_DISCONNECTED"
        self.total_ms[actuator] = 0
        # Firmware runs five 500 ms pulses and five 1000 ms pulses, with an
        # equal discharge and rest period after each pulse.
        for active_s in (0.5,) * 5 + (1.0,) * 5:
            self._evolve_actuator_current(actuator, 1.0, active_s)
            self._evolve_actuator_current(actuator, 0.0, active_s * 2)
        return "OK:INI"

    def _runtime(self, params: list[str]) -> str:
        if len(params) > 1:
            return "ER:TIM_PARAM_COUNT"
        if not params:
            return "OK:" + ",".join(map(str, self.total_ms))
        actuator = self._parse_int(params[0], 0, ACTUATOR_COUNT - 1)
        return "ER:TIM_ACTUATOR" if actuator is None else f"OK:{actuator},{self.total_ms[actuator]}"

    def _reset_runtimes(self, params: list[str]) -> str:
        if params:
            return "ER:RST_PARAM_COUNT"
        self.total_ms = [0] * ACTUATOR_COUNT
        return "OK:RST"

    def _reboot(self, params: list[str]) -> str:
        if params:
            return "ER:RBT_PARAM_COUNT"
        self.psu = self.psc = self.debug = False
        self.safe = True
        self._stream_mode = False
        self._stream_packet.clear()
        self._line.clear()
        self.values = [0] * ACTUATOR_COUNT
        self.manual = [(0, 0)] * ACTUATOR_COUNT
        self._active_since = [None] * ACTUATOR_COUNT
        return "OK:RBT"

    def _config(self, params: list[str]) -> str:
        if len(params) not in {1, 2}:
            return "ER:CFG_PARAM_COUNT"
        key = params[0].upper()
        mapping = {"MAX": "max_active_ms", "DIS": "discharge_ms", "SAFE": "safe", "DEBUG": "debug"}
        attr = mapping.get(key)
        if attr is None:
            return "ER:CFG_KEY"
        if len(params) == 2:
            if key in {"MAX", "DIS"}:
                value = self._parse_int(params[1], 0, 2**31 - 1)
            else:
                value = self._parse_bool(params[1])
            if value is None:
                return "ER:CFG_VALUE"
            setattr(self, attr, value)
            return f"OK:CFG_{key}"
        value = getattr(self, attr)
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        return f"OK:{value}"

    def _status(self, params: list[str]) -> str | list[str]:
        if params:
            return "ER:STS_PARAM_COUNT"
        states = [1 if value else 0 for value in self.values]
        active_ms = [round((time.monotonic() - start) * 1000) if start else 0 for start in self._active_since]
        out_fields = ",".join(f"A{i}P>{p},A{i}N>{n}" for i, (p, n) in enumerate(self.manual))
        return [
            f"OK:PSU>{'ON' if self.psu else 'OFF'},PSC>{'ON' if self.psc else 'OFF'},VLT>{self._voltage_value():.3f},CUR>{self._current_value():.3f},CFG_MAX>{self.max_active_ms},CFG_DIS>{self.discharge_ms},SAFE>{'ON' if self.safe else 'OFF'},DEBUG>{'ON' if self.debug else 'OFF'},STREAM>{'BINARY' if self._stream_mode else 'TEXT'}",
            "OK:ACT_VALUES>" + ",".join(map(str, self.values)),
            "OK:OUT_VALUES>" + out_fields,
            "OK:ACT_STATES>" + ",".join(map(str, states)),
            "OK:ACTIVE_MS>" + ",".join(map(str, active_ms)),
            "OK:TOTAL_MS>" + ",".join(map(str, self.total_ms)),
            "OK:DISCHARGE_MS_LEFT>" + ",".join("0" for _ in range(ACTUATOR_COUNT)),
        ]

    def _stream(self, params: list[str]) -> str:
        if params:
            return "ER:STR_PARAM_COUNT"
        self._stream_mode = True
        return "OK:STR"

    def _write_line(self, line: str) -> None:
        if self.response_delay_s:
            time.sleep(self.response_delay_s)
        if self._log is not None:
            self._log(f"TX: {line}")
        payload = line.encode(TEXT_ENCODING, errors="strict") + RESPONSE_TERMINATOR
        # Deliberately split writes to exercise host-side partial reads.
        midpoint = max(1, len(payload) // 2)
        self._write_bytes(payload[:midpoint])
        self._write_bytes(payload[midpoint:])


class LansingTcpServer:
    """Single-controller TCP server that returns to accept after disconnect."""

    def __init__(
        self,
        config: SimulatorConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        response_delay_s: float = 0.0,
        diagnosis_delay_s: float = DEFAULT_DIAGNOSIS_DELAY_S,
        log: Callable[[str], None] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port
        self.response_delay_s = response_delay_s
        self.diagnosis_delay_s = diagnosis_delay_s
        self.log = log
        self.state_path = state_path
        self._runtime_state = self._load_runtime_state()
        self.last_engine: LansingDeviceSimulator | None = None
        self._server: TcpDeviceListener | None = None
        self._connection: TcpDeviceConnection | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _load_runtime_state(self) -> dict[str, Any]:
        if self.state_path is None or not self.state_path.is_file():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            state = data.get("simulation_state", {})
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _checkpoint_engine(self, engine: LansingDeviceSimulator) -> None:
        self._runtime_state = engine.snapshot()
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            data["simulation_state"] = self._runtime_state
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.state_path)
        except (OSError, ValueError, TypeError) as exc:
            if self.log is not None:
                self.log(f"STATE save failed: {exc}")

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("TCP server is not running")
        return self._server.address

    @property
    def endpoint(self) -> str:
        host, port = self.address
        return f"tcp://{host}:{port}"

    def start(self) -> "LansingTcpServer":
        if self._thread is not None:
            raise RuntimeError("TCP server is already running")
        self._server = TcpDeviceListener(self.host, self.port).start()
        self._thread = threading.Thread(target=self.serve_forever, name="lansing-tcp-server", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                connection = self._server.accept(timeout=0.1)
            except TimeoutError:
                continue
            except OSError:
                break
            self._connection = connection
            try:
                if self.log is not None:
                    self.log(f"CLIENT connected from {connection.address[0]}:{connection.address[1]}")
                engine = LansingDeviceSimulator(
                    connection.write_bytes,
                    self.config,
                    response_delay_s=self.response_delay_s,
                    diagnosis_delay_s=self.diagnosis_delay_s,
                    log=self.log,
                    state_changed=self._checkpoint_engine,
                )
                engine.restore(self._runtime_state)
                self.last_engine = engine
                while chunk := connection.read_bytes(4096):
                    engine.feed(chunk)
            except (ConnectionError, OSError, TransportError):
                pass
            finally:
                if self.last_engine is not None:
                    self._checkpoint_engine(self.last_engine)
                try:
                    connection.close()
                finally:
                    self._connection = None
                    if self.log is not None:
                        self.log("CLIENT disconnected; listening for the next SDK connection")

    def stop(self) -> None:
        if self.last_engine is not None:
            self._checkpoint_engine(self.last_engine)
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._server = None

    def __enter__(self) -> "LansingTcpServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def print_connection_instructions(endpoint: str) -> None:
    print(f"Listening TCP endpoint: {endpoint}")
    mapping = f"COM66={endpoint}"
    print(f'PowerShell: $env:FLUID_REALITY_VIRTUAL_PORTS="{mapping}"')
    print(f"Command Prompt: set FLUID_REALITY_VIRTUAL_PORTS={mapping}")
    print(f'macOS/Linux: export FLUID_REALITY_VIRTUAL_PORTS="{mapping}"')
    print("WARNING: This raw TCP protocol is not authenticated or encrypted.")


def run_tcp_simulator(config_path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    config = SimulatorConfig.load(config_path)
    with LansingTcpServer(config, host=host, port=port, log=print, state_path=config_path) as server:
        print(f"Lansing simulator: {config.name}")
        print_connection_instructions(server.endpoint)
        if host not in {"127.0.0.1", "localhost"}:
            print("WARNING: Non-loopback binding exposes the unauthenticated protocol to the network.")
        try:
            while True:
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Lansing device over its raw TCP byte stream")
    parser.add_argument("config", type=Path, help="Single-file Lansing board JSON")
    parser.add_argument(
        "--tcp",
        metavar="HOST:PORT",
        default="127.0.0.1:8765",
        help="Raw TCP listen address (default: 127.0.0.1:8765)",
    )
    args = parser.parse_args()
    try:
        host, port_text = args.tcp.rsplit(":", 1)
        port = int(port_text)
        if not 0 <= port <= 65535:
            raise ValueError("port must be 0..65535")
    except ValueError as exc:
        parser.error(f"--tcp must be HOST:PORT: {exc}")
    run_tcp_simulator(args.config, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
