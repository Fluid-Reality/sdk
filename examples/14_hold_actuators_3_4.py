"""Hold actuators 3 and 4 at 100% output until interrupted.

Press Ctrl+C to command both actuators off and power down the board.  The
controller's safety limits remain in effect; this program deliberately does
not re-enable an output that firmware has stopped.
"""

from __future__ import annotations

import argparse
import time

from fluid_reality import ActuatorState

from _common import add_connection_arguments, open_board, shutdown_power


ACTUATORS = (3, 4)
FULL_OUTPUT = 255


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument(
        "--status-interval-s",
        type=float,
        default=5.0,
        help="Seconds between status reports (default: 5).",
    )
    args = parser.parse_args()
    if args.status_interval_s <= 0:
        parser.error("--status-interval-s must be positive")

    with open_board(args) as board:
        board.force_text_mode()
        board.power_on()
        try:
            for actuator in ACTUATORS:
                state = board.detect(actuator)
                if state is not ActuatorState.READY:
                    raise RuntimeError(f"Actuator {actuator} is {state.value}")

            for actuator in ACTUATORS:
                board.set_actuator(actuator, FULL_OUTPUT)

            print(
                "Actuators 3 and 4 are commanded to 100%. "
                "Press Ctrl+C to turn them off."
            )
            while True:
                values = board.get_actuators()
                print(
                    f"outputs: actuator 3={values[3]}, actuator 4={values[4]}"
                )
                time.sleep(args.status_interval_s)
        except KeyboardInterrupt:
            print("Stopping: turning actuators 3 and 4 off.")
        finally:
            shutdown_power(board)


if __name__ == "__main__":
    main()
