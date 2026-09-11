"""Update board firmware over USB serial, TCP, or TLS."""

from __future__ import annotations

import argparse
from pathlib import Path

from _common import add_connection_arguments, open_board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument(
        "--firmware", required=True, type=Path, help="Firmware .bin image."
    )
    args = parser.parse_args()

    last_percent = -1

    def show_progress(written: int, total: int) -> None:
        nonlocal last_percent
        percent = round(written * 100 / total)
        if percent != last_percent:
            print(f"\rUploading: {percent:3d}%", end="", flush=True)
            last_percent = percent

    with open_board(args) as board:
        result = board.update_firmware(args.firmware, progress=show_progress)
    print()
    print(f"Verified {result.size:,} bytes")
    print(f"SHA-256: {result.sha256}")
    print("The board is rebooting into the new firmware.")


if __name__ == "__main__":
    main()
