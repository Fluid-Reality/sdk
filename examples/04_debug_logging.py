"""Enable SDK and firmware debug output."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from fluid_reality import ActuatorState, Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=120)
    parser.add_argument(
        "--debug-out",
        default="lansing-debug.log",
        help="Debug destination file path, or none",
    )
    args = parser.parse_args()

    debug_destination = None if args.debug_out.lower() == "none" else Path(args.debug_out)

    with Lansing(args.port) as board:
        board.set_debug_out(debug_destination)
        board.firmware_debug(True)
        board.psu_on()
        board.psc_on()
        state = board.detect(args.actuator)
        if state is not ActuatorState.READY:
            raise RuntimeError(f"Actuator {args.actuator} is {state.value}")

        board.set_actuator(args.actuator, args.value)
        time.sleep(0.5)
        board.all_actuators_off()
        board.firmware_debug(False)


if __name__ == "__main__":
    main()

