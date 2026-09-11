from __future__ import annotations

import os
import base64
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QSizePolicy,
)

from fluid_reality import BluetoothDevice, Board, FirmwareError, Lansing, Rockford, WifiNetwork
from apps.fluidreality_dashboard.app import (
    ActuatorCard,
    BoardSettingsDialog,
    BoardWorker,
    BluetoothConfigDialog,
    ConnectionDialog,
    DashboardWindow,
    DiagnosisVoltageCurrentPlot,
    FluidRealityBoard,
    FirmwareUpdateDialog,
    FactoryResetDialog,
    NetworkConfigDialog,
    SecurityEncryptionDialog,
    WifiConfigDialog,
    build_network_endpoint,
    describe_connection_error,
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
    dt0_error_ma: float | None = None,
    dt1_error_ma: float | None = None,
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
    if dt0_error_ma is not None:
        summary += f",DT0_ERR>{dt0_error_ma:.2f}"
    if dt1_error_ma is not None:
        summary += f",DT1_ERR>{dt1_error_ma:.2f}"
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
        status_lines(
            8,
            include_balance=True,
            detection_limit_ma=0.34,
            dt0_error_ma=11.0,
            dt1_error_ma=4.0,
        )
    )
    board = FluidRealityBoard(transport=transport)

    status = board.status()

    assert status["detection_current_limit_ma"] == pytest.approx(0.34)
    assert board.not_connected_delta_ma == pytest.approx(0.34)
    assert board.initial_detection_error_delta_ma == pytest.approx(11.0)
    assert board.error_delta_ma == pytest.approx(4.0)
    assert status["config"]["dt0_error_threshold_ma"] == pytest.approx(11.0)
    assert status["config"]["dt1_error_threshold_ma"] == pytest.approx(4.0)


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


def test_factory_reset_reconnects_to_the_same_serial_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReconnectingBoard(Board):
        opened: list[tuple[str, dict[str, object]]] = []
        reset_calls = 0

        def __init__(self, port: str, **kwargs: object) -> None:
            type(self).opened.append((port, dict(kwargs)))

        def set_debug_out(self, callback: object) -> None:
            return None

        def force_text_mode(self) -> None:
            return None

        def firmware_version(self) -> object:
            return SimpleNamespace(firmware="Rockford", version="1.0")

        def capabilities(self) -> dict[str, str]:
            return {"FCR": "1"}

        def connect_power(self) -> str:
            return "NONE"

        def status(self) -> dict[str, int]:
            return {"actuator_count": 8}

        def raw_command(self, command: str, *params: object) -> list[object]:
            assert (command, params) == ("CFG", ("FACTORY_RESET",))
            type(self).reset_calls += 1
            return []

        def close(self) -> None:
            return None

    monkeypatch.setattr("apps.fluidreality_dashboard.app.time.sleep", lambda _: None)
    worker = BoardWorker(ReconnectingBoard)
    original = ReconnectingBoard("COM19", baudrate=250000)
    worker._board = original
    worker._endpoint = "COM19"
    worker._connection_options = {"baudrate": 250000}

    worker._factory_reset()

    assert ReconnectingBoard.reset_calls == 1
    assert ReconnectingBoard.opened == [
        ("COM19", {"baudrate": 250000}),
        ("COM19", {"baudrate": 250000}),
    ]
    assert worker._board is not original


def test_square_wave_uses_equal_one_second_positive_and_reverse_phases() -> None:
    class SquareBoard:
        direct_top_bottom_output = True

        def __init__(self) -> None:
            self.phases: list[tuple[int, int, str, int]] = []

        def _initialization_output_current(
            self,
            actuator: int,
            output: int,
            phase: str,
            duration_ms: int,
        ) -> float:
            self.phases.append((actuator, output, phase, duration_ms))
            return 0.6 if phase == "positive" else 0.1

    worker = BoardWorker()
    board = SquareBoard()
    worker._board = board  # type: ignore[assignment]
    worker._square_actuators = [2]
    worker._square_phase = "forward"
    worker._square_start_time = time.monotonic()
    worker._square_baseline_ma = 0.1
    worker._square_supply_voltage = 120.0
    progress: list[dict] = []
    worker.square_progress.connect(progress.append)

    worker._service_square_wave()
    worker._service_square_wave()

    assert board.phases == [
        (2, Board.max_output, "positive", 1000),
        (2, Board.max_output, "negative", 1000),
    ]
    assert worker._square_phase == "forward"
    assert [entry["phase"] for entry in progress] == [
        "positive",
        "positive_measurement",
        "discharging",
    ]
    assert progress[-1]["status"] == "Discharging"


def test_diagnosis_warms_up_then_sweeps_zero_to_200_volts() -> None:
    class DiagnosisBoard:
        direct_top_bottom_output = True

        def __init__(self) -> None:
            self.outputs: list[tuple[int, int, str, int]] = []
            self.safety_enabled = True

        def voltage(self) -> float:
            return 200.0

        def safety(self, enabled: bool | None = None) -> bool:
            previous = self.safety_enabled
            if enabled is not None:
                self.safety_enabled = enabled
            return previous

        def _initialization_output_current(
            self,
            actuator: int,
            output: int,
            phase: str,
            duration_ms: int,
        ) -> float:
            self.outputs.append((actuator, output, phase, duration_ms))
            return 0.1 + 2.0 * output / Board.max_output

        def _set_initialization_output(
            self, actuator: int, output: int, phase: str
        ) -> None:
            self.final_output = (actuator, output, phase)

        def classify_diagnosis(self, diagnosis) -> SimpleNamespace:
            return SimpleNamespace(
                actuator=diagnosis.actuator,
                state="Ready",
                baseline_ma=diagnosis.baseline_ma,
                forward_ma=diagnosis.forward_ma,
                discharge_ma=diagnosis.discharge_ma,
                delta_ma=diagnosis.forward_ma - diagnosis.baseline_ma,
                initial_forward_ma=None,
                initial_delta_ma=None,
            )

    worker = BoardWorker()
    board = DiagnosisBoard()
    worker._board = board  # type: ignore[assignment]
    worker._emit_status = lambda: None  # type: ignore[method-assign]
    progress: list[dict] = []
    ready: list[dict] = []
    finished: list[tuple[bool, str]] = []
    worker.diagnosis_progress.connect(progress.append)
    worker.diagnosis_ready.connect(ready.append)
    worker.diagnosis_finished.connect(
        lambda completed, status: finished.append((completed, status))
    )

    worker._diagnose_actuator(2)

    assert board.outputs[:6] == [
        (2, Board.max_output, "positive", 1000),
        (2, Board.max_output, "negative", 1000),
    ] * 3
    assert [entry[3] for entry in board.outputs[6:]] == [250] * 21
    assert [entry[2] for entry in board.outputs[6:]] == ["off"] + ["positive"] * 20
    assert [entry[1] for entry in board.outputs[6:]] == [
        round(Board.max_output * voltage / 200) for voltage in range(0, 201, 10)
    ]
    assert [entry["phase"] for entry in progress[:6]] == ["warmup"] * 6
    assert [entry["sent_voltage"] for entry in progress[:6]] == [
        200.0,
        -200.0,
    ] * 3
    assert [entry["voltage_v"] for entry in progress[6:]] == list(
        range(0, 201, 10)
    )
    assert ready[-1]["sample_count"] == 21
    assert ready[-1]["max_voltage_v"] == 200.0
    assert finished == [(True, "Completed")]
    assert board.final_output == (2, 0, "off")
    assert board.safety_enabled


def test_automatic_recovery_advances_when_current_is_below_each_curve_target() -> None:
    class RecoveryBoard:
        direct_top_bottom_output = True

        def __init__(self) -> None:
            self.outputs: list[tuple[int, int, str, int]] = []
            self.safety_enabled = True
            self.positive_measurements = 0

        def voltage(self) -> float:
            return 200.0

        def safety(self, enabled: bool | None = None) -> bool:
            previous = self.safety_enabled
            if enabled is not None:
                self.safety_enabled = enabled
            return previous

        def _initialization_output_current(
            self,
            actuator: int,
            output: int,
            phase: str,
            duration_ms: int,
        ) -> float:
            self.outputs.append((actuator, output, phase, duration_ms))
            if phase == "positive":
                self.positive_measurements += 1
                return 2.0 if self.positive_measurements == 2 else 0.2
            return 0.1

        def set_manual_output(self, actuator: int, positive: int, negative: int) -> None:
            self.final_output = (actuator, positive, negative)

    worker = BoardWorker()
    board = RecoveryBoard()
    worker._board = board  # type: ignore[assignment]
    worker._emit_status = lambda: None  # type: ignore[method-assign]
    progress: list[dict] = []
    ready: list[dict] = []
    finished: list[tuple[str, bool, str]] = []
    worker.recovery_progress.connect(progress.append)
    worker.recovery_ready.connect(ready.append)
    worker.actuator_tool_finished.connect(
        lambda tool, completed, status: finished.append((tool, completed, status))
    )

    worker._recover_actuator(3)

    expected_outputs = [
        round(Board.max_output * voltage / 200.0)
        for voltage in Board.initialization_stages_v
    ]
    expected_cycles = [5, 3, 3, 3]
    assert board.outputs == [(3, 0, "off", 500)] + [
        entry
        for output, cycles in zip(expected_outputs, expected_cycles)
        for _cycle in range(cycles)
        for entry in ((3, output, "positive", 500), (3, output, "negative", 500))
    ]
    positive_progress = progress[1::2]
    assert [entry["stage_index"] for entry in positive_progress] == [
        1,
        1,
        1,
        1,
        1,
        2,
        2,
        2,
        3,
        3,
        3,
        4,
        4,
        4,
    ]
    assert [entry["target_delta_ma"] for entry in progress[1::2]] == pytest.approx(
        [
            0.9 * DiagnosisVoltageCurrentPlot.upper_reference_current(voltage)
            for voltage, cycles in zip(Board.initialization_stages_v, expected_cycles)
            for _cycle in range(cycles)
        ]
    )
    assert [entry["qualified_s"] for entry in positive_progress] == [
        1.0,
        0.0,
        1.0,
        2.0,
        3.0,
    ] + [1.0, 2.0, 3.0] * 3
    assert [entry["stage_complete"] for entry in positive_progress[:5]] == [
        False,
        False,
        False,
        False,
        True,
    ]
    assert [entry["stage_complete"] for entry in positive_progress[5:]] == [
        False,
        False,
        True,
    ] * 3
    assert ready[-1]["completed_stages"] == 4
    assert finished == [("recover", True, "Completed")]
    assert board.final_output == (3, 0, 0)
    assert board.safety_enabled


def test_board_settings_dialog_edits_all_cfg_values(qt_app: QApplication) -> None:
    dialog = BoardSettingsDialog(
        {
            "max_active_ms": 5000,
            "discharge_ms": 2000,
            "safe": True,
            "debug": False,
        }
    )
    try:
        dialog.set_loading(False)
        assert dialog.values() == {
            "max_active_ms": 5000,
            "discharge_ms": 2000,
            "safe": True,
            "debug": False,
        }
        assert isinstance(dialog.safety, LabeledToggle)
        assert isinstance(dialog.debug, LabeledToggle)
        assert dialog.save_button.text() == "Save settings"
    finally:
        dialog.close()


def test_board_settings_detection_fields_require_det_capability(
    qt_app: QApplication,
) -> None:
    unsupported = BoardSettingsDialog({}, detection_supported=False)
    supported = BoardSettingsDialog(
        {
            "detection_current_limit_ma": 0.20,
            "dt0_error_threshold_ma": 10.0,
            "dt1_error_threshold_ma": 3.0,
        },
        detection_supported=True,
    )
    try:
        unsupported.set_loading(False)
        supported.set_loading(False)
        assert unsupported.detection_current.isHidden()
        assert unsupported.dt0_error.isHidden()
        assert unsupported.dt1_error.isHidden()
        assert not supported.detection_current.isHidden()
        assert not supported.dt0_error.isHidden()
        assert not supported.dt1_error.isHidden()
        assert supported.values()["detection_current_limit_ma"] == pytest.approx(0.20)
        assert supported.values()["dt0_error_threshold_ma"] == pytest.approx(10.0)
        assert supported.values()["dt1_error_threshold_ma"] == pytest.approx(3.0)
    finally:
        unsupported.close()
        supported.close()


def test_wifi_config_dialog_copies_network_setup_wifi_controls(
    qt_app: QApplication,
) -> None:
    dialog = WifiConfigDialog()
    requested: list[bool] = []
    scans: list[bool] = []
    disconnects: list[bool] = []
    dialog.wifi_enabled_requested.connect(requested.append)
    dialog.scan_requested.connect(lambda: scans.append(True))
    dialog.disconnect_requested.connect(lambda: disconnects.append(True))
    try:
        assert dialog.windowTitle() == "Wi-Fi Config"
        assert dialog.wifi_enabled.text() == "Wi-Fi enabled"
        assert not hasattr(dialog, "apply_wifi")
        assert dialog.scan_button.text() == ""
        assert dialog.scan_button.objectName() == "quietButton"
        assert dialog.scan_button.toolTip() == "Refresh networks"
        assert not dialog.scan_button.icon().isNull()
        assert dialog.network_list.headerItem().text(0) == "Network"
        assert dialog.network_list.headerItem().text(4) == "Access point MAC"
        assert dialog.connection_button.text() == "Connect"
        assert not hasattr(dialog, "hidden_toggle")
        assert not hasattr(dialog, "forget_button")
        assert not hasattr(dialog, "disconnect_button")
        dialog.wifi_enabled.setChecked(True)
        assert requested == [True]
        saved_ssid = base64.b64encode(b"MICASA.DEVICES").decode("ascii")
        dialog.set_status(
            {
                "WIFI": "ON",
                "STATE": "CONNECTED",
                "SSID64": saved_ssid,
                "IP": "10.0.6.143",
                "CURRENT_BSSID": "AA:BB:CC:DD:EE:FF",
            }
        )
        assert requested == [True]
        assert dialog.status_label.text() == "Connected · MICASA.DEVICES"
        assert "10.0.6.143" not in dialog.status_label.text()
        assert dialog.status_label.property("kind") == "ok"
        dialog.set_networks(
            [
                WifiNetwork(0, "Other", -30, "OPEN", 6, "11:22:33:44:55:66"),
                WifiNetwork(1, "MICASA.DEVICES", -46, "WPA2_PSK", 1, "AA:BB:CC:DD:EE:FF"),
            ]
        )
        assert dialog.network_list.currentItem().text(0) == "MICASA.DEVICES"
        assert not dialog.network_list.currentItem().icon(0).isNull()
        assert dialog.connection_button.text() == "Disconnect"
        dialog._activate_selected()
        assert disconnects == [True]
        dialog.set_busy(False)
        dialog.set_hardware_safe(False)
        assert not dialog.scan_button.isEnabled()
        assert not dialog.wifi_enabled.isEnabled()
        assert not dialog.connection_button.isEnabled()
        assert not dialog.safety_warning.isHidden()
    finally:
        dialog.close()


def test_wifi_config_auto_scans_once_and_saved_network_reconnects() -> None:
    dialog = WifiConfigDialog()
    scans: list[bool] = []
    enabled: list[bool] = []
    dialog.scan_requested.connect(lambda: scans.append(True))
    dialog.wifi_enabled_requested.connect(enabled.append)
    try:
        dialog.start_automatic_scan()
        dialog.start_automatic_scan()
        assert scans == [True]
        dialog.set_status(
            {
                "WIFI": "OFF",
                "STATE": "DISCONNECTED",
                "SSID64": base64.b64encode(b"Saved network").decode("ascii"),
            }
        )
        dialog.set_networks(
            [WifiNetwork(0, "Saved network", -42, "WPA2_PSK", 1, "AA:BB:CC:DD:EE:FF")]
        )
        assert dialog.network_list.currentItem().text(0) == "Saved network"
        assert dialog.connection_button.text() == "Connect"
        assert dialog.network_password.isHidden()
        dialog._activate_selected()
        assert enabled == [True]
    finally:
        dialog.close()


def test_wifi_config_supports_client_and_access_point_modes(
    qt_app: QApplication,
) -> None:
    dialog = WifiConfigDialog(access_point_supported=True)
    modes: list[str] = []
    configurations: list[tuple[str, str, int]] = []
    dialog.mode_requested.connect(modes.append)
    dialog.access_point_config_requested.connect(
        lambda ssid, password, channel: configurations.append(
            (ssid, password, channel)
        )
    )
    dialog.show()
    try:
        dialog.set_status(
            {"WIFI": "ON", "STATE": "ACTIVE", "WIFI_MODE": "ACCESS_POINT"}
        )
        dialog.set_access_point_status(
            {
                "STATE": "ACTIVE",
                "SSID64": base64.b64encode(b"Rockford-Lab").decode("ascii"),
                "CHANNEL": "6",
                "IP": "192.168.4.1",
                "CLIENTS": "2",
            }
        )
        qt_app.processEvents()

        assert dialog.mode.currentData() == "ACCESS_POINT"
        assert dialog.access_point_panel.isVisible()
        assert dialog.network_list.isHidden()
        assert dialog.access_point_ssid.text() == "Rockford-Lab"
        assert dialog.access_point_channel.value() == 6
        assert "2 clients" in dialog.access_point_status.text()

        dialog.access_point_password.setText("secret123")
        dialog.apply_access_point.click()
        assert configurations == [("Rockford-Lab", "secret123", 6)]

        dialog.set_busy(False)
        dialog.mode.setCurrentIndex(0)
        dialog._mode_selected()
        assert modes == ["CLIENT"]
    finally:
        dialog.close()


def test_wifi_refresh_clears_rows_and_groups_access_points_by_ssid() -> None:
    dialog = WifiConfigDialog()
    scans: list[bool] = []
    dialog.scan_requested.connect(lambda: scans.append(True))
    networks = [
        WifiNetwork(0, "MICASA.DEVICES", -70, "WPA2_PSK", 1, "00:00:00:00:00:01"),
        WifiNetwork(1, "Other", -50, "OPEN", 6, "00:00:00:00:00:02"),
        WifiNetwork(2, "MICASA.DEVICES", -40, "WPA2_PSK", 3, "00:00:00:00:00:03"),
        WifiNetwork(3, "MICASA.DEVICES", -60, "WPA2_PSK", 11, "00:00:00:00:00:04"),
        WifiNetwork(4, "MICASA.DEVICES", -55, "WPA2_PSK", 8, "00:00:00:00:00:05"),
    ]
    try:
        dialog.set_status(
            {
                "WIFI": "ON",
                "STATE": "CONNECTED",
                "SSID64": base64.b64encode(b"MICASA.DEVICES").decode("ascii"),
                "CURRENT_BSSID": "00:00:00:00:00:01",
            }
        )
        dialog.set_networks(networks)

        assert dialog.network_list.topLevelItemCount() == 2
        grouped = dialog.network_list.topLevelItem(0)
        assert grouped.text(0) == "MICASA.DEVICES"
        assert grouped.text(1) == "-40 dBm"
        assert grouped.text(3) == "1,3,11,8"
        assert grouped.text(4) == "00:00:00:00:00:03"
        assert grouped.data(0, Qt.UserRole).index == 2
        assert not grouped.icon(0).isNull()

        dialog.scan_button.click()

        assert scans == [True]
        assert dialog.network_list.topLevelItemCount() == 0
        assert not dialog.connection_button.isEnabled()
        assert dialog.status_label.text() == "Refreshing networks…"
        assert dialog.status_label.property("kind") == "active"
    finally:
        dialog.close()


def test_wifi_disconnect_then_close_preserves_dashboard_serial_connection(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    try:
        window._on_connected_changed(True, "COM18 - Rockford 1.0")
        window._on_capabilities_ready({"WIFI": "1"})
        window._show_wifi_config()
        dialog = window._wifi_config_dialog
        assert dialog is not None
        assert dialog.isModal()
        commands.clear()

        dialog.set_status(
            {
                "WIFI": "ON",
                "STATE": "CONNECTED",
                "SSID64": base64.b64encode(b"MICASA.DEVICES").decode("ascii"),
                "CURRENT_BSSID": "AA:BB:CC:DD:EE:FF",
            }
        )
        dialog.set_networks(
            [WifiNetwork(0, "MICASA.DEVICES", -46, "WPA2_PSK", 1, "AA:BB:CC:DD:EE:FF")]
        )
        dialog._activate_selected()
        dialog.close()
        qt_app.processEvents()

        assert commands == [("wifi_disconnect", ())]
        assert window._connected
        assert not window.disconnect_btn.isHidden()
    finally:
        if window._wifi_config_dialog is not None:
            window._wifi_config_dialog.close()
        window.close()


def test_worker_wifi_disconnect_keeps_existing_board_transport() -> None:
    class SerialBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            operation = str(parts[1])
            if operation == "STATUS":
                fields = {"WIFI": "OFF", "STATE": "DISCONNECTED"}
            elif operation == "DIAG":
                fields = {"CURRENT_BSSID": "NONE"}
            else:
                fields = {"OP": operation}
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = SerialBoard()
    worker._board = board
    worker._capabilities = {"WIFI": "1"}

    worker._disconnect_wifi()

    assert worker._board is board
    assert board.commands == [
        ("NET", "FORGET"),
        ("NET", "STATUS"),
        ("NET", "DIAG"),
    ]


def test_network_config_dialog_copies_ip_and_tcp_sections() -> None:
    dialog = NetworkConfigDialog()
    saved: list[dict[str, object]] = []
    dialog.save_requested.connect(saved.append)
    try:
        assert dialog.windowTitle() == "Network Config"
        assert dialog.ip_panel.findChild(QLabel, "SectionTitle").text() == "IP addressing"
        assert dialog.tcp_panel.findChild(QLabel, "SectionTitle").text() == "TCP server"
        assert dialog.use_dhcp.text() == "Automatically obtain an IP address (DHCP)"
        assert dialog.tcp_enabled.text() == "Enable TCP server"
        assert dialog.save_button.text() == "Apply"
        assert dialog.footer.itemAt(dialog.footer.count() - 1).widget() is dialog.close_button
        assert dialog.tcp_port.maximum() == 65535
        assert dialog.tcp_port.width() == 110

        dialog.set_values(
            {
                "MODE": "STATIC",
                "IP": "10.0.6.143",
                "MASK": "255.255.255.0",
                "GW": "10.0.6.1",
                "DNS1": "1.1.1.1",
                "DNS2": "8.8.8.8",
                "HOST": "rockford-a172e0",
                "TCP": "ON",
                "PORT": "49765",
                "BIND": "WIFI",
            },
            ("WIFI", "ETH"),
            True,
        )
        assert dialog.ip_interface.itemText(0) == "Wi-Fi"
        assert dialog.tcp_bind.currentData() == "WIFI"
        assert not dialog.use_dhcp.isChecked()
        assert dialog.address.text() == "10.0.6.143"
        assert dialog.hostname.text() == "rockford-a172e0"
        assert dialog.tcp_enabled.isChecked()
        assert dialog.tcp_port.value() == 49765

        dialog._submit()
        assert saved[0]["address"] == "10.0.6.143"
        assert saved[0]["interface"] == "WIFI"
        assert saved[0]["tcp_bind"] == "WIFI"

        dialog.show_applied_status({"STATE": "CONNECTED", "IP": "10.0.6.143"})
        assert dialog.status_label.text() == (
            "Network status: Connected · IP address: 10.0.6.143"
        )
    finally:
        dialog.close()


def test_network_config_dialog_shows_fixed_access_point_address(
    qt_app: QApplication,
) -> None:
    dialog = NetworkConfigDialog()
    saved: list[dict[str, object]] = []
    dialog.save_requested.connect(saved.append)
    dialog.show()
    try:
        dialog.set_values(
            {
                "WIFI_MODE": "ACCESS_POINT",
                "MODE": "DHCP",
                "IP": "192.168.4.1",
                "MASK": "255.255.255.0",
                "GW": "192.168.4.1",
                "DNS1": "192.168.4.1",
                "HOST": "rockford-a172e0",
                "TCP": "ON",
                "PORT": "49765",
            },
            ("WIFI",),
            True,
        )
        qt_app.processEvents()

        assert dialog.use_dhcp.isHidden()
        assert dialog.ap_assignment.isVisible()
        assert dialog.ap_assignment.text() == "Fixed by access-point mode"
        assert dialog.address.text() == "192.168.4.1"
        assert dialog.subnet.text() == "255.255.255.0"
        assert dialog.address.isEnabled()
        assert dialog.subnet.isEnabled()
        assert not dialog.ip_form.isRowVisible(dialog.gateway)
        assert not dialog.ip_form.isRowVisible(dialog.dns1)
        assert not dialog.ip_form.isRowVisible(dialog.dns2)

        dialog._submit()
        assert saved[0]["apply_ip"] is True
        assert saved[0]["access_point_mode"] is True
    finally:
        dialog.close()


def test_dashboard_network_config_button_opens_embedded_dialog() -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    try:
        window._on_connected_changed(True, "COM18 - Rockford 1.0")
        window._on_capabilities_ready({"NET": "1", "NET_IF": "1", "WIFI": "1"})
        window.network_config_btn.click()

        assert window._network_config_dialog is not None
        assert window._network_config_dialog.isModal()
        assert commands == [("read_network_config", ())]
    finally:
        if window._network_config_dialog is not None:
            window._network_config_dialog.close()
        window.close()


def test_worker_reads_and_writes_network_config() -> None:
    class NetworkBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            if parts[1:3] == ("IF", "LIST"):
                fields = {"IFACES": "WIFI|ETH"}
            elif parts[1] == "STATUS":
                fields = {"MODE": "DHCP", "HOST": "rockford", "TCP": "ON", "PORT": "49765"}
            else:
                fields = {"OP": str(parts[1])}
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = NetworkBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "NET_IF": "1", "WIFI": "1", "ETH": "1"}
    worker._write_network_config(
        {
            "dhcp": False,
            "address": "10.0.6.143",
            "subnet": "255.255.255.0",
            "gateway": "10.0.6.1",
            "dns1": "1.1.1.1",
            "dns2": "8.8.8.8",
            "hostname": "rockford",
            "tcp_enabled": True,
            "tcp_port": 49765,
            "interface": "WIFI",
            "tcp_bind": "WIFI",
            "scoped": True,
        }
    )

    assert board.commands[:5] == [
        ("NET", "IF", "WIFI", "IP", "STATIC", "10.0.6.143", "255.255.255.0", "10.0.6.1", "1.1.1.1", "8.8.8.8"),
        ("NET", "HOST", "rockford"),
        ("NET", "TCP", "BIND", "WIFI"),
        ("NET", "TCP", "ON"),
        ("NET", "TCP", "PORT", 49765),
    ]


def test_worker_writes_separate_access_point_ip_config() -> None:
    class NetworkBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            fields = {
                "WIFI": "ON",
                "WIFI_MODE": "ACCESS_POINT",
                "STATE": "ACTIVE",
                "IP": "192.168.4.1",
            }
            if parts[1:3] == ("IF", "LIST"):
                fields = {"IFACES": "WIFI"}
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = NetworkBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "NET_IF": "1", "WIFI": "1", "AP": "1"}
    worker._write_network_config(
        {
            "apply_ip": True,
            "access_point_mode": True,
            "dhcp": True,
            "address": "192.168.4.1",
            "subnet": "255.255.255.0",
            "gateway": "192.168.4.1",
            "dns1": "192.168.4.1",
            "dns2": "0.0.0.0",
            "hostname": "rockford",
            "tcp_enabled": True,
            "tcp_port": 49765,
            "interface": "WIFI",
            "tcp_bind": "ANY",
            "scoped": True,
        }
    )

    assert ("NET", "AP", "IP", "192.168.4.1", "255.255.255.0") in board.commands
    assert not any(command[1:3] == ("IF", "WIFI") and "IP" in command for command in board.commands)
    assert ("NET", "HOST", "rockford") in board.commands


def test_worker_waits_for_dhcp_address_after_applying_network_config(
    monkeypatch,
) -> None:
    statuses = iter(
        (
            {
                "WIFI": "ON",
                "SSID64": "dGVzdA==",
                "STATE": "DISCONNECTED",
                "IP": "0.0.0.0",
            },
            {
                "WIFI": "ON",
                "SSID64": "dGVzdA==",
                "STATE": "CONNECTED",
                "IP": "10.0.6.201",
            },
        )
    )
    worker = BoardWorker()
    worker._network_command = lambda *parts: {}
    worker._network_config_snapshot = lambda: (next(statuses), ("WIFI",), True)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    saved: list[dict[str, str]] = []
    worker.network_config_saved.connect(
        lambda status, _interfaces, _scoped: saved.append(status)
    )

    worker._write_network_config(
        {
            "dhcp": True,
            "address": "",
            "subnet": "",
            "gateway": "",
            "dns1": "",
            "dns2": "",
            "hostname": "rockford",
            "tcp_enabled": True,
            "tcp_port": 49765,
            "interface": "WIFI",
            "tcp_bind": "ANY",
            "scoped": True,
        }
    )

    assert saved == [
        {
            "WIFI": "ON",
            "SSID64": "dGVzdA==",
            "STATE": "CONNECTED",
            "IP": "10.0.6.201",
        }
    ]


def test_security_encryption_dialog_copies_token_and_tls_sections() -> None:
    dialog = SecurityEncryptionDialog(auth_supported=True, tls_supported=True)
    applied: list[dict[str, object]] = []
    dialog.apply_requested.connect(applied.append)
    try:
        assert dialog.windowTitle() == "Security & Encryption"
        assert dialog.token.isReadOnly()
        assert dialog.generate_token_btn.text() == ""
        assert dialog.generate_token_btn.accessibleName() == "Generate new access token"
        assert not dialog.generate_token_btn.icon().isNull()
        assert dialog.copy_token_btn.text() == ""
        assert dialog.copy_token_btn.accessibleName() == "Copy access token"
        assert not dialog.copy_token_btn.icon().isNull()
        assert dialog.generate_token_btn.objectName() != "dangerButton"
        assert dialog.use_access_token.text() == "Use access token"
        assert dialog.tls_enabled.text() == "Enable TLS encryption"
        assert not hasattr(dialog, "tls_apply_btn")
        assert not hasattr(dialog, "tls_install_btn")
        assert not hasattr(dialog, "tls_clear_btn")
        assert dialog.close_button.text() == "Close"

        dialog.set_values("existing-token", {"STATE": "OFF", "READY": "YES"})
        assert dialog.token.text() == "existing-token"
        assert not dialog.apply_button.isEnabled()
        dialog._generate_token()
        generated = dialog.token.text()
        assert len(generated) == 32
        dialog.tls_enabled.setChecked(True)
        assert dialog.apply_button.isEnabled()
        dialog._apply()
        assert applied == [
            {
                    "token": generated,
                    "token_dirty": True,
                    "auth_enabled": True,
                    "auth_dirty": False,
                    "tls_enabled": True,
                "tls_dirty": True,
                "credentials_dirty": False,
            }
        ]
    finally:
        dialog.close()


def test_bluetooth_config_dialog_emits_only_changed_values() -> None:
    dialog = BluetoothConfigDialog()
    applied: list[dict[str, object]] = []
    dialog.save_requested.connect(applied.append)
    try:
        dialog.set_values(
            {"STATE": "ON", "NAME": "Rockford-A172E0", "SEC": "OFF"}
        )
        assert dialog.enabled.isChecked()
        assert dialog.name_prefix.text() == "FR-"
        assert dialog.name.text() == "A172E0"
        assert not dialog.secure.isChecked()
        assert dialog.apply_button.isEnabled()

        dialog.name.setText("LabBoard_7")
        dialog.secure.setChecked(True)
        dialog._apply()

        assert applied == [
            {
                "enabled": True,
                "enabled_dirty": False,
                "name": "LabBoard_7",
                "name_dirty": True,
                "secure": True,
                "security_dirty": True,
            }
        ]
    finally:
        dialog.close()


def test_worker_writes_bluetooth_configuration() -> None:
    class BluetoothBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            return [
                SimpleNamespace(
                    fields={"STATE": "ON", "NAME": "FR-LabBoard_7", "SEC": "ON"}
                )
            ]

    worker = BoardWorker()
    board = BluetoothBoard()
    worker._board = board
    worker._capabilities = {"BLT": "1"}

    worker._write_bluetooth_config(
        {
            "enabled": True,
            "enabled_dirty": False,
            "name": "LabBoard_7",
            "name_dirty": True,
            "secure": True,
            "security_dirty": True,
        }
    )

    assert board.commands == [
        ("BLT", "NAME", "LabBoard_7"),
        ("BLT", "SEC", "ON"),
        ("BLT", "STATUS"),
    ]


def test_security_apply_includes_selected_tls_credentials(tmp_path) -> None:
    certificate = tmp_path / "board.crt"
    private_key = tmp_path / "board.key"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        encoding="ascii",
    )
    dialog = SecurityEncryptionDialog(auth_supported=True, tls_supported=True)
    applied: list[dict[str, object]] = []
    dialog.apply_requested.connect(applied.append)
    try:
        dialog.set_values("existing-token", {"STATE": "ON", "READY": "YES"})
        dialog.tls_certificate_path.setText(str(certificate))
        dialog.tls_key_path.setText(str(private_key))
        dialog.tls_key_password.setText("key-password")
        dialog._apply()

        assert applied[0]["credentials_dirty"] is True
        assert applied[0]["certificate"] == certificate.read_bytes()
        assert applied[0]["private_key"] == private_key.read_bytes()
        assert applied[0]["key_password"] == "key-password"
    finally:
        dialog.close()


def test_dashboard_security_button_opens_embedded_dialog() -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    try:
        window._on_connected_changed(True, "COM18 - Rockford 1.0")
        window._on_capabilities_ready({"NET": "1", "AUTH": "1", "TLS": "1"})
        window.security_config_btn.click()

        assert window._security_config_dialog is not None
        assert window._security_config_dialog.isModal()
        assert commands == [("read_security_config", ())]
    finally:
        if window._security_config_dialog is not None:
            window._security_config_dialog.close()
        window.close()


def test_worker_applies_access_token_and_tls_together() -> None:
    class SecurityBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            if parts[1:3] == ("KEY", "SET"):
                fields = {"TOKEN": str(parts[3])}
            elif parts[1:3] == ("TLS", "ON"):
                fields = {"STATE": "ON", "READY": "YES"}
            else:
                fields = {}
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = SecurityBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "AUTH": "1", "TLS": "1"}
    ready: list[tuple[str, dict[str, str]]] = []
    worker.security_config_ready.connect(
        lambda token, tls: ready.append((token, tls))
    )

    worker._write_security_config(
        {
            "token": "new-token",
            "token_dirty": True,
            "tls_enabled": True,
            "tls_dirty": True,
        }
    )

    assert board.commands == [
        ("NET", "KEY", "SET", "new-token"),
        ("NET", "TLS", "ON"),
    ]
    assert ready == [
        (
            "new-token",
            {"STATE": "ON", "READY": "YES", "AUTH_STATE": "ON"},
        )
    ]


def test_access_token_toggle_disables_token_controls_and_updates_board() -> None:
    dialog = SecurityEncryptionDialog(auth_supported=True, tls_supported=False)
    applied: list[dict[str, object]] = []
    dialog.apply_requested.connect(applied.append)
    try:
        dialog.set_values(
            "existing-token", {"STATE": "OFF", "AUTH_STATE": "ON"}
        )
        assert dialog.use_access_token.isChecked()
        assert dialog.generate_token_btn.isEnabled()

        dialog.use_access_token.setChecked(False)

        assert not dialog.token.isEnabled()
        assert not dialog.copy_token_btn.isEnabled()
        assert not dialog.generate_token_btn.isEnabled()
        assert dialog.apply_button.isEnabled()
        dialog._apply()
        assert applied == [
            {
                "token": "existing-token",
                "token_dirty": False,
                "auth_enabled": False,
                "auth_dirty": True,
                "tls_enabled": False,
                "tls_dirty": False,
                "credentials_dirty": False,
            }
        ]
    finally:
        dialog.close()


def test_worker_disables_access_token_without_erasing_it() -> None:
    class SecurityBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            if parts[1:] == ("KEY",):
                fields = {"STATE": "ON", "TOKEN": "existing-token"}
            elif parts[1:] == ("KEY", "OFF"):
                fields = {"STATE": "OFF", "TOKEN": "existing-token"}
            else:
                fields = {}
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = SecurityBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "AUTH": "1", "TLS": "0"}

    worker._write_security_config(
        {
            "token": "existing-token",
            "token_dirty": False,
            "auth_enabled": False,
            "auth_dirty": True,
            "tls_enabled": False,
            "tls_dirty": False,
        }
    )

    assert board.commands == [
        ("NET", "KEY"),
        ("NET", "KEY", "OFF"),
    ]


def test_worker_installs_tls_credentials_before_enabling_tls() -> None:
    operations: list[tuple[object, ...]] = []

    class SecurityBoard:
        def raw_command(self, *parts: object):
            operations.append(("command", *parts))
            return [SimpleNamespace(fields={"STATE": "ON", "READY": "YES"})]

    worker = BoardWorker()
    worker._board = SecurityBoard()
    worker._capabilities = {"NET": "1", "AUTH": "0", "TLS": "1"}
    worker._network_secret_command = lambda *parts: operations.append(
        ("secret", *parts)
    ) or {}
    worker._upload_tls_pem = lambda kind, data: operations.append(
        ("upload", kind, data)
    )

    worker._write_security_config(
        {
            "credentials_dirty": True,
            "certificate": b"certificate",
            "private_key": b"private-key",
            "key_password": "password",
            "tls_dirty": True,
            "tls_enabled": True,
        }
    )

    assert operations == [
        ("secret", "TLS", "PASS", "cGFzc3dvcmQ="),
        ("upload", "CERT", b"certificate"),
        ("upload", "KEY", b"private-key"),
        ("command", "NET", "TLS", "ON"),
    ]


def test_worker_erases_tls_credentials_when_apply_has_tls_off() -> None:
    class SecurityBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            fields = (
                {"TOKEN": "new-token"}
                if parts[1:3] == ("KEY", "SET")
                else {"STATE": "OFF", "READY": "NO"}
            )
            return [SimpleNamespace(fields=fields)]

    worker = BoardWorker()
    board = SecurityBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "AUTH": "1", "TLS": "1"}

    worker._write_security_config(
        {
            "token": "new-token",
            "token_dirty": True,
            "tls_enabled": False,
            "tls_dirty": False,
            "credentials_dirty": False,
        }
    )

    assert board.commands == [
        ("NET", "KEY", "SET", "new-token"),
        ("NET", "TLS", "CLEAR"),
    ]


def test_auth_only_apply_does_not_send_tls_commands() -> None:
    class AuthBoard:
        def __init__(self) -> None:
            self.commands: list[tuple[object, ...]] = []

        def raw_command(self, *parts: object):
            self.commands.append(parts)
            return [SimpleNamespace(fields={"TOKEN": "new-token"})]

    worker = BoardWorker()
    board = AuthBoard()
    worker._board = board
    worker._capabilities = {"NET": "1", "AUTH": "1", "TLS": "0"}

    worker._write_security_config(
        {
            "token": "new-token",
            "token_dirty": True,
            "tls_enabled": False,
            "tls_dirty": False,
            "credentials_dirty": False,
        }
    )

    assert board.commands == [("NET", "KEY", "SET", "new-token")]


def test_worker_writes_and_reads_board_settings() -> None:
    calls: list[tuple[str, object]] = []

    class ConfigBoard:
        def max_active_time_ms(self, value=None):
            if value is not None:
                calls.append(("MAX", value))
            return 6000

        def discharge_time_ms(self, value=None):
            if value is not None:
                calls.append(("DIS", value))
            return 1500

        def safety(self, value=None):
            if value is not None:
                calls.append(("SAFE", value))
            return False

        def firmware_debug(self, value=None):
            if value is not None:
                calls.append(("DEBUG", value))
            return True

        def read_config(self):
            return SimpleNamespace(
                max_active_ms=6000,
                discharge_ms=1500,
                safe=False,
                debug=True,
            )

        def status(self):
            return {"config": {}}

    worker = BoardWorker()
    worker._board = ConfigBoard()
    saved: list[dict[str, object]] = []
    worker.board_config_saved.connect(saved.append)

    worker._write_board_config(
        {
            "max_active_ms": 6000,
            "discharge_ms": 1500,
            "safe": False,
            "debug": True,
        }
    )

    assert calls == [
        ("MAX", 6000),
        ("DIS", 1500),
        ("SAFE", False),
        ("DEBUG", True),
    ]
    assert saved == [
        {
            "max_active_ms": 6000,
            "discharge_ms": 1500,
            "safe": False,
            "debug": True,
        }
    ]


def test_worker_includes_detection_thresholds_for_det_capability() -> None:
    calls: list[tuple[str, float | None]] = []

    class DetectionConfigBoard:
        def read_config(self):
            return SimpleNamespace(
                max_active_ms=5000, discharge_ms=2000, safe=True, debug=False
            )

        def detection_current_limit_ma(self, value=None):
            calls.append(("DET_MIN", value))
            return 0.2 if value is None else value

        def dt0_error_threshold_ma(self, value=None):
            calls.append(("DT0_ERR", value))
            return 10.0 if value is None else value

        def dt1_error_threshold_ma(self, value=None):
            calls.append(("DT1_ERR", value))
            return 3.0 if value is None else value

    worker = BoardWorker()
    worker._board = DetectionConfigBoard()
    worker._capabilities = {"DET": "1"}
    configs: list[dict[str, object]] = []
    worker.board_config_ready.connect(configs.append)

    worker._read_board_config()

    assert calls == [("DET_MIN", None), ("DT0_ERR", None), ("DT1_ERR", None)]
    assert configs[0]["detection_current_limit_ma"] == pytest.approx(0.2)
    assert configs[0]["dt0_error_threshold_ma"] == pytest.approx(10.0)
    assert configs[0]["dt1_error_threshold_ma"] == pytest.approx(3.0)


def test_wifi_join_rescans_and_matches_bssid_when_index_expires() -> None:
    original = WifiNetwork(7, "MICASA.DEVICES", -46, "WPA2_PSK", 1, "AA:BB:CC:DD:EE:FF")
    refreshed = WifiNetwork(2, "MICASA.DEVICES", -48, "WPA2_PSK", 1, "AA:BB:CC:DD:EE:FF")
    worker = BoardWorker()
    attempts: list[WifiNetwork] = []
    waits: list[str] = []

    def send(network: WifiNetwork, _password: str) -> None:
        attempts.append(network)
        if len(attempts) == 1:
            raise FirmwareError(
                "NET", "ER:NET,OP>JOIN,REASON>INDEX", {"OP": "JOIN", "REASON": "INDEX"}
            )

    worker._send_wifi_join = send
    worker._collect_wifi_networks = lambda: [refreshed]
    worker._wait_for_wifi = lambda ssid: waits.append(ssid)

    worker._join_wifi(original, "secret123")

    assert attempts == [original, refreshed]
    assert waits == ["MICASA.DEVICES"]


def test_wifi_scan_releases_results_and_restores_network_immediately() -> None:
    worker = BoardWorker()
    calls: list[tuple[object, ...]] = []

    worker._require_wifi_capability = lambda: object()
    worker._wait_for_tcp_after_wifi_scan = lambda: None

    def command(operation: str, *params: object) -> dict[str, str]:
        calls.append((operation, *params))
        if (operation == "SCAN"):
            return {"STATE": "SCANNING"}
        if params == ():
            return {"COUNT": "1"}
        if params == (0,):
            return {
                "IDX": "0",
                "SSID64": "TUlDQVNBLkRFVklDRVM=",
                "RSSI": "-45",
                "SEC": "WPA2_PSK",
                "CH": "1",
                "BSSID": "AA:BB:CC:DD:EE:FF",
            }
        assert params == ("DONE",)
        return {"STATE": "DONE"}

    worker._wifi_command = command

    networks = worker._collect_wifi_networks()

    assert [network.ssid for network in networks] == ["MICASA.DEVICES"]
    assert calls == [("SCAN",), ("LIST",), ("LIST", 0), ("LIST", "DONE")]


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


def test_detect_all_uses_progressive_firmware_detection_when_advertised() -> None:
    calls: list[str] = []

    class FirmwareDetectionBoard:
        actuator_count = 2
        not_connected_delta_ma = 0.2

        def status(self) -> dict[str, str]:
            return {"psu": "ON", "psc": "ON", "voltage": "225", "current": "0.1"}

        def detect_all_firmware(self, progress_callback):
            calls.append("DT0")
            detections = []
            for actuator, state in ((0, "Ready"), (1, "Not connected")):
                detection = SimpleNamespace(
                    actuator=actuator,
                    state=SimpleNamespace(value=state),
                    baseline_ma=0.1,
                    forward_ma=0.4 if actuator == 0 else 0.11,
                    discharge_ma=0.0,
                    delta_ma=0.3 if actuator == 0 else 0.01,
                    initial_forward_ma=0.4 if actuator == 0 else 0.11,
                    initial_delta_ma=0.3 if actuator == 0 else 0.01,
                )
                detections.append(detection)
                progress_callback(detection)
            return tuple(detections)

        def detect_actuator(self, *_args, **_kwargs):
            raise AssertionError("SDK detection must not run when DET is advertised")

    worker = BoardWorker()
    worker._board = FirmwareDetectionBoard()
    worker._capabilities = {"DET": "1"}
    worker._detect_all()

    assert calls == ["DT0"]
    assert worker._actuator_health[0]["state"] == "idle"
    assert worker._actuator_health[1]["state"] == "disconnected"


def test_network_endpoint_supports_plain_tls_and_ipv6() -> None:
    assert build_network_endpoint("tcp", "rockford.local", 49765) == (
        "tcp://rockford.local:49765"
    )
    assert build_network_endpoint("tls", "10.0.6.143", 443) == "tls://10.0.6.143:443"
    assert build_network_endpoint("tls", "fe80::1", 49765) == "tls://[fe80::1]:49765"


def test_network_auth_required_has_actionable_connection_error() -> None:
    error = FirmwareError(
        "NET",
        "ER:NET,OP>AUTH,REASON>REQUIRED",
        {"OP": "AUTH", "REASON": "REQUIRED"},
    )

    assert describe_connection_error("tcp://10.0.16.100:49765", {}, error) == (
        "This board requires an access token. Enter it in the Access token "
        "field and try again."
    )


@pytest.mark.parametrize(
    ("scheme", "host", "port"),
    [("http", "board.local", 49765), ("tcp", "", 49765), ("tcp", "bad host", 49765)],
)
def test_network_endpoint_rejects_invalid_fields(scheme: str, host: str, port: int) -> None:
    with pytest.raises(ValueError):
        build_network_endpoint(scheme, host, port)


def test_worker_passes_tcp_and_tls_options_to_board() -> None:
    class RecordingBoard(Board):
        opened: tuple[str, dict[str, object]] | None = None
        forced_text = False

        def __init__(self, port: str, **kwargs: object) -> None:
            type(self).opened = (port, kwargs)

        def set_debug_out(self, callback: object) -> None:
            pass

        def force_text_mode(self) -> None:
            type(self).forced_text = True

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

    worker._connect("tls://rockford.local:49765", options)

    assert RecordingBoard.opened == ("tls://rockford.local:49765", options)
    assert RecordingBoard.forced_text is False


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_connection_dialog_has_serial_and_network_choices(qt_app: QApplication) -> None:
    dialog = ConnectionDialog()

    assert dialog.connection_tabs.count() == 3
    assert dialog.connection_tabs.tabText(0) == "Serial"
    assert dialog.connection_tabs.tabText(1) == "Network"
    assert dialog.connection_tabs.tabText(2) == "Bluetooth"
    assert isinstance(dialog.network_encryption, LabeledToggle)
    assert dialog.network_encryption.text() == "Encryption"

    assert not hasattr(dialog, "tls_ca_file")
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

    assert attempts == [("tcp://127.0.0.1:49765", {})]
    dialog.close()


def test_dashboard_tls_connection_does_not_require_a_certificate(
    qt_app: QApplication,
) -> None:
    dialog = ConnectionDialog()
    attempts: list[tuple[str, dict[str, object]]] = []
    dialog.attempt_requested.connect(
        lambda endpoint, options: attempts.append((endpoint, options))
    )
    dialog.connection_tabs.setCurrentIndex(1)
    dialog.network_encryption.setChecked(True)
    dialog.network_host.setText("10.0.6.143")
    dialog._attempt_connection()

    assert attempts == [
        (
            "tls://10.0.6.143:49765",
            {"tls_verify_certificate": False},
        )
    ]
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


def test_dashboard_uses_short_timeout_for_network_connections(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    try:
        window._request_connection(
            "tcp://10.0.16.100:49765", {"network_token": "secret"}
        )

        assert commands == [
            (
                "connect",
                (
                    "tcp://10.0.16.100:49765",
                    {"network_token": "secret", "connect_timeout": 3.0},
                ),
            )
        ]
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
        assert all(
            window.actuator_grid.getItemPosition(index)[1] == 0
            for index in range(8)
        )
        assert [
            window.actuator_grid.getItemPosition(index)[0]
            for index in range(8)
        ] == list(range(8))
        assert all(card.height() == 54 for card in window._cards)

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

        assert window.redetect_all_btn.text() == ""
        assert not window.redetect_all_btn.icon().isNull()
        assert window.redetect_all_btn.toolTip() == "Redetect all actuators"
        assert commands == [("detect_all", ())]
    finally:
        window.close()


def test_actuator_tool_availability_follows_detection_state(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    try:
        assert window.actuator_tools_title.text() == "Actuator Tools"
        window._connected = True
        window._configure_actuator_cards(1)
        window._set_board_controls_enabled(True)
        window._cards[0].reset_detection()
        window._update_action_availability()
        assert not window.square_target_btn.isEnabled()
        assert not window.init_btn.isEnabled()
        assert not window.fast_init_btn.isEnabled()
        assert not window.diag_btn.isEnabled()
        assert not window.recover_btn.isEnabled()

        window._cards[0].set_health({"state": "idle", "delta_ma": 0.4})
        window._update_action_availability()
        assert window.square_target_btn.isEnabled()
        assert window.init_btn.isEnabled()
        assert window.fast_init_btn.isEnabled()
        assert window.diag_btn.isEnabled()
        assert not window.recover_btn.isEnabled()
        assert not window.recover_run_btn.isEnabled()

        window._cards[0].set_health({"state": "error", "delta_ma": 3.5})
        window._update_action_availability()
        assert not window.square_target_btn.isEnabled()
        assert window.init_btn.isEnabled()
        assert window.fast_init_btn.isEnabled()
        assert window.diag_btn.isEnabled()
        assert window.recover_btn.isEnabled()

        window._cards[0].set_health({"state": "disconnected"})
        window._update_action_availability()
        assert not window.square_target_btn.isEnabled()
        assert not window.init_btn.isEnabled()
        assert not window.fast_init_btn.isEnabled()
        assert not window.diag_btn.isEnabled()
        assert not window.recover_btn.isEnabled()
    finally:
        window.close()


def test_telemetry_cards_use_compact_status_bar_dimensions(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    try:
        window.show()
        qt_app.processEvents()
        cards = (
            window.psc_card,
            window.voltage_card,
            window.current_card,
        )
        assert all(card.height() == 58 for card in cards)
        assert max(card.width() for card in cards) - min(card.width() for card in cards) <= 1
        assert all(
            card.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
            for card in cards
        )
        assert window.psc_card.toggle.size().width() == 44
        assert window.psc_card.toggle.size().height() == 24
        assert window.psc_card.title_label.text() == "Power"
        assert window.psc_card.value_label.text() == "OFF"
    finally:
        window.close()


def test_output_connection_operates_psu_and_psc_in_safe_order() -> None:
    class PowerBoard:
        def __init__(self) -> None:
            self.actions: list[tuple[str, bool]] = []
            self.power_connection_supported = True

        def connect_power(self, enabled: bool) -> None:
            self.actions.append(("psc", enabled))

        def power_supply(self, enabled: bool) -> None:
            self.actions.append(("psu", enabled))

    worker = BoardWorker()
    board = PowerBoard()
    worker._board = board

    worker._set_power_path(True)
    worker._set_power_path(False)

    assert board.actions == [
        ("psu", True),
        ("psc", True),
        ("psc", False),
        ("psu", False),
    ]


def test_power_toggle_skips_psc_when_board_does_not_have_one() -> None:
    class PowerBoard:
        power_connection_supported = False

        def __init__(self) -> None:
            self.actions: list[tuple[str, bool]] = []

        def connect_power(self, enabled: bool | None = None) -> None:
            raise AssertionError("PSC must not be called")

        def power_supply(self, enabled: bool) -> None:
            self.actions.append(("psu", enabled))

    worker = BoardWorker()
    board = PowerBoard()
    worker._board = board

    worker._set_power_path(True)
    worker._set_power_path(False)

    assert board.actions == [("psu", True), ("psu", False)]


def test_tool_parameters_live_in_hidden_popup_dialogs(
    qt_app: QApplication,
    tmp_path,
) -> None:
    window = DashboardWindow()
    try:
        assert len(window.tool_dialogs) == 5
        assert all(dialog.isHidden() for dialog in window.tool_dialogs)
        assert all(dialog.objectName() == "ToolDialog" for dialog in window.tool_dialogs)
        assert window.fast_init_target_spin.window() is window.fast_init_dialog
        assert window.fast_init_voltage_plot.window() is window.fast_init_dialog
        assert window.fast_init_current_delta_plot.window() is window.fast_init_dialog
        assert window.init_voltage_plot.window() is window.init_dialog
        assert window.init_current_delta_plot.window() is window.init_dialog
        assert window.square_voltage_plot.window() is window.square_dialog
        assert window.square_current_delta_plot.window() is window.square_dialog
        assert window.diagnosis_plot.window() is window.diag_dialog
        assert window.recover_voltage_plot.window() is window.recover_dialog
        assert window.recover_current_delta_plot.window() is window.recover_dialog
        assert window.init_dialog.minimumWidth() == 650
        assert window.square_dialog.minimumWidth() == 650
        assert window.diag_dialog.minimumWidth() == 650
        assert window.recover_progress.window() is window.recover_dialog
        tool_action_sets = (
            (
                window.init_dialog,
                window.init_dialog.content.itemAt(0).layout(),
                window.init_run_btn,
                window.init_stop_btn,
                window.init_save_csv_btn,
                0,
                [1, 2, 3],
            ),
            (
                window.fast_init_dialog,
                window.fast_init_dialog.content.itemAt(0).layout(),
                window.fast_init_run_btn,
                window.fast_init_stop_btn,
                window.fast_init_save_csv_btn,
                2,
                [3, 4, 5],
            ),
            (
                window.diag_dialog,
                window.diag_dialog.content.itemAt(0).layout(),
                window.diag_run_btn,
                window.diag_stop_btn,
                window.diag_save_csv_btn,
                0,
                [1, 2, 3],
            ),
            (
                window.recover_dialog,
                window.recover_dialog.content.itemAt(0).layout(),
                window.recover_run_btn,
                window.recover_stop_btn,
                window.recover_save_csv_btn,
                0,
                [1, 2, 3],
            ),
            (
                window.square_dialog,
                window.square_dialog.content.itemAt(0).layout(),
                window.square_start_btn,
                window.square_stop_btn,
                window.square_save_csv_btn,
                0,
                [1, 2, 3],
            ),
        )
        for (
            dialog,
            action_bar,
            play,
            stop,
            save,
            spacer_index,
            button_indexes,
        ) in tool_action_sets:
            assert action_bar is not None
            assert action_bar.itemAt(spacer_index).spacerItem() is not None
            assert [action_bar.indexOf(button) for button in (play, stop, save)] == button_indexes
            assert all(button.text() == "" for button in (play, stop, save))
            assert all(not button.icon().isNull() for button in (play, stop, save))
            assert all(button.objectName() == "quietButton" for button in (play, stop, save))
            assert all(dialog.footer.indexOf(button) == -1 for button in (play, stop, save))
        fast_action_bar = window.fast_init_dialog.content.itemAt(0).layout()
        assert fast_action_bar.indexOf(window.fast_init_target_spin) == 1
        assert window.fast_init_target_spin.width() == 100

        window._set_active_wave_tool("init")
        all_wave_actions = [
            button
            for _dialog, _bar, play, stop, save, _spacer, _indexes in tool_action_sets
            for button in (play, stop, save)
        ]
        assert [button for button in all_wave_actions if button.isEnabled()] == [
            window.init_stop_btn
        ]
        window._set_active_wave_tool(None)
        window._set_active_wave_tool("diagnose")
        assert [button for button in all_wave_actions if button.isEnabled()] == [
            window.diag_stop_btn
        ]
        assert not window.diag_dialog.close_button.isEnabled()
        window._set_active_wave_tool(None)

        window._show_tool_dialog(window.init_dialog)
        qt_app.processEvents()

        assert window.init_dialog.isVisible()
        assert all(
            dialog.isHidden()
            for dialog in window.tool_dialogs
            if dialog is not window.init_dialog
        )

        window._on_initialization_progress(
            {
                "actuator": 2,
                "elapsed_s": 45.5,
                "total_s": 120,
                "stage_index": 2,
                "stage_count": 4,
                "stage_voltage": 50,
                "sent_voltage": -50,
                "phase_interval_s": 0.5,
                "baseline_current_ma": 0.75,
                "current_ma": 1.10,
                "delta_ma": 0.35,
            }
        )
        qt_app.processEvents()
        assert window.init_progress.value() == 91
        assert window.init_voltage_plot.elapsed_s == 45.5
        assert window.init_voltage_plot.visible_range_s == (15.5, 45.5)
        assert window.init_voltage_plot.samples == ((45.5, -50.0),)
        assert window.init_voltage_plot.axis_limit_v == 250.0
        assert window.init_voltage_plot._format_cursor_value(-50) == "-50 V"
        assert window.init_current_delta_plot.elapsed_s == 45.5
        assert window.init_current_delta_plot.visible_range_s == (15.5, 45.5)
        assert window.init_current_delta_plot.samples == ((45.5, 0.35),)
        assert window.init_current_delta_plot.axis_limit_ma == 0.5
        assert window.init_current_delta_plot._axis_bounds() == (0.0, 0.5)
        assert window.init_current_delta_plot._normalized_y(0.0) == 1.0
        assert window.init_current_delta_plot._format_cursor_value(0.35) == "+0.35 mA"
        assert window.init_save_csv_btn.isEnabled()
        assert not hasattr(window, "init_sent_voltage_label")
        assert not hasattr(window, "init_current_delta_label")
        csv_path = tmp_path / "initialization.csv"
        window._write_initialization_csv(csv_path)
        assert csv_path.read_text(encoding="utf-8").splitlines() == [
            "actuator,elapsed_s,stage,phase,voltage_v,current_delta_ma",
            "2,45.5,2,,-50.0,0.35",
        ]

        window._configure_actuator_cards(8)
        window._on_fast_init_progress(
            {
                "actuator": 2,
                "elapsed_s": 3.0,
                "duration_s": 60.0,
                "target_delta_ma": 2.0,
                "target_voltage": 100.0,
                "next_voltage": 90.0,
                "sent_voltage": 100.0,
                "phase": "positive",
                "status": "reducing",
                "delta_ma": 0.35,
            }
        )
        window._on_fast_init_progress(
            {
                "actuator": 2,
                "elapsed_s": 3.5,
                "duration_s": 60.0,
                "target_delta_ma": 2.0,
                "target_voltage": 100.0,
                "next_voltage": 90.0,
                "sent_voltage": -100.0,
                "phase": "negative",
                "status": "reducing",
            }
        )
        assert window.fast_init_voltage_plot.samples == ((3.0, 100.0),)
        assert window.fast_init_voltage_plot._axis_bounds() == (0.0, 250.0)
        assert window.fast_init_current_delta_plot.samples == ((3.0, 0.35),)
        assert window.fast_init_current_delta_plot._axis_bounds() == (0.0, 0.5)
        assert window.fast_init_current_delta_plot._normalized_y(0.0) == 1.0
        assert window.fast_init_current_delta_plot.elapsed_s == 3.5
        assert window.fast_init_save_csv_btn.isEnabled()
        assert "target 2.00 mA" in window.fast_init_status_label.text()
        fast_csv_path = tmp_path / "fast-init.csv"
        window._write_fast_initialization_csv(fast_csv_path)
        assert fast_csv_path.read_text(encoding="utf-8").splitlines() == [
            "actuator,elapsed_s,phase,voltage_v,current_delta_ma,target_delta_ma,next_voltage_v,status",
            "2,3.0,positive,100.0,0.35,2.0,90.0,reducing",
            "2,3.5,negative,-100.0,,2.0,90.0,reducing",
        ]

        window._on_square_progress(
            {
                "actuators": [2],
                "elapsed_s": 0.0,
                "phase": "baseline",
                "status": "Starting",
                "sent_voltage": 0.0,
            }
        )
        window._on_square_progress(
            {
                "actuators": [2],
                "elapsed_s": 1.0,
                "phase": "positive",
                "status": "Full output",
                "sent_voltage": 120.0,
            }
        )
        window._on_square_progress(
            {
                "actuators": [2],
                "elapsed_s": 2.0,
                "phase": "positive_measurement",
                "status": "Positive current measured",
                "current_delta_ma": 0.35,
            }
        )
        window._on_square_progress(
            {
                "actuators": [2],
                "elapsed_s": 2.1,
                "phase": "discharging",
                "status": "Discharging",
                "sent_voltage": -120.0,
            }
        )
        assert window.square_voltage_plot.samples == (
            (0.0, 0.0),
            (1.0, 120.0),
            (2.1, -120.0),
        )
        assert window.square_voltage_plot._axis_bounds() == (-250.0, 250.0)
        assert window.square_current_delta_plot.samples == ((2.0, 0.35),)
        assert window.square_current_delta_plot._normalized_y(0.0) == 1.0
        assert window.square_save_csv_btn.isEnabled()
        square_csv_path = tmp_path / "square-wave.csv"
        window._write_square_wave_csv(square_csv_path)
        assert square_csv_path.read_text(encoding="utf-8").splitlines() == [
            "actuator,elapsed_s,phase,voltage_v,current_delta_ma,status",
            "2,0.0,baseline,0.0,,Starting",
            "2,1.0,positive,120.0,,Full output",
            "2,2.0,positive_measurement,,0.35,Positive current measured",
            "2,2.1,discharging,-120.0,,Discharging",
        ]
        assert "stage 2/4" in window.init_elapsed_label.text()
        assert "±50 V" in window.init_elapsed_label.text()

        window._on_diagnosis_progress(
            {
                "actuator": 2,
                "phase": "warmup",
                "warmup_cycle": 1,
                "sent_voltage": -200.0,
                "completed_steps": 2,
                "total_steps": 27,
            }
        )
        assert window.diagnosis_progress_bar.value() == 2
        assert window.diagnosis_label.text() == "Warmup — cycle 1 of 3 — Reverse"
        window._on_diagnosis_progress(
            {
                "actuator": 2,
                "phase": "testing",
                "voltage_v": 20.0,
                "current_ma": -0.35,
                "completed_steps": 9,
                "total_steps": 27,
            }
        )
        assert window.diagnosis_progress_bar.value() == 9
        assert window.diagnosis_plot.samples == ((20.0, 0.35),)
        assert window.diagnosis_plot.format_current_value(0.35) == "0.35 mA"
        assert window._diagnosis_rows == [
            {"actuator": 2, "voltage_v": 20.0, "current_ma": 0.35}
        ]
        assert window.diagnosis_plot.current_axis_limit_ma == 5.0
        assert window.diagnosis_plot.hasHeightForWidth()
        assert window.diagnosis_plot.heightForWidth(480) == 360
        assert window.diagnosis_plot.lower_reference_current(0) == 0.3
        assert window.diagnosis_plot.lower_reference_current(200) == 1.5
        assert window.diagnosis_plot.upper_reference_current(0) == 0.6
        assert window.diagnosis_plot.upper_reference_current(200) == 3.0
        assert window.diagnosis_plot.lower_reference_current(100) > 0.9
        assert window.diagnosis_plot.upper_reference_current(100) > 1.8
        assert window.diagnosis_plot.current_zone(0, 0.3) == "green"
        assert window.diagnosis_plot.current_zone(0, 0.4) == "yellow"
        assert window.diagnosis_plot.current_zone(0, 0.7) == "red"
        assert "great shape" in window.diagnosis_plot.health_summary(
            [(0, 0.2), (100, 0.8), (200, 1.4)]
        )
        assert "initialization routines" in window.diagnosis_plot.health_summary(
            [(0, 0.7), (100, 1.5), (200, 2.0)]
        )
        assert "Recover routine" in window.diagnosis_plot.health_summary(
            [(0, 0.2), (100, 1.5), (200, 3.1)]
        )
        assert window.diagnosis_label.text() == "Testing — 20 V — 0.35 mA"
        window._on_diagnosis_ready(
            {
                "actuator": 2,
                "sample_count": 21,
                "max_voltage_v": 200.0,
                "health_summary": "Your actuator is in great shape.",
            }
        )
        assert window.diagnosis_label.text() == "Your actuator is in great shape."
        diagnosis_csv_path = tmp_path / "diagnosis.csv"
        window._write_diagnosis_csv(diagnosis_csv_path)
        assert diagnosis_csv_path.read_text(encoding="utf-8").splitlines() == [
            "actuator,voltage_v,current_ma",
            "2,20.0,0.35",
        ]

        window._on_recovery_progress(
            {
                "actuator": 2,
                "elapsed_s": 1.5,
                "stage_index": 1,
                "stage_count": 4,
                "stage_voltage": 25.0,
                "sent_voltage": 25.0,
                "phase": "positive",
                "current_ma": 1.2,
                "delta_ma": 1.1,
                "target_delta_ma": 1.05,
                "stage_complete": True,
            }
        )
        window._on_recovery_progress(
            {
                "actuator": 2,
                "elapsed_s": 2.0,
                "stage_index": 1,
                "stage_count": 4,
                "stage_voltage": 25.0,
                "sent_voltage": -25.0,
                "phase": "negative",
                "delta_ma": 1.1,
                "target_delta_ma": 1.05,
                "stage_complete": True,
            }
        )
        assert window.recover_progress.value() == 1
        assert window.recover_voltage_plot.samples == ((1.5, 25.0), (2.0, -25.0))
        assert window.recover_current_delta_plot.samples == ((1.5, 1.1),)
        assert window.recover_voltage_plot.markers == ((1.5, "↑ 25 V"),)
        assert window.recover_current_delta_plot.markers == ((1.5, "↑ 25 V"),)
        recovery_csv_path = tmp_path / "recovery.csv"
        window._write_recovery_csv(recovery_csv_path)
        assert recovery_csv_path.read_text(encoding="utf-8").splitlines() == [
            "actuator,elapsed_s,stage,phase,voltage_v,current_delta_ma,target_current_delta_ma",
            "2,1.5,1,positive,25.0,1.1,1.05",
            "2,2.0,1,negative,-25.0,,1.05",
        ]
    finally:
        window.close()


def test_board_tools_are_beside_actuator_tools(qt_app: QApplication) -> None:
    window = DashboardWindow()
    try:
        assert window.width() == 856
        assert window.height() == 811
        assert window.maximumWidth() == 876
        assert window.dashboard_content.maximumWidth() == 840
        assert window.actuator_panel.maximumWidth() == 220
        assert window.centralWidget().layout().contentsMargins().left() == 8
        assert window.actuator_scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        assert window.tool_sections.indexOf(window.actuator_tools_panel) == 0
        assert window.tool_sections.indexOf(window.board_tools_panel) == 1
        assert window.tool_sections.itemAt(0).alignment() & Qt.AlignTop
        assert window.tool_sections.itemAt(1).alignment() & Qt.AlignTop
        assert not hasattr(window, "selected_actuator_label")
        assert window.actuator_tools_panel.size() == window.board_tools_panel.size()
        tool_buttons = (
            window.init_btn,
            window.fast_init_btn,
            window.diag_btn,
            window.recover_btn,
            window.square_target_btn,
            window.board_settings_btn,
            window.bluetooth_config_btn,
            window.wifi_config_btn,
            window.network_config_btn,
            window.security_config_btn,
            window.fluid_mesh_btn,
            window.firmware_update_btn,
            window.factory_reset_btn,
        )
        assert {button.height() for button in tool_buttons} == {40}
        assert window.board_settings_btn.text() == "Board Settings"
        assert window.bluetooth_config_btn.text() == "Bluetooth Config"
        assert window.wifi_config_btn.text() == "Wi-Fi Config"
        assert window.network_config_btn.text() == "Network Config"
        assert window.security_config_btn.text() == "Security && Encryption"
        assert window.security_config_btn.accessibleName() == "Security & Encryption"
        assert window.fluid_mesh_btn.text() == "Fluid Mesh"
        assert not window.fluid_mesh_btn.isEnabled()
        assert window.firmware_update_btn.text() == "Update Firmware"
        assert not window.firmware_update_btn.isEnabled()
        assert window.factory_reset_btn.text() == "Factory Reset"
        assert not window.factory_reset_btn.isEnabled()

        window._connected = True
        window._set_board_controls_enabled(True)
        window._on_capabilities_ready({"FWU": "1"})
        assert window.firmware_update_btn.isEnabled()
        assert not window.fluid_mesh_btn.isEnabled()

        window._on_capabilities_ready({"MESH": "1"})
        assert window.fluid_mesh_btn.isEnabled()
        assert not window.firmware_update_btn.isEnabled()

        window._on_capabilities_ready({"BLT": "1"})
        assert window.bluetooth_config_btn.isEnabled()

        window._active_endpoint = "COM18"
        window._on_capabilities_ready({"FCR": "1"})
        assert window.factory_reset_btn.isEnabled()

        window._active_endpoint = "tcp://192.168.24.1:49765"
        window._on_capabilities_ready({"FCR": "1"})
        assert not window.factory_reset_btn.isEnabled()

        window._on_capabilities_ready({"FWU": "0", "MESH": "0"})
        assert not window.firmware_update_btn.isEnabled()
        assert not window.fluid_mesh_btn.isEnabled()
        assert not window.bluetooth_config_btn.isEnabled()
        assert not window.factory_reset_btn.isEnabled()
    finally:
        window.close()


def test_factory_reset_button_warns_then_dispatches_over_serial(
    qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    monkeypatch.setattr(window, "_show_factory_reset_warning", lambda: True)
    try:
        window._connected = True
        window._active_endpoint = "COM19"
        window._set_board_controls_enabled(True)
        window._on_capabilities_ready({"FCR": "1"})

        window._confirm_factory_reset()

        assert commands == [("factory_reset", ())]
        assert not window.factory_reset_btn.isEnabled()
    finally:
        window.close()


def test_factory_reset_confirmation_uses_dashboard_dialog_style(
    qt_app: QApplication,
) -> None:
    dialog = FactoryResetDialog()
    try:
        assert dialog.objectName() == "ToolDialog"
        assert dialog.windowTitle() == "Factory Reset"
        assert dialog.isModal()
        assert dialog.width() == 500
        assert dialog.reset_button.text() == "Factory Reset"
        assert dialog.reset_button.objectName() == "dangerButton"
        assert dialog.cancel_button.text() == "Cancel"
        assert dialog.cancel_button.objectName() == "SecondaryButton"
        assert dialog.cancel_button.isDefault()
        assert "cannot be undone" in dialog.warning_label.text()
    finally:
        dialog.close()


def test_wifi_config_button_opens_dashboard_owned_window(
    qt_app: QApplication,
) -> None:
    window = DashboardWindow()
    commands: list[tuple[str, tuple[object, ...]]] = []
    window.worker.enqueue = lambda command, *args: commands.append((command, args))
    try:
        window._connected = True
        window._set_board_controls_enabled(True)
        window._on_capabilities_ready({"WIFI": "1"})
        window.wifi_config_btn.click()
        qt_app.processEvents()

        assert window._wifi_config_dialog is not None
        assert window._wifi_config_dialog.isVisible()
        assert commands == [("wifi_status", ()), ("wifi_scan", ())]
    finally:
        if window._wifi_config_dialog is not None:
            window._wifi_config_dialog.close()
        window.close()


def test_firmware_update_dialog_emits_selected_image(tmp_path, qt_app: QApplication) -> None:
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"firmware")
    dialog = FirmwareUpdateDialog()
    requested: list[str] = []
    dialog.update_requested.connect(requested.append)
    try:
        dialog.path_edit.setText(str(image))
        assert dialog.update_button.isEnabled()
        dialog.update_button.click()
        qt_app.processEvents()
        assert requested == [str(image)]
        assert not dialog.abort_button.isHidden()
        assert not dialog.close_button.isEnabled()
        assert not dialog.progress.isTextVisible()
        dialog.set_progress(50, 200)
        assert dialog.status_label.text() == "Transferring firmware… 25.0% (50 / 200 bytes)"
    finally:
        dialog.set_updating(False)
        dialog.close()


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


def test_actuator_card_shows_present_between_dt0_and_dt1(
    qt_app: QApplication,
) -> None:
    card = ActuatorCard(2)
    try:
        card.set_health({"state": "present", "delta_ma": 0.42})

        assert card.state.text() == "Present"
        assert card.value.text() == "current 0.42 mA"
        assert card.runtime.text() == ""
    finally:
        card.close()


def test_undetected_actuator_card_is_compact_and_blank(qt_app: QApplication) -> None:
    card = ActuatorCard(0)
    try:
        assert card.height() == 54
        assert card.minimumWidth() == 0
        assert card.value.text() == ""
        assert card.runtime.text() == ""

        card.reset_detection()
        assert card.value.text() == ""
        assert card.runtime.text() == ""
    finally:
        card.close()
