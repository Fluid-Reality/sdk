"""Local TLS credential generation for the Network Setup app."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class GeneratedTlsFiles:
    certificate: Path
    private_key: Path


def certificate_server_name(certificate_file: str | Path) -> str:
    """Return the preferred DNS identity from a PEM server certificate."""

    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - covered by the app requirements.
        raise RuntimeError(
            "Reading certificate identities requires the 'cryptography' package."
        ) from exc

    certificate = x509.load_pem_x509_certificate(Path(certificate_file).read_bytes())
    try:
        alternative_names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        alternative_names = None
    if alternative_names is not None:
        dns_names = alternative_names.get_values_for_type(x509.DNSName)
        if dns_names:
            return dns_names[0]
        ip_addresses = alternative_names.get_values_for_type(x509.IPAddress)
        if ip_addresses:
            return str(ip_addresses[0])
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return common_names[0].value if common_names else ""


def create_self_signed_tls_files(
    output_directory: str | Path,
    server_name: str,
    *,
    validity_days: int = 825,
    password: str = "",
    private_key_file: str | Path | None = None,
    certificate_file: str | Path | None = None,
    private_key_output_file: str | Path | None = None,
    overwrite: bool = False,
) -> GeneratedTlsFiles:
    """Create a self-signed RSA certificate and matching PEM private key."""

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover - covered by the app requirements.
        raise RuntimeError(
            "Certificate creation requires the 'cryptography' package. "
            "Install the Network Setup requirements and try again."
        ) from exc

    directory = Path(output_directory).expanduser()
    name = server_name.strip()
    if len(name) > 253 or any(character.isspace() for character in name):
        raise ValueError("Enter a valid board hostname or IP address.")
    if not directory.is_dir():
        raise ValueError("Choose an existing output folder.")
    if not 1 <= int(validity_days) <= 3650:
        raise ValueError("Certificate validity must be between 1 and 3650 days.")

    san_name: x509.GeneralName | None = None
    if name:
        try:
            san_name = x509.IPAddress(ipaddress.ip_address(name))
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name):
                raise ValueError("Enter a valid board hostname or IP address.") from None
            san_name = x509.DNSName(name)

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "fluid-reality-board"
    requested_certificate_path = (
        Path(certificate_file).expanduser() if certificate_file else None
    )
    if requested_certificate_path is not None:
        certificate_path = requested_certificate_path
        if not certificate_path.parent.is_dir():
            raise ValueError("Choose an existing output folder.")
        if certificate_path.exists() and not overwrite:
            raise ValueError("The selected certificate file already exists.")
    existing_key_path = (
        Path(private_key_file).expanduser() if private_key_file else None
    )
    requested_private_key_path = (
        Path(private_key_output_file).expanduser()
        if private_key_output_file
        else None
    )
    if existing_key_path is not None and requested_private_key_path is not None:
        raise ValueError("Choose either an existing private key or a new key filename.")
    if existing_key_path is not None:
        if not existing_key_path.is_file():
            raise ValueError("Choose an existing private-key file.")
        try:
            key = serialization.load_pem_private_key(
                existing_key_path.read_bytes(),
                password=password.encode("utf-8") if password else None,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError(
                "Could not read the private key. Check the file and key password."
            ) from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("The existing private key must be an RSA private key.")
        if requested_certificate_path is None:
            certificate_path = _available_certificate_path(directory, stem)
        private_key_path = existing_key_path.resolve()
        if certificate_path.resolve() == private_key_path:
            raise ValueError("The certificate and private key must use different files.")
    else:
        if requested_certificate_path is None:
            if requested_private_key_path is None:
                certificate_path, private_key_path = _available_paths(directory, stem)
            else:
                certificate_path = _available_certificate_path(directory, stem)
                private_key_path = requested_private_key_path
        else:
            if requested_private_key_path is not None:
                private_key_path = requested_private_key_path
            else:
                certificate_stem = certificate_path.stem
                if certificate_stem.lower().endswith("-certificate"):
                    certificate_stem = certificate_stem[: -len("-certificate")]
                private_key_path = certificate_path.with_name(
                    f"{certificate_stem or stem}-private-key.pem"
                )
        if not private_key_path.parent.is_dir():
            raise ValueError("Choose an existing folder for the new private key.")
        if private_key_path.exists() and not overwrite:
            raise ValueError(
                f"The selected private-key file already exists: {private_key_path.name}"
            )
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject_attributes = [
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fluid Reality")
    ]
    if name:
        subject_attributes.append(x509.NameAttribute(NameOID.COMMON_NAME, name))
    subject = issuer = x509.Name(subject_attributes)
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=int(validity_days)))
    )
    if san_name is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([san_name]), critical=False
        )
    certificate = (
        builder
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    if existing_key_path is None:
        encryption = (
            serialization.BestAvailableEncryption(password.encode("utf-8"))
            if password
            else serialization.NoEncryption()
        )
        try:
            private_key_path.write_bytes(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=encryption,
                )
            )
        except Exception:
            certificate_path.unlink(missing_ok=True)
            raise
    return GeneratedTlsFiles(certificate_path, private_key_path)


def _available_certificate_path(directory: Path, stem: str) -> Path:
    suffix = ""
    index = 1
    while True:
        certificate = directory / f"{stem}{suffix}-certificate.pem"
        if not certificate.exists():
            return certificate
        index += 1
        suffix = f"-{index}"


def _available_paths(directory: Path, stem: str) -> tuple[Path, Path]:
    suffix = ""
    index = 1
    while True:
        certificate = directory / f"{stem}{suffix}-certificate.pem"
        private_key = directory / f"{stem}{suffix}-private-key.pem"
        if not certificate.exists() and not private_key.exists():
            return certificate, private_key
        index += 1
        suffix = f"-{index}"
