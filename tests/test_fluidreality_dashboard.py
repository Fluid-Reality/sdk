from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QLineEdit

from fluid_reality import BluetoothDevice, Board, Lansing, Rockford
from apps.fluidreality_dashboard.app import (
    ActuatorCard,
    BoardWorker,
    ConnectionDialog,
    DashboardWindow,
    FluidRealityBoard,
    build_network_endpoint,
    power_connection_is_ready,
)
from shared.toggle import LabeledToggle


class FakeTransport:
    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)
        self.writes: list[str | bytes] = []

    def write_line(self, line: str) -> None:
        self.writes.append(line)

    def read_line(self) -> str:
        if not self.lines:
            raise AssertionError("No fake response queued")
        return self.lines.pop(0)

    def write_bytes(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        return None


def status_lines(
    actuator_count: int,
    *,
    include_balance: bool,
    detection_limit_ma: float | None = None,
) -> list[str]:
    values = ",".join("0" for _ in range(actuator_count))
    outputs = ",".join(
        f"A{actuator}P>0,A{actuator}N>0" for actuator in range(actuator_count)
    )
    summary = (
        "OK:PSU>OFF,PSC>OFF,VLT>0.00,CUR>0.00,CFG_MAX>5000,CFG_DIS>2000,"
        "SAFE>ON,DEBUG>OFF,STREAM>TEXT"
    )
    if detection_limit_ma is not None:
        summary += f",DET_MIN>{detection_limit_ma:.2f}"
    lines = [
        summary,
        f"OK:ACT_VALUES>{values}",
        f"OK:OUT_VALUES>{outputs}",
        f"OK:ACT_STATES>{values}",
        f"OK:ACTIVE_MS>{values}",
    ]
    if include_balance:
        lines.append(f"OK:BALANCE_MS>{values}")
    lines.extend(
        [
            f"OK:TOTAL_MS>{values}",
            f"OK:DISCHARGE_MS_LEFT>{values}",
        ]
    )
    return lines


def test_status_discovers_eight_actuator_board() -> None:
    transport = FakeTransport(status_lines(8, include_balance=True))
    board = FluidRealityBoard(transport=transport)

    status = board.status()

    assert transport.writes == ["STS"]
    assert status["actuator_count"] == 8
    assert len(status["actuator_values"]) == 8
    assert len(status["manual_outputs"]) == 8
    assert status["balance_ms"] == (0,) * 8
    board._validate_actuator(7)


def test_status_discovers_legacy_24_actuator_board_without_balance() -> None:
    transport = FakeTransport(status_lines(24, include_balance=False))
    board = FluidRealityBoard(transport=transport)

    status = board.status()

    assert transport.writes == ["STS"]
    assert status["actuator_count"] == 24
    assert len(status["actuator_values"]) == 24
    assert len(status["manual_outputs"]) == 24
    assert status["balance_ms"] == (0,) * 24
    board._validate_actuator(23)


def test_status_overrides_default_detection_current_limit_when_reported() -> None:
    transport = FakeTransport(
        status_lines(8, include_balance=True, detection_limit_ma=0.34)
    )
    board = FluidRealityBoard(transport=transport)

    status = board.status()

    assert status["detection_current_limit_ma"] == pytest.approx(0.34)
    assert board.not_connected_delta_ma == pytest.approx(0.34)


def test_none_power_connection_is_reported_and_treated_as_capability() -> None:
    lines = ["OK:NONE", *status_lines(8, include_balance=True)]
    transport = FakeTransport(lines)
    board = FluidRealityBoard(transport=transport)

    assert board.connect_power() == "NONE"
    status = board.status()

    assert board.power_connection_supported is False
    assert status["psc"] == "NONE"
    assert board.connect_power(True) == "NONE"
    assert transport.writes == ["PSC", "STS"]


def test_power_connection_ready_accepts_none_and_on() -> None:
    assert power_connection_is_ready("ON")
    assert power_connection_is_ready("NONE")
    assert not power_connection_is_ready("OFF")


def test_dashboard_board_uses_only_shared_board_interface() -> None:
    assert FluidRealityBoard.__mro__[1] is Board
    assert not hasattr(FluidRealityBoard, "network_status")


def test_worker_accepts_any_board_subclass() -> None:
    assert BoardWorker(Lansing)._board_class is Lansing
    assert BoardWorker(Rockford)._board_class is Rockford


def test_detect_all_reuses_one_baseline_for_every_actuator() -> None:
    baselines: list[float | None] = []

    class DetectionBoard:
        actuator_count = 3
        not_connected_delta_ma = 0.34

        def status(self) -> dict[str, str]:
            return {"psu": "ON", "psc": "ON", "voltage": "225", "current": "1.2"}

        def detect_actuator(self, actuator: int, *, baseline_ma: float | None = None):
            baselines.append(baseline_ma)
            measured_baseline = 1.2 if baseline_ma is None else baseline_ma
            return SimpleNamespace(
                actuator=actuator,
                state=SimpleNamespace(value="Idle"),
                baseline_ma=measured_baseline,
                forward_ma=1.3,
                discharge_ma=1.1,
                delta_ma=0.1,
                initial_forward_ma=1.3,
                initial_delta_ma=0.1,
            )

    worker = BoardWorker()
    worker._board = DetectionBoard()
    worker._detect_all()

    assert baselines == [None, 1.2, 1.2]


def test_network_endpoint_supports_plain_tls_and_ipv6() -> None:
    assert build_network_endpoint("tcp", "rockford.local", 8765) == (
        "tcp://rockford.local:8765"
    )
    assert build_network_endpoint("tls", "10.0.6.143", 443) == "tls://10.0.6.143:443"
    assert build_network_endpoint("tls", "fe80::1", 8765) == "tls://[fe80::1]:8765"


@pytest.mark.parametrize(
    ("scheme", "host", "port"),
    [("http", "board.local", 8765), ("tcp", "", 8765), ("tcp", "bad host", 8765)],
)
def test_network_endpoint_rejects_invalid_fields(scheme: str, host: str, port: int) -> None:
    with pytest.raises(ValueError):
        build_network_endpoint(scheme, host, port)


def test_worker_passes_tcp_and_tls_options_to_board() -> None:
    class RecordingBoard(Board):
        opened: tuple[str, dict[str, object]] | None = None

        def __init__(self, port: str, **kwargs: object) -> None:
            type(self).opened = (port, kwargs)

        def set_debug_out(self, callback: object) -> None:
            pass

        def force_text_mode(self) -> None:
            pass

        def firmware_version(self) -> object:
            return SimpleNamespace(firmware="Rockford", version="0.7")

        def connect_power(self) -> str:
            return "NONE"

        def status(self) -> dict[str, int]:
            return {"actuator_count": 8}

        def close(self) -> None:
            pass

    worker = BoardWorker(RecordingBoard)
    options = {
        "network_token": "secret",
        "tls_fingerprint": "AA" * 32,
    }

    worker._connect("tls://rockford.local:8765", options)

    assert RecordingBoard.opened == ("tls://rockford.local:8765", options)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_connection_dialog_has_serial_and_network_choices(qt_app: QApplication) -> None:
    dialog = ConnectionDialog()

    assert dialog.connection_tabs.count() == 3
    assert dialog.connection_tabs.tabText(0) == "Serial"
    assert dialog.connection_tabs.tabText(1) == "Network"
    assert dialog.connection_tabs.tabText(2) == "Bluetooth"
    assert dialog.tls_options.isHidden()
    assert isinstance(dialog.network_encryption, LabeledToggle)
    assert dialog.network_encryption.text() == "Encryption"

    dialog.network_encryption.setChecked(True)
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.show()
    qt_app.processEvents()

    assert dialog.tls_options.isVisible()
    dialog.close()


def test_connection_token_uses_trailing_eye_action(qt_app: QApplication) -> None:
    dialog = ConnectionDialog()
    try:
        assert dialog.network_token_visibility in dialog.network_token.actions()
        assert dialog.bluetooth_token_visibility in dialog.bluetooth_token.actions()
        assert dialog.network_token.echoMode() == QLineEdit.Password
        dialog.network_token_visibility.trigger()
        assert dialog.network_token.echoMode() == QLineEdit.Normal
    finally:
        dialog.close()


def test_connection_field_labels_are_explicitly_styled(qt_app: QApplication) -> None:
    dialog = ConnectionDialog()
    try:
        expected = {
            "Serial port",
            "Host",
            "Port",
            "Access token",
            "Certificate",
            "Certificate name",
            "Board",
        }
        labels = {
            label.text(): label.objectName()
            for label in dialog.findChildren(QLabel)
            if label.text() in expected
        }

        assert set(labels) == expected
        assert all(object_name == "FormLabel" for object_name in labels.values())
    finally:
        dialog.close()


def test_bluetooth_connection_uses_discovered_device_and_options(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    attempts: list[tuple[str, dict[str, object]]] = []
    dialog.attempt_requested.connect(
        lambda endpoint, options: attempts.append((endpoint, options))
    )
    device = BluetoothDevice(
        identifier="Rockford-3D3731",
        name="Rockford-3D3731",
        address="platform-address",
        rssi=-47,
    )
    dialog._on_bluetooth_devices((device,))
    dialog.connection_tabs.setCurrentIndex(2)
    dialog.bluetooth_token.setText("secret")
    dialog.bluetooth_pair.setChecked(True)

    dialog._attempt_connection()

    assert attempts == [
        ("ble://Rockford-3D3731", {"pair": True, "network_token": "secret"})
    ]
    dialog.close()


def test_failed_connection_stays_in_dialog_and_preserves_fields(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.network_host.setText("rockford.local")
    dialog.network_token.setText("secret")
    dialog.show()
    dialog.set_connecting(True)

    dialog.connection_failed("Connection refused")
    qt_app.processEvents()

    assert dialog.isVisible()
    assert dialog.error_label.text() == "Connection refused"
    assert dialog.error_label.isVisible()
    assert dialog.network_host.text() == "rockford.local"
    assert dialog.network_token.text() == "secret"
    assert dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
    dialog.close()


def test_network_connection_allows_an_empty_access_token(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    attempts: list[tuple[str, dict[str, object]]] = []
    dialog.attempt_requested.connect(
        lambda endpoint, options: attempts.append((endpoint, options))
    )
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.network_host.setText("127.0.0.1")
    dialog.network_token.clear()

    dialog._attempt_connection()

    assert attempts == [("tcp://127.0.0.1:8765", {})]
    dialog.close()


def test_dashboard_connection_bar_matches_network_setup_behavior(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    try:
        assert window.connection_label.text() == "Not connected"
        assert window.connection_label.property("kind") == "neutral"
        assert window.disconnect_btn.isHidden()
        assert window.connect_btn.isEnabled()

        window._on_connected_changed(True, "Rockford 1.0")

        assert window.connection_label.text() == "Rockford 1.0"
        assert window.connection_label.property("kind") == "ok"
        assert not window.disconnect_btn.isHidden()
        assert window.disconnect_btn.isEnabled()
        assert not window.connect_btn.isEnabled()

        window._on_connected_changed(False, "Connection closed")

        assert window.connection_label.text() == "Not connected"
        assert window.connection_label.property("kind") == "neutral"
        assert window.disconnect_btn.isHidden()
        assert window.connect_btn.isEnabled()
    finally:
        window.close()


def test_dashboard_removes_actuator_cards_on_disconnect_and_rebuilds_on_reconnect(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    try:
        window._configure_actuator_cards(8)
        assert len(window._cards) == 8
        assert window.actuator_grid.count() == 8
        assert window._actuator_count == 8

        window._on_connected_changed(False, "Connection closed")

        assert window._cards == []
        assert window.actuator_grid.count() == 0
        assert window._actuator_count == 0
        assert window.group_combo.count() == 0
        assert window.group_combo.isHidden()
        assert window.group_label.isHidden()

        window._on_connected_changed(True, "Rockford 1.0")
        window._configure_actuator_cards(8)
        assert len(window._cards) == 8
        assert window.actuator_grid.count() == 8
    finally:
        window.close()


def test_redetect_all_button_queues_full_detection(qt_app: QApplication) -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    try:
        window.worker.enqueue = lambda command, *args: commands.append((command, args))
        window._on_connected_changed(True, "Rockford 1.0")
        window.redetect_all_btn.click()

        assert window.redetect_all_btn.text() == "Redetect all"
        assert commands == [("detect_all", ())]
    finally:
        window.close()


@pytest.mark.parametrize("state", ["idle", "error"])
def test_detected_actuator_card_shows_current_without_raw_measurements(
    qt_app: QApplication,
    state: str,
) -> None:
    card = ActuatorCard(0)
    try:
        card.set_health(
            {
                "state": state,
                "delta_ma": 0.42,
                "baseline_ma": 0.96,
                "forward_ma": 1.38,
            }
        )

        assert card.value.text() == "current 0.42 mA"
        assert card.runtime.text() == ""
        assert "base" not in card.value.text()
        assert "fwd" not in card.value.text()
    finally:
        card.close()


def test_not_connected_actuator_card_shows_no_current_measurements(
    qt_app: QApplication,
) -> None:
    card = ActuatorCard(0)
    try:
        card.set_health(
            {
                "state": "disconnected",
                "delta_ma": 0.02,
                "baseline_ma": 0.09,
                "forward_ma": 0.11,
            }
        )

        assert card.state.text() == "Not connected"
        assert card.value.text() == ""
        assert card.runtime.text() == ""
    finally:
        card.close()
