"""Use direct electrode output for controlled bench testing."""

from __future__ import annotations

import argparse
import time

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--positive", type=int, default=180)
    parser.add_argument("--negative", type=int, default=0)
    parser.add_argument("--hold-s", type=float, default=1.0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        board.safety(False)
        try:
            board.set_manual_output(args.actuator, args.positive, args.negative)
            time.sleep(args.hold_s)
            print(board.get_manual_output(args.actuator))
        finally:
            board.set_manual_output(args.actuator, 0, 0)
            board.safety(True)


if __name__ == "__main__":
    main()

