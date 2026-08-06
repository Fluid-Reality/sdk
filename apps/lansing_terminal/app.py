"""Interactive terminal app for Lansing boards.

Run from the SDK root with:

    python -m apps.lansing_terminal.app
"""

from __future__ import annotations

import argparse
import cmd
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

from serial.tools import list_ports

from fluid_reality import ActuatorState, FirmwareError, Lansing


PROMPT = "lansing> "
DISCONNECTED_PROMPT = "lansing(disconnected)> "


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive terminal controller for the Fluid Reality Lansing board.",
    )
    parser.add_argument(
        "--port",
        help="Serial port to connect on startup, for example COM4, /dev/cu.usbmodem..., or /dev/ttyACM0.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print SDK debug output as commands run.",
    )
    parser.add_argument(
        "-c",
        "--command",
        help=(
            "Run semicolon-separated Lansing terminal commands and exit. "
            "Example: -c \"connect COM4; status; psu on\""
        ),
    )
    return parser.parse_args(argv)


def yes_no(value: object) -> str:
    text = str(value).strip().upper()
    if text in {"ON", "1", "TRUE", "YES"}:
        return "on"
    if text in {"OFF", "0", "FALSE", "NO"}:
        return "off"
    return str(value)


def bool_arg(value: str) -> bool:
    text = value.strip().lower()
    if text in {"on", "1", "true", "yes", "closed", "connect", "connected"}:
        return True
    if text in {"off", "0", "false", "no", "open", "disconnect", "disconnected"}:
        return False
    raise ValueError(f"Expected on/off, got {value!r}.")


def parse_actuator(value: str) -> int:
    actuator = int(value)
    Lansing._validate_actuator(actuator)
    return actuator


def parse_output(value: str) -> int:
    output = int(value)
    Lansing._validate_output(output)
    return output


def format_ms(ms: int | float | None) -> str:
    if ms is None:
        return "-"
    value = int(ms)
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f} min"
    return f"{minutes / 60.0:.1f} h"


def state_name(state: ActuatorState | object) -> str:
    if isinstance(state, ActuatorState):
        return state.value
    return str(state)


class SquareWaveRunner:
    """Background 1 Hz square wave that respects firmware discharge debug."""

    def __init__(
        self,
        board_factory: Callable[[], Lansing],
        lock: threading.RLock,
        log: Callable[[str], None],
    ) -> None:
        self._board_factory = board_factory
        self._lock = lock
        self._log = log
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._actuators: list[int] = []
        self._restore_debug: bool | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def actuators(self) -> tuple[int, ...]:
        return tuple(self._actuators)

    def start(self, actuators: Iterable[int]) -> None:
        if self.running:
            raise RuntimeError("Square wave is already running. Use 'square stop' first.")
        unique = sorted(set(actuators))
        if not unique:
            raise ValueError("Provide at least one actuator.")
        self._actuators = unique
        self._stop.clear()
        with self._lock:
            board = self._board_factory()
            self._restore_debug = board.firmware_debug()
            if not self._restore_debug:
                board.firmware_debug(True)
            board.flush_debug_lines()
        self._thread = threading.Thread(target=self._run, name="lansing-square-wave", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        assert self._thread is not None
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            self._log("Square wave thread did not stop within 5 seconds.")
        self._thread = None
        self._actuators = []
        self._restore_firmware_debug()

    def _run(self) -> None:
        self._log(
            "Square wave started for "
            + ", ".join(str(actuator) for actuator in self._actuators)
            + "."
        )
        try:
            while not self._stop.is_set():
                with self._lock:
                    board = self._board_factory()
                    for actuator in self._actuators:
                        board.set_actuator(actuator, Lansing.max_output)
                if self._stop.wait(1.0):
                    break

                with self._lock:
                    board = self._board_factory()
                    for actuator in self._actuators:
                        board.set_actuator(actuator, 0)
                if self._stop.wait(1.0):
                    break

                self._wait_for_discharge()
        except Exception as exc:
            self._log(f"Square wave stopped: {exc}")
        finally:
            try:
                with self._lock:
                    board = self._board_factory()
                    for actuator in self._actuators:
                        try:
                            board.set_actuator(actuator, 0)
                        except FirmwareError as exc:
                            if exc.code != "ACT_FAILED":
                                raise
            except Exception as exc:
                self._log(f"Square wave stop cleanup failed: {exc}")
            self._restore_firmware_debug()
            self._log("Square wave stopped.")

    def _wait_for_discharge(self) -> None:
        last_warning = 0.0
        while not self._stop.is_set():
            with self._lock:
                board = self._board_factory()
                try:
                    board.status()
                except Exception as exc:
                    self._log(f"Square wave status check failed: {exc}")
                debug_lines = board.flush_debug_lines()
            if self._debug_confirms_discharge_complete(debug_lines):
                return
            now = time.monotonic()
            if now - last_warning > 2.0:
                self._log("Square wave waiting for firmware discharge completion.")
                last_warning = now
            self._stop.wait(0.1)

    def _debug_confirms_discharge_complete(self, debug_lines: tuple[str, ...]) -> bool:
        stopped: set[int] = set()
        for line in debug_lines:
            if not line.startswith("DBG:DISCHARGE_STOP,ACT>"):
                continue
            payload = line.removeprefix("DBG:DISCHARGE_STOP,ACT>")
            actuator_text = payload.split(",", 1)[0]
            try:
                stopped.add(int(actuator_text))
            except ValueError:
                continue
        return all(actuator in stopped for actuator in self._actuators)

    def _restore_firmware_debug(self) -> None:
        if self._restore_debug is None:
            return
        try:
            with self._lock:
                self._board_factory().firmware_debug(self._restore_debug)
        except Exception as exc:
            self._log(f"Could not restore firmware debug setting: {exc}")
        finally:
            self._restore_debug = None


class LansingTerminal(cmd.Cmd):
    intro = "Fluid Reality Lansing terminal. Type 'help' for commands."
    prompt = DISCONNECTED_PROMPT
    ruler = "-"

    def __init__(self, *, verbose: bool = False) -> None:
        super().__init__()
        self.board: Lansing | None = None
        self.connected_port: str | None = None
        self.verbose = verbose
        self.log_lines: list[str] = []
        self.error_count = 0
        self._lock = threading.RLock()
        self.square = SquareWaveRunner(self._require_board, self._lock, self._log)

    def emptyline(self) -> None:
        return None

    def default(self, line: str) -> None:
        self._error(f"Unknown command: {line}. Type 'help' for available commands.")

    def precmd(self, line: str) -> str:
        return line.strip()

    def postcmd(self, stop: bool, line: str) -> bool:
        self.prompt = PROMPT if self.board is not None else DISCONNECTED_PROMPT
        return stop

    def onecmd(self, line: str) -> bool:
        try:
            return super().onecmd(line)
        except (FirmwareError, RuntimeError, ValueError, OSError) as exc:
            self._error(str(exc))
            return False

    def do_ports(self, arg: str) -> None:
        """List serial ports visible to Python."""

        ports = list(list_ports.comports())
        if not ports:
            print("No serial ports found.")
            return
        for item in ports:
            description = item.description or "serial port"
            print(f"{item.device}\t{description}")

    def do_connect(self, arg: str) -> None:
        """connect <port>

        Open a Lansing board connection on the selected serial port.
        """

        parts = shlex.split(arg)
        if len(parts) != 1:
            raise ValueError("Usage: connect <port>")
        self.square.stop()
        self._close_board()
        port = parts[0]
        board = Lansing(port)
        with self._lock:
            try:
                board.set_debug_out(self._debug)
                board.force_text_mode()
                version = board.firmware_version()
            except Exception:
                board.close()
                raise
            self.board = board
        self.connected_port = port
        self._log(f"Connected to {version.firmware} firmware {version.version} on {port}.")
        self.do_status("")

    def do_disconnect(self, arg: str) -> None:
        """Disconnect from the board and stop any running square wave."""

        self.square.stop()
        self._close_board()
        self._log("Disconnected.")

    def do_status(self, arg: str) -> None:
        """Show power, telemetry, timing, and actuator summary."""

        with self._lock:
            status = self._require_board().status()
            states = self._require_board().actuator_states
        print(f"Port: {self.connected_port}")
        print(f"Power supply: {yes_no(status['psu'])}")
        print(f"Output connection: {'connected' if yes_no(status['psc']) == 'on' else 'open'}")
        print(f"Voltage: {float(status['voltage']):.2f} V")
        print(f"Current: {float(status['current']):.2f} mA")
        config = status["config"]
        print(
            "Timing: max "
            f"{format_ms(config['max_active_ms'])}, discharge {format_ms(config['discharge_ms'])}"
        )
        print(f"Safety: {yes_no(config['safe'])}; firmware debug: {yes_no(config['debug'])}")
        print(f"Square wave: {'running' if self.square.running else 'stopped'}")
        counts = {state: states.count(state) for state in ActuatorState}
        print(
            "Actuators: "
            f"{counts[ActuatorState.READY]} Ready, "
            f"{counts[ActuatorState.ERROR]} Error, "
            f"{counts[ActuatorState.NOT_CONNECTED]} Not connected, "
            f"{counts[ActuatorState.UNKNOWN]} Unknown"
        )

    def do_psu(self, arg: str) -> None:
        """psu [on|off]

        Read or set the high-voltage power supply state.
        """

        state = self._optional_bool(arg, "psu [on|off]")
        with self._lock:
            result = self._require_board().power_supply(state)
        print(f"Power supply: {yes_no(result)}")

    def do_output(self, arg: str) -> None:
        """output [on|off]

        Read or set the output connection between the power supply and actuators.
        """

        state = self._optional_bool(arg, "output [on|off]")
        with self._lock:
            result = self._require_board().connect_power(state)
        print(f"Output connection: {'connected' if yes_no(result) == 'on' else 'open'}")

    def do_voltage(self, arg: str) -> None:
        """voltage [measurement_ms]

        Read measured power-supply voltage.
        """

        parts = shlex.split(arg)
        if len(parts) > 1:
            raise ValueError("Usage: voltage [measurement_ms]")
        measurement_ms = int(parts[0]) if parts else None
        with self._lock:
            value = self._require_board().voltage(measurement_ms)
        print(f"{value:.2f} V")

    def do_current(self, arg: str) -> None:
        """Read current draw in milliamps."""

        with self._lock:
            value = self._require_board().current()
        print(f"{value:.2f} mA")

    def do_config(self, arg: str) -> None:
        """config [show|get <MAX|DIS|SAFE|DEBUG>|set <MAX|DIS|SAFE|DEBUG> <value>]

        Read or modify timing, safety, and firmware debug configuration.
        """

        parts = shlex.split(arg)
        if not parts or parts[0] == "show":
            with self._lock:
                config = self._require_board().read_config()
            print(f"max_active_ms: {config.max_active_ms}")
            print(f"discharge_ms: {config.discharge_ms}")
            print(f"safe: {yes_no(config.safe)}")
            print(f"debug: {yes_no(config.debug)}")
            return
        if len(parts) == 2 and parts[0] == "get":
            with self._lock:
                value = self._require_board().config(parts[1])
            print(value)
            return
        if len(parts) == 3 and parts[0] == "set":
            with self._lock:
                value = self._require_board().config(parts[1], parts[2])
            print(value)
            return
        raise ValueError("Usage: config [show|get <key>|set <key> <value>]")

    def do_safety(self, arg: str) -> None:
        """safety [on|off]

        Read or set firmware manual-output safety.
        """

        state = self._optional_bool(arg, "safety [on|off]")
        with self._lock:
            value = self._require_board().safety(state)
        print(f"Safety: {yes_no(value)}")

    def do_detect(self, arg: str) -> None:
        """detect <actuator>|group <0|1|2>

        Run SDK detection. Detection turns the actuator group off, diagnoses
        current delta, and records Ready, Error, or Not connected state.
        """

        parts = shlex.split(arg)
        if len(parts) == 1:
            actuator = parse_actuator(parts[0])
            self._detect_one(actuator)
            return
        if len(parts) == 2 and parts[0] == "group":
            group = int(parts[1])
            if not 0 <= group <= 2:
                raise ValueError("group must be 0, 1, or 2")
            for actuator in range(group * 8, group * 8 + 8):
                self._detect_one(actuator)
            return
        raise ValueError("Usage: detect <actuator>|group <0|1|2>")

    def do_diagnose(self, arg: str) -> None:
        """diagnose <actuator>

        Run a diagnostic and update the actuator state classification.
        """

        actuator = self._single_actuator(arg, "diagnose <actuator>")
        with self._lock:
            board = self._require_board()
            diagnosis = board.diagnose_actuator(actuator)
            detection = board.classify_diagnosis(diagnosis)
        self._print_detection(detection)

    def do_initialize(self, arg: str) -> None:
        """initialize <actuator>

        Run the staged initialization sequence, then diagnose. Use this first
        when an actuator is in Error state.
        """

        actuator = self._single_actuator(arg, "initialize <actuator>")

        def progress(data: dict[str, float | int]) -> None:
            elapsed = float(data["elapsed_s"])
            total = float(data["total_s"])
            stage = int(data["stage_index"])
            count = int(data["stage_count"])
            voltage = float(data["stage_voltage"])
            print(
                f"\rInitializing actuator {actuator}: "
                f"{elapsed:5.1f}/{total:.0f}s, stage {stage}/{count}, +/-{voltage:.0f} V",
                end="",
                flush=True,
            )

        with self._lock:
            board = self._require_board()
            state = board.initialize(actuator, progress_callback=progress)
            detection = board.last_detection(actuator)
        print()
        print(f"Initialization complete: {state.value}")
        if detection is not None:
            self._print_detection(detection)

    def do_recover(self, arg: str) -> None:
        """recover <actuator> [voltage=50] [duration_s=60]

        Advanced recovery. Temporarily disables safety, alternates manual
        positive/negative output at 1 Hz, reports current delta each second,
        restores safety, and leaves final classification to a later diagnose.
        """

        parts = shlex.split(arg)
        if not 1 <= len(parts) <= 3:
            raise ValueError("Usage: recover <actuator> [voltage=50] [duration_s=60]")
        actuator = parse_actuator(parts[0])
        target_voltage = float(parts[1]) if len(parts) >= 2 else 50.0
        duration_s = int(parts[2]) if len(parts) >= 3 else 60
        if target_voltage <= 0:
            raise ValueError("Recovery voltage must be greater than 0.")
        if duration_s <= 0:
            raise ValueError("Recovery duration must be greater than 0.")

        with self._lock:
            board = self._require_board()
            supply_voltage = board.voltage()
            if supply_voltage <= 0:
                raise RuntimeError("Cannot recover: measured PSU voltage is 0 V.")
            output_value = self._voltage_to_output(target_voltage, supply_voltage)
            baseline_ma = board.current()
            previous_safety = board.safety()

        print(
            f"Recovering actuator {actuator} at +/-{target_voltage:.1f} V for {duration_s}s "
            f"(raw {output_value}/255 from {supply_voltage:.1f} V PSU)."
        )
        samples: list[float] = []
        try:
            with self._lock:
                board = self._require_board()
                if previous_safety:
                    board.safety(False)
            start = time.monotonic()
            next_phase = start
            next_report_second = 1
            phase = 0
            while True:
                now = time.monotonic()
                elapsed = now - start
                if elapsed >= duration_s:
                    break
                with self._lock:
                    board = self._require_board()
                    if now >= next_phase:
                        if phase % 2 == 0:
                            board.set_manual_output(actuator, output_value, 0)
                        else:
                            board.set_manual_output(actuator, 0, output_value)
                        phase += 1
                        next_phase = now + 0.5
                    current_ma = board.current()
                samples.append(current_ma)
                delta_ma = abs(current_ma - baseline_ma)
                whole_second = int(elapsed)
                if whole_second >= next_report_second:
                    print(
                        f"Recovery {actuator}: {whole_second}/{duration_s}s, "
                        f"current {current_ma:.2f} mA, delta {delta_ma:.2f} mA"
                    )
                    next_report_second = whole_second + 1
                time.sleep(0.1)
        finally:
            with self._lock:
                board = self._require_board()
                try:
                    board.set_manual_output(actuator, 0, 0)
                finally:
                    board.safety(previous_safety)
        recovery_ma = sum(samples) / len(samples) if samples else baseline_ma
        delta_ma = abs(recovery_ma - baseline_ma)
        print(
            f"Recovery complete: baseline {baseline_ma:.2f} mA, "
            f"average {recovery_ma:.2f} mA, delta {delta_ma:.2f} mA."
        )
        print("Run 'diagnose {0}' to update the actuator state.".format(actuator))

    def do_set(self, arg: str) -> None:
        """set <actuator> <value>

        Set normal actuator output. Value is 0-255. The SDK only allows this
        when the actuator state is Ready.
        """

        parts = shlex.split(arg)
        if len(parts) != 2:
            raise ValueError("Usage: set <actuator> <value>")
        actuator = parse_actuator(parts[0])
        value = parse_output(parts[1])
        with self._lock:
            self._require_board().set_actuator(actuator, value)
        print(f"Actuator {actuator} set to {value}.")

    def do_off(self, arg: str) -> None:
        """off <actuator>|all

        Command one actuator or all actuators off.
        """

        parts = shlex.split(arg)
        if len(parts) != 1:
            raise ValueError("Usage: off <actuator>|all")
        with self._lock:
            board = self._require_board()
            if parts[0] == "all":
                board.all_actuators_off()
                print("All actuators commanded off.")
            else:
                actuator = parse_actuator(parts[0])
                board.set_actuator(actuator, 0)
                print(f"Actuator {actuator} commanded off.")

    def do_square(self, arg: str) -> None:
        """square start <actuator> [actuator...] | square stop | square status

        Run an indefinite 1 Hz square wave: 1 second full on, 1 second off,
        then wait for firmware discharge confirmation before reactivating.
        """

        parts = shlex.split(arg)
        if parts == ["stop"]:
            self.square.stop()
            return
        if parts == ["status"]:
            if self.square.running:
                print("Square wave running on " + ", ".join(map(str, self.square.actuators)))
            else:
                print("Square wave stopped.")
            return
        if len(parts) >= 2 and parts[0] == "start":
            actuators = [parse_actuator(value) for value in parts[1:]]
            with self._lock:
                board = self._require_board()
                for actuator in actuators:
                    if board.actuator_state(actuator) is not ActuatorState.READY:
                        raise RuntimeError(
                            f"Actuator {actuator} is {board.actuator_state(actuator).value}; "
                            f"run detect {actuator} first."
                        )
            self.square.start(actuators)
            return
        raise ValueError("Usage: square start <actuator> [actuator...] | square stop | square status")

    def do_runtime(self, arg: str) -> None:
        """runtime [actuator]

        Show total runtime for one actuator or all actuators.
        """

        parts = shlex.split(arg)
        if len(parts) > 1:
            raise ValueError("Usage: runtime [actuator]")
        with self._lock:
            board = self._require_board()
            if not parts:
                runtimes = board.runtime()
            else:
                actuator = parse_actuator(parts[0])
                print(f"{actuator}: {format_ms(board.runtime(actuator))}")
                return
        assert isinstance(runtimes, tuple)
        for index, runtime_ms in enumerate(runtimes):
            print(f"{index:02d}: {format_ms(runtime_ms)}")

    def do_reset_runtimes(self, arg: str) -> None:
        """Reset all actuator runtime counters."""

        with self._lock:
            self._require_board().reset_runtimes()
        print("Runtime counters reset.")

    def do_states(self, arg: str) -> None:
        """states [group <0|1|2>]

        Show SDK actuator states and last detection measurements.
        """

        parts = shlex.split(arg)
        if not parts:
            actuators = range(Lansing.actuator_count)
        elif len(parts) == 2 and parts[0] == "group":
            group = int(parts[1])
            if not 0 <= group <= 2:
                raise ValueError("group must be 0, 1, or 2")
            actuators = range(group * 8, group * 8 + 8)
        else:
            raise ValueError("Usage: states [group <0|1|2>]")
        with self._lock:
            board = self._require_board()
            for actuator in actuators:
                state = board.actuator_state(actuator)
                detection = board.last_detection(actuator)
                if detection is None:
                    print(f"{actuator:02d}: {state.value}")
                else:
                    print(
                        f"{actuator:02d}: {state.value}, delta {detection.delta_ma:.2f} mA "
                        f"(base {detection.baseline_ma:.2f}, fwd {detection.forward_ma:.2f}, "
                        f"dis {detection.discharge_ma:.2f})"
                    )

    def do_manual(self, arg: str) -> None:
        """manual get <actuator> | manual set <actuator> <positive> <negative>

        Advanced bench control using raw positive/negative manual outputs.
        """

        parts = shlex.split(arg)
        with self._lock:
            board = self._require_board()
            if len(parts) == 2 and parts[0] == "get":
                output = board.get_manual_output(parse_actuator(parts[1]))
                print(f"{output.actuator}: positive {output.positive}, negative {output.negative}")
                return
            if len(parts) == 4 and parts[0] == "set":
                actuator = parse_actuator(parts[1])
                positive = parse_output(parts[2])
                negative = parse_output(parts[3])
                board.set_manual_output(actuator, positive, negative)
                print(f"Manual output {actuator}: positive {positive}, negative {negative}")
                return
        raise ValueError("Usage: manual get <actuator> | manual set <actuator> <positive> <negative>")

    def do_debug(self, arg: str) -> None:
        """debug on|off|file <path>

        Configure SDK debug output. 'on' prints debug lines in the terminal.
        'file <path>' writes future debug lines to a file.
        """

        parts = shlex.split(arg)
        if parts == ["on"]:
            self.verbose = True
            with self._lock:
                if self.board is not None:
                    self.board.set_debug_out(self._debug)
            print("Terminal debug output enabled.")
            return
        if parts == ["off"]:
            self.verbose = False
            with self._lock:
                if self.board is not None:
                    self.board.set_debug_out(self._debug)
            print("Terminal debug output disabled.")
            return
        if len(parts) == 2 and parts[0] == "file":
            with self._lock:
                self._require_board().set_debug_out(Path(parts[1]))
            print(f"Future SDK debug output will be written to {parts[1]}.")
            return
        raise ValueError("Usage: debug on|off|file <path>")

    def do_log(self, arg: str) -> None:
        """log show|clear|save <path>

        Show, clear, or save the terminal event log.
        """

        parts = shlex.split(arg)
        if parts == ["show"]:
            print("\n".join(self.log_lines))
            return
        if parts == ["clear"]:
            self.log_lines.clear()
            print("Log cleared.")
            return
        if len(parts) == 2 and parts[0] == "save":
            Path(parts[1]).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            print(f"Log saved to {parts[1]}.")
            return
        raise ValueError("Usage: log show|clear|save <path>")

    def do_reboot(self, arg: str) -> None:
        """Reboot the connected board."""

        with self._lock:
            self._require_board().reboot()
        print("Reboot command sent.")

    def do_exit(self, arg: str) -> bool:
        """Exit the terminal app."""

        self.square.stop()
        self._close_board()
        return True

    def do_quit(self, arg: str) -> bool:
        """Exit the terminal app."""

        return self.do_exit(arg)

    def do_EOF(self, arg: str) -> bool:
        """Exit on Ctrl-D."""

        print()
        return self.do_exit(arg)

    def run_script(self, script: str) -> int:
        """Run semicolon-separated commands and return a process exit code."""

        commands = [command.strip() for command in script.split(";") if command.strip()]
        for command in commands:
            before_errors = self.error_count
            print(f"{self.prompt}{command}")
            stop = self._run_command(command)
            if self.error_count > before_errors:
                self.square.stop()
                self._close_board()
                return 1
            if stop:
                return 0
        self.square.stop()
        self._close_board()
        return 0

    def _run_command(self, command: str) -> bool:
        line = self.precmd(command)
        stop = self.onecmd(line)
        return self.postcmd(stop, line)

    def _detect_one(self, actuator: int) -> None:
        print(f"Detecting actuator {actuator}...")
        with self._lock:
            detection = self._require_board().detect_actuator(actuator)
        self._print_detection(detection)

    def _print_detection(self, detection: object) -> None:
        print(
            "Actuator {actuator}: {state}, delta {delta:.2f} mA "
            "(baseline {baseline:.2f}, forward {forward:.2f}, discharge {discharge:.2f})".format(
                actuator=getattr(detection, "actuator"),
                state=state_name(getattr(detection, "state")),
                delta=getattr(detection, "delta_ma"),
                baseline=getattr(detection, "baseline_ma"),
                forward=getattr(detection, "forward_ma"),
                discharge=getattr(detection, "discharge_ma"),
            )
        )

    def _single_actuator(self, arg: str, usage: str) -> int:
        parts = shlex.split(arg)
        if len(parts) != 1:
            raise ValueError(f"Usage: {usage}")
        return parse_actuator(parts[0])

    def _optional_bool(self, arg: str, usage: str) -> bool | None:
        parts = shlex.split(arg)
        if not parts:
            return None
        if len(parts) != 1:
            raise ValueError(f"Usage: {usage}")
        return bool_arg(parts[0])

    def _require_board(self) -> Lansing:
        if self.board is None:
            raise RuntimeError("Connect to a Lansing board first.")
        return self.board

    def _close_board(self) -> None:
        if self.board is None:
            return
        with self._lock:
            try:
                self.board.close()
            finally:
                self.board = None
                self.connected_port = None

    def _debug(self, line: str) -> None:
        self.log_lines.append(line)
        if self.verbose:
            print(f"[debug] {line}")

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.log_lines.append(line)
        print(line)

    def _error(self, message: str) -> None:
        self.error_count += 1
        self._log(f"Error: {message}")

    @staticmethod
    def _voltage_to_output(target_voltage: float, supply_voltage: float) -> int:
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(1, min(Lansing.max_output, int(Lansing.max_output * ratio)))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shell = LansingTerminal(verbose=args.verbose)
    if args.port:
        shell._run_command(f"connect {shlex.quote(args.port)}")
        if shell.error_count:
            return 1
    if args.command:
        return shell.run_script(args.command)
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        print()
        shell.do_exit("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
