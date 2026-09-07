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
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    if names:
        return names[0]
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return common_names[0].value if common_names else ""


def create_self_signed_tls_files(
    output_directory: str | Path,
    server_name: str,
    *,
    validity_days: int = 825,
    password: str = "",
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
    if not name or len(name) > 253 or any(character.isspace() for character in name):
        raise ValueError("Enter a valid board hostname or IP address.")
    if not directory.is_dir():
        raise ValueError("Choose an existing output folder.")
    if not 1 <= int(validity_days) <= 3650:
        raise ValueError("Certificate validity must be between 1 and 3650 days.")

    try:
        san_name: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", name):
            raise ValueError("Enter a valid board hostname or IP address.") from None
        san_name = x509.DNSName(name)

    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "fluid-reality-board"
    certificate_path, private_key_path = _available_paths(directory, stem)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Fluid Reality"),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=int(validity_days)))
        .add_extension(x509.SubjectAlternativeName([san_name]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    encryption = (
        serialization.BestAvailableEncryption(password.encode("utf-8"))
        if password
        else serialization.NoEncryption()
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
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
