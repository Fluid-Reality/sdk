from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
)
from apps.network_config.app import (
    ConnectionDialog,
    NetworkConfigWindow,
    NetworkWorker,
    TlsCertificateDialog,
    build_network_endpoint,
    decode_ssid,
    signal_level,
)
from apps.device_bridge.bridge import DeviceBridgeBoard
from shared.toggle import LabeledToggle
from fluid_reality import (
    Board,
    ConfigurableNetworkBoard,
    EthernetBoard,
    FirmwareError,
    NetworkBoard,
    WifiBoard,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_decode_ssid_handles_utf8_and_invalid_input() -> None:
    encoded = base64.b64encode("Lab network ✓".encode()).decode()

    assert decode_ssid(encoded) == "Lab network ✓"
    assert decode_ssid("not base64!") == "<invalid SSID>"


def test_signal_level_uses_four_clear_strength_bands() -> None:
    assert signal_level(-45) == 4
    assert signal_level(-55) == 3
    assert signal_level(-65) == 2
    assert signal_level(-75) == 1
    assert signal_level(-90) == 0


def test_network_endpoint_supports_tcp_tls_and_ipv6() -> None:
    assert build_network_endpoint("tcp", "rockford.local", 49765) == (
        "tcp://rockford.local:49765"
    )
    assert build_network_endpoint("tls", "fe80::1", 443) == "tls://[fe80::1]:443"


def test_connection_dialog_has_all_supported_transport_choices(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()

    assert dialog.connection_tabs.count() == 3
    assert dialog.connection_tabs.tabText(0) == "Serial"
    assert dialog.connection_tabs.tabText(1) == "Network"
    assert dialog.connection_tabs.tabText(2) == "Bluetooth"
    assert dialog.serial_port_label.text() == "Serial port"
    assert dialog.serial_port_label.objectName() == "FormLabel"
    assert isinstance(dialog.network_encryption, LabeledToggle)
    assert dialog.network_encryption.text() == "Encryption"
    assert dialog.network_host_label.text() == "Host"
    assert dialog.network_port_label.text() == "Port"
    assert dialog.network_token_label.text() == "Access token"
    assert dialog.network_host_label.objectName() == "FormLabel"
    assert isinstance(dialog.bluetooth_pair, LabeledToggle)
    assert dialog.bluetooth_pair.text() == "Pair and encrypt link"

    assert not hasattr(dialog, "tls_ca_file")
    dialog.close()


def test_every_form_field_has_an_explicit_visible_label(
    qt_app: QApplication,
) -> None:
    widgets = [
        ConnectionDialog(),
        TlsCertificateDialog("rockford.local"),
        NetworkConfigWindow(),
    ]
    try:
        for widget in widgets:
            for form in widget.findChildren(QFormLayout):
                for row in range(form.rowCount()):
                    field_item = form.itemAt(row, QFormLayout.FieldRole)
                    if field_item is None or field_item.widget() is None:
                        continue
                    label = form.labelForField(field_item.widget())
                    if label is None:  # A deliberate full-width row, such as a toggle.
                        continue
                    assert isinstance(label, QLabel)
                    assert label.text().strip(), f"blank label in {type(widget).__name__}"
                    assert label.objectName() == "FormLabel", label.text()

        for widget in widgets:
            for label in widget.findChildren(QLabel):
                if not label.text().strip() or label.property("kind") is not None:
                    continue
                assert label.objectName(), (
                    f"unstyled label {label.text()!r} in {type(widget).__name__}"
                )
    finally:
        for widget in widgets:
            widget.close()


def test_tls_certificate_dialog_uses_light_tool_dialog_style() -> None:
    dialog = TlsCertificateDialog()
    try:
        assert dialog.objectName() == "ToolDialog"
        assert dialog.server_name.text() == ""
        assert "Optional" in dialog.server_name.placeholderText()
        assert dialog.private_key_file.text() == ""
        assert dialog.new_private_key_button.icon().isNull() is False
    finally:
        dialog.close()


def test_new_private_key_button_opens_save_dialog(
    qt_app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "new-key.pem"
    proposed_paths: list[str] = []

    def choose_file(_parent, _title, proposed, _filter, **_kwargs):
        proposed_paths.append(proposed)
        return str(selected), "PEM private key (*.pem)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_file)
    dialog = TlsCertificateDialog()
    try:
        dialog.server_name.setText("shared-board.local")
        dialog._choose_new_private_key()

        assert Path(proposed_paths[0]).name == "shared-board.local-private-key.pem"
        assert dialog.private_key_file.text() == str(selected)
        assert dialog._private_key_is_new is True
    finally:
        dialog.close()


def test_create_files_opens_save_dialog_with_recommended_filename(
    qt_app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "chosen-certificate.pem"
    proposed_paths: list[str] = []

    def choose_file(_parent, _title, proposed, _filter, **_kwargs):
        proposed_paths.append(proposed)
        return str(selected), "PEM certificate (*.pem)"

    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_file)
    dialog = TlsCertificateDialog()
    dialog.server_name.setText("shared-board.local")

    dialog._create()

    assert Path(proposed_paths[0]).name == "shared-board.local-certificate.pem"
    assert dialog.generated_files is not None
    assert dialog.generated_files.certificate == selected
    assert dialog.generated_files.private_key == tmp_path / "chosen-private-key.pem"


def test_create_files_asks_before_overwriting(
    qt_app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "existing-certificate.pem"
    selected.write_text("old certificate", encoding="utf-8")
    answers: list[str] = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(selected), "PEM certificate (*.pem)"),
    )

    def confirm(_parent, title, *_args, **_kwargs):
        answers.append(title)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    dialog = TlsCertificateDialog()

    dialog._create()

    assert answers == ["Overwrite existing files?"]
    assert dialog.generated_files is not None
    assert b"BEGIN CERTIFICATE" in selected.read_bytes()


def test_secret_fields_use_trailing_eye_actions(qt_app: QApplication) -> None:
    dialog = ConnectionDialog()
    window = NetworkConfigWindow()
    try:
        pairs = [
            (dialog.network_token, dialog.network_token_visibility),
            (window.network_password, window.network_password_visibility),
            (window.hidden_password, window.hidden_password_visibility),
            (window.token, window.token_visibility),
            (window.tls_key_password, window.tls_key_password_visibility),
        ]
        for field, action in pairs:
            assert action in field.actions()
            assert field.echoMode() == QLineEdit.Password
            action.trigger()
            assert field.echoMode() == QLineEdit.Normal
            action.trigger()
            assert field.echoMode() == QLineEdit.Password
    finally:
        dialog.close()
        window.close()


def test_failed_connection_preserves_network_fields(qt_app: QApplication) -> None:
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

    assert attempts == [("tcp://127.0.0.1:49765", {})]
    dialog.close()


def test_tls_connection_does_not_require_a_certificate(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    attempts: list[tuple[str, dict[str, object]]] = []
    dialog.attempt_requested.connect(
        lambda endpoint, options: attempts.append((endpoint, options))
    )
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.network_encryption.setChecked(True)
    dialog.network_host.setText("127.0.0.1")

    dialog._attempt_connection()

    assert attempts[0][1] == {"tls_verify_certificate": False}
    dialog.close()


def test_network_form_stays_top_aligned_when_encryption_is_toggled(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.show()
    qt_app.processEvents()
    initial_top = dialog.network_host.mapTo(dialog, dialog.network_host.rect().topLeft()).y()

    dialog.network_encryption.setChecked(True)
    qt_app.processEvents()
    dialog.network_encryption.setChecked(False)
    qt_app.processEvents()

    final_top = dialog.network_host.mapTo(dialog, dialog.network_host.rect().topLeft()).y()
    assert final_top == initial_top
    dialog.close()


def test_worker_accepts_combined_network_capability_class() -> None:
    class BluetoothBoard(Board):
        pass

    class CombinedBoard(NetworkBoard, BluetoothBoard):
        pass

    worker = NetworkWorker(CombinedBoard)

    assert worker._board_class is CombinedBoard


def test_worker_rejects_non_network_board() -> None:
    with pytest.raises(TypeError, match="NetworkBoard"):
        NetworkWorker(Board)


def test_worker_passes_tls_connection_options_to_network_board() -> None:
    class RecordingBoard(NetworkBoard):
        opened: tuple[str, dict[str, object]] | None = None

        def __init__(self, port: str, **kwargs: object) -> None:
            type(self).opened = (port, kwargs)

        def force_text_mode(self) -> None:
            pass

        def firmware_version(self) -> object:
            return SimpleNamespace(firmware="Rockford", version="0.7")

        def close(self) -> None:
            pass

    worker = NetworkWorker(RecordingBoard)
    options = {"network_token": "secret", "tls_ca_file": "rockford-ca.pem"}

    worker._connect("tls://rockford.local:49765", options)

    assert RecordingBoard.opened == ("tls://rockford.local:49765", options)
    assert RecordingBoard.supports_network_configuration() is False


def test_tls_status_only_reports_unsupported_for_firmware_capability_errors() -> None:
    class UnsupportedBoard(ConfigurableNetworkBoard):
        def __init__(self) -> None:
            pass

        def tls_status(self) -> dict[str, str]:
            raise FirmwareError(
                code="NET", raw="ER:NET,OP>TLS,REASON>UNSUPPORTED",
                fields={"OP": "TLS", "REASON": "UNSUPPORTED"},
            )

    worker = NetworkWorker()
    worker._board = UnsupportedBoard()
    responses: list[dict[str, str]] = []
    worker.tls_ready.connect(responses.append)

    worker._emit_tls_status()

    assert responses == [{"SUPPORTED": "NO"}]


def test_tls_status_does_not_hide_connection_failures_as_unsupported() -> None:
    class DisconnectedBoard(ConfigurableNetworkBoard):
        def __init__(self) -> None:
            pass

        def tls_status(self) -> dict[str, str]:
            raise RuntimeError("network connection closed")

    worker = NetworkWorker()
    worker._board = DisconnectedBoard()

    with pytest.raises(RuntimeError, match="connection closed"):
        worker._emit_tls_status()


def test_wifi_wait_reaches_connected_state(monkeypatch) -> None:
    class FakeBoard(WifiBoard):
        def __init__(self) -> None:
            self.states = iter(
                [
                    {"STATE": "CONNECTING", "IP": "0.0.0.0"},
                    {"STATE": "CONNECTED", "IP": "192.168.1.20"},
                ]
            )

        def network_status(self) -> dict[str, str]:
            return next(self.states)

    worker = NetworkWorker()
    worker._board = FakeBoard()
    monkeypatch.setattr("apps.network_config.app.time.sleep", lambda _seconds: None)

    worker._wait_for_wifi("Test network", -40)


def test_wifi_wait_reports_authentication_failure(monkeypatch) -> None:
    class FakeBoard(WifiBoard):
        def __init__(self) -> None:
            pass

        def network_status(self) -> dict[str, str]:
            return {"STATE": "AUTH_FAILED"}

    worker = NetworkWorker()
    worker._board = FakeBoard()
    monkeypatch.setattr("apps.network_config.app.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="Authentication failed"):
        worker._wait_for_wifi("Test network")


def test_window_defaults_to_generic_wifi_capability() -> None:
    app = QApplication.instance() or QApplication([])
    window = NetworkConfigWindow()
    try:
        assert window.worker._board_class is WifiBoard
        assert isinstance(window.ip_tcp_tab, QScrollArea)
        assert window.ip_tcp_tab.widgetResizable()
        assert window.tabs.indexOf(window.ip_tcp_tab) >= 0
        assert all(
            window.tabs.tabText(index) != "TLS encryption"
            for index in range(window.tabs.count())
        )
    finally:
        window.close()


def test_board_details_are_hidden_and_cleared_when_disconnected(
    qt_app: QApplication,
) -> None:
    window = NetworkConfigWindow()
    try:
        assert not window.disconnected_space.isHidden()
        assert window.board_details.isHidden()
        assert window.activity_panel.isHidden()
        assert window.activity_toggle.isHidden()
        assert not window.activity_toggle.isChecked()
        assert window.disconnect_btn.isHidden()
        window._on_connected(True, "Rockford 0.8")
        assert window.disconnected_space.isHidden()
        assert not window.board_details.isHidden()
        assert window.activity_panel.isHidden()
        assert not window.activity_toggle.isHidden()
        window.activity_toggle.setChecked(True)
        assert not window.activity_panel.isHidden()
        window.activity_toggle.setChecked(False)
        assert window.activity_panel.isHidden()
        assert not window.disconnect_btn.isHidden()
        window.wifi_status.value.setText("Connected")
        window.token.setText("stale-secret")

        window._on_connected(False, "Not connected")

        assert not window.disconnected_space.isHidden()
        assert window.board_details.isHidden()
        assert window.activity_panel.isHidden()
        assert window.activity_toggle.isHidden()
        assert window.disconnect_btn.isHidden()
        assert window.wifi_status.value.text() == "—"
        assert window.token.text() == ""
    finally:
        window.close()


def test_saving_over_serial_does_not_warn_or_disconnect(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = NetworkConfigWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    try:
        window._connected_over_network = False
        monkeypatch.setattr(
            window.worker, "enqueue",
            lambda command, *args: commands.append((command, args)),
        )
        monkeypatch.setattr(
            QMessageBox,
            "warning",
            lambda *_args, **_kwargs: pytest.fail("serial save must not show warning"),
        )
        monkeypatch.setattr(
            QMessageBox,
            "question",
            lambda *_args, **_kwargs: pytest.fail(
                "saving settings must not prompt to create a connection file"
            ),
        )

        window._regenerate_token()
        pending_token = window.token.text()
        assert len(pending_token) == 32
        assert window._token_dirty is True
        window._apply_settings()

        assert commands and commands[0][0] == "apply"
        settings = commands[0][1][0]
        assert settings["network_token"] == pending_token
        assert settings["token_dirty"] is True
        assert window._connected_over_network is False
    finally:
        window.close()


def test_access_token_controls_are_staged_and_use_normal_button_style(
    qt_app: QApplication,
) -> None:
    window = NetworkConfigWindow()
    try:
        assert not hasattr(window, "read_token_btn")
        assert window.generate_token_btn.objectName() != "dangerButton"
        assert window.token.isReadOnly()
        assert window.token.placeholderText() == "No access token configured"
    finally:
        window.close()


def test_connection_file_prompt_builds_portable_tls_yaml(
    qt_app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    certificate = tmp_path / "board.pem"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    destination = tmp_path / "rockford.connection.yaml"
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes
    )
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "YAML"),
    )
    monkeypatch.setattr(
        "apps.network_config.app.certificate_server_name",
        lambda _path: "rockford.local",
    )
    window = NetworkConfigWindow()
    try:
        window._network_features = ("HOST", "TCP", "AUTH", "TLS")
        window.hostname.setText("rockford.local")
        window.token.setText("saved-token")
        window.tls_enabled.setChecked(True)
        window.tls_certificate_path.setText(str(certificate))

        window._create_connection_file()

        saved = destination.read_text(encoding="utf-8")
        assert "transport: tls" in saved
        assert "access_token: saved-token" in saved
        assert "certificate: |" in saved
        assert "private_key" not in saved
    finally:
        window.close()


def test_connection_file_uses_active_network_endpoint_when_board_has_no_ip(
    qt_app: QApplication, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "bridge.connection.yaml"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "YAML"),
    )
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *_args, **_kwargs: pytest.fail(
            "an active network endpoint should supply the connection address"
        ),
    )
    window = NetworkConfigWindow()
    try:
        window._network_features = ("TCP", "AUTH")
        window._last_status = {"IP": "0.0.0.0"}
        window.address.setText("0.0.0.0")
        window._connected_over_network = True
        window._active_endpoint = "tcp://127.0.0.1:49765"

        window._create_connection_file()

        saved = destination.read_text(encoding="utf-8")
        assert "host: 127.0.0.1" in saved
    finally:
        window.close()


def test_worker_reads_access_token_immediately_after_serial_connection() -> None:
    class TokenBoard(ConfigurableNetworkBoard):
        network_interface_capability = "HOST"
        default_network_configuration_features = ("AUTH", "TLS")

        def __init__(self, _port: str, **_options: object) -> None:
            self.transport = SimpleNamespace(redirected=False)

        def force_text_mode(self) -> None:
            pass

        def firmware_version(self) -> object:
            return SimpleNamespace(firmware="Test", version="1.0")

        def network_status(self) -> dict[str, str]:
            return {}

        def network_interfaces(self) -> tuple[str, ...]:
            self._network_interface_commands_supported = True
            return ("HOST",)

        def network_key(self, *, regenerate: bool = False) -> str:
            assert regenerate is False
            return "existing-token"

        def tls_status(self) -> dict[str, str]:
            return {"STATE": "OFF"}

        def close(self) -> None:
            pass

    worker = NetworkWorker(TokenBoard)
    tokens: list[str] = []
    worker.token_ready.connect(tokens.append)

    worker._connect("COM18")

    assert tokens == ["existing-token"]


def test_worker_sets_pending_access_token_only_during_apply() -> None:
    class TokenBoard(ConfigurableNetworkBoard):
        def __init__(self) -> None:
            self.saved: list[str] = []

        def set_network_key(self, token: str) -> str:
            self.saved.append(token)
            return token

        def network_status(self) -> dict[str, str]:
            return {}

    board = TokenBoard()
    worker = NetworkWorker()
    worker._board = board

    worker._apply(
        {
            "features": ("AUTH",),
            "token_dirty": True,
            "network_token": "pending-token",
        }
    )

    assert board.saved == ["pending-token"]


def test_saving_over_network_requires_confirmation(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = NetworkConfigWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    prompts: list[str] = []
    try:
        window._connected_over_network = True
        monkeypatch.setattr(
            window.worker, "enqueue",
            lambda command, *args: commands.append((command, args)),
        )

        def cancel(_parent, _title, message, *_args) -> QMessageBox.StandardButton:
            prompts.append(message)
            return QMessageBox.Cancel

        monkeypatch.setattr(QMessageBox, "warning", cancel)
        window._apply_settings()
        assert commands == []
        assert "connection" in prompts[0].lower()
        assert "close" in prompts[0].lower()

        monkeypatch.setattr(
            QMessageBox, "warning", lambda *_args, **_kwargs: QMessageBox.Yes
        )
        window._apply_settings()
        assert commands and commands[0][0] == "apply"
    finally:
        window.close()


def test_window_shows_only_declared_interface_tabs(qt_app: QApplication) -> None:
    wifi_window = NetworkConfigWindow(WifiBoard)
    ethernet_window = NetworkConfigWindow(EthernetBoard)
    try:
        assert wifi_window.tabs.isTabVisible(
            wifi_window.tabs.indexOf(wifi_window.wifi_tab)
        )
        assert not wifi_window.tabs.isTabVisible(
            wifi_window.tabs.indexOf(wifi_window.ethernet_tab)
        )
        assert not ethernet_window.tabs.isTabVisible(
            ethernet_window.tabs.indexOf(ethernet_window.wifi_tab)
        )
        assert ethernet_window.tabs.isTabVisible(
            ethernet_window.tabs.indexOf(ethernet_window.ethernet_tab)
        )
    finally:
        wifi_window.close()
        ethernet_window.close()


def test_network_setup_exposes_access_point_mode_when_firmware_reports_it(
    qt_app: QApplication,
) -> None:
    window = NetworkConfigWindow(WifiBoard)
    try:
        window._on_configuration(True)
        window._on_interfaces(("WIFI",), True)
        window._on_features(("IP", "HOST", "TCP", "AUTH", "TLS", "AP"))
        window._on_connected(True, "Rockford")
        window._on_status(
            {
                "WIFI": "ON",
                "STATE": "ACTIVE",
                "WIFI_MODE": "ACCESS_POINT",
                "IP": "192.168.4.1",
                "MASK": "255.255.255.0",
                "GW": "192.168.4.1",
                "DNS1": "192.168.4.1",
            }
        )
        window._on_access_point_status(
            {
                "SSID64": base64.b64encode(b"Rockford-Lab").decode("ascii"),
                "CHANNEL": "11",
                "STATE": "ACTIVE",
                "IP": "192.168.4.1",
                "CLIENTS": "1",
            }
        )
        window.show()
        qt_app.processEvents()

        assert window.wifi_mode.isVisible()
        assert window.wifi_mode.currentData() == "ACCESS_POINT"
        assert window.access_point_panel.isVisible()
        assert window.network_list.isHidden()
        assert window.access_point_ssid.text() == "Rockford-Lab"
        assert window.access_point_channel.value() == 11
        assert "1 client" in window.access_point_state.text()
        assert window.use_dhcp.isHidden()
        assert not window.ap_assignment.isHidden()
        assert window.address.text() == "192.168.4.1"
        assert window.address.isEnabled()
        assert window.subnet.isEnabled()
        assert not window.ip_form.isRowVisible(window.gateway)
    finally:
        window.close()


def test_window_populates_dual_interface_configuration(qt_app: QApplication) -> None:
    class DualNetworkBoard(WifiBoard, EthernetBoard):
        pass

    window = NetworkConfigWindow(DualNetworkBoard)
    try:
        window._on_interfaces(("WIFI", "ETH"), True)

        assert window.tabs.isTabVisible(window.tabs.indexOf(window.wifi_tab))
        assert window.tabs.isTabVisible(window.tabs.indexOf(window.ethernet_tab))
        assert [window.ip_interface.itemData(index) for index in range(2)] == [
            "WIFI",
            "ETH",
        ]
        assert [window.tcp_bind.itemData(index) for index in range(3)] == [
            "ANY",
            "WIFI",
            "ETH",
        ]
        assert window.ip_interface.isEnabled()
        assert window.tcp_bind.isEnabled()
        assert not window.tabs.tabBar().isHidden()
    finally:
        window.close()


def test_connection_only_board_hides_device_network_configuration(
    qt_app: QApplication,
) -> None:
    window = NetworkConfigWindow(NetworkBoard)
    try:
        assert window.tabs.isTabVisible(
            window.tabs.indexOf(window.connection_only_tab)
        )
        for tab in (
            window.wifi_tab,
            window.ethernet_tab,
            window.ip_tcp_tab,
        ):
            assert not window.tabs.isTabVisible(window.tabs.indexOf(tab))
        assert NetworkBoard.supports_network_configuration() is False
        assert ConfigurableNetworkBoard.supports_network_configuration() is True
    finally:
        window.close()


def test_bridge_profile_shows_only_bridge_owned_network_features(
    qt_app: QApplication,
) -> None:
    window = NetworkConfigWindow(DeviceBridgeBoard)
    try:
        window._on_interfaces(("HOST",), True)
        window._on_features(("TCP", "TLS", "AUTH"))
        window._on_connected(True, "Device Bridge")
        window.show()
        qt_app.processEvents()

        assert window.address.isHidden()
        assert window.use_dhcp.isHidden()
        assert window.hostname.isHidden()
        assert window.tcp_panel.isVisible()
        assert window.tcp_port.width() == 110
        assert window.tcp_settings_row.itemAt(0).widget().text() == "Port"
        assert window.tcp_settings_row.itemAt(3).widget().text() == "Interface"
        assert window.tcp_settings_row.itemAt(4).widget() is window.tcp_bind
        assert window.access_panel.isVisible()
        assert all(
            window.tabs.tabText(index) != "Access token"
            for index in range(window.tabs.count())
        )
        assert window.token.parent() is window.access_panel
        assert window.encryption_panel.isVisible()
        assert window.tabs.tabBar().isHidden()
        assert window.tabs.property("singlePage") is True
        credential_labels = {
            label.text()
            for label in window.tls_credentials_panel.findChildren(QLabel)
            if label.objectName() == "FormLabel"
        }
        assert credential_labels == {
            "Server certificate",
            "Private key",
            "Key password",
        }
        window.tls_enabled.setChecked(True)
        assert window.tls_credentials_panel.isEnabled()
        window.tls_enabled.setChecked(False)
        assert not window.tls_credentials_panel.isEnabled()
        assert all(
            window.tabs.tabText(index) != "TLS encryption"
            for index in range(window.tabs.count())
        )
        assert not window.tabs.isTabVisible(
            window.tabs.indexOf(window.connection_only_tab)
        )
    finally:
        window.close()
