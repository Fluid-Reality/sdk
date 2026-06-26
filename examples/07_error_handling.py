"""Show structured handling for firmware ER responses."""

from __future__ import annotations

import argparse

from fluid_reality import FirmwareError, Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        try:
            board.set_actuator(args.actuator, 180)
        except FirmwareError as error:
            print(f"code: {error.code}")
            if error.info is not None:
                print(f"meaning: {error.info.meaning}")
                print(f"common cause: {error.info.common_cause}")
                print(f"recovery: {error.info.recovery}")


if __name__ == "__main__":
    main()

