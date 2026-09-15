"""Connect to a board, detect one actuator, and run a safe output pulse."""

from __future__ import annotations

import argparse
import time

from fluid_reality import ActuatorState

from _common import add_connection_arguments, open_board, shutdown_power


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=180)
    parser.add_argument("--hold-s", type=float, default=1.0)
    args = parser.parse_args()

    with open_board(args) as board:
        board.power_on()
        try:
            time.sleep(0.5)
            voltage_deadline = time.monotonic() + 1.5
            supply_voltage = board.voltage()
            while supply_voltage <= 0.0 and time.monotonic() < voltage_deadline:
                time.sleep(0.1)
                supply_voltage = board.voltage()
            if supply_voltage <= 0.0:
                raise RuntimeError(
                    "Supply voltage remained at 0 V after power-on; check the "
                    "power adapter and controller voltage feedback."
                )
            print(f"supply voltage: {supply_voltage:.2f} V")
            state = board.detect(args.actuator)
            if state is not ActuatorState.READY:
                raise RuntimeError(f"Actuator {args.actuator} is {state.value}")
            board.set_actuator(args.actuator, args.value)
            time.sleep(args.hold_s)
            print(f"current while active: {board.current():.2f} mA")
        finally:
            shutdown_power(board)


if __name__ == "__main__":
    main()
