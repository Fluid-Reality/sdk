"""Pulse one actuator until a key is pressed.

Each cycle:
1. Enable actuator with ACT for the configured active time.
2. Disable actuator with ACT 0, which starts firmware discharge.
3. Wait at least the configured rest time and keep polling until discharge is over.
"""

from __future__ import annotations

import argparse
import sys
import time

from fluid_reality import ActuatorState, Lansing


def key_pressed() -> bool:
    if sys.platform.startswith("win"):
        import msvcrt

        if msvcrt.kbhit():
            msvcrt.getch()
            return True
        return False

    import select

    readable, _, _ = select.select([sys.stdin], [], [], 0)
    if readable:
        sys.stdin.readline()
        return True
    return False


def wait_for_discharge(board: Lansing, actuator: int, minimum_wait_s: float) -> None:
    start = time.perf_counter()
    while True:
        status = board.status()
        discharge_left = status["discharge_ms_left"][actuator]
        elapsed = time.perf_counter() - start
        if elapsed >= minimum_wait_s and discharge_left <= 0:
            return
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--value", type=int, default=180)
    parser.add_argument("--active-s", type=float, default=1.0)
    parser.add_argument("--rest-s", type=float, default=1.0)
    args = parser.parse_args()

    print("Press any key to stop after the current cycle.")

    with Lansing(args.port) as board:
        board.force_text_mode()
        board.psu_on()
        board.psc_on()
        state = board.detect(args.actuator)
        if state is not ActuatorState.READY:
            raise RuntimeError(f"Actuator {args.actuator} is {state.value}")

        cycle = 0
        while not key_pressed():
            cycle += 1
            print(f"cycle {cycle}: ACT {args.actuator} {args.value}")
            board.set_actuator(args.actuator, args.value)
            time.sleep(args.active_s)

            print(f"cycle {cycle}: ACT {args.actuator} 0")
            board.set_actuator(args.actuator, 0)
            wait_for_discharge(board, args.actuator, args.rest_s)

        board.all_actuators_off()
        wait_for_discharge(board, args.actuator, args.rest_s)
        print("stopped")


if __name__ == "__main__":
    main()

