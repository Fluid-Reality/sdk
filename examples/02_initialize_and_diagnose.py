"""Detect, initialize when needed, and report one actuator's current delta."""

from __future__ import annotations

import argparse

from fluid_reality import ActuatorState

from _common import add_connection_arguments, open_board, shutdown_power


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    with open_board(args) as board:
        board.power_on()
        try:
            state = board.detect(args.actuator)
            if state is ActuatorState.ERROR:
                state = board.initialize(args.actuator)
            if state is not ActuatorState.READY:
                raise RuntimeError(f"Actuator {args.actuator} is {state.value}")

            diagnosis = board.last_detection(args.actuator)
            if diagnosis is None:
                raise RuntimeError("No actuator detection result is available")

            print(f"actuator: {diagnosis.actuator}")
            print(f"baseline: {diagnosis.baseline_ma:.2f} mA")
            print(f"forward: {diagnosis.forward_ma:.2f} mA")
            print(f"discharge: {diagnosis.discharge_ma:.2f} mA")
            print(f"delta: {diagnosis.delta_ma:.2f} mA")
            print(f"state: {state.value}")
        finally:
            shutdown_power(board)


if __name__ == "__main__":
    main()
