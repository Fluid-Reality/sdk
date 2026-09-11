"""Use low-level output and timed current measurement for controlled bench tests."""

from __future__ import annotations

import argparse
import time

from _common import add_connection_arguments, connect_power, open_board, shutdown_power


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--actuator", type=int, default=0)
    parser.add_argument(
        "--output-a",
        type=int,
        default=180,
        help="Rockford top output or Lansing positive-electrode output.",
    )
    parser.add_argument(
        "--output-b",
        type=int,
        default=0,
        help="Rockford digital bottom state (0/1) or Lansing negative output.",
    )
    parser.add_argument("--hold-s", type=float, default=1.0)
    parser.add_argument("--measurement-ms", type=int, default=250)
    args = parser.parse_args()

    with open_board(args) as board:
        connect_power(board)
        board.safety(False)
        try:
            if board.direct_top_bottom_output:
                current_ma = board.manual_output_current(
                    args.actuator,
                    args.output_a,
                    args.output_b,
                    args.measurement_ms,
                )
                print(f"measured current: {current_ma:.2f} mA")
                remaining_s = max(0.0, args.hold_s - args.measurement_ms / 1000.0)
                time.sleep(remaining_s)
            else:
                board.set_manual_output(args.actuator, args.output_a, args.output_b)
                time.sleep(args.hold_s)
                print(board.get_manual_output(args.actuator))
        finally:
            if board.direct_top_bottom_output:
                board.manual_output_current(args.actuator, 0, 0, 1)
            else:
                board.set_manual_output(args.actuator, 0, 0)
            try:
                board.safety(True)
            finally:
                shutdown_power(board)


if __name__ == "__main__":
    main()
