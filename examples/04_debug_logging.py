"""Enable firmware debug messages and route DBG lines through Python logging."""

from __future__ import annotations

import argparse
import logging
import time

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=120)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    logger = logging.getLogger("fluid_reality.debug")

    with Lansing(args.port, debug_logger=logger, log_debug_messages=True) as board:
        board.firmware_debug(True)
        board.psu_on()
        board.psc_on()
        board.set_actuator(args.actuator, args.value)
        time.sleep(0.5)
        board.set_actuator(args.actuator, 0)
        board.firmware_debug(False)


if __name__ == "__main__":
    main()

