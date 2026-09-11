"""Print firmware identity, capabilities, and a full board status snapshot."""

from __future__ import annotations

import argparse
import pprint

from _common import add_connection_arguments, open_board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    args = parser.parse_args()

    with open_board(args) as board:
        pprint.pp(board.firmware_version())
        pprint.pp(board.capabilities())
        pprint.pp(board.status())


if __name__ == "__main__":
    main()
