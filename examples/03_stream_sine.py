"""Stream a sine wave to one actuator and print the achieved refresh rate."""

from __future__ import annotations

import argparse
import sys

from fluid_reality import ActuatorState

from _common import (
    add_connection_arguments,
    open_board,
    shutdown_power,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument("--duration-s", type=float, default=5.0)
    parser.add_argument("--frequency-hz", type=float, default=1.0)
    parser.add_argument("--update-hz", type=float, default=200.0)
    parser.add_argument(
        "--minimum",
        type=int,
        default=1,
        help="Minimum streamed value. Default is 1 because 0 means off/discharge.",
    )
    parser.add_argument("--maximum", type=int, default=255)
    parser.add_argument(
        "--max-active-ms",
        type=int,
        default=None,
        help="Lansing only: optionally set persistent firmware CFG MAX before streaming.",
    )
    parser.add_argument(
        "--vt-limit-vs",
        type=int,
        default=None,
        help="Rockford only: set the persistent per-actuator VT budget in V·s.",
    )
    parser.add_argument(
        "--confirm-vt-risk",
        action="store_true",
        help="Required with --vt-limit-vs; confirms the hardware-damage warning.",
    )
    parser.add_argument(
        "--print-values",
        action="store_true",
        help="Print each actuator value as it is streamed.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip startup serial recovery. By default the example forces text mode before sending commands.",
    )
    args = parser.parse_args()

    def print_value(actuator: int, value: int, elapsed_s: float) -> None:
        print(f"{elapsed_s:.6f}s actuator={actuator} value={value}")

    with open_board(args) as board:
        if not args.no_sync:
            board.force_text_mode()
        vt_config = getattr(board, "vt_limit_vs", None)
        if vt_config is not None:
            if args.max_active_ms is not None:
                parser.error("--max-active-ms applies to Lansing, not Rockford")
            if args.vt_limit_vs is not None:
                if not args.confirm_vt_risk:
                    parser.error(
                        "--vt-limit-vs requires --confirm-vt-risk because an incorrect "
                        "limit can permanently damage actuators or board electronics"
                    )
                vt_config(args.vt_limit_vs)
            print(
                f"Rockford VT budget: {vt_config():,} V·s; the firmware integrates "
                "actual drive voltage and forces a gentle discharge at the limit."
            )
        else:
            if args.vt_limit_vs is not None:
                parser.error("--vt-limit-vs applies to Rockford, not Lansing")
            if args.max_active_ms is not None:
                board.max_active_time_ms(args.max_active_ms)
            max_active_ms = board.max_active_time_ms()
            if args.duration_s * 1000 >= max_active_ms:
                print(
                    "warning: duration is longer than CFG MAX; firmware will force discharge mid-stream. "
                    f"Use --max-active-ms {int(args.duration_s * 1000) + 1000} for this test.",
                    file=sys.stderr,
                )
        board.power_on()
        try:
            state = board.detect(args.actuator)
            if state is not ActuatorState.READY:
                raise RuntimeError(f"Actuator {args.actuator} is {state.value}")
            board.enter_stream_mode()
            completed = False
            try:
                rate = board.stream_sine(
                    args.actuator,
                    duration_s=args.duration_s,
                    frequency_hz=args.frequency_hz,
                    update_hz=args.update_hz,
                    minimum=args.minimum,
                    maximum=args.maximum,
                    value_callback=print_value if args.print_values else None,
                )
                completed = True
            finally:
                if not completed:
                    board.stream_actuator(args.actuator, 0)
                    if args.print_values:
                        print_value(args.actuator, 0, args.duration_s)
                board.exit_stream_mode()
        finally:
            shutdown_power(board)

        print(f"achieved refresh rate: {rate:.1f} Hz")


if __name__ == "__main__":
    main()
