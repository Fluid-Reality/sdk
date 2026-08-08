"""Local console and script harness for the simulator core."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .clock import ManualClock
from .config import load_config
from .engine import LansingSimulator, load_script


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lansing-simulator",
        description=(
            "Run the Lansing firmware simulator through a local command harness. "
            "A serial backend will be selected separately."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config.example.toml"), help="TOML configuration"
    )
    parser.add_argument("--script", type=Path, help="Execute commands from a UTF-8 text file")
    parser.add_argument(
        "--real-time",
        action="store_true",
        help="Use configured wall-clock timing when executing a script",
    )
    parser.add_argument("--no-banner", action="store_true", help="Do not print OK:READY")
    return parser


def _write(data: bytes) -> None:
    if data:
        sys.stdout.write(data.decode("ascii", errors="replace"))
        sys.stdout.flush()


def _run_line(simulator: LansingSimulator, line: str, manual_clock: ManualClock | None) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped in {".quit", ".exit"}:
        return False
    if stripped.startswith(".advance "):
        if manual_clock is None:
            print("Local command .advance requires scripted/manual time.", file=sys.stderr)
            return True
        seconds = float(stripped.split(maxsplit=1)[1])
        manual_clock.advance(seconds)
        _write(simulator.tick())
        return True
    if stripped == ".tick":
        _write(simulator.tick())
        return True
    _write(simulator.feed_bytes(line.encode("ascii") + b"\n"))
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        manual_clock = ManualClock() if args.script and not args.real_time else None
        simulator = LansingSimulator(config, clock=manual_clock)
        try:
            if not args.no_banner:
                _write(simulator.startup_bytes())
            if args.script:
                for line in load_script(args.script):
                    if not _run_line(simulator, line, manual_clock):
                        break
                return 0

            print(
                "Local Lansing simulator console. Enter firmware commands; use .tick or .quit.",
                file=sys.stderr,
            )
            while True:
                try:
                    line = input("sim> ")
                except (EOFError, KeyboardInterrupt):
                    print(file=sys.stderr)
                    break
                if not _run_line(simulator, line, None):
                    break
            return 0
        finally:
            simulator.close()
    except (OSError, ValueError) as exc:
        print(f"lansing-simulator: {exc}", file=sys.stderr)
        return 2
