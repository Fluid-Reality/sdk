"""Tests for Network Setup's local TLS credential generator."""

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

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


def test_create_self_signed_tls_files_allows_an_empty_server_name(tmp_path) -> None:
    generated = create_self_signed_tls_files(tmp_path, "")
    certificate = x509.load_pem_x509_certificate(generated.certificate.read_bytes())

    assert generated.certificate.name.startswith("fluid-reality-board")
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME) == []
    assert certificate_server_name(generated.certificate) == ""
    try:
        certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        pass
    else:
        raise AssertionError("A nameless certificate must not contain a hostname SAN")


def test_create_certificate_can_reuse_an_existing_private_key(tmp_path) -> None:
    original = create_self_signed_tls_files(
        tmp_path, "", password="key-password"
    )

    reused = create_self_signed_tls_files(
        tmp_path,
        "rockford.local",
        password="key-password",
        private_key_file=original.private_key,
    )

    certificate = x509.load_pem_x509_certificate(reused.certificate.read_bytes())
    private_key = serialization.load_pem_private_key(
        original.private_key.read_bytes(), password=b"key-password"
    )
    assert reused.private_key == original.private_key.resolve()
    assert certificate.public_key().public_numbers() == private_key.public_key().public_numbers()


def test_create_certificate_uses_requested_certificate_filename(tmp_path) -> None:
    certificate_path = tmp_path / "shared-board.pem"

    generated = create_self_signed_tls_files(
        tmp_path, "", certificate_file=certificate_path
    )

    assert generated.certificate == certificate_path
    assert generated.private_key == tmp_path / "shared-board-private-key.pem"
    assert generated.certificate.exists()
    assert generated.private_key.exists()


def test_create_certificate_can_overwrite_requested_output_files(tmp_path) -> None:
    certificate_path = tmp_path / "board-certificate.pem"
    key_path = tmp_path / "board-private-key.pem"
    certificate_path.write_text("old certificate", encoding="utf-8")
    key_path.write_text("old key", encoding="utf-8")

    generated = create_self_signed_tls_files(
        tmp_path,
        "",
        certificate_file=certificate_path,
        private_key_output_file=key_path,
        overwrite=True,
    )

    assert b"BEGIN CERTIFICATE" in generated.certificate.read_bytes()
    assert b"PRIVATE KEY" in generated.private_key.read_bytes()
