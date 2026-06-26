"""Print a full Lansing status snapshot."""

from __future__ import annotations

import argparse
import pprint

from fluid_reality import Lansing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("port", help="Serial port, for example COM5")
    args = parser.parse_args()

    with Lansing(args.port) as board:
        pprint.pp(board.status())


if __name__ == "__main__":
    main()

