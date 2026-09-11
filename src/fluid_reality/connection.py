"""Portable YAML connection profiles for Fluid Reality boards."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml

from .boards.board import Board

PROFILE_FORMAT = "fluid-reality-connection"
PROFILE_VERSION = 1
BoardType = TypeVar("BoardType", bound=Board)


class _ConnectionDumper(yaml.SafeDumper):
    pass


def _represent_connection_string(
    dumper: yaml.SafeDumper, value: str
) -> yaml.nodes.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ConnectionDumper.add_representer(str, _represent_connection_string)


@dataclass(frozen=True)
class ConnectionProfile:
    """Validated parameters required to open one board connection."""

    transport: str
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    bluetooth_device: str | None = None
    bluetooth_pair: bool = False
    baudrate: int = 250000
    access_token: str | None = field(default=None, repr=False)
    tls_certificate: str | None = field(default=None, repr=False)
    tls_certificate_file: str | None = None
    tls_server_hostname: str | None = None
    tls_verify_hostname: bool = True

    def __post_init__(self) -> None:
        transport = self.transport.strip().lower()
        object.__setattr__(self, "transport", transport)
        if transport not in {"serial", "tcp", "tls", "bluetooth"}:
            raise ValueError("connection transport must be serial, tcp, tls, or bluetooth")
        if transport == "serial":
            if not self.serial_port or not self.serial_port.strip():
                raise ValueError("serial connection requires serial_port")
            if self.baudrate <= 0:
                raise ValueError("baudrate must be greater than zero")
        elif transport in {"tcp", "tls"}:
            if not self.host or not self.host.strip():
                raise ValueError("network connection requires host")
            if self.port is None or not 1 <= int(self.port) <= 65535:
                raise ValueError("network connection port must be between 1 and 65535")
        elif not self.bluetooth_device or not self.bluetooth_device.strip():
            raise ValueError("bluetooth connection requires bluetooth_device")
        if self.tls_certificate and self.tls_certificate_file:
            raise ValueError("use either embedded TLS certificate or certificate_file")
        if transport != "tls" and (
            any((self.tls_certificate, self.tls_certificate_file, self.tls_server_hostname))
            or not self.tls_verify_hostname
        ):
            raise ValueError("TLS certificate options require transport: tls")

    @property
    def endpoint(self) -> str:
        if self.transport == "serial":
            assert self.serial_port is not None
            return self.serial_port
        if self.transport == "bluetooth":
            assert self.bluetooth_device is not None
            return f"ble://{self.bluetooth_device}"
        assert self.host is not None and self.port is not None
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.transport}://{host}:{self.port}"

    def board_options(self, *, source: str | Path | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.transport == "serial":
            options["baudrate"] = self.baudrate
        if self.transport == "bluetooth":
            options["pair"] = self.bluetooth_pair
        if self.access_token:
            options["network_token"] = self.access_token
        if self.transport == "tls":
            if self.tls_certificate:
                options["tls_ca_data"] = self.tls_certificate
            elif self.tls_certificate_file:
                certificate_path = Path(self.tls_certificate_file).expanduser()
                if source is not None and not certificate_path.is_absolute():
                    certificate_path = Path(source).expanduser().resolve().parent / certificate_path
                options["tls_ca_file"] = str(certificate_path)
            if self.tls_server_hostname:
                options["tls_server_hostname"] = self.tls_server_hostname
            if not self.tls_verify_hostname:
                options["tls_check_hostname"] = False
        return options

    def to_mapping(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "transport": self.transport,
        }
        if self.transport == "serial":
            data.update(serial_port=self.serial_port, baudrate=self.baudrate)
        elif self.transport == "bluetooth":
            data.update(device=self.bluetooth_device, pair=self.bluetooth_pair)
        else:
            data.update(host=self.host, port=self.port)
        if self.access_token:
            data["access_token"] = self.access_token
        if self.transport == "tls":
            tls: dict[str, Any] = {}
            if self.tls_certificate:
                tls["certificate"] = self.tls_certificate
            if self.tls_certificate_file:
                tls["certificate_file"] = self.tls_certificate_file
            if self.tls_server_hostname:
                tls["server_hostname"] = self.tls_server_hostname
            if not self.tls_verify_hostname:
                tls["verify_hostname"] = False
            data["tls"] = tls
        return data

    def save(self, path: str | Path) -> Path:
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            yaml.dump(
                self.to_mapping(),
                Dumper=_ConnectionDumper,
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        return destination

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ConnectionProfile":
        if data.get("format") != PROFILE_FORMAT:
            raise ValueError("not a Fluid Reality connection file")
        if data.get("version") != PROFILE_VERSION:
            raise ValueError("unsupported connection file version")
        tls = data.get("tls") or {}
        if not isinstance(tls, Mapping):
            raise ValueError("tls must be a mapping")
        return cls(
            transport=str(data.get("transport", "")),
            host=None if data.get("host") is None else str(data["host"]),
            port=None if data.get("port") is None else int(data["port"]),
            serial_port=(
                None if data.get("serial_port") is None else str(data["serial_port"])
            ),
            bluetooth_device=(
                None if data.get("device") is None else str(data["device"])
            ),
            bluetooth_pair=bool(data.get("pair", False)),
            baudrate=int(data.get("baudrate", 250000)),
            access_token=(
                None if data.get("access_token") is None else str(data["access_token"])
            ),
            tls_certificate=(
                None if tls.get("certificate") is None else str(tls["certificate"])
            ),
            tls_certificate_file=(
                None
                if tls.get("certificate_file") is None
                else str(tls["certificate_file"])
            ),
            tls_server_hostname=(
                None if tls.get("server_hostname") is None else str(tls["server_hostname"])
            ),
            tls_verify_hostname=bool(tls.get("verify_hostname", True)),
        )


def load_connection_file(path: str | Path) -> ConnectionProfile:
    source = Path(path).expanduser()
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError("connection file must contain a YAML mapping")
    return ConnectionProfile.from_mapping(data)


def open_board_from_connection_file(
    board_class: type[BoardType], path: str | Path, **overrides: Any
) -> BoardType:
    """Open ``board_class`` using a validated YAML connection profile."""

    profile = load_connection_file(path)
    options = profile.board_options(source=path)
    options.update(overrides)
    return board_class(profile.endpoint, **options)
