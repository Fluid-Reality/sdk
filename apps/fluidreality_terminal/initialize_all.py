"""Cross-platform actuator initialization automation for Fluid Reality Terminal."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ActuatorResult:
    actuator: int
    status: str
    delta_ma: float | None
    attempts: int
    detail: str


class LansingInitializer:
    def __init__(
        self,
        *,
        port: str,
        target_delta_ma: float,
        actuators: Sequence[int],
        python_executable: str,
        terminal_path: Path,
        max_attempts: int,
        minimum_improvement_ma: float,
        leave_power_on: bool,
        verbose: bool,
    ) -> None:
        self.port = port
        self.target_delta_ma = target_delta_ma
        self.actuators = tuple(sorted(set(actuators)))
        self.python_executable = python_executable
        self.terminal_path = terminal_path
        self.max_attempts = max_attempts
        self.minimum_improvement_ma = minimum_improvement_ma
        self.leave_power_on = leave_power_on
        self.verbose = verbose
        self.results: list[ActuatorResult] = []
        self.failed = False
        self.power_may_be_on = False

    def run(self) -> int:
        interrupted = False
        try:
            for actuator in self.actuators:
                self._process_actuator(actuator)
        except KeyboardInterrupt:
            interrupted = True
            self.failed = True
            print("\nInterrupted by operator.", file=sys.stderr)
        finally:
            self._shutdown_power()

        self._print_summary()
        if interrupted:
            return 130
        return 1 if self.failed else 0

    def _process_actuator(self, actuator: int) -> None:
        print(f"\nActuator {actuator}: detecting...")
        self.power_may_be_on = True
        detection_run = self._invoke_terminal(
            f"psu on; psuc on; detect {actuator}",
            show_progress=False,
        )
        if detection_run.returncode != 0:
            self._record_failure(
                actuator,
                "Command failed",
                None,
                0,
                f"Detection failed: {self._error_message(detection_run.records)}",
            )
            return

        detection = self._last_detection(detection_run.records, actuator)
        if detection is None:
            self._record_failure(
                actuator,
                "No result",
                None,
                0,
                "Detection completed without an actuator result.",
            )
            return

        state = str(detection["state"])
        previous_delta = float(detection["delta_ma"])
        print(f"Actuator {actuator}: {state}, delta {previous_delta:.3f} mA")

        if state == "Not connected":
            print(f"Actuator {actuator}: not connected; skipping.")
            self.results.append(
                ActuatorResult(
                    actuator,
                    "Not connected",
                    previous_delta,
                    0,
                    "No initialization attempted",
                )
            )
            return

        if state == "Ready" and previous_delta <= self.target_delta_ma:
            print(
                f"Actuator {actuator}: target reached "
                f"({previous_delta:.3f} mA <= {self.target_delta_ma:.3f} mA)."
            )
            self.results.append(
                ActuatorResult(
                    actuator,
                    "Target reached",
                    previous_delta,
                    0,
                    "Target reached on initial detection",
                )
            )
            return

        if state not in {"Ready", "Error"}:
            self._record_failure(
                actuator,
                "Unexpected state",
                previous_delta,
                0,
                f"Unexpected actuator state {state!r}.",
            )
            return

        if state == "Ready":
            print(
                f"Actuator {actuator}: Ready, but delta {previous_delta:.3f} mA "
                f"is above target {self.target_delta_ma:.3f} mA; "
                "continuing initialization."
            )

        for attempt in range(1, self.max_attempts + 1):
            print(
                f"Actuator {actuator}: initialization attempt "
                f"{attempt}/{self.max_attempts}; previous delta "
                f"{previous_delta:.3f} mA"
            )
            initialization_run = self._invoke_terminal(
                f"psu on; psuc on; detect {actuator}; init {actuator}",
                show_progress=True,
            )
            if initialization_run.returncode != 0:
                self._record_failure(
                    actuator,
                    "Command failed",
                    previous_delta,
                    attempt,
                    "Initialization failed: "
                    + self._error_message(initialization_run.records),
                )
                return

            after = self._last_detection(initialization_run.records, actuator)
            if after is None:
                self._record_failure(
                    actuator,
                    "No result",
                    previous_delta,
                    attempt,
                    "Initialization completed without a final detection result.",
                )
                return

            new_state = str(after["state"])
            new_delta = float(after["delta_ma"])
            improvement = previous_delta - new_delta
            print(
                f"Actuator {actuator}: {new_state}, delta {new_delta:.3f} mA, "
                f"improvement {improvement:.3f} mA"
            )

            if new_state == "Ready" and new_delta <= self.target_delta_ma:
                print(
                    f"Actuator {actuator}: target reached "
                    f"({new_delta:.3f} mA <= {self.target_delta_ma:.3f} mA)."
                )
                self.results.append(
                    ActuatorResult(
                        actuator,
                        "Target reached",
                        new_delta,
                        attempt,
                        "Target delta reached",
                    )
                )
                return

            if new_state not in {"Ready", "Error"}:
                self._record_failure(
                    actuator,
                    "Unexpected state",
                    new_delta,
                    attempt,
                    f"Unexpected post-initialization state {new_state!r}.",
                )
                return

            if improvement <= self.minimum_improvement_ma:
                self._record_failure(
                    actuator,
                    "Stalled",
                    new_delta,
                    attempt,
                    f"Delta stopped decreasing: {previous_delta:.3f} mA -> "
                    f"{new_delta:.3f} mA (required improvement > "
                    f"{self.minimum_improvement_ma:.3f} mA).",
                )
                return

            previous_delta = new_delta

        self._record_failure(
            actuator,
            "Above target",
            previous_delta,
            self.max_attempts,
            f"Still above target {self.target_delta_ma:.3f} mA after "
            f"{self.max_attempts} initialization attempts; last delta "
            f"{previous_delta:.3f} mA.",
        )

    def _invoke_terminal(self, command: str, *, show_progress: bool) -> CommandResult:
        arguments = [
            self.python_executable,
            str(self.terminal_path),
            "-j",
            "--port",
            self.port,
            "-c",
            command,
        ]
        if self.verbose:
            print("Running: " + " ".join(repr(value) for value in arguments))

        records: list[dict[str, Any]] = []
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        progress_active = False
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if progress_active:
                    print()
                    progress_active = False
                print(f"Warning: non-JSON terminal output: {line}", file=sys.stderr)
                continue
            if not isinstance(record, dict):
                continue
            records.append(record)
            if show_progress and record.get("event") == "initialization_progress":
                elapsed = float(record["elapsed_s"])
                total = float(record["total_s"])
                print(
                    f"\rInitializing actuator {record['actuator']}: "
                    f"{elapsed:5.1f}/{total:.0f}s, stage "
                    f"{record['stage']}/{record['stage_count']}",
                    end="",
                    flush=True,
                )
                progress_active = True
        returncode = process.wait()
        if progress_active:
            print()
        return CommandResult(returncode, tuple(records))

    def _shutdown_power(self) -> None:
        if not self.power_may_be_on or self.leave_power_on:
            return
        print("\nTurning the PSU connection off, then turning the PSU off...")
        try:
            shutdown = self._invoke_terminal(
                "psuc off; psu off",
                show_progress=False,
            )
        except Exception as exc:
            print(f"Warning: automatic power shutdown failed: {exc}", file=sys.stderr)
            self.failed = True
            return
        if shutdown.returncode != 0:
            print(
                "Warning: automatic power shutdown failed: "
                + self._error_message(shutdown.records),
                file=sys.stderr,
            )
            self.failed = True

    def _record_failure(
        self,
        actuator: int,
        status: str,
        delta_ma: float | None,
        attempts: int,
        detail: str,
    ) -> None:
        print(f"Actuator {actuator}: {detail}", file=sys.stderr)
        self.results.append(
            ActuatorResult(actuator, status, delta_ma, attempts, detail)
        )
        self.failed = True

    def _print_summary(self) -> None:
        print("\nInitialization summary")
        rows = [
            (
                str(result.actuator),
                result.status,
                "-" if result.delta_ma is None else f"{result.delta_ma:.3f}",
                str(result.attempts),
                result.detail,
            )
            for result in self.results
        ]
        headers = ("Actuator", "Status", "Delta mA", "Attempts", "Detail")
        widths = [len(value) for value in headers]
        for row in rows:
            for index, value in enumerate(row):
                widths[index] = max(widths[index], len(value))
        template = "  ".join(f"{{:<{width}}}" for width in widths)
        print(template.format(*headers))
        print(template.format(*(('-' * width) for width in widths)))
        for row in rows:
            print(template.format(*row))
        if self.leave_power_on:
            print("\nWarning: the PSU connection and PSU were intentionally left on.")

    @staticmethod
    def _last_detection(
        records: Sequence[dict[str, Any]], actuator: int
    ) -> dict[str, Any] | None:
        detections = [
            record
            for record in records
            if record.get("actuator") == actuator
            and "state" in record
            and "delta_ma" in record
        ]
        return detections[-1] if detections else None

    @staticmethod
    def _error_message(records: Sequence[dict[str, Any]]) -> str:
        errors = [record for record in records if record.get("event") == "error"]
        if not errors:
            return "The terminal command failed without a structured error message."
        return str(errors[-1].get("message", "Unknown terminal error."))


def target_delta(value: str) -> float:
    parsed = float(value)
    if not 0.05 <= parsed <= 3.0:
        raise argparse.ArgumentTypeError("must be between 0.05 and 3.0 mA")
    return parsed


def actuator_index(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 23:
        raise argparse.ArgumentTypeError("must be between 0 and 23")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect Lansing actuators and initialize each one until its current "
            "delta reaches a target or stops improving."
        )
    )
    parser.add_argument("--port", required=True, help="Lansing serial port")
    parser.add_argument(
        "--target-delta-ma",
        required=True,
        type=target_delta,
        help="target current delta in mA (0.05 through 3.0)",
    )
    parser.add_argument(
        "--actuators",
        nargs="+",
        type=actuator_index,
        default=list(range(24)),
        metavar="N",
        help="actuator indices to process (default: 0 through 23)",
    )
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Python executable used to run fluidreality_terminal.py (default: current Python)",
    )
    parser.add_argument(
        "--terminal-path",
        type=Path,
        default=Path(__file__).with_name("fluidreality_terminal.py"),
        help="path to Fluid Reality Terminal fluidreality_terminal.py",
    )
    parser.add_argument(
        "--max-initialization-attempts",
        type=int,
        default=10,
        metavar="N",
        help="maximum initialization attempts per actuator (default: 10)",
    )
    parser.add_argument(
        "--minimum-delta-improvement-ma",
        type=float,
        default=0.0,
        metavar="MA",
        help="required strict delta improvement per attempt (default: 0)",
    )
    parser.add_argument(
        "--leave-power-on",
        action="store_true",
        help="do not turn the PSU connection and PSU off during cleanup",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_initialization_attempts < 1:
        parser.error("--max-initialization-attempts must be at least 1")
    if args.minimum_delta_improvement_ma < 0:
        parser.error("--minimum-delta-improvement-ma must be non-negative")
    terminal_path = args.terminal_path.resolve()
    if not terminal_path.is_file():
        parser.error(f"terminal was not found at {terminal_path}")

    initializer = LansingInitializer(
        port=args.port,
        target_delta_ma=args.target_delta_ma,
        actuators=args.actuators,
        python_executable=args.python_executable,
        terminal_path=terminal_path,
        max_attempts=args.max_initialization_attempts,
        minimum_improvement_ma=args.minimum_delta_improvement_ma,
        leave_power_on=args.leave_power_on,
        verbose=args.verbose,
    )
    return initializer.run()


if __name__ == "__main__":
    raise SystemExit(main())
