"""Stream a sine wave to one actuator and print the achieved refresh rate."""

from __future__ import annotations

import argparse
import sys

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
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
        help="Optionally set firmware CFG MAX before streaming. This config is persistent on the board.",
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

    with Lansing(args.port) as board:
        if not args.no_sync:
            board.force_text_mode()
        if args.max_active_ms is not None:
            board.max_active_time_ms(args.max_active_ms)
        else:
            max_active_ms = board.max_active_time_ms()
            if args.duration_s * 1000 >= max_active_ms:
                print(
                    "warning: duration is longer than CFG MAX; firmware will force discharge mid-stream. "
                    f"Use --max-active-ms {int(args.duration_s * 1000) + 1000} for this test.",
                    file=sys.stderr,
                )
        board.psu_on()
        board.psc_on()
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

        print(f"achieved refresh rate: {rate:.1f} Hz")


if __name__ == "__main__":
    main()
