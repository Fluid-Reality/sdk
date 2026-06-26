"""Initialize one actuator and then run the firmware diagnosis routine."""

from __future__ import annotations

import argparse

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        board.psu_on()
        board.psc_on()

        board.initialize_actuator(args.actuator)
        diagnosis = board.diagnose_actuator(args.actuator)

        print(f"actuator: {diagnosis.actuator}")
        print(f"baseline: {diagnosis.baseline_ma:.2f} mA")
        print(f"forward: {diagnosis.forward_ma:.2f} mA")
        print(f"discharge: {diagnosis.discharge_ma:.2f} mA")


if __name__ == "__main__":
    main()

