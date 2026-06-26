"""Turn on PSU, measure voltage/current, and pulse one actuator."""

from __future__ import annotations

import argparse
import time

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=180)
    parser.add_argument("--hold-s", type=float, default=1.0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        board.psu_on()
        print(f"voltage before connect: {board.voltage():.2f} V")

        board.psc_on()
        board.set_actuator(args.actuator, args.value)
        time.sleep(args.hold_s)

        print(f"current while active: {board.current():.2f} mA")
        board.set_actuator(args.actuator, 0)


if __name__ == "__main__":
    main()

