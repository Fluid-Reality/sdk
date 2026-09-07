"""Local NET protocol and persistent settings for Device Bridge."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class HostInterface:
    token: str
    name: str
    address: str
    mac: str


def available_host_interfaces() -> tuple[HostInterface, ...]:
    """Return active, non-loopback host interfaces with usable IPv4 addresses."""

    try:
        import psutil
    except ImportError:
        return ()

    stats = psutil.net_if_stats()
    interfaces: list[HostInterface] = []
    used_tokens: set[str] = set()
    for name, addresses in psutil.net_if_addrs().items():
        if not stats.get(name) or not stats[name].isup:
            continue
        ipv4 = next(
            (
                item.address for item in addresses
                if item.family == socket.AF_INET and not item.address.startswith("127.")
            ),
            "",
        )
        if not ipv4:
            continue
        mac = next(
            (
                item.address for item in addresses
                if getattr(psutil, "AF_LINK", object()) == item.family
            ),
            "",
        )
        base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "INTERFACE"
        token = base
        suffix = 2
        while token in used_tokens:
            token = f"{base}_{suffix}"
            suffix += 1
        used_tokens.add(token)
        interfaces.append(HostInterface(token, name, ipv4, mac))
    return tuple(interfaces)


@dataclass
class BridgeNetworkSettings:
    schema_version: int = 2
    host: str = "127.0.0.1"
    port: int = 8765
    tcp_enabled: bool = True
    hostname: str = "fluidreality-bridge"
    bind_interface: str = "HOST"
    network_token: str | None = None
    tls_enabled: bool = False
    tls_certfile: str | None = None
    tls_keyfile: str | None = None
    tls_key_password: str | None = None

    @classmethod
    def load(cls, path: Path) -> BridgeNetworkSettings:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})

    def save(self, path: Path) -> Path | None:
        """Atomically save settings and timestamp-rename the previous file."""

        path.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if path.exists():
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
            backup = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
            path.replace(backup)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(asdict(self), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return backup


class BridgeNetProtocol:
    """Process the bridge-owned subset of the firmware NET protocol."""

    def __init__(self, settings: BridgeNetworkSettings, config_path: Path | None) -> None:
        self.settings = settings
        self.config_path = config_path
        self._upload_kind: str | None = None
        self._upload_length = 0
        self._upload_data = bytearray()

    def save(self) -> None:
        if self.config_path is not None:
            self.settings.save(self.config_path)

    def _interfaces(self) -> dict[str, HostInterface]:
        return {interface.token: interface for interface in available_host_interfaces()}

    def _ok(self, *fields: str) -> bytes:
        return ("OK:NET" + ("," + ",".join(fields) if fields else "") + "\n").encode()

    def _error(self, operation: str, reason: str) -> bytes:
        return f"ER:NET,OP>{operation},REASON>{reason}\n".encode()

    def _summary(self, operation: str = "STATUS") -> bytes:
        settings = self.settings
        mac = ":".join(f"{(uuid.getnode() >> shift) & 0xff:02X}" for shift in range(40, -1, -8))
        state = "CONNECTED" if settings.tcp_enabled else "DISABLED"
        return self._ok(
            f"OP>{operation}", f"STATE>{state}", "IF>HOST", "MODE>EXTERNAL",
            f"HOST>{settings.hostname}", f"IP>{settings.host}", f"MAC>{mac}",
            f"TCP>{'ON' if settings.tcp_enabled else 'OFF'}", f"PORT>{settings.port}",
            f"TLS>{'ON' if settings.tls_enabled else 'OFF'}",
            f"BIND>{settings.bind_interface}",
        )

    def handle(self, raw_line: bytes) -> tuple[bytes, bool]:
        """Return ``(response, restart_listener)`` for one NET command line."""

        try:
            text = raw_line.decode("ascii").strip("\r\n")
        except UnicodeDecodeError:
            return self._error("UNKNOWN", "ENCODING"), False
        parts = text.split()
        if not parts or parts[0].upper() != "NET":
            raise ValueError("BridgeNetProtocol only accepts NET commands")
        params = parts[1:]
        if not params:
            return self._summary(), False
        operation = params[0].upper()

        if operation == "STATUS":
            return (self._summary(), False) if len(params) == 1 else (
                self._error("STATUS", "PARAM_COUNT"), False
            )
        if operation == "IF":
            return self._interface(params[1:])
        if operation == "TCP":
            return self._tcp(params[1:])
        if operation == "TLS":
            return self._tls(params[1:])
        if operation == "KEY":
            return self._key(params[1:])
        if operation == "HOST":
            return self._hostname(params[1:])
        if operation == "DIAG":
            return self._diagnostics(params[1:])
        if operation == "AUTH":
            return self._error("AUTH", "ALREADY_AUTHENTICATED"), False
        if operation == "IP":
            return self._error("IP", "EXTERNALLY_MANAGED"), False
        if operation in {
            "SCAN", "LIST", "JOIN", "WIFI", "DISCONNECT", "FORGET"
        }:
            return self._error(operation, "UNSUPPORTED"), False
        return self._error(operation, "UNKNOWN_OPERATION"), False

    def _interface(self, params: list[str]) -> tuple[bytes, bool]:
        if len(params) == 1 and params[0].upper() == "LIST":
            names = ["HOST", *self._interfaces()]
            return self._ok(
                "OP>IF", f"IFACES>{'|'.join(names)}", "CONFIG>TCP|TLS|AUTH"
            ), False
        if len(params) >= 2:
            interface_name = params[0].upper()
            operation = params[1].upper()
            if interface_name == "HOST" and operation == "STATUS" and len(params) == 2:
                return self._summary("STATUS"), False
            if interface_name == "HOST" and operation == "DIAG" and len(params) == 2:
                return self._diagnostics([])
            interface = self._interfaces().get(interface_name)
            if interface is not None and operation in {"STATUS", "DIAG"} and len(params) == 2:
                name64 = base64.b64encode(interface.name.encode("utf-8")).decode("ascii")
                return self._ok(
                    f"OP>{operation}", "STATE>CONNECTED", f"IF>{interface.token}",
                    "MODE>EXTERNAL", f"NAME64>{name64}", f"IP>{interface.address}",
                    f"MAC>{interface.mac or 'UNKNOWN'}",
                    f"TCP>{'ON' if self.settings.tcp_enabled else 'OFF'}",
                    f"PORT>{self.settings.port}",
                    f"TLS>{'ON' if self.settings.tls_enabled else 'OFF'}",
                    f"BIND>{self.settings.bind_interface}",
                ), False
            if operation == "IP":
                return self._error("IP", "EXTERNALLY_MANAGED"), False
        return self._error("IF", "INTERFACE"), False

    def _set_bind(self, bind: str) -> bool:
        bind = bind.upper()
        if bind == "KEEP":
            return True
        if bind == "ANY":
            self.settings.host = "0.0.0.0"
            self.settings.bind_interface = "ANY"
            return True
        if bind == "HOST":
            self.settings.bind_interface = "HOST"
            return True
        interface = self._interfaces().get(bind)
        if interface is None:
            return False
        self.settings.host = interface.address
        self.settings.bind_interface = interface.token
        return True

    def _tcp(self, params: list[str]) -> tuple[bytes, bool]:
        settings = self.settings
        if not params:
            return self._ok(
                "OP>TCP", f"STATE>{'ON' if settings.tcp_enabled else 'OFF'}",
                f"PORT>{settings.port}", f"BIND>{settings.bind_interface}",
            ), False
        action = params[0].upper()
        if len(params) == 4 and action == "SET":
            state, port_text, bind = params[1].upper(), params[2].upper(), params[3].upper()
            valid_binds = {"ANY", "HOST", "KEEP", *self._interfaces()}
            if state not in {"ON", "OFF", "KEEP"} or bind not in valid_binds:
                return self._error("TCP", "PARAM_VALUE"), False
            if port_text != "KEEP":
                try:
                    port = int(port_text)
                except ValueError:
                    return self._error("TCP", "PARAM_VALUE"), False
                if not 1 <= port <= 65535:
                    return self._error("TCP", "PARAM_VALUE"), False
                settings.port = port
            self._set_bind(bind)
            if state != "KEEP":
                settings.tcp_enabled = state == "ON"
        elif len(params) == 1 and action in {"ON", "OFF"}:
            settings.tcp_enabled = action == "ON"
        elif len(params) == 2 and action == "PORT":
            try:
                port = int(params[1])
            except ValueError:
                return self._error("TCP", "PARAM_VALUE"), False
            if not 1 <= port <= 65535:
                return self._error("TCP", "PARAM_VALUE"), False
            settings.port = port
        elif len(params) == 2 and action == "BIND":
            if not self._set_bind(params[1]):
                return self._error("TCP", "PARAM_VALUE"), False
        else:
            return self._error("TCP", "PARAM_VALUE"), False
        self.save()
        return self._ok(
            "OP>TCP", f"STATE>{'ON' if settings.tcp_enabled else 'OFF'}",
            f"PORT>{settings.port}", f"BIND>{settings.bind_interface}", "RECONNECT>1",
        ), True

    def _key(self, params: list[str]) -> tuple[bytes, bool]:
        if not params:
            return self._ok(
                "OP>KEY", f"TOKEN>{self.settings.network_token or ''}"
            ), False
        if len(params) == 1 and params[0].upper() == "NEW":
            self.settings.network_token = secrets.token_urlsafe(24)
            self.save()
            return self._ok("OP>KEY", f"TOKEN>{self.settings.network_token}"), False
        if len(params) == 2 and params[0].upper() == "SET":
            token = params[1]
            if not token or len(token) > 32 or not token.isascii() or not all(
                character.isalnum() or character in "-_" for character in token
            ):
                return self._error("KEY", "PARAM_VALUE"), False
            self.settings.network_token = token
            self.save()
            return self._ok("OP>KEY", f"TOKEN>{token}"), False
        return self._error("KEY", "PARAM_COUNT"), False

    def _hostname(self, params: list[str]) -> tuple[bytes, bool]:
        if not params:
            return self._ok("OP>HOST", f"HOST>{self.settings.hostname}"), False
        hostname = params[0]
        if len(params) != 1 or len(hostname) > 63 or not all(
            character.isalnum() or character in "-." for character in hostname
        ):
            return self._error("HOST", "PARAM_VALUE"), False
        self.settings.hostname = hostname
        self.save()
        return self._ok("OP>HOST", f"HOST>{hostname}"), False

    def _diagnostics(self, params: list[str]) -> tuple[bytes, bool]:
        if params:
            return self._error("DIAG", "PARAM_COUNT"), False
        return self._ok(
            "OP>DIAG", "STATE>CONNECTED", "IF>HOST", "MODE>EXTERNAL",
            f"HOST>{socket.gethostname()}", f"IP>{self.settings.host}",
            f"BIND>{self.settings.bind_interface}",
            f"PORT>{self.settings.port}",
            f"TLS>{'ON' if self.settings.tls_enabled else 'OFF'}",
        ), False

    def _tls(self, params: list[str]) -> tuple[bytes, bool]:
        settings = self.settings
        if not params:
            ready = bool(settings.tls_certfile and settings.tls_keyfile)
            return self._ok(
                "OP>TLS", f"STATE>{'ON' if settings.tls_enabled else 'OFF'}",
                f"READY>{'YES' if ready else 'NO'}",
                f"CERT>{'VALID' if settings.tls_certfile else 'MISSING'}",
                f"KEY>{'VALID' if settings.tls_keyfile else 'MISSING'}",
            ), False
        action = params[0].upper()
        if len(params) == 1 and action in {"ON", "OFF"}:
            if action == "ON" and not (settings.tls_certfile and settings.tls_keyfile):
                return self._error("TLS", "CREDENTIALS"), False
            settings.tls_enabled = action == "ON"
            self.save()
            return self._ok(
                "OP>TLS", f"STATE>{action}", "RECONNECT>1"
            ), True
        if len(params) == 1 and action == "CLEAR":
            settings.tls_enabled = False
            settings.tls_certfile = None
            settings.tls_keyfile = None
            settings.tls_key_password = None
            self.save()
            return self._ok("OP>TLS", "STATE>OFF", "READY>NO", "RECONNECT>1"), True
        if action == "PASS" and len(params) == 2:
            if params[1].upper() == "CLEAR":
                settings.tls_key_password = None
            else:
                try:
                    settings.tls_key_password = base64.b64decode(
                        params[1], validate=True
                    ).decode("utf-8")
                except (ValueError, UnicodeError):
                    return self._error("TLS", "BASE64"), False
            self.save()
            return self._ok("OP>TLS", "PASS>SET"), False
        if action in {"CERT", "KEY"}:
            return self._tls_material(action, params[1:])
        return self._error("TLS", "PARAM_VALUE"), False

    def _tls_material(self, kind: str, params: list[str]) -> tuple[bytes, bool]:
        if len(params) == 2 and params[0].upper() == "BEGIN":
            try:
                length = int(params[1])
            except ValueError:
                return self._error("TLS", "PARAM_VALUE"), False
            if not 1 <= length <= 8192:
                return self._error("TLS", "PARAM_VALUE"), False
            self._upload_kind = kind
            self._upload_length = length
            self._upload_data.clear()
            return self._ok("OP>TLS", f"{kind}>BEGIN"), False
        if len(params) == 2 and params[0].upper() == "DATA":
            if self._upload_kind != kind:
                return self._error("TLS", "UPLOAD_STATE"), False
            try:
                self._upload_data.extend(base64.b64decode(params[1], validate=True))
            except ValueError:
                return self._error("TLS", "BASE64"), False
            if len(self._upload_data) > self._upload_length:
                return self._error("TLS", "UPLOAD_LENGTH"), False
            return self._ok("OP>TLS", f"{kind}>DATA"), False
        if len(params) == 1 and params[0].upper() == "END":
            if self._upload_kind != kind or len(self._upload_data) != self._upload_length:
                return self._error("TLS", "UPLOAD_LENGTH"), False
            if self.config_path is None:
                return self._error("TLS", "CONFIG_PATH"), False
            suffix = "cert.pem" if kind == "CERT" else "key.pem"
            material_path = self.config_path.with_name(
                f"{self.config_path.stem}.{suffix}"
            )
            material_path.write_bytes(bytes(self._upload_data))
            if kind == "CERT":
                self.settings.tls_certfile = str(material_path)
            else:
                self.settings.tls_keyfile = str(material_path)
            self._upload_kind = None
            self._upload_data.clear()
            self.save()
            return self._ok("OP>TLS", f"{kind}>VALID"), False
        return self._error("TLS", "PARAM_VALUE"), False
