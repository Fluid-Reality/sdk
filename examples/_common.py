"""Shared command-line connection helpers for Fluid Reality examples."""

from __future__ import annotations

import argparse
from pathlib import Path

from fluid_reality import Board, Lansing, Rockford


BOARD_TYPES: dict[str, type[Board]] = {
    "rockford": Rockford,
    "lansing": Lansing,
}


def add_connection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "endpoint",
        nargs="?",
        help=(
            "USB serial port, tcp://host:port, tls://host:port, or ble://device. "
            "Omit when using --connection-file."
        ),
    )
    parser.add_argument(
        "--board",
        choices=sorted(BOARD_TYPES),
        default="rockford",
        help="Board hardware profile (default: rockford).",
    )
    parser.add_argument(
        "--connection-file",
        type=Path,
        help="Fluid Reality YAML connection profile; overrides endpoint options.",
    )
    parser.add_argument(
        "--access-token",
        help="Access token for TCP, TLS, or Bluetooth authentication.",
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="Ask the operating system to pair when connecting over Bluetooth.",
    )
    parser.add_argument(
        "--tls-ca-file",
        help="CA or self-signed board certificate for a direct TLS endpoint.",
    )
    parser.add_argument(
        "--tls-server-hostname",
        help="Expected TLS certificate hostname when it differs from the endpoint.",
    )


def open_board(args: argparse.Namespace) -> Board:
    """Create the selected board from shared parsed CLI arguments."""

    board_type = BOARD_TYPES[args.board]
    if args.connection_file is not None:
        if args.endpoint is not None:
            raise ValueError("Do not provide endpoint with --connection-file")
        overrides = {}
        if args.access_token is not None:
            overrides["network_token"] = args.access_token
        if args.pair:
            overrides["pair"] = True
        return board_type.from_connection_file(args.connection_file, **overrides)

    if not args.endpoint:
        raise ValueError("Provide an endpoint or --connection-file")
    options: dict[str, object] = {}
    if args.access_token is not None:
        options["network_token"] = args.access_token
    if args.pair:
        options["pair"] = True
    if args.tls_ca_file is not None:
        options["tls_ca_file"] = args.tls_ca_file
    if args.tls_server_hostname is not None:
        options["tls_server_hostname"] = args.tls_server_hostname
    return board_type(args.endpoint, **options)


def connect_power(board: Board) -> None:
    """Turn on the supply and connect its output when the board exposes PSC."""

    board.psu_on()
    board.psc_on()


def shutdown_power(board: Board) -> None:
    """Best-effort output and power shutdown for example cleanup paths."""

    for operation in (board.all_actuators_off, board.psc_off, board.psu_off):
        try:
            operation()
        except Exception:
            pass


def describe_connection_help() -> str:
    return (
        "Examples: COM18, tcp://192.168.24.1:49765, "
        "ble://FR-Rockford; use --connection-file for TLS profiles."
    )
