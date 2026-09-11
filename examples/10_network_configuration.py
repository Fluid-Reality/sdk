"""Inspect or update Rockford Wi-Fi, IP, hostname, and TCP configuration."""

from __future__ import annotations

import argparse
import pprint

from fluid_reality import WifiBoard

from _common import add_connection_arguments, open_board


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_connection_arguments(parser)
    parser.add_argument("--mode", choices=("client", "access-point"))
    parser.add_argument("--hostname")
    parser.add_argument("--dhcp", action="store_true")
    parser.add_argument(
        "--static",
        nargs=5,
        metavar=("ADDRESS", "SUBNET", "GATEWAY", "DNS1", "DNS2"),
    )
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--tcp-port", type=int)
    args = parser.parse_args()

    with open_board(args) as board:
        if not isinstance(board, WifiBoard):
            parser.error("Network configuration requires a Wi-Fi-capable board")
        if args.mode:
            pprint.pp(board.set_wifi_mode(args.mode))
        if args.hostname:
            pprint.pp(board.network_hostname(args.hostname))
        if args.dhcp:
            pprint.pp(board.use_dhcp("WIFI"))
        if args.static:
            address, subnet, gateway, dns1, dns2 = args.static
            pprint.pp(
                board.set_static_ipv4(
                    address, subnet, gateway, dns1, dns2, interface="WIFI"
                )
            )
        if args.tcp_port is not None:
            pprint.pp(board.configure_tcp(enabled=True, port=args.tcp_port))
        if args.scan:
            pprint.pp(board.start_wifi_scan())
            print("Scan started; query again after it completes to list networks.")

        print(f"Wi-Fi mode: {board.wifi_mode()}")
        print("Interfaces:", board.network_interfaces())
        pprint.pp(board.network_status("WIFI"))
        pprint.pp(board.network_diagnostics("WIFI"))


if __name__ == "__main__":
    main()
