"""Show structured handling for firmware ER responses."""

from __future__ import annotations

import argparse

from fluid_reality import ActuatorState, FirmwareError, FluidRealityError

from _common import add_connection_arguments, open_board, shutdown_power


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    args = parser.parse_args()

    try:
        with open_board(args) as board:
            try:
                board.power_on()
                state = board.detect(args.actuator)
                if state is not ActuatorState.READY:
                    raise RuntimeError(f"Actuator {args.actuator} is {state.value}")
                board.set_actuator(args.actuator, 180)
            finally:
                shutdown_power(board)
    except FirmwareError as error:
        print(f"code: {error.code}")
        if error.info is not None:
            print(f"meaning: {error.info.meaning}")
            print(f"common cause: {error.info.common_cause}")
            print(f"recovery: {error.info.recovery}")
    except RuntimeError as error:
        print(f"runtime error: {error}")
    except FluidRealityError as error:
        print(f"SDK or transport error: {error}")


if __name__ == "__main__":
    main()
