"""Restore a Rockford board to factory settings over USB serial."""

from __future__ import annotations

import argparse

from fluid_reality import Rockford

from _common import add_connection_arguments, open_board


CONFIRMATION = "FACTORY_RESET"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument(
        "--confirm",
        metavar=CONFIRMATION,
        help=f"Required safety confirmation: --confirm {CONFIRMATION}",
    )
    args = parser.parse_args()
    if args.board != "rockford":
        parser.error("Factory reset is implemented for Rockford boards")
    if args.confirm != CONFIRMATION:
        parser.error(f"factory reset requires --confirm {CONFIRMATION}")

    with open_board(args) as board:
        if not isinstance(board, Rockford):
            parser.error("Factory reset requires a Rockford board")
        endpoint = str(getattr(board.transport, "endpoint", ""))
        if endpoint.lower().startswith(("tcp://", "tls://", "ble://")):
            parser.error("Factory reset is available only over USB serial")
        board.factory_reset()
    print("Factory settings restored. The board is rebooting.")


if __name__ == "__main__":
    main()
