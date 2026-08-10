"""Command-line serial-to-TCP Device Bridge."""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path
from typing import TextIO

SDK_SOURCE = Path(__file__).resolve().parents[2] / "src"
if SDK_SOURCE.is_dir():
    sys.path.insert(0, str(SDK_SOURCE))

from bridge import DeviceBridge, TraceEvent, wait_until_stopped
from serial.tools import list_ports


def tcp_endpoint(value: str) -> tuple[str, int]:
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError("expected HOST:PORT")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("TCP port must be an integer") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("TCP port must be between 0 and 65535")
    return host.strip("[]"), port


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Bridge a physical serial device to raw TCP and optionally trace every byte.",
    )
    result.add_argument("serial_port", nargs="?", help="Physical port, such as COM9 or /dev/ttyACM0")
    result.add_argument("--tcp", default=("127.0.0.1", 8765), type=tcp_endpoint, metavar="HOST:PORT")
    result.add_argument("--baud", type=int, default=250000)
    result.add_argument("--alias", default="COM66", help="SDK virtual-port name shown in setup hints")
    result.add_argument("--list-ports", action="store_true", help="List physical serial ports and exit")
    result.add_argument("--trace", choices=("hex", "ascii", "both"), help="Print TX/RX data to the terminal")
    result.add_argument("--log", type=Path, metavar="FILE", help="Write a complete TX/RX trace to a file")
    result.add_argument("--append", action="store_true", help="Append instead of replacing --log")
    result.add_argument("--no-timestamps", action="store_true")
    result.add_argument("--quiet", action="store_true", help="Suppress status output (not trace output)")
    result.add_argument("--read-size", type=int, default=4096)
    result.add_argument("--serial-timeout", type=float, default=0.1)
    result.add_argument("--write-timeout", type=float, default=1.0)
    result.add_argument("--bytesize", type=int, choices=(5, 6, 7, 8), default=8)
    result.add_argument("--parity", choices=("N", "E", "O", "M", "S"), default="N")
    result.add_argument("--stopbits", type=float, choices=(1, 1.5, 2), default=1)
    result.add_argument("--xonxoff", action="store_true")
    result.add_argument("--rtscts", action="store_true")
    result.add_argument("--dsrdtr", action="store_true")
    return result


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def print_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("No physical serial ports found.")
        return
    for item in ports:
        print(f"{item.device}\t{item.description}\t{item.hwid}")


def run(arguments: argparse.Namespace) -> int:
    if arguments.list_ports:
        print_ports()
        return 0
    if not arguments.serial_port:
        parser().error("serial_port is required unless --list-ports is used")

    log_file: TextIO | None = None
    if arguments.log:
        log_file = arguments.log.open("a" if arguments.append else "w", encoding="utf-8", buffering=1)

    def status(message: str) -> None:
        if not arguments.quiet:
            print(f"STATUS {message}", flush=True)

    def trace(event: TraceEvent) -> None:
        line = event.format(arguments.trace or "both", timestamp=not arguments.no_timestamps)
        if arguments.trace:
            print(line, flush=True)
        if log_file is not None:
            print(line, file=log_file, flush=True)

    host, port = arguments.tcp
    bridge = DeviceBridge(
        arguments.serial_port,
        baudrate=arguments.baud,
        host=host,
        port=port,
        serial_timeout=arguments.serial_timeout,
        write_timeout=arguments.write_timeout,
        read_size=arguments.read_size,
        bytesize=arguments.bytesize,
        parity=arguments.parity,
        stopbits=arguments.stopbits,
        xonxoff=arguments.xonxoff,
        rtscts=arguments.rtscts,
        dsrdtr=arguments.dsrdtr,
        trace=trace if arguments.trace or log_file is not None else None,
        status=status,
    )
    try:
        bridge.start()
        mapping = f"{arguments.alias}={bridge.endpoint}"
        print(f'PowerShell:     $env:FLUID_REALITY_VIRTUAL_PORTS="{mapping}"')
        print(f"Command Prompt: set FLUID_REALITY_VIRTUAL_PORTS={mapping}")
        print(f'macOS/Linux:    export FLUID_REALITY_VIRTUAL_PORTS="{mapping}"')
        if not is_loopback(host):
            print("WARNING: TCP is unauthenticated and unencrypted; use only on a trusted network.")
            print("Remote clients must replace the bind address with this computer's reachable IP.")
        print("Press Ctrl+C to stop.")
        wait_until_stopped(bridge)
        return 0
    finally:
        bridge.stop()
        if log_file is not None:
            log_file.close()


def main() -> int:
    return run(parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
