"""Detect and initialize one actuator with the SDK stateful workflow."""

from __future__ import annotations

import argparse

from fluid_reality import ActuatorState, Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        board.psu_on()
        board.psc_on()

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
        print(f"state: {state.value}")


if __name__ == "__main__":
    main()

