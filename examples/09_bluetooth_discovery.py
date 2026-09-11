"""Discover Fluid Reality Bluetooth boards and optionally connect to one."""

from __future__ import annotations

import argparse

from fluid_reality import Rockford, discover_bluetooth_boards


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--connect",
        type=int,
        metavar="INDEX",
        help="Connect to the board at this result index.",
    )
    parser.add_argument("--access-token", help="Optional Bluetooth access token.")
    parser.add_argument("--pair", action="store_true")
    args = parser.parse_args()

    devices = discover_bluetooth_boards(timeout=args.timeout)
    if not devices:
        print("No Fluid Reality Bluetooth boards found.")
        return
    for index, device in enumerate(devices):
        signal = "unknown" if device.rssi is None else f"{device.rssi} dBm"
        print(f"[{index}] {device.name} — {signal} — {device.endpoint}")

    if args.connect is None:
        return
    if not 0 <= args.connect < len(devices):
        parser.error(f"--connect must be between 0 and {len(devices) - 1}")
    selected = devices[args.connect]
    with Rockford(
        selected.endpoint,
        network_token=args.access_token,
        pair=args.pair,
    ) as board:
        print(board.firmware_version())
        print(board.bluetooth_status())


if __name__ == "__main__":
    main()
