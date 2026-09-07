"""Tests for Network Setup's local TLS credential generator."""

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from apps.network_config.tls_files import (
    certificate_server_name,
    create_self_signed_tls_files,
)


def test_create_self_signed_tls_files_produces_matching_pem_pair(tmp_path) -> None:
    generated = create_self_signed_tls_files(
        tmp_path, "rockford.local", validity_days=30, password="test-password"
    )

    certificate = x509.load_pem_x509_certificate(generated.certificate.read_bytes())
    private_key = serialization.load_pem_private_key(
        generated.private_key.read_bytes(), password=b"test-password"
    )

    assert "rockford.local" in certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    assert certificate.public_key().public_numbers() == private_key.public_key().public_numbers()
    assert certificate_server_name(generated.certificate) == "rockford.local"


def test_create_self_signed_tls_files_does_not_overwrite_existing_files(tmp_path) -> None:
    first = create_self_signed_tls_files(tmp_path, "192.168.1.20")
    second = create_self_signed_tls_files(tmp_path, "192.168.1.20")

    assert first != second
    assert first.certificate.exists()
    assert first.private_key.exists()
    assert second.certificate.exists()
    assert second.private_key.exists()
