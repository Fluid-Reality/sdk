"""Composable IP, Wi-Fi, and Ethernet capabilities for Fluid Reality boards."""

from __future__ import annotations

import base64
import ipaddress
import re
from dataclasses import dataclass

from ..errors import FirmwareError, ProtocolError
from ..protocol import parse_response_line
from .board import Board
from .lansing_errors import LANSING_ERROR_INFO


def _normalize_interface(interface: str) -> str:
    value = str(interface).strip().upper()
    value = {"ETHERNET": "ETH", "WI-FI": "WIFI"}.get(value, value)
    if not re.fullmatch(r"[A-Z0-9_]{1,63}", value):
        raise ValueError("network interface must be a valid interface identifier")
    return value


@dataclass(frozen=True)
class WifiNetwork:
    """One Wi-Fi access point returned by a ``NET LIST`` command."""

    index: int
    ssid: str
    rssi: int
    security: str
    channel: int
    bssid: str = ""


class NetworkBoard(Board):
    """A board that can be reached through a network transport.

    This base intentionally makes no claim that the board owns or can configure
    its network interface. It is suitable for boards piggybacking a host whose
    TCP/TLS endpoint is managed externally.
    """

    network_interface_capability: str | None = None
    network_configuration_capability = False

    @classmethod
    def declared_network_interfaces(cls) -> tuple[str, ...]:
        interfaces: list[str] = []
        for base in cls.__mro__:
            capability = base.__dict__.get("network_interface_capability")
            if capability and capability not in interfaces:
                interfaces.append(capability)
        return tuple(interfaces)

    @classmethod
    def supports_network_configuration(cls) -> bool:
        """Whether the board exposes device-side ``NET`` configuration."""

        return bool(cls.network_configuration_capability)


class ConfigurableNetworkBoard(NetworkBoard):
    """Interface-neutral IPv4, TCP, authentication, and TLS configuration.

    Hardware profiles combine this with :class:`WifiBoard`,
    :class:`EthernetBoard`, or both. These classes contain no constructor or
    duplicated board state, so cooperative multiple inheritance remains safe.
    """

    network_configuration_capability = True
    default_network_configuration_features = ("IP", "HOST", "TCP", "AUTH", "TLS")

    def network_status(self, interface: str | None = None) -> dict[str, str]:
        if interface is None:
            return self.raw_command("NET", "STATUS")[0].fields
        return self.raw_command(
            "NET", "IF", _normalize_interface(interface), "STATUS"
        )[0].fields

    def network_interfaces(self) -> tuple[str, ...]:
        """Discover physical interfaces, falling back for legacy firmware."""

        declared = self.declared_network_interfaces()
        try:
            fields = self.raw_command("NET", "IF", "LIST")[0].fields
        except FirmwareError:
            self._network_interface_commands_supported = False
            return declared
        self._network_interface_commands_supported = True
        features = fields.get("CONFIG", "")
        self._network_configuration_features = tuple(
            item.strip().upper() for item in features.split("|") if item.strip()
        ) or self.default_network_configuration_features
        encoded = fields.get("IFACES", fields.get("INTERFACES", ""))
        if not encoded:
            return declared
        interfaces: list[str] = []
        for item in encoded.replace(",", "|").split("|"):
            normalized = _normalize_interface(item)
            if normalized not in interfaces:
                interfaces.append(normalized)
        return tuple(interfaces)

    @property
    def network_configuration_features(self) -> tuple[str, ...]:
        """Device-side NET features reported during interface discovery."""

        return tuple(
            getattr(
                self,
                "_network_configuration_features",
                self.default_network_configuration_features,
            )
        )

    @property
    def network_interface_commands_supported(self) -> bool:
        """Whether firmware accepted the interface-scoped ``NET IF`` protocol."""

        return bool(getattr(self, "_network_interface_commands_supported", False))

    def use_dhcp(self, interface: str | None = None) -> dict[str, str]:
        if interface is None:
            return self.raw_command("NET", "IP", "DHCP")[0].fields
        return self.raw_command(
            "NET", "IF", _normalize_interface(interface), "IP", "DHCP"
        )[0].fields

    def set_static_ipv4(
        self,
        address: str,
        subnet: str,
        gateway: str,
        dns1: str,
        dns2: str = "0.0.0.0",
        *,
        interface: str | None = None,
    ) -> dict[str, str]:
        params: tuple[object, ...] = (
            "IP", "STATIC", address, subnet, gateway, dns1, dns2
        )
        if interface is None:
            return self.raw_command("NET", *params)[0].fields
        return self.raw_command(
            "NET", "IF", _normalize_interface(interface), *params
        )[0].fields

    def network_hostname(self, hostname: str | None = None) -> dict[str, str]:
        params = ("HOST",) if hostname is None else ("HOST", hostname)
        return self.raw_command("NET", *params)[0].fields

    def configure_tcp(
        self,
        *,
        enabled: bool | None = None,
        port: int | None = None,
        bind: str | None = None,
    ) -> dict[str, str]:
        result: dict[str, str] | None = None
        if bind is not None:
            target = str(bind).strip().upper()
            if target not in {"ANY", "WIFI", "ETH"}:
                target = _normalize_interface(target)
            result = self.raw_command("NET", "TCP", "BIND", target)[0].fields
        if enabled is not None and port is not None:
            self.raw_command("NET", "TCP", "ON" if enabled else "OFF")
        if port is not None:
            return self.raw_command("NET", "TCP", "PORT", port)[0].fields
        if enabled is not None:
            return self.raw_command(
                "NET", "TCP", "ON" if enabled else "OFF"
            )[0].fields
        if result is not None:
            return result
        return self.raw_command("NET", "TCP")[0].fields

    def network_key(self, *, regenerate: bool = False) -> str:
        params = ("KEY", "NEW") if regenerate else ("KEY",)
        self.transport.write_line("NET " + " ".join(params))
        response = parse_response_line(self.transport.read_line())
        if not response.ok:
            raise FirmwareError(
                code=response.error_code,
                raw=response.raw,
                fields=response.fields,
                info=LANSING_ERROR_INFO.get(response.error_code),
            )
        return response.fields["TOKEN"]

    def set_network_key(self, token: str) -> str:
        """Persist an explicit client access token on the board."""

        value = str(token).strip()
        if not value or len(value) > 32 or not value.isascii() or not all(
            character.isalnum() or character in "-_" for character in value
        ):
            raise ValueError(
                "network token must be 1-32 ASCII letters, digits, hyphens, or underscores"
            )
        response = self.raw_command("NET", "KEY", "SET", value)[0]
        return response.fields["TOKEN"]

    def network_diagnostics(self, interface: str | None = None) -> dict[str, str]:
        """Return sanitized diagnostics without credentials or tokens."""

        if interface is None:
            return self.raw_command("NET", "DIAG")[0].fields
        return self.raw_command(
            "NET", "IF", _normalize_interface(interface), "DIAG"
        )[0].fields

    def tls_status(self) -> dict[str, str]:
        return self.raw_command("NET", "TLS")[0].fields

    def configure_tls(self, enabled: bool) -> dict[str, str]:
        return self.raw_command("NET", "TLS", "ON" if enabled else "OFF")[0].fields

    def set_tls_private_key_password(self, password: str | None) -> dict[str, str]:
        encoded = (
            "CLEAR"
            if not password
            else base64.b64encode(password.encode("utf-8")).decode("ascii")
        )
        return self._network_secret_command("TLS", "PASS", encoded)

    def _network_secret_command(self, *params: object) -> dict[str, str]:
        self.transport.write_line("NET " + " ".join(str(param) for param in params))
        return self.protocol.read_result(ok_lines=1)[0].fields

    def _upload_tls_pem(self, kind: str, pem: str | bytes) -> dict[str, str]:
        kind = kind.upper()
        if kind not in {"CERT", "KEY"}:
            raise ValueError("TLS PEM kind must be CERT or KEY")
        data = pem.encode("ascii") if isinstance(pem, str) else bytes(pem)
        if not data or len(data) > 8192:
            raise ValueError("TLS PEM data must contain between 1 and 8192 bytes")
        if b"\0" in data:
            raise ValueError("TLS PEM data cannot contain NUL bytes")
        self.raw_command("NET", "TLS", kind, "BEGIN", len(data))
        for offset in range(0, len(data), 48):
            chunk = base64.b64encode(data[offset : offset + 48]).decode("ascii")
            if kind == "KEY":
                self._network_secret_command("TLS", kind, "DATA", chunk)
            else:
                self.raw_command("NET", "TLS", kind, "DATA", chunk)
        return self.raw_command("NET", "TLS", kind, "END")[0].fields

    def upload_tls_certificate(self, pem: str | bytes) -> dict[str, str]:
        return self._upload_tls_pem("CERT", pem)

    def upload_tls_private_key(self, pem: str | bytes) -> dict[str, str]:
        return self._upload_tls_pem("KEY", pem)

    def provision_tls(
        self,
        certificate_pem: str | bytes,
        private_key_pem: str | bytes,
        *,
        private_key_password: str | None = None,
        enable: bool = True,
    ) -> dict[str, str]:
        self.set_tls_private_key_password(private_key_password)
        self.upload_tls_certificate(certificate_pem)
        self.upload_tls_private_key(private_key_pem)
        return self.configure_tls(enable) if enable else self.tls_status()

    def clear_tls(self) -> dict[str, str]:
        return self.raw_command("NET", "TLS", "CLEAR")[0].fields


class WifiBoard(ConfigurableNetworkBoard):
    """Wi-Fi scanning, association, and credential-management capability."""

    network_interface_capability = "WIFI"

    def wifi_mode(self) -> str:
        """Return ``CLIENT`` or ``ACCESS_POINT`` for the active Wi-Fi mode."""

        fields = self.raw_command("NET", "MODE")[0].fields
        return fields.get("MODE", "CLIENT").upper()

    def set_wifi_mode(self, mode: str) -> dict[str, str]:
        """Select persistent client or access-point operation."""

        normalized = str(mode).strip().upper().replace("-", "_").replace(" ", "_")
        normalized = {"AP": "ACCESS_POINT", "STATION": "CLIENT"}.get(
            normalized, normalized
        )
        if normalized not in {"CLIENT", "ACCESS_POINT"}:
            raise ValueError("Wi-Fi mode must be CLIENT or ACCESS_POINT")
        return self.raw_command("NET", "MODE", normalized)[0].fields

    def access_point_status(self) -> dict[str, str]:
        """Return the saved access-point configuration and live state."""

        return self.raw_command("NET", "AP", "STATUS")[0].fields

    def configure_access_point(
        self,
        ssid: str,
        password: str | None = None,
        *,
        channel: int = 1,
    ) -> dict[str, str]:
        """Save AP credentials and channel, restarting AP mode when active."""

        if not ssid or len(ssid.encode("utf-8")) > 32:
            raise ValueError("access-point SSID must contain 1 to 32 UTF-8 bytes")
        if not 1 <= int(channel) <= 13:
            raise ValueError("access-point channel must be between 1 and 13")
        encoded_ssid = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
        if password is None or password == "":
            return self.raw_command(
                "NET", "AP", "CONFIG", encoded_ssid, "OPEN", int(channel)
            )[0].fields
        if not 8 <= len(password.encode("utf-8")) <= 63:
            raise ValueError("access-point password must contain 8 to 63 UTF-8 bytes")
        encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
        return self.raw_command(
            "NET", "AP", "CONFIG", encoded_ssid, "PSK", encoded_password,
            int(channel),
        )[0].fields

    def configure_access_point_ipv4(
        self, address: str, subnet: str
    ) -> dict[str, str]:
        """Set the persistent address and subnet used by access-point mode."""

        normalized_address = str(ipaddress.IPv4Address(address.strip()))
        normalized_subnet = str(ipaddress.IPv4Address(subnet.strip()))
        ipaddress.IPv4Network(f"0.0.0.0/{normalized_subnet}")
        if normalized_address == "0.0.0.0" or normalized_subnet == "0.0.0.0":
            raise ValueError("access-point address and subnet must be non-zero")
        return self.raw_command(
            "NET", "AP", "IP", normalized_address, normalized_subnet
        )[0].fields

    def start_wifi_scan(self) -> dict[str, str]:
        return self.raw_command("NET", "SCAN")[0].fields

    def wifi_networks(self) -> list[WifiNetwork]:
        summary = self.raw_command("NET", "LIST")[0]
        if summary.fields.get("STATE") == "SCANNING":
            return []
        count = int(summary.fields.get("COUNT", "0"))
        networks: list[WifiNetwork] = []
        for index in range(count):
            fields = self.raw_command("NET", "LIST", index)[0].fields
            encoded_ssid = fields.get("SSID64", "")
            try:
                ssid = base64.b64decode(encoded_ssid, validate=True).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError(
                    f"Invalid SSID64 in NET LIST response: {encoded_ssid!r}"
                ) from exc
            networks.append(
                WifiNetwork(
                    index=int(fields["IDX"]),
                    ssid=ssid,
                    rssi=int(fields["RSSI"]),
                    security=fields["SEC"],
                    channel=int(fields["CH"]),
                    bssid=fields.get("BSSID", ""),
                )
            )
        return networks

    def join_wifi(self, index: int, password: str | None = None) -> dict[str, str]:
        params = ["JOIN", str(index)]
        if password is None:
            params.append("OPEN")
        else:
            encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
            params.extend(("PSK", encoded))
        self.transport.write_line("NET " + " ".join(params))
        return self.protocol.read_result(ok_lines=1)[0].fields

    def join_hidden_wifi(self, ssid: str, password: str) -> dict[str, str]:
        encoded_ssid = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
        encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
        self.transport.write_line(
            f"NET JOIN HIDDEN PSK {encoded_ssid} {encoded_password}"
        )
        return self.protocol.read_result(ok_lines=1)[0].fields

    def disconnect_wifi(self) -> dict[str, str]:
        return self.raw_command("NET", "DISCONNECT")[0].fields

    def forget_wifi(self) -> dict[str, str]:
        return self.raw_command("NET", "FORGET")[0].fields

    def set_wifi_enabled(self, enabled: bool) -> dict[str, str]:
        return self.raw_command("NET", "WIFI", "ON" if enabled else "OFF")[0].fields


class EthernetBoard(ConfigurableNetworkBoard):
    """Wired-Ethernet capability, reusable alone or with :class:`WifiBoard`."""

    network_interface_capability = "ETH"

    def ethernet_status(self) -> dict[str, str]:
        return self.network_status("ETH")

    def set_ethernet_enabled(self, enabled: bool) -> dict[str, str]:
        return self.raw_command(
            "NET", "IF", "ETH", "ON" if enabled else "OFF"
        )[0].fields


__all__ = [
    "ConfigurableNetworkBoard",
    "EthernetBoard",
    "NetworkBoard",
    "WifiBoard",
    "WifiNetwork",
]
