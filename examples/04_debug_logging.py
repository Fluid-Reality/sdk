"""Capture SDK and firmware debug output while pulsing one actuator."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from fluid_reality import ActuatorState

from _common import add_connection_arguments, connect_power, open_board, shutdown_power


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=120)
    parser.add_argument(
        "--debug-out",
        default="fluid-reality-debug.log",
        help="Debug destination file path, or none",
    )
    args = parser.parse_args()

    debug_destination = None if args.debug_out.lower() == "none" else Path(args.debug_out)

    with open_board(args) as board:
        board.set_debug_out(debug_destination)
        board.firmware_debug(True)
        connect_power(board)
        state = board.detect(args.actuator)
        if state is not ActuatorState.READY:
            raise RuntimeError(f"Actuator {args.actuator} is {state.value}")

        try:
            board.set_actuator(args.actuator, args.value)
            time.sleep(0.5)
        finally:
            try:
                board.firmware_debug(False)
            finally:
                shutdown_power(board)


if __name__ == "__main__":
    main()
