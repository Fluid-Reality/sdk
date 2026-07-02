"""Show structured handling for firmware ER responses."""

from __future__ import annotations

import argparse

from fluid_reality import ActuatorState, FirmwareError, Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    with Lansing(args.port) as board:
        try:
            board.psu_on()
            board.psc_on()
            state = board.detect(args.actuator)
            if state is not ActuatorState.READY:
                raise RuntimeError(f"Actuator {args.actuator} is {state.value}")
            board.set_actuator(args.actuator, 180)
        except FirmwareError as error:
            print(f"code: {error.code}")
            if error.info is not None:
                print(f"meaning: {error.info.meaning}")
                print(f"common cause: {error.info.common_cause}")
                print(f"recovery: {error.info.recovery}")
        except RuntimeError as error:
            print(f"runtime error: {error}")


if __name__ == "__main__":
    main()

