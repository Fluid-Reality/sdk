from __future__ import annotations

from pathlib import Path

import pytest

from fluid_reality import (
    Board,
    ConnectionProfile,
    load_connection_file,
    open_board_from_connection_file,
)


def test_tls_connection_profile_round_trip_with_embedded_certificate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rockford.connection.yaml"
    profile = ConnectionProfile(
        transport="tls",
        host="rockford.local",
        port=49765,
        access_token="secret-token",
        tls_certificate="-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        tls_server_hostname="rockford.local",
    )

    profile.save(path)
    loaded = load_connection_file(path)

    assert loaded == profile
    assert loaded.endpoint == "tls://rockford.local:49765"
    assert loaded.board_options()["tls_ca_data"].startswith("-----BEGIN CERTIFICATE")
    text = path.read_text(encoding="utf-8")
    assert "format: fluid-reality-connection" in text
    assert "private_key" not in text


def test_relative_certificate_file_is_resolved_from_profile_directory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profile.yaml"
    profile = ConnectionProfile(
        transport="tls",
        host="10.0.1.7",
        port=49765,
        tls_certificate_file="board.pem",
    )
    profile.save(path)

    options = load_connection_file(path).board_options(source=path)

    assert options["tls_ca_file"] == str(tmp_path / "board.pem")


def test_tls_connection_profile_can_disable_hostname_verification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared-certificate.connection.yaml"
    profile = ConnectionProfile(
        transport="tls",
        host="10.0.1.7",
        port=49765,
        tls_certificate_file="shared.pem",
        tls_verify_hostname=False,
    )

    profile.save(path)
    loaded = load_connection_file(path)

    assert loaded.tls_verify_hostname is False
    assert loaded.board_options(source=path)["tls_check_hostname"] is False
    assert "verify_hostname: false" in path.read_text(encoding="utf-8")


def test_tls_connection_profile_can_disable_certificate_verification(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trusted-network.connection.yaml"
    profile = ConnectionProfile(
        transport="tls",
        host="192.168.24.1",
        port=49765,
        tls_verify_certificate=False,
        tls_verify_hostname=False,
    )

    profile.save(path)
    loaded = load_connection_file(path)

    assert loaded.board_options() == {
        "tls_verify_certificate": False,
        "tls_check_hostname": False,
    }
    assert "verify_certificate: false" in profile.to_yaml()


def test_sdk_opens_board_directly_from_connection_file(tmp_path: Path) -> None:
    path = tmp_path / "board.yaml"
    ConnectionProfile(
        transport="tcp",
        host="10.0.1.7",
        port=49765,
        access_token="secret-token",
    ).save(path)

    class RecordingBoard(Board):
        opened: tuple[str, dict[str, object]] | None = None

        def __init__(self, port: str, **options: object) -> None:
            type(self).opened = (port, options)

    board = open_board_from_connection_file(RecordingBoard, path, timeout=3.0)
    classmethod_board = RecordingBoard.from_connection_file(path)

    assert isinstance(board, RecordingBoard)
    assert isinstance(classmethod_board, RecordingBoard)
    assert RecordingBoard.opened == (
        "tcp://10.0.1.7:49765",
        {"network_token": "secret-token"},
    )


def test_invalid_connection_profile_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("format: something-else\nversion: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not a Fluid Reality"):
        load_connection_file(path)


def test_bluetooth_connection_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "rockford-bluetooth.yaml"
    original = ConnectionProfile(
        transport="bluetooth",
        bluetooth_device="device-id",
        bluetooth_pair=True,
        access_token="secret",
    )

    original.save(path)
    loaded = load_connection_file(path)

    assert loaded == original
    assert loaded.endpoint == "ble://device-id"
