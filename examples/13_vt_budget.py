"""Inspect or deliberately change Rockford's persistent actuator VT budget."""

from __future__ import annotations

import argparse

from _common import add_connection_arguments, open_board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument(
        "--set",
        type=int,
        dest="new_limit_vs",
        help="New per-actuator voltage-time limit in V·s.",
    )
    parser.add_argument(
        "--confirm-risk",
        action="store_true",
        help="Required with --set; confirms the permanent hardware-damage warning.",
    )
    args = parser.parse_args()

    with open_board(args) as board:
        method = getattr(board, "vt_limit_vs", None)
        if method is None:
            raise RuntimeError("The selected board does not support a VT budget.")

        before = board.read_config()
        print(f"VT budget: {before.vt_limit_vs:,} V·s")
        print(
            "Configuration history: "
            + ("user modified" if before.vt_limit_modified else "factory default")
        )

        if args.new_limit_vs is None:
            return
        if not args.confirm_risk:
            parser.error(
                "--set requires --confirm-risk. An incorrect VT budget can permanently "
                "damage actuators or board electronics."
            )

        method(args.new_limit_vs)
        after = board.read_config()
        print(f"New VT budget: {after.vt_limit_vs:,} V·s")
        print("Configuration history: user modified (permanent audit mark)")


if __name__ == "__main__":
    main()
