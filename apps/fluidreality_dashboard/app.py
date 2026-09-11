"""Fluid Reality board dashboard.

Run from the SDK root with:

    python apps/fluidreality_dashboard/app.py
"""

from __future__ import annotations

import base64
import csv
import html
import ipaddress
import queue
import re
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
APPS_ROOT = APP_ROOT.parent
LOGO_PATH = APP_ROOT / "assets" / "fluid_reality_logo_transparent.png"
APP_ICON_PATH = APPS_ROOT / "shared" / "assets" / "fluid-reality-icon.png"
CONNECTED_ICON_PATH = APP_ROOT / "assets" / "connected.svg"
COPY_ICON_PATH = APP_ROOT / "assets" / "copy.svg"
NETWORK_CONNECT_TIMEOUT_S = 3.0
NETWORK_SCAN_TCP_READY_WAIT_S = 8.0
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from PySide6.QtCore import QPointF, QProcess, QSize, QThread, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QDoubleSpinBox,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fluid_reality import (
    ActuatorState,
    BluetoothDevice,
    Board,
    Diagnosis,
    FirmwareError,
    WifiNetwork,
    discover_bluetooth_boards,
    is_virtual_port,
    list_ports,
)
from shared.secret_fields import add_secret_visibility
from shared.icon_buttons import configure_refresh_button
from shared.toggle import LabeledToggle
from network_config.app import TlsCertificateDialog, security_icon, signal_icon


STATE_NAMES = {
    0: "Idle",
    1: "Forward",
    2: "Discharge",
}


class ActuatorToolStopped(RuntimeError):
    """Raised inside the worker to stop an actuator waveform safely."""


def configure_tool_action_button(
    button: QPushButton,
    standard_icon: QStyle.StandardPixmap,
    accessible_name: str,
) -> QPushButton:
    """Apply the shared icon-only presentation used by live waveform tools."""

    button.setText("")
    button.setObjectName("quietButton")
    button.setAccessibleName(accessible_name)
    button.setToolTip(accessible_name)
    button.setIcon(button.style().standardIcon(standard_icon))
    button.setIconSize(QSize(20, 20))
    button.setFixedSize(40, 36)
    return button


def tool_action_bar(*buttons: QPushButton) -> QHBoxLayout:
    """Return a right-aligned action row for a waveform tool."""

    layout = QHBoxLayout()
    layout.setSpacing(8)
    layout.addStretch()
    for button in buttons:
        layout.addWidget(button)
    return layout


def format_ms(ms: int | float | None) -> str:
    if ms is None:
        return "-"
    value = int(ms)
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    return f"{minutes / 60:.1f} h"


def state_class(state: int) -> str:
    if state == 1:
        return "forward"
    if state == 2:
        return "discharge"
    return "idle"


def form_label(text: str) -> QLabel:
    """Create a consistently styled label for an editable dashboard field."""

    label = QLabel(text)
    label.setObjectName("FormLabel")
    return label


def build_network_endpoint(scheme: str, host: str, port: int) -> str:
    """Build a validated TCP/TLS endpoint from dashboard connection fields."""

    scheme = scheme.strip().lower()
    host = host.strip()
    if scheme not in {"tcp", "tls"}:
        raise ValueError("Network transport must be TCP or TLS")
    if not host or any(character.isspace() for character in host) or "/" in host:
        raise ValueError("Enter a valid board hostname or IP address")
    if not 1 <= int(port) <= 65535:
        raise ValueError("TCP port must be between 1 and 65535")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}"


def describe_connection_error(
    endpoint: str, options: dict[str, Any], error: Exception
) -> str:
    """Return an actionable dashboard message for connection failures."""

    message = str(error)
    if not endpoint.lower().startswith(("tcp://", "tls://")):
        return message

    fields = getattr(error, "fields", {})
    operation = str(fields.get("OP", "")).upper()
    reason = str(fields.get("REASON", "")).upper()
    if operation == "AUTH" and reason == "REQUIRED":
        return (
            "This board requires an access token. Enter it in the Access token "
            "field and try again."
        )
    if operation == "AUTH" and reason in {"FAILED", "INVALID"}:
        return "The access token was rejected. Check the token and try again."
    if "authentication failed" in message.lower() or "token was rejected" in message.lower():
        return "The access token was rejected. Check the token and try again."
    if not options.get("network_token") and (
        "closed" in message.lower() or "reset" in message.lower()
    ):
        return (
            "The board closed the network connection. If access-token protection "
            "is enabled, enter the token and try again."
        )
    return message


def power_connection_is_ready(state: object) -> bool:
    """Return whether a physical connection is closed or not applicable."""

    return str(state).strip().upper() in {"ON", "NONE"}


class FluidRealityBoard(Board):
    """Backward-compatible dashboard alias using only the shared board protocol."""


class BluetoothScanWorker(QThread):
    """Run BLE discovery without blocking the connection dialog."""

    devices_ready = Signal(tuple)
    scan_failed = Signal(str)

    def __init__(self, timeout: float = 5.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.timeout = timeout

    def run(self) -> None:
        try:
            self.devices_ready.emit(discover_bluetooth_boards(timeout=self.timeout))
        except Exception as exc:
            self.scan_failed.emit(str(exc))


class ConnectionDialog(QDialog):
    """Collect a serial, network, or Bluetooth connection without closing on errors."""

    attempt_requested = Signal(str, dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ConnectionDialog")
        self.setWindowTitle("Connect to a board")
        self.setModal(True)
        self.setMinimumWidth(570)
        self._bluetooth_scan: BluetoothScanWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)

        title = QLabel("Connect to a board")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.connection_tabs = QTabWidget()
        self.connection_tabs.setObjectName("ConnectionTabs")
        self.connection_tabs.addTab(self._build_serial_tab(), "Serial")
        self.connection_tabs.addTab(self._build_network_tab(), "Network")
        self.connection_tabs.addTab(self._build_bluetooth_tab(), "Bluetooth")
        layout.addWidget(self.connection_tabs)

        self.error_label = QLabel()
        self.error_label.setObjectName("ConnectionError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.buttons.accepted.connect(self._attempt_connection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._update_network_protocol()
        self.refresh_serial_ports()

    def _build_serial_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(10)
        explanation = QLabel("Select the USB serial port connected to the board.")
        explanation.setObjectName("ConnectionHint")
        layout.addWidget(explanation)

        row = QHBoxLayout()
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.setMinimumWidth(300)
        self.refresh_ports_button = configure_refresh_button(
            QPushButton(), "Refresh serial ports"
        )
        self.refresh_ports_button.clicked.connect(self.refresh_serial_ports)
        row.addWidget(form_label("Serial port"))
        row.addWidget(self.serial_port, 1)
        row.addWidget(self.refresh_ports_button)
        layout.addLayout(row)
        layout.addStretch()
        return tab

    def _build_network_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(12)
        explanation = QLabel("Use TCP on a trusted network, or TLS for encrypted traffic.")
        explanation.setObjectName("ConnectionHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.network_encryption = LabeledToggle("Encryption")
        self.network_encryption.toggled.connect(self._update_network_protocol)
        self.network_host = QLineEdit()
        self.network_host.setPlaceholderText("rockford.local or 10.0.6.143")
        self.network_port = QSpinBox()
        self.network_port.setRange(1, 65535)
        self.network_port.setValue(49765)
        self.network_token = QLineEdit()
        self.network_token.setPlaceholderText("Optional access token")
        self.network_token_visibility = add_secret_visibility(
            self.network_token, secret_name="access token"
        )
        form.addRow(form_label("Host"), self.network_host)
        form.addRow(form_label("Port"), self.network_port)
        form.addRow(form_label("Access token"), self.network_token)
        form.addRow(self.network_encryption)
        layout.addLayout(form)

        layout.addStretch()
        return tab

    def _build_bluetooth_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(12)
        explanation = QLabel(
            "Scan for nearby Fluid Reality boards, then select the board to connect."
        )
        explanation.setObjectName("ConnectionHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        device_row = QHBoxLayout()
        device_label = form_label("Board")
        self.bluetooth_device = QComboBox()
        self.bluetooth_device.setMinimumWidth(300)
        self.refresh_bluetooth_button = configure_refresh_button(
            QPushButton(), "Scan for Bluetooth boards"
        )
        self.refresh_bluetooth_button.clicked.connect(self.refresh_bluetooth_devices)
        device_row.addWidget(device_label)
        device_row.addWidget(self.bluetooth_device, 1)
        device_row.addWidget(self.refresh_bluetooth_button)
        layout.addLayout(device_row)

        self.bluetooth_scan_status = QLabel("Select “Scan for boards” to begin.")
        self.bluetooth_scan_status.setObjectName("ConnectionHint")
        layout.addWidget(self.bluetooth_scan_status)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.bluetooth_token = QLineEdit()
        self.bluetooth_token.setPlaceholderText("Optional access token")
        self.bluetooth_token_visibility = add_secret_visibility(
            self.bluetooth_token, secret_name="Bluetooth access token"
        )
        self.bluetooth_pair = LabeledToggle("Pair and encrypt link")
        form.addRow(form_label("Access token"), self.bluetooth_token)
        form.addRow(self.bluetooth_pair)
        layout.addLayout(form)
        layout.addStretch()
        return tab

    def refresh_serial_ports(self) -> None:
        current = self.serial_port.currentText().strip()
        try:
            ports = [port for port in list_ports() if not is_virtual_port(port)]
        except Exception:
            ports = []
        if current and current not in ports:
            ports.insert(0, current)
        self.serial_port.blockSignals(True)
        self.serial_port.clear()
        self.serial_port.addItems(ports)
        if current:
            self.serial_port.setCurrentText(current)
        self.serial_port.blockSignals(False)

    def refresh_bluetooth_devices(self) -> None:
        if self._bluetooth_scan is not None and self._bluetooth_scan.isRunning():
            return
        self.bluetooth_device.clear()
        self.bluetooth_scan_status.setText("Scanning for nearby boards…")
        self.refresh_bluetooth_button.setEnabled(False)
        worker = BluetoothScanWorker(parent=self)
        self._bluetooth_scan = worker
        worker.devices_ready.connect(self._on_bluetooth_devices)
        worker.scan_failed.connect(self._on_bluetooth_scan_failed)
        worker.finished.connect(self._bluetooth_scan_finished)
        worker.start()

    def _on_bluetooth_devices(self, devices: tuple[BluetoothDevice, ...]) -> None:
        for device in devices:
            signal = "" if device.rssi is None else f"  ·  {device.rssi} dBm"
            self.bluetooth_device.addItem(f"{device.name}{signal}", device)
        count = len(devices)
        self.bluetooth_scan_status.setText(
            f"Found {count} board{'s' if count != 1 else ''}."
            if count
            else "No Fluid Reality Bluetooth boards were found."
        )

    def _on_bluetooth_scan_failed(self, message: str) -> None:
        self.bluetooth_scan_status.setText(message)

    def _bluetooth_scan_finished(self) -> None:
        self.refresh_bluetooth_button.setEnabled(True)
        self._bluetooth_scan = None

    def _update_network_protocol(self) -> None:
        self.adjustSize()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _attempt_connection(self) -> None:
        self.error_label.hide()
        if self.connection_tabs.currentIndex() == 0:
            endpoint = self.serial_port.currentText().strip()
            if not endpoint:
                self._show_error("Select an available serial port.")
                return
            options: dict[str, Any] = {}
        elif self.connection_tabs.currentIndex() == 1:
            protocol = "tls" if self.network_encryption.isChecked() else "tcp"
            try:
                endpoint = build_network_endpoint(
                    protocol, self.network_host.text(), self.network_port.value()
                )
            except ValueError as exc:
                self._show_error(str(exc))
                return
            token = self.network_token.text().strip()
            options = {"network_token": token} if token else {}
            if protocol == "tls":
                options["tls_verify_certificate"] = False
        else:
            device = self.bluetooth_device.currentData()
            if not isinstance(device, BluetoothDevice):
                self._show_error("Scan for and select a Bluetooth board.")
                return
            endpoint = device.endpoint
            token = self.bluetooth_token.text().strip()
            options = {"pair": self.bluetooth_pair.isChecked()}
            if token:
                options["network_token"] = token

        self.set_connecting(True)
        self.attempt_requested.emit(endpoint, options)

    def set_connecting(self, connecting: bool) -> None:
        self.connection_tabs.setEnabled(not connecting)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(not connecting)
        self.buttons.button(QDialogButtonBox.Cancel).setEnabled(not connecting)
        self.buttons.button(QDialogButtonBox.Ok).setText(
            "Connecting…" if connecting else "OK"
        )

    def connection_succeeded(self) -> None:
        super().accept()

    def connection_failed(self, message: str) -> None:
        self.set_connecting(False)
        self._show_error(message or "Could not connect to the board.")

    def closeEvent(self, event: Any) -> None:
        worker = self._bluetooth_scan
        if worker is not None and worker.isRunning():
            worker.wait(6000)
        super().closeEvent(event)


class BoardWorker(QThread):
    connected_changed = Signal(bool, str)
    capabilities_ready = Signal(dict)
    actuator_count_ready = Signal(int)
    status_ready = Signal(dict)
    diagnosis_ready = Signal(dict)
    diagnosis_progress = Signal(dict)
    diagnosis_finished = Signal(bool, str)
    initialization_progress = Signal(dict)
    fast_init_progress = Signal(dict)
    fast_init_ready = Signal(dict)
    recovery_ready = Signal(dict)
    recovery_progress = Signal(dict)
    health_ready = Signal(int, object)
    square_changed = Signal(bool, list, str)
    square_progress = Signal(dict)
    actuator_tool_finished = Signal(str, bool, str)
    board_config_ready = Signal(dict)
    board_config_saved = Signal(dict)
    board_config_failed = Signal(str)
    bluetooth_config_ready = Signal(dict)
    bluetooth_config_saved = Signal(dict)
    bluetooth_bonds_cleared = Signal(dict)
    bluetooth_config_failed = Signal(str)
    wifi_status_ready = Signal(dict)
    wifi_access_point_ready = Signal(dict)
    wifi_networks_ready = Signal(list)
    wifi_operation_failed = Signal(str)
    network_config_ready = Signal(dict, tuple, bool)
    network_config_saved = Signal(dict, tuple, bool)
    network_config_failed = Signal(str)
    security_config_ready = Signal(str, dict)
    security_config_failed = Signal(str)
    firmware_update_progress = Signal(int, int)
    firmware_update_finished = Signal(str, str)
    firmware_update_failed = Signal(str)
    factory_reset_finished = Signal()
    factory_reset_failed = Signal(str)
    busy_changed = Signal(str)
    message = Signal(str, str)

    def __init__(self, board_class: type[Board] = FluidRealityBoard) -> None:
        super().__init__()
        if not issubclass(board_class, Board):
            raise TypeError("board_class must inherit from Board")
        self._board_class = board_class
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()
        self._board: Board | None = None
        self._square_actuators: list[int] = []
        self._square_phase = "idle"
        self._square_restore_safety: bool | None = None
        self._square_start_time = 0.0
        self._square_baseline_ma = 0.0
        self._square_supply_voltage = 0.0
        self._last_status = 0.0
        self._actuator_health: dict[int, dict[str, Any]] = {}
        self._capabilities: dict[str, str] = {}
        self._endpoint = ""
        self._connection_options: dict[str, Any] = {}
        self._firmware_update_abort = threading.Event()
        self._actuator_tool_abort = threading.Event()

    def enqueue(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def prepare_actuator_tool(self) -> None:
        self._actuator_tool_abort.clear()

    def request_actuator_tool_stop(self) -> None:
        self._actuator_tool_abort.set()

    def _raise_if_actuator_tool_stopped(self) -> None:
        if self._actuator_tool_abort.is_set() or self.isInterruptionRequested():
            raise ActuatorToolStopped("Stopped")

    def run(self) -> None:
        while not self.isInterruptionRequested():
            self._process_pending_commands()
            self._service_square_wave()
            self._poll_status_if_due()
            time.sleep(0.025)

        self._close_board()

    def _process_pending_commands(self) -> None:
        while True:
            try:
                command, args = self._commands.get_nowait()
            except queue.Empty:
                return

            try:
                if command == "connect":
                    options = dict(args[1]) if len(args) > 1 else {}
                    self._connect(str(args[0]), options)
                elif command == "disconnect":
                    self._disconnect()
                elif command == "refresh":
                    self._emit_status()
                elif command == "psc":
                    self._set_power_path(bool(args[0]))
                    self._emit_status()
                elif command == "init":
                    self._initialize_actuator(int(args[0]))
                elif command == "fast_init":
                    self._fast_initialize_actuator(int(args[0]), float(args[1]))
                elif command == "diagnose":
                    self._diagnose_actuator(int(args[0]))
                elif command == "recover":
                    self._recover_actuator(int(args[0]))
                elif command == "detect_group":
                    self._detect_group(int(args[0]))
                elif command == "detect_all":
                    self._detect_all()
                elif command == "square_start":
                    self._start_square_wave(list(args[0]))
                elif command == "square_stop":
                    self._stop_square_wave()
                elif command == "all_off":
                    self._all_off()
                elif command == "read_board_config":
                    self._read_board_config()
                elif command == "write_board_config":
                    self._write_board_config(dict(args[0]))
                elif command == "read_bluetooth_config":
                    self._read_bluetooth_config()
                elif command == "write_bluetooth_config":
                    self._write_bluetooth_config(dict(args[0]))
                elif command == "clear_bluetooth_bonds":
                    self._clear_bluetooth_bonds()
                elif command == "wifi_status":
                    self._emit_wifi_status()
                elif command == "wifi_scan":
                    self._scan_wifi()
                elif command == "wifi_enable":
                    self._set_wifi_enabled(bool(args[0]))
                elif command == "wifi_mode":
                    self._set_wifi_mode(str(args[0]))
                elif command == "wifi_ap_status":
                    self._emit_wifi_access_point_status()
                elif command == "wifi_ap_config":
                    self._configure_wifi_access_point(
                        str(args[0]), str(args[1]), int(args[2])
                    )
                elif command == "wifi_join":
                    self._join_wifi(args[0], str(args[1]))
                elif command == "wifi_disconnect":
                    self._disconnect_wifi()
                elif command == "read_network_config":
                    self._read_network_config()
                elif command == "write_network_config":
                    self._write_network_config(dict(args[0]))
                elif command == "read_security_config":
                    self._read_security_config()
                elif command == "write_security_config":
                    self._write_security_config(dict(args[0]))
                elif command == "tls_enable":
                    self._set_tls_enabled(bool(args[0]))
                elif command == "tls_provision":
                    self._provision_tls(bytes(args[0]), bytes(args[1]), str(args[2]))
                elif command == "firmware_update":
                    self._update_firmware(str(args[0]))
                elif command == "factory_reset":
                    self._factory_reset()
                else:
                    self.message.emit(f"Unknown worker command: {command}", "error")
            except Exception as exc:
                self.busy_changed.emit("")
                error_message = str(exc)
                if command == "connect":
                    options = dict(args[1]) if len(args) > 1 else {}
                    endpoint = str(args[0]) if args else ""
                    error_message = describe_connection_error(endpoint, options, exc)
                elif command == "wifi_join" and getattr(exc, "fields", {}).get("REASON") == "INDEX":
                    error_message = "The Wi-Fi network list expired. Scan again and retry."
                self.message.emit(error_message, "error")
                if command in {"read_board_config", "write_board_config"}:
                    self.board_config_failed.emit(error_message)
                if command in {
                    "read_bluetooth_config",
                    "write_bluetooth_config",
                    "clear_bluetooth_bonds",
                }:
                    self.bluetooth_config_failed.emit(error_message)
                if command.startswith("wifi_"):
                    self.wifi_operation_failed.emit(error_message)
                if command in {"read_network_config", "write_network_config"}:
                    self.network_config_failed.emit(error_message)
                if command in {
                    "read_security_config",
                    "write_security_config",
                    "tls_enable",
                    "tls_provision",
                }:
                    self.security_config_failed.emit(error_message)
                if command == "firmware_update":
                    if self._board is not None:
                        try:
                            self._board.force_text_mode()
                        except Exception:
                            pass
                    self.firmware_update_failed.emit(error_message)
                    if self._board is None:
                        self.connected_changed.emit(False, "Disconnected after firmware update")
                if command == "factory_reset":
                    self.factory_reset_failed.emit(error_message)
                    if self._board is None:
                        self.connected_changed.emit(False, "Disconnected after factory reset")
                if command == "connect":
                    self._close_board()
                    self.connected_changed.emit(False, error_message)
                if command in {"init", "fast_init", "recover"}:
                    self.actuator_tool_finished.emit(command, False, error_message)
                if command == "diagnose":
                    self.diagnosis_finished.emit(False, error_message)
                if command == "square_start":
                    self.square_changed.emit(False, [], "idle")
                if self._board is not None and command == "psc":
                    try:
                        self._emit_status()
                    except Exception:
                        pass

    def _connect(
        self,
        port: str,
        options: dict[str, Any] | None = None,
    ) -> None:
        self._close_board()
        self.busy_changed.emit("Connecting")
        self._board = self._board_class(port, **(options or {}))
        self._endpoint = port
        self._connection_options = dict(options or {})
        self._board.set_debug_out(self._emit_debug)
        # A new TCP/TLS socket always starts at a command boundary. Sending
        # the serial recovery bytes here creates a deliberate BAD_COMMAND;
        # after a Wi-Fi scan that delayed response can arrive after the drain
        # window and make an otherwise healthy network connection fail.
        if not port.lower().startswith(("tcp://", "tls://")):
            self._board.force_text_mode()
        version = self._board.firmware_version()
        try:
            capabilities = self._board.capabilities()
        except Exception:
            capabilities = {}
        self._capabilities = {
            str(key): str(value) for key, value in capabilities.items()
        }
        self.capabilities_ready.emit(capabilities)
        self._board.connect_power()
        status = self._board.status()
        actuator_count = int(status["actuator_count"])
        self._actuator_health.clear()
        self.actuator_count_ready.emit(actuator_count)
        self.connected_changed.emit(True, f"{port} - {version.firmware} {version.version}")
        self.message.emit(f"Connected to {version.firmware} firmware {version.version}", "ok")
        self.busy_changed.emit("")
        self.status_ready.emit(status)
        self._last_status = time.monotonic()

    def request_firmware_update_abort(self) -> None:
        self._firmware_update_abort.set()

    def _update_firmware(self, path: str) -> None:
        board = self._require_board()
        endpoint = self._endpoint
        options = dict(self._connection_options)
        self._firmware_update_abort.clear()
        self.busy_changed.emit("Updating firmware")
        last_progress_emit = 0.0

        def emit_progress(written: int, total: int) -> None:
            nonlocal last_progress_emit
            now = time.monotonic()
            if written == 0 or written >= total or now - last_progress_emit >= 0.1:
                last_progress_emit = now
                self.firmware_update_progress.emit(written, total)

        result = board.update_firmware(
            path,
            progress=emit_progress,
            should_abort=lambda: self._firmware_update_abort.is_set()
            or self.isInterruptionRequested(),
        )
        self.message.emit("Firmware image verified; the board is rebooting.", "ok")
        self._close_board()

        deadline = time.monotonic() + 30.0
        last_error: Exception | None = None
        while time.monotonic() < deadline and not self.isInterruptionRequested():
            time.sleep(1.0)
            try:
                self._connect(endpoint, options)
                version = self._board.firmware_version() if self._board is not None else None
                version_text = version.version if version is not None else "unknown"
                self.firmware_update_finished.emit(result.sha256, version_text)
                self.message.emit(
                    f"Firmware update complete; board reconnected with firmware {version_text}.",
                    "ok",
                )
                return
            except Exception as exc:
                last_error = exc
                self._close_board()
        raise RuntimeError(
            "Firmware was installed, but the board did not reconnect after reboot"
            + (f": {last_error}" if last_error is not None else ".")
        )

    def _factory_reset(self) -> None:
        board = self._require_board()
        endpoint = self._endpoint
        options = dict(self._connection_options)
        if not endpoint or "://" in endpoint:
            raise RuntimeError("Factory reset is available only over USB serial.")

        self.busy_changed.emit("Restoring factory settings")
        board.raw_command("CFG", "FACTORY_RESET")
        self.message.emit("Factory settings restored; the board is rebooting.", "ok")
        self._close_board()

        deadline = time.monotonic() + 30.0
        last_error: Exception | None = None
        while time.monotonic() < deadline and not self.isInterruptionRequested():
            time.sleep(1.0)
            try:
                self._connect(endpoint, options)
                self.factory_reset_finished.emit()
                self.message.emit(
                    "Factory reset complete; reconnected over USB serial.", "ok"
                )
                return
            except Exception as exc:
                last_error = exc
                self._close_board()
        raise RuntimeError(
            "Factory reset completed, but the board did not reconnect after reboot"
            + (f": {last_error}" if last_error is not None else ".")
        )

    def _emit_debug(self, line: str) -> None:
        self.message.emit(line, "debug")

    def _disconnect(self) -> None:
        self._stop_square_wave()
        self._close_board()
        self.connected_changed.emit(False, "Disconnected")
        self.message.emit("Disconnected", "info")

    def _close_board(self) -> None:
        if self._board is None:
            return
        try:
            for actuator in list(self._square_actuators):
                self._board._set_initialization_output(actuator, 0, "off")
        except Exception:
            pass
        self._restore_square_safety()
        try:
            self._board.close()
        except Exception:
            pass
        self._board = None
        self._square_actuators = []
        self.square_changed.emit(False, [], "idle")

    def _require_board(self) -> Board:
        if self._board is None:
            raise RuntimeError("Connect to a Fluid Reality board first.")
        return self._board

    def _emit_status(self) -> None:
        board = self._require_board()
        status = board.status()
        self.status_ready.emit(status)
        self._last_status = time.monotonic()

    @staticmethod
    def _config_values(config: Any) -> dict[str, Any]:
        values = {
            "safe": bool(config.safe),
            "debug": bool(config.debug),
        }
        if hasattr(config, "vt_limit_vs"):
            values.update(
                vt_limit_vs=int(config.vt_limit_vs),
                vt_limit_modified=bool(config.vt_limit_modified),
            )
        else:
            values.update(
                max_active_ms=int(config.max_active_ms),
                discharge_ms=int(config.discharge_ms),
            )
        return values

    def _read_board_config(self) -> None:
        board = self._require_board()
        config = self._config_values(board.read_config())
        if "DET" in self._capabilities and self._capabilities["DET"] != "0":
            config.update(
                detection_current_limit_ma=board.detection_current_limit_ma(),
                dt0_error_threshold_ma=board.dt0_error_threshold_ma(),
                dt1_error_threshold_ma=board.dt1_error_threshold_ma(),
            )
        self.board_config_ready.emit(config)

    def _write_board_config(self, config: dict[str, Any]) -> None:
        board = self._require_board()
        if self._capabilities.get("VT") == "1":
            vt_limit = getattr(board, "vt_limit_vs", None)
            if vt_limit is None:
                raise RuntimeError("The connected board does not expose VT configuration.")
            vt_limit(int(config["vt_limit_vs"]))
        else:
            board.max_active_time_ms(int(config["max_active_ms"]))
            board.discharge_time_ms(int(config["discharge_ms"]))
        board.safety(bool(config["safe"]))
        board.firmware_debug(bool(config["debug"]))
        detection_supported = (
            "DET" in self._capabilities and self._capabilities["DET"] != "0"
        )
        if detection_supported:
            target_detection = float(config["detection_current_limit_ma"])
            target_dt0 = float(config["dt0_error_threshold_ma"])
            target_dt1 = float(config["dt1_error_threshold_ma"])
            current_detection = board.detection_current_limit_ma()
            if target_detection < current_detection:
                board.detection_current_limit_ma(target_detection)
            board.dt0_error_threshold_ma(target_dt0)
            board.dt1_error_threshold_ma(target_dt1)
            if target_detection >= current_detection:
                board.detection_current_limit_ma(target_detection)
        saved = self._config_values(board.read_config())
        if detection_supported:
            saved.update(
                detection_current_limit_ma=board.detection_current_limit_ma(),
                dt0_error_threshold_ma=board.dt0_error_threshold_ma(),
                dt1_error_threshold_ma=board.dt1_error_threshold_ma(),
            )
        self.board_config_saved.emit(saved)
        self.message.emit("Board settings saved", "ok")
        self._emit_status()

    def _require_bluetooth_capability(self) -> Board:
        board = self._require_board()
        if self._capabilities.get("BLT") in {None, "0"}:
            raise RuntimeError(
                "This board does not report Bluetooth configuration support."
            )
        return board

    def _bluetooth_status(self) -> dict[str, str]:
        board = self._require_bluetooth_capability()
        return dict(board.raw_command("BLT", "STATUS")[0].fields)

    def _read_bluetooth_config(self) -> None:
        self.bluetooth_config_ready.emit(self._bluetooth_status())

    def _write_bluetooth_config(self, values: dict[str, Any]) -> None:
        board = self._require_bluetooth_capability()
        if values.get("name_dirty"):
            board.raw_command("BLT", "NAME", str(values["name"]))
        if values.get("security_dirty"):
            board.raw_command(
                "BLT", "SEC", "ON" if values.get("secure") else "OFF"
            )
        if values.get("enabled_dirty"):
            board.raw_command(
                "BLT", "ON" if values.get("enabled") else "OFF"
            )
        status = self._bluetooth_status()
        self.bluetooth_config_saved.emit(status)
        self.message.emit("Bluetooth settings saved", "ok")

    def _clear_bluetooth_bonds(self) -> None:
        board = self._require_bluetooth_capability()
        board.raw_command("BLT", "BONDS", "CLEAR")
        self.bluetooth_bonds_cleared.emit(self._bluetooth_status())
        self.message.emit("Bluetooth paired devices forgotten", "ok")

    def _require_wifi_capability(self) -> Board:
        board = self._require_board()
        if self._capabilities.get("WIFI") in {None, "0"}:
            raise RuntimeError("This board does not report Wi-Fi configuration support.")
        return board

    def _set_power_path(self, enabled: bool) -> None:
        """Operate the PSU and the optional PSC as one safe control."""

        board = self._require_board()
        if board.power_connection_supported is None:
            board.connect_power()
        psc_available = board.power_connection_supported is not False
        if enabled:
            board.power_supply(True)
            try:
                if psc_available:
                    board.connect_power(True)
            except Exception:
                board.power_supply(False)
                raise
        else:
            try:
                if psc_available:
                    board.connect_power(False)
            finally:
                board.power_supply(False)
        self.message.emit(f"Power {'ON' if enabled else 'OFF'}", "ok")

    def _wifi_command(self, operation: str, *params: object) -> dict[str, str]:
        response = self._require_wifi_capability().raw_command(
            "NET", operation, *params
        )[0]
        return dict(response.fields)

    def _network_command(self, operation: str, *params: object) -> dict[str, str]:
        board = self._require_board()
        if self._capabilities.get("NET") in {None, "0"}:
            raise RuntimeError("This board does not report network configuration support.")
        return dict(board.raw_command("NET", operation, *params)[0].fields)

    def _emit_wifi_status(self) -> None:
        self.wifi_status_ready.emit(self._wifi_status_fields())

    def _wifi_status_fields(self) -> dict[str, str]:
        fields = self._wifi_command("STATUS")
        try:
            diagnostics = self._wifi_command("DIAG")
            fields["CURRENT_BSSID"] = diagnostics.get("CURRENT_BSSID", "NONE")
        except Exception:
            fields["CURRENT_BSSID"] = "NONE"
        return fields

    def _set_wifi_enabled(self, enabled: bool) -> None:
        self._wifi_command("WIFI", "ON" if enabled else "OFF")
        self.message.emit(f"Wi-Fi {'enabled' if enabled else 'disabled'}", "ok")
        fields = self._wifi_status_fields()
        self.wifi_status_ready.emit(fields)
        if enabled and fields.get("STATE", "").upper() != "CONNECTED":
            encoded_ssid = fields.get("SSID64", "")
            try:
                ssid = base64.b64decode(encoded_ssid, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                ssid = ""
            if ssid:
                self._wait_for_wifi(ssid)

    def _set_wifi_mode(self, mode: str) -> None:
        normalized = "ACCESS_POINT" if mode.upper() in {"AP", "ACCESS_POINT"} else "CLIENT"
        self._wifi_command("MODE", normalized)
        self.message.emit(
            "Wi-Fi access point mode selected"
            if normalized == "ACCESS_POINT"
            else "Wi-Fi client mode selected",
            "ok",
        )
        self._emit_wifi_status()
        self._emit_wifi_access_point_status()

    def _emit_wifi_access_point_status(self) -> None:
        if self._capabilities.get("AP") in {None, "0"}:
            return
        self.wifi_access_point_ready.emit(self._wifi_command("AP", "STATUS"))

    def _configure_wifi_access_point(
        self, ssid: str, password: str, channel: int
    ) -> None:
        encoded_ssid = base64.b64encode(ssid.encode("utf-8")).decode("ascii")
        if password:
            encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
            fields = self._wifi_command(
                "AP", "CONFIG", encoded_ssid, "PSK", encoded_password, channel
            )
        else:
            fields = self._wifi_command("AP", "CONFIG", encoded_ssid, "OPEN", channel)
        self.wifi_access_point_ready.emit(fields)
        self._emit_wifi_status()
        self.message.emit("Access point settings saved", "ok")

    def _scan_wifi(self) -> None:
        networks = self._collect_wifi_networks()
        self.wifi_networks_ready.emit(networks)
        self.message.emit(
            f"Found {len(networks)} Wi-Fi network{'s' if len(networks) != 1 else ''}",
            "ok" if networks else "info",
        )

    def _wait_for_tcp_after_wifi_scan(self) -> None:
        """Wait briefly for the station radio to resume carrying IP traffic."""

        try:
            fields = self._wifi_status_fields()
            if (
                fields.get("WIFI_MODE", "CLIENT").upper() != "CLIENT"
                or fields.get("STATE", "").upper() != "CONNECTED"
                or fields.get("TCP", "").upper() != "ON"
            ):
                return
            host = fields.get("IP", "").strip()
            port = int(fields.get("PORT", "0"))
            if not host or host == "0.0.0.0" or not 1 <= port <= 65535:
                return
        except Exception:
            return

        deadline = time.monotonic() + NETWORK_SCAN_TCP_READY_WAIT_S
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.75) as probe:
                    # Complete one protocol exchange so the single-client
                    # listener accepts and then promptly releases this probe.
                    # With TLS enabled, the raw request is simply rejected;
                    # the successful TCP handshake is all we need here.
                    probe.settimeout(0.75)
                    probe.sendall(b"VER\n")
                    try:
                        probe.recv(256)
                    except OSError:
                        pass
                    try:
                        probe.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    time.sleep(0.25)
                    return
            except OSError:
                time.sleep(0.15)

    def _disconnect_wifi(self) -> None:
        """Disconnect Wi-Fi and erase its credentials without closing the board."""

        board = self._require_wifi_capability()
        # NET FORGET already disconnects the station and erases its SSID and
        # password. Do not send NET DISCONNECT first: that command also turns
        # the persistent Wi-Fi-enabled setting off.
        self._wifi_command("FORGET")
        # Keep the existing Board and its USB/BLE transport intact. Only the
        # firmware's Wi-Fi station and saved credentials are changed here.
        if self._board is not board:
            raise RuntimeError("The dashboard board connection changed unexpectedly")
        self._emit_wifi_status()
        self.message.emit(
            "Wi-Fi disconnected and saved network credentials erased; "
            "the dashboard board connection remains active.",
            "info",
        )

    def _network_config_snapshot(self) -> tuple[dict[str, str], tuple[str, ...], bool]:
        if self._capabilities.get("NET") in {None, "0"}:
            raise RuntimeError("This board does not report network configuration support.")
        status = self._network_command("STATUS")
        scoped = self._capabilities.get("NET_IF") not in {None, "0"}
        interfaces: tuple[str, ...] = ()
        if scoped:
            fields = self._network_command("IF", "LIST")
            encoded = fields.get("IFACES", fields.get("INTERFACES", ""))
            interfaces = tuple(
                item.strip().upper()
                for item in encoded.replace(",", "|").split("|")
                if item.strip()
            )
        if not interfaces:
            interfaces = tuple(
                name
                for name in ("WIFI", "ETH")
                if self._capabilities.get(name) not in {None, "0"}
            )
        return status, interfaces, scoped

    def _read_network_config(self) -> None:
        status, interfaces, scoped = self._network_config_snapshot()
        self.network_config_ready.emit(status, interfaces, scoped)

    def _write_network_config(self, settings: dict[str, Any]) -> None:
        interface = str(settings.get("interface") or "").upper()
        scoped = bool(settings.get("scoped")) and bool(interface)
        prefix: tuple[object, ...] = ("IF", interface) if scoped else ()
        if settings.get("access_point_mode", False):
            self._network_command(
                "AP", "IP", settings["address"], settings["subnet"]
            )
        elif settings.get("apply_ip", True):
            if settings["dhcp"]:
                self._network_command(*prefix, "IP", "DHCP")
            else:
                self._network_command(
                    *prefix,
                    "IP",
                    "STATIC",
                    settings["address"],
                    settings["subnet"],
                    settings["gateway"],
                    settings["dns1"],
                    settings["dns2"],
                )
        self._network_command("HOST", settings["hostname"])
        if scoped:
            self._network_command("TCP", "BIND", settings.get("tcp_bind") or "ANY")
        self._network_command("TCP", "ON" if settings["tcp_enabled"] else "OFF")
        self._network_command("TCP", "PORT", int(settings["tcp_port"]))
        status, interfaces, scoped = self._network_config_snapshot()
        if (
            status.get("WIFI", "").upper() == "ON"
            and bool(status.get("SSID64"))
            and not self._network_status_is_connected(status)
        ):
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                time.sleep(0.4)
                status, interfaces, scoped = self._network_config_snapshot()
                if self._network_status_is_connected(status):
                    break
                if status.get("STATE", "").upper() in {
                    "AUTH_FAILED",
                    "NO_SSID",
                    "CONNECTION_LOST",
                }:
                    break
        self.network_config_saved.emit(status, interfaces, scoped)
        connected = self._network_status_is_connected(status)
        self.message.emit(
            "Network settings applied — "
            f"{status.get('STATE', 'UNKNOWN').replace('_', ' ').title()}, "
            f"IP {status.get('IP', '0.0.0.0')}",
            "ok" if connected else "info",
        )

    @staticmethod
    def _network_status_is_connected(status: dict[str, str]) -> bool:
        address = status.get("IP", "").strip()
        state = status.get("STATE", "").upper()
        access_point_active = (
            status.get("WIFI_MODE", "").upper() == "ACCESS_POINT"
            and state == "ACTIVE"
        )
        return (
            (state == "CONNECTED" or access_point_active)
            and address not in {"", "0.0.0.0"}
        )

    def _read_security_config(self) -> None:
        token = ""
        tls = {"SUPPORTED": "NO"}
        if self._capabilities.get("AUTH") not in {None, "0"}:
            auth = self._network_command("KEY")
            token = auth.get("TOKEN", "")
        else:
            auth = {"STATE": "OFF"}
        if self._capabilities.get("TLS") not in {None, "0"}:
            tls = self._network_command("TLS")
        tls["AUTH_STATE"] = auth.get("STATE", "ON" if token else "OFF")
        self.security_config_ready.emit(token, tls)

    def _write_security_token(self, token: str) -> None:
        fields = self._network_command("KEY", "SET", token)
        saved_token = fields.get("TOKEN", token)
        tls = (
            self._network_command("TLS")
            if self._capabilities.get("TLS") not in {None, "0"}
            else {"SUPPORTED": "NO"}
        )
        self.security_config_ready.emit(saved_token, tls)
        self.message.emit("Access token saved", "ok")

    def _write_security_config(self, settings: dict[str, Any]) -> None:
        token = str(settings.get("token", ""))
        if settings.get("token_dirty"):
            fields = self._network_command("KEY", "SET", token)
            token = fields.get("TOKEN", token)
        elif self._capabilities.get("AUTH") not in {None, "0"}:
            token = self._network_command("KEY").get("TOKEN", token)

        auth_enabled = bool(settings.get("auth_enabled", True))
        if (
            self._capabilities.get("AUTH") not in {None, "0"}
            and settings.get("auth_dirty")
        ):
            auth = self._network_command(
                "KEY", "ON" if auth_enabled else "OFF"
            )
        else:
            auth = {"STATE": "ON" if auth_enabled else "OFF"}

        tls_supported = self._capabilities.get("TLS") not in {None, "0"}
        tls_enabled = bool(settings.get("tls_enabled"))
        if settings.get("credentials_dirty") and tls_enabled and tls_supported:
            key_password = str(settings.get("key_password", ""))
            encoded_password = (
                "CLEAR"
                if not key_password
                else base64.b64encode(key_password.encode("utf-8")).decode("ascii")
            )
            self._network_secret_command("TLS", "PASS", encoded_password)
            self._upload_tls_pem("CERT", bytes(settings["certificate"]))
            self._upload_tls_pem("KEY", bytes(settings["private_key"]))

        if tls_supported and not tls_enabled:
            tls = self._network_command("TLS", "CLEAR")
        elif tls_supported and settings.get("tls_dirty"):
            tls = self._network_command(
                "TLS", "ON"
            )
        elif tls_supported:
            tls = self._network_command("TLS")
        else:
            tls = {"SUPPORTED": "NO"}

        tls["AUTH_STATE"] = auth.get(
            "STATE", "ON" if auth_enabled else "OFF"
        )

        self.security_config_ready.emit(token, tls)
        self.message.emit(
            "Security settings applied"
            if tls_enabled or not tls_supported
            else "Security settings applied; TLS credentials erased",
            "ok",
        )

    def _set_tls_enabled(self, enabled: bool) -> None:
        tls = self._network_command("TLS", "ON" if enabled else "OFF")
        token = (
            self._network_command("KEY").get("TOKEN", "")
            if self._capabilities.get("AUTH") not in {None, "0"}
            else ""
        )
        self.security_config_ready.emit(token, tls)
        self.message.emit("TLS enabled" if enabled else "TLS disabled", "ok")

    def _network_secret_command(self, *params: object) -> dict[str, str]:
        board = self._require_board()
        board.transport.write_line("NET " + " ".join(str(param) for param in params))
        return dict(board.protocol.read_result(ok_lines=1)[0].fields)

    def _upload_tls_pem(self, kind: str, data: bytes) -> None:
        if not data or len(data) > 8192 or b"\0" in data:
            raise ValueError("TLS PEM data must contain 1-8192 bytes and no NUL bytes")
        self._network_command("TLS", kind, "BEGIN", len(data))
        for offset in range(0, len(data), 48):
            encoded = base64.b64encode(data[offset : offset + 48]).decode("ascii")
            if kind == "KEY":
                self._network_secret_command("TLS", kind, "DATA", encoded)
            else:
                self._network_command("TLS", kind, "DATA", encoded)
        self._network_command("TLS", kind, "END")

    def _provision_tls(
        self, certificate: bytes, private_key: bytes, key_password: str
    ) -> None:
        encoded_password = (
            "CLEAR"
            if not key_password
            else base64.b64encode(key_password.encode("utf-8")).decode("ascii")
        )
        self._network_secret_command("TLS", "PASS", encoded_password)
        self._upload_tls_pem("CERT", certificate)
        self._upload_tls_pem("KEY", private_key)
        tls = self._network_command("TLS")
        token = (
            self._network_command("KEY").get("TOKEN", "")
            if self._capabilities.get("AUTH") not in {None, "0"}
            else ""
        )
        self.security_config_ready.emit(token, tls)
        self.message.emit("TLS credentials installed and saved permanently", "ok")

    def _clear_tls(self) -> None:
        tls = self._network_command("TLS", "CLEAR")
        token = (
            self._network_command("KEY").get("TOKEN", "")
            if self._capabilities.get("AUTH") not in {None, "0"}
            else ""
        )
        self.security_config_ready.emit(token, tls)
        self.message.emit("TLS credentials erased", "ok")

    def _collect_wifi_networks(self) -> list[WifiNetwork]:
        self._require_wifi_capability()
        self._wifi_command("SCAN")
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                summary = self._wifi_command("LIST")
                if "COUNT" not in summary:
                    time.sleep(0.25)
                    continue
                networks: list[WifiNetwork] = []
                for index in range(int(summary["COUNT"])):
                    fields = self._wifi_command("LIST", index)
                    encoded_ssid = fields.get("SSID64", "")
                    ssid = base64.b64decode(encoded_ssid, validate=True).decode(
                        "utf-8", errors="replace"
                    )
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
            raise TimeoutError("Wi-Fi scan timed out")
        finally:
            # NET SCAN temporarily pauses the board's TCP listener. Release
            # the scan immediately after copying its results so the board can
            # reconnect and accept TCP clients without the legacy 30 s wait.
            self._wifi_command("LIST", "DONE")
            self._wait_for_tcp_after_wifi_scan()

    def _join_wifi(self, network: WifiNetwork, password: str) -> None:
        try:
            self._send_wifi_join(network, password)
        except FirmwareError as exc:
            if exc.fields.get("REASON") != "INDEX":
                raise
            self.message.emit(
                "The Wi-Fi scan expired; rescanning and locating the same access point.",
                "info",
            )
            networks = self._collect_wifi_networks()
            self.wifi_networks_ready.emit(networks)
            refreshed = next(
                (
                    candidate
                    for candidate in networks
                    if network.bssid
                    and candidate.bssid.upper() == network.bssid.upper()
                ),
                None,
            )
            if refreshed is None:
                refreshed = next(
                    (
                        candidate
                        for candidate in networks
                        if candidate.ssid == network.ssid
                        and candidate.security == network.security
                    ),
                    None,
                )
            if refreshed is None:
                raise RuntimeError(
                    f"{network.ssid or 'The selected access point'} was not found during the rescan."
                ) from exc
            self._send_wifi_join(refreshed, password)
        self.message.emit(f"Connecting to {network.ssid}", "info")
        self._wait_for_wifi(network.ssid)

    def _send_wifi_join(self, network: WifiNetwork, password: str) -> None:
        board = self._require_wifi_capability()
        params = ["JOIN", str(network.index)]
        if network.security.upper() == "OPEN":
            params.append("OPEN")
        else:
            params.extend(
                ("PSK", base64.b64encode(password.encode("utf-8")).decode("ascii"))
            )
        board.transport.write_line("NET " + " ".join(params))
        board.protocol.read_result(ok_lines=1)

    def _wait_for_wifi(self, ssid: str, timeout_s: float = 35.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            fields = self._wifi_status_fields()
            self.wifi_status_ready.emit(fields)
            state = fields.get("STATE", "").upper()
            if state == "CONNECTED":
                self.message.emit(
                    f"Connected to {ssid} — IP {fields.get('IP', '0.0.0.0')}", "ok"
                )
                return
            if fields.get("FAIL", "NONE").upper() not in {"", "NONE"}:
                raise RuntimeError(
                    f"Wi-Fi connection failed: {fields['FAIL'].replace('_', ' ').title()}"
                )
            time.sleep(0.5)
        raise TimeoutError(f"Could not connect to {ssid}. Check the password and signal strength.")

    def _poll_status_if_due(self) -> None:
        if self._board is None:
            return
        if time.monotonic() - self._last_status < 0.8:
            return
        try:
            self._emit_status()
        except Exception as exc:
            self.message.emit(f"Status refresh failed: {exc}", "error")
            self._last_status = time.monotonic()

    def _initialize_actuator(self, actuator: int) -> None:
        self._ensure_actuator_recoverable(actuator)
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
        self.busy_changed.emit(f"Initializing actuator {actuator}")
        self.message.emit(
            f"Initializing actuator {actuator}: measure baseline current, then use "
            "a 1 Hz bipolar drive at +/-25 V, +/-50 V, +/-100 V, +/-200 V while "
            "measuring after every voltage change and plotting only positive-output "
            "current deltas, then diagnose.",
            "warn",
        )
        def publish_progress(result: dict[str, object]) -> None:
            self._raise_if_actuator_tool_stopped()
            self.initialization_progress.emit(result)

        try:
            self._raise_if_actuator_tool_stopped()
            state = board.initialize(
                actuator,
                progress_callback=publish_progress,
            )
        except ActuatorToolStopped:
            self.message.emit(f"Initialization stopped for actuator {actuator}", "warn")
            self.busy_changed.emit("")
            self.actuator_tool_finished.emit("init", False, "Stopped")
            return
        detection = board.last_detection(actuator)
        if detection is None:
            raise RuntimeError(
                f"Initialization completed with state {state.value}, but no diagnosis was recorded."
            )
        self.message.emit(f"Initialization drive complete for actuator {actuator}; diagnosing.", "info")
        health = self._health_from_detection(detection)
        self._actuator_health[actuator] = health
        self.health_ready.emit(actuator // 8, {actuator: health})
        self.diagnosis_ready.emit(
            {
                "actuator": detection.actuator,
                "baseline_ma": detection.baseline_ma,
                "forward_ma": detection.forward_ma,
                "discharge_ma": detection.discharge_ma,
            }
        )
        self.message.emit(f"Actuator {actuator} initialized", "ok")
        self.message.emit(
            f"Actuator {actuator} classified as {health['state']} after initialization.",
            "ok" if health["state"] == "idle" else "warn",
        )
        self.busy_changed.emit("")
        self.actuator_tool_finished.emit("init", True, "Completed")
        self._emit_status()

    def _fast_initialize_actuator(self, actuator: int, target_delta_ma: float) -> None:
        self._ensure_actuator_recoverable(actuator)
        if not 0 < target_delta_ma < Board.error_delta_ma:
            raise ValueError(
                f"Fast Init target must be greater than 0 and below "
                f"{Board.error_delta_ma:.1f} mA."
            )
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()

        max_duration_s = 60.0
        supply_voltage = board.voltage()
        if supply_voltage <= 0:
            raise RuntimeError("Cannot fast initialize: measured PSU voltage is 0 V.")

        self.busy_changed.emit(f"Fast initializing actuator {actuator}")
        self.message.emit(
            f"Fast Init actuator {actuator}: target {target_delta_ma:.2f} mA, "
            "1 Hz bipolar manual drive, adaptive voltage control.",
            "warn",
        )

        previous_safety = board.safety()
        target_voltage = float(supply_voltage)
        start = time.monotonic()
        last_result: dict[str, Any] | None = None
        success = False
        cancelled = False
        status_text = "failed"
        measurement_ms = round(Board.initialization_phase_interval_s * 1000)

        try:
            if previous_safety:
                board.safety(False)
            self._raise_if_actuator_tool_stopped()

            # Establish the zero-output reference once. Reusing it lets Fast Init
            # alternate directly between positive and negative drive instead of
            # inserting a 0 V stop before every cycle.
            baseline_ma = board._initialization_output_current(
                actuator, 0, "off", measurement_ms
            )
            self._raise_if_actuator_tool_stopped()
            baseline_elapsed_s = min(time.monotonic() - start, max_duration_s)
            self.fast_init_progress.emit(
                {
                    "actuator": actuator,
                    "elapsed_s": baseline_elapsed_s,
                    "duration_s": max_duration_s,
                    "target_delta_ma": target_delta_ma,
                    "target_voltage": target_voltage,
                    "next_voltage": target_voltage,
                    "sent_voltage": 0.0,
                    "phase": "baseline",
                    "status": "measuring",
                }
            )

            while True:
                self._raise_if_actuator_tool_stopped()
                elapsed_s = time.monotonic() - start
                if elapsed_s > max_duration_s:
                    status_text = "failed"
                    break

                drive_voltage = target_voltage
                output_value = self._voltage_to_output_allow_zero(drive_voltage, supply_voltage)
                forward_ma = board._initialization_output_current(
                    actuator, output_value, "positive", measurement_ms
                )
                self._raise_if_actuator_tool_stopped()
                positive_elapsed_s = min(time.monotonic() - start, max_duration_s)
                delta_ma = abs(forward_ma - baseline_ma)
                reverse_ma = board._initialization_output_current(
                    actuator, output_value, "negative", measurement_ms
                )
                self._raise_if_actuator_tool_stopped()
                negative_elapsed_s = min(time.monotonic() - start, max_duration_s)

                error_ma = abs(delta_ma - target_delta_ma)
                step_v = self._fast_init_step_v(error_ma)
                at_max_voltage = output_value >= Board.max_output
                if at_max_voltage and delta_ma <= target_delta_ma:
                    success = True
                    status_text = "success"
                elif delta_ma > target_delta_ma:
                    target_voltage = max(0.0, target_voltage - step_v)
                    status_text = "reducing"
                elif delta_ma < target_delta_ma:
                    target_voltage = min(float(supply_voltage), target_voltage + step_v)
                    status_text = "raising"
                else:
                    status_text = "holding"

                positive_result = {
                    "actuator": actuator,
                    "elapsed_s": positive_elapsed_s,
                    "duration_s": max_duration_s,
                    "target_delta_ma": target_delta_ma,
                    "target_voltage": drive_voltage,
                    "next_voltage": target_voltage,
                    "sent_voltage": drive_voltage,
                    "phase": "positive",
                    "supply_voltage": supply_voltage,
                    "baseline_ma": baseline_ma,
                    "forward_ma": forward_ma,
                    "reverse_ma": reverse_ma,
                    "delta_ma": delta_ma,
                    "error_ma": error_ma,
                    "step_v": step_v,
                    "status": status_text,
                }
                negative_result = {
                    key: value
                    for key, value in positive_result.items()
                    if key != "delta_ma"
                }
                negative_result.update(
                    {
                        "elapsed_s": negative_elapsed_s,
                        "sent_voltage": -drive_voltage,
                        "phase": "negative",
                    }
                )
                self.fast_init_progress.emit(positive_result)
                self.fast_init_progress.emit(negative_result)
                last_result = dict(positive_result)
                last_result["elapsed_s"] = negative_elapsed_s
                self.message.emit(
                    "Fast Init {actuator}: {elapsed_s:.0f}/{duration_s:.0f}s, "
                    "delta {delta_ma:.2f} mA, target {target_delta_ma:.2f} mA, "
                    "drive {target_voltage:.0f} V, {status}.".format(
                        **last_result
                    ),
                    "ok" if success else "info",
                )
                if success:
                    break
        except ActuatorToolStopped:
            cancelled = True
            status_text = "stopped"
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(previous_safety)

        if cancelled:
            self.message.emit(f"Fast Init stopped for actuator {actuator}", "warn")
            self.busy_changed.emit("")
            self.actuator_tool_finished.emit("fast_init", False, "Stopped")
            return

        detection = None
        try:
            diagnosis = board.diagnose_actuator(actuator)
            detection = board.classify_diagnosis(diagnosis)
            health = self._health_from_detection(detection)
            self._actuator_health[actuator] = health
            self.health_ready.emit(actuator // 8, {actuator: health})
            self.diagnosis_ready.emit(
                {
                    "actuator": diagnosis.actuator,
                    "baseline_ma": diagnosis.baseline_ma,
                    "forward_ma": diagnosis.forward_ma,
                    "discharge_ma": diagnosis.discharge_ma,
                }
            )
        except Exception as exc:
            self.message.emit(f"Fast Init post-diagnosis failed: {exc}", "error")

        if last_result is None:
            last_result = {
                "actuator": actuator,
                "elapsed_s": max_duration_s,
                "duration_s": max_duration_s,
                "target_delta_ma": target_delta_ma,
                "target_voltage": target_voltage,
                "next_voltage": target_voltage,
                "supply_voltage": supply_voltage,
                "baseline_ma": 0.0,
                "forward_ma": 0.0,
                "reverse_ma": 0.0,
                "delta_ma": 0.0,
                "error_ma": 0.0,
                "step_v": 0.0,
                "status": status_text,
            }
        last_result = dict(last_result)
        last_result["success"] = success
        last_result["final_state"] = (
            str(detection.state.value) if detection is not None else "Unknown"
        )
        self.fast_init_ready.emit(last_result)
        self.message.emit(
            "Fast Init {actuator} {outcome}: final delta {delta_ma:.2f} mA, "
            "drive {target_voltage:.0f} V, SDK state {final_state}.".format(
                outcome="succeeded" if success else "failed",
                **last_result,
            ),
            "ok" if success else "error",
        )
        self.busy_changed.emit("")
        self.actuator_tool_finished.emit("fast_init", True, "Completed")
        self._emit_status()

    def _diagnose_actuator(self, actuator: int) -> None:
        self._ensure_actuator_diagnosable(actuator)
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
        supply_voltage = float(board.voltage())
        if supply_voltage <= 0:
            raise RuntimeError("Cannot diagnose: measured PSU voltage is 0 V.")
        self.busy_changed.emit(f"Diagnosing actuator {actuator}")
        self.message.emit(
            f"Diagnosing actuator {actuator}: three full-voltage warmup cycles, "
            "then a 0-200 V current sweep.",
            "info",
        )

        sweep_voltages = tuple(float(value) for value in range(0, 201, 10))
        warmup_steps = 6
        total_steps = warmup_steps + len(sweep_voltages)
        completed_steps = 0
        previous_safety = board.safety()
        samples: list[tuple[float, float]] = []
        try:
            if previous_safety:
                board.safety(False)
            for cycle in range(1, 4):
                for phase, voltage in (
                    ("positive", supply_voltage),
                    ("negative", -supply_voltage),
                ):
                    self._raise_if_actuator_tool_stopped()
                    self._timed_output_current(
                        actuator, Board.max_output, phase, 1000
                    )
                    self._raise_if_actuator_tool_stopped()
                    completed_steps += 1
                    self.diagnosis_progress.emit(
                        {
                            "actuator": actuator,
                            "phase": "warmup",
                            "warmup_cycle": cycle,
                            "sent_voltage": voltage,
                            "completed_steps": completed_steps,
                            "total_steps": total_steps,
                        }
                    )

            for voltage in sweep_voltages:
                self._raise_if_actuator_tool_stopped()
                output = self._voltage_to_output_allow_zero(
                    voltage, supply_voltage
                )
                phase = "off" if voltage == 0 else "positive"
                current_ma = abs(
                    self._timed_output_current(
                        actuator, output, phase, 250
                    )
                )
                self._raise_if_actuator_tool_stopped()
                samples.append((voltage, current_ma))
                completed_steps += 1
                self.diagnosis_progress.emit(
                    {
                        "actuator": actuator,
                        "phase": "testing",
                        "voltage_v": voltage,
                        "current_ma": current_ma,
                        "completed_steps": completed_steps,
                        "total_steps": total_steps,
                    }
                )
        finally:
            try:
                board._set_initialization_output(actuator, 0, "off")
            finally:
                board.safety(previous_safety)

        baseline_ma = samples[0][1]
        forward_ma = samples[-1][1]
        result = Diagnosis(
            actuator=actuator,
            baseline_ma=baseline_ma,
            forward_ma=forward_ma,
            discharge_ma=baseline_ma,
        )
        detection = board.classify_diagnosis(result)
        health = self._health_from_detection(detection)
        self._actuator_health[actuator] = health
        self.health_ready.emit(actuator // 8, {actuator: health})
        self.diagnosis_ready.emit(
            {
                "actuator": result.actuator,
                "baseline_ma": result.baseline_ma,
                "forward_ma": result.forward_ma,
                "discharge_ma": result.discharge_ma,
                "sample_count": len(samples),
                "max_voltage_v": sweep_voltages[-1],
                "health_summary": DiagnosisVoltageCurrentPlot.health_summary(samples),
            }
        )
        self.message.emit(f"Diagnosis complete for actuator {actuator}", "ok")
        self.message.emit(
            f"Actuator {actuator} classified as {health['state']} after diagnosis.",
            "ok" if health["state"] == "idle" else "warn",
        )
        self.busy_changed.emit("")
        self.diagnosis_finished.emit(True, "Completed")
        self._emit_status()

    def _recover_actuator(self, actuator: int) -> None:
        self._ensure_actuator_recoverable(actuator)
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
        supply_voltage = float(board.voltage())
        if supply_voltage <= 0:
            raise RuntimeError("Cannot recover: measured PSU voltage is 0 V.")
        stages = tuple(float(value) for value in Board.initialization_stages_v)
        measurement_ms = round(Board.initialization_phase_interval_s * 1000)
        qualification_duration_s = 3.0
        cycle_duration_s = measurement_ms * 2 / 1000.0
        self.busy_changed.emit(f"Recovering actuator {actuator}")
        self.message.emit(
            f"Automatically recovering actuator {actuator} at "
            + ", ".join(f"±{voltage:.0f} V" for voltage in stages)
            + "; each stage continues until current remains at or below 90% of "
            "its error threshold for three consecutive seconds.",
            "warn",
        )
        previous_safety = board.safety()
        start = time.monotonic()
        baseline_ma = 0.0
        final_delta_ma = 0.0
        completed_stages = 0
        try:
            if previous_safety:
                board.safety(False)
            self._raise_if_actuator_tool_stopped()
            baseline_ma = self._timed_output_current(
                actuator, 0, "off", measurement_ms
            )
            self.recovery_progress.emit(
                {
                    "actuator": actuator,
                    "elapsed_s": time.monotonic() - start,
                    "stage_index": 0,
                    "stage_count": len(stages),
                    "stage_voltage": 0.0,
                    "sent_voltage": 0.0,
                    "phase": "baseline",
                    "target_delta_ma": 0.0,
                }
            )

            for stage_index, target_voltage in enumerate(stages, start=1):
                output_value = self._voltage_to_output_allow_zero(
                    target_voltage, supply_voltage
                )
                target_delta_ma = (
                    DiagnosisVoltageCurrentPlot.upper_reference_current(target_voltage)
                    * 0.9
                )
                stage_complete = False
                qualified_s = 0.0
                while not stage_complete:
                    self._raise_if_actuator_tool_stopped()
                    current_ma = self._timed_output_current(
                        actuator, output_value, "positive", measurement_ms
                    )
                    self._raise_if_actuator_tool_stopped()
                    final_delta_ma = abs(current_ma - baseline_ma)
                    if final_delta_ma <= target_delta_ma:
                        qualified_s = min(
                            qualification_duration_s,
                            qualified_s + cycle_duration_s,
                        )
                    else:
                        qualified_s = 0.0
                    stage_complete = qualified_s >= qualification_duration_s
                    positive_progress = {
                        "actuator": actuator,
                        "elapsed_s": time.monotonic() - start,
                        "stage_index": stage_index,
                        "stage_count": len(stages),
                        "stage_voltage": target_voltage,
                        "sent_voltage": target_voltage,
                        "phase": "positive",
                        "current_ma": current_ma,
                        "delta_ma": final_delta_ma,
                        "target_delta_ma": target_delta_ma,
                        "qualified_s": qualified_s,
                        "qualification_duration_s": qualification_duration_s,
                        "supply_voltage": supply_voltage,
                        "output_value": output_value,
                        "stage_complete": stage_complete,
                    }
                    self.recovery_progress.emit(positive_progress)
                    self.message.emit(
                        "Recovery {actuator}: stage {stage_index}/{stage_count} at "
                        "{stage_voltage:.0f} V, delta {delta_ma:.2f} mA / "
                        "target {target_delta_ma:.2f} mA.".format(**positive_progress),
                        "info",
                    )
                    self._timed_output_current(
                        actuator, output_value, "negative", measurement_ms
                    )
                    self._raise_if_actuator_tool_stopped()
                    self.recovery_progress.emit(
                        {
                            **positive_progress,
                            "elapsed_s": time.monotonic() - start,
                            "sent_voltage": -target_voltage,
                            "phase": "negative",
                        }
                    )
                completed_stages = stage_index
        except ActuatorToolStopped:
            self.message.emit(f"Recovery stopped for actuator {actuator}", "warn")
            self.busy_changed.emit("")
            self.actuator_tool_finished.emit("recover", False, "Stopped")
            return
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(previous_safety)

        result = {
            "actuator": actuator,
            "baseline_ma": baseline_ma,
            "delta_ma": final_delta_ma,
            "elapsed_s": time.monotonic() - start,
            "stage_count": len(stages),
            "completed_stages": completed_stages,
            "supply_voltage": supply_voltage,
        }
        self.recovery_ready.emit(result)
        self.message.emit(
            f"Automatic recovery complete for actuator {actuator}.",
            "ok",
        )
        self.busy_changed.emit("")
        self.actuator_tool_finished.emit("recover", True, "Completed")
        self._emit_status()

    def _detect_group(self, group: int) -> None:
        board = self._require_board()
        group_count = (board.actuator_count + 7) // 8
        if not 0 <= group < group_count:
            raise ValueError(f"group must be 0..{group_count - 1}")
        start = group * 8
        actuators = list(range(start, min(start + 8, board.actuator_count)))
        self._detect_actuators(actuators, f"group {group}")

    def _detect_all(self) -> None:
        board = self._require_board()
        self._detect_actuators(list(range(board.actuator_count)), "all actuators")

    def _detect_actuators(self, actuators: list[int], scope: str) -> None:
        """Detect a set of actuators using one shared baseline measurement."""

        board = self._require_board()
        if not actuators:
            return
        status = board.status()
        self.status_ready.emit(status)
        self._last_status = time.monotonic()
        if str(status["psu"]).upper() != "ON" or not power_connection_is_ready(
            status["psc"]
        ):
            self.message.emit("Detection waits until PSU is on and output is connected.", "warn")
            return

        self.busy_changed.emit(f"Detecting {scope}")
        self.message.emit(
            f"Detection {scope}: actuators {actuators[0]}-{actuators[-1]}, "
            f"PSU {status['psu']}, output {status['psc']}, "
            f"voltage {float(status.get('voltage', 0.0)):.2f} V, "
            f"current {float(status.get('current', 0.0)):.2f} mA.",
            "info",
        )
        firmware_detection = self._capabilities.get("DET") == "1"
        if firmware_detection:
            self.message.emit(
                "Detection: using firmware DT0 with one shared 500 ms baseline, "
                "then a 250 ms settle + 500 ms capture per actuator; results "
                "arrive progressively.",
                "info",
            )
        else:
            self.message.emit(
                "Detection: 250 ms settle + 500 ms current capture "
                f"(<{board.not_connected_delta_ma:.2f} mA not connected, "
                ">10.00 mA error), then 2.00 s continuous "
                "forward + 500 ms capture; final >=3.00 mA error, otherwise ready.",
                "info",
            )
        if self._square_actuators:
            self._stop_square_wave()

        results: dict[int, dict[str, Any]] = {}
        for actuator in actuators:
            self.health_ready.emit(
                actuator // 8, {actuator: {"state": "detecting"}}
            )

        def publish_detection(detection: Any) -> None:
            actuator = int(detection.actuator)
            if actuator not in actuators:
                return
            entry = self._health_from_detection(detection)
            results[actuator] = entry
            self._actuator_health[actuator] = entry
            self.health_ready.emit(actuator // 8, {actuator: entry})
            self.message.emit(
                f"Detection actuator {actuator}: baseline {detection.baseline_ma:.2f} mA, "
                f"initial forward {detection.initial_forward_ma:.2f} mA "
                f"(delta {detection.initial_delta_ma:.2f} mA), final forward "
                f"{detection.forward_ma:.2f} mA (delta {entry['delta_ma']:.2f} mA) -> "
                f"{'ready' if entry['state'] == 'idle' else entry['state']}.",
                "ok" if entry["state"] == "idle" else "warn",
            )

        if firmware_detection:
            # DET-capable firmware owns both stages: one batch DT0 establishes
            # the shared baseline, then the SDK requests DT1 only for PRESENT
            # actuators. Filter callbacks when the UI requested one group.
            board.detect_all_firmware(progress_callback=publish_detection)
        else:
            baseline_ma: float | None = None
            for actuator in actuators:
                self.message.emit(
                    f"Detection actuator {actuator}: zeroing all outputs, then running "
                    "the forward-only 250 ms settle/500 ms capture and 2 s checks.",
                    "info",
                )
                detection = board.detect_actuator(actuator, baseline_ma=baseline_ma)
                if baseline_ma is None:
                    baseline_ma = detection.baseline_ma
                    self.message.emit(
                        f"Detection {scope}: shared baseline "
                        f"{baseline_ma:.2f} mA; reusing it for all actuators.",
                        "info",
                    )
                publish_detection(detection)

        for group in sorted({actuator // 8 for actuator in actuators}):
            self.health_ready.emit(
                group,
                {
                    actuator: result
                    for actuator, result in results.items()
                    if actuator // 8 == group
                },
            )
        self.busy_changed.emit("")
        connected = sum(1 for result in results.values() if result["state"] == "idle")
        errors = sum(1 for result in results.values() if result["state"] == "error")
        missing = sum(1 for result in results.values() if result["state"] == "disconnected")
        self.message.emit(
            f"{scope.capitalize()} detection: {connected} ready, "
            f"{missing} not connected, {errors} error.",
            "ok" if errors == 0 else "warn",
        )
        self._emit_status()

    def _start_square_wave(self, actuators: list[int]) -> None:
        board = self._require_board()
        unique = sorted({int(actuator) for actuator in actuators})
        if not unique:
            raise ValueError("Choose at least one actuator for square wave output.")
        for actuator in unique:
            board._validate_actuator(actuator)
            self._ensure_actuator_available(actuator)
        self._square_baseline_ma = float(board.current())
        self._square_supply_voltage = float(board.voltage())
        self._square_restore_safety = board.safety()
        if self._square_restore_safety:
            board.safety(False)
        self._square_start_time = time.monotonic()
        self._square_actuators = unique
        self._square_phase = "forward"
        self.square_changed.emit(True, unique, "arming")
        self._emit_square_progress(
            "baseline",
            sent_voltage=0.0,
            status="Starting",
        )
        self.message.emit(
            "Square wave started for "
            + ", ".join(str(value) for value in unique)
            + "; running equal one-second positive and reverse phases",
            "ok",
        )

    def _stop_square_wave(self) -> None:
        if not self._square_actuators:
            self.square_changed.emit(False, [], "idle")
            return
        board = self._require_board()
        actuators = list(self._square_actuators)
        self._square_actuators = []
        self._square_phase = "idle"
        for actuator in actuators:
            board._set_initialization_output(actuator, 0, "off")
        self._restore_square_safety()
        self.square_changed.emit(False, [], "idle")
        self.message.emit("Square wave stopped", "ok")
        self._emit_status()

    def _emit_square_progress(self, phase: str, **values: Any) -> None:
        if not self._square_actuators:
            return
        result: dict[str, Any] = {
            "actuators": list(self._square_actuators),
            "elapsed_s": max(0.0, time.monotonic() - self._square_start_time),
            "phase": phase,
            "status": phase.replace("_", " ").title(),
        }
        result.update(values)
        self.square_progress.emit(result)

    def _restore_square_safety(self) -> None:
        if self._board is None or self._square_restore_safety is None:
            self._square_restore_safety = None
            return
        try:
            self._board.safety(self._square_restore_safety)
        finally:
            self._square_restore_safety = None

    @staticmethod
    def _health_from_detection(detection: Any) -> dict[str, Any]:
        state = str(detection.state.value if hasattr(detection.state, "value") else detection.state)
        if state == "Ready":
            health_state = "idle"
        elif state == "Present":
            health_state = "present"
        elif state == "Not connected":
            health_state = "disconnected"
        elif state == "Error":
            health_state = "error"
        else:
            health_state = "na"
        return {
            "state": health_state,
            "baseline_ma": detection.baseline_ma,
            "forward_ma": detection.forward_ma,
            "discharge_ma": detection.discharge_ma,
            "delta_ma": detection.delta_ma,
            "initial_forward_ma": detection.initial_forward_ma,
            "initial_delta_ma": detection.initial_delta_ma,
        }

    def _ensure_actuator_available(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if not health:
            return
        state = health.get("state")
        if state == "idle":
            return
        if state == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")
        if state == "error":
            raise RuntimeError(
                f"Actuator {actuator} is in error state; current delta is "
                f"{health.get('delta_ma', 0):.2f} mA."
            )

    def _ensure_actuator_diagnosable(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if health and health.get("state") == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")

    def _ensure_actuator_recoverable(self, actuator: int) -> None:
        health = self._actuator_health.get(actuator)
        if health and health.get("state") == "disconnected":
            raise RuntimeError(f"Actuator {actuator} is not connected.")

    @staticmethod
    def _voltage_to_output(target_voltage: float, supply_voltage: float) -> int:
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(1, min(Board.max_output, int(Board.max_output * ratio)))

    @staticmethod
    def _voltage_to_output_allow_zero(target_voltage: float, supply_voltage: float) -> int:
        if target_voltage <= 0 or supply_voltage <= 0:
            return 0
        ratio = min(target_voltage / supply_voltage, 1.0)
        return max(0, min(Board.max_output, round(Board.max_output * ratio)))

    @staticmethod
    def _fast_init_step_v(error_ma: float) -> float:
        if error_ma <= 0.2:
            return 5.0
        if error_ma <= 1.0:
            return 10.0
        return 20.0

    def _service_square_wave(self) -> None:
        if self._board is None or not self._square_actuators:
            return
        try:
            if self._square_phase == "forward":
                self._emit_square_progress(
                    "positive",
                    sent_voltage=self._square_supply_voltage,
                    status="Full output",
                )
                self.square_changed.emit(True, list(self._square_actuators), "full on")
                positive_current_ma = 0.0
                for actuator in list(self._square_actuators):
                    positive_current_ma = self._run_square_phase(
                        actuator, "positive"
                    )
                current_delta_ma = abs(
                    positive_current_ma - self._square_baseline_ma
                )
                self._emit_square_progress(
                    "positive_measurement",
                    current_delta_ma=current_delta_ma,
                    status="Positive current measured",
                )
                self._square_phase = "reverse"
                return

            if self._square_phase == "reverse":
                self._emit_square_progress(
                    "discharging",
                    sent_voltage=-self._square_supply_voltage,
                    status="Discharging",
                )
                self.square_changed.emit(
                    True, list(self._square_actuators), "discharging"
                )
                for actuator in list(self._square_actuators):
                    self._run_square_phase(actuator, "negative")
                self._square_phase = "forward"
                return

            self._square_phase = "forward"
        except Exception as exc:
            self.message.emit(f"Square wave stopped: {exc}", "error")
            actuators = list(self._square_actuators)
            self._square_actuators = []
            self._square_phase = "idle"
            for actuator in actuators:
                try:
                    self._board._set_initialization_output(actuator, 0, "off")
                except Exception:
                    pass
            self._restore_square_safety()
            self.square_changed.emit(False, [], "idle")
            return

    def _run_square_phase(self, actuator: int, phase: str) -> float:
        """Drive one exact one-second phase and return its ending current."""
        return self._timed_output_current(actuator, Board.max_output, phase, 1000)

    def _timed_output_current(
        self, actuator: int, output: int, phase: str, duration_ms: int
    ) -> float:
        """Hold an output phase for a fixed period and read current at its end."""
        assert self._board is not None
        if self._board.direct_top_bottom_output:
            return self._board._initialization_output_current(
                actuator,
                output,
                phase,
                duration_ms,
            )

        phase_start = time.monotonic()
        self._board._set_initialization_output(
            actuator,
            output,
            phase,
        )
        remaining_s = duration_ms / 1000.0 - (time.monotonic() - phase_start)
        if remaining_s > 0:
            time.sleep(remaining_s)
        return float(self._board.current())

    def _all_off(self) -> None:
        board = self._require_board()
        if self._square_actuators:
            self._stop_square_wave()
            return
        board.all_actuators_off()
        self.message.emit("All actuators commanded off", "ok")
        self._emit_status()


class StatusPill(QLabel):
    def __init__(self, text: str = "Disconnected", kind: str = "neutral") -> None:
        super().__init__(text)
        self.setAlignment(Qt.AlignCenter)
        self.setProperty("kind", kind)

    def set(self, text: str, kind: str) -> None:
        self.setText(text)
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "-", unit: str = "") -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self.setFixedHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")

        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.unit_label = QLabel(unit)
        self.unit_label.setObjectName("MetricUnit")
        layout.addWidget(self.title_label)
        layout.addStretch()
        layout.addWidget(self.value_label)
        layout.addWidget(self.unit_label, 0, Qt.AlignVCenter)

    def set_value(self, value: str, unit: str | None = None) -> None:
        self.value_label.setText(value)
        if unit is not None:
            self.unit_label.setText(unit)


class SwitchToggle(QAbstractButton):
    def __init__(self, *, on_color: str = "#ee2c24", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_color = QColor(on_color)
        self._off_color = QColor("#d9d9d9")
        self._thumb_color = QColor("#ffffff")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 24)

    def sizeHint(self) -> QSize:
        return QSize(44, 24)

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        margin = 2
        track = self.rect().adjusted(margin, margin, -margin, -margin)
        radius = track.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._on_color if self.isChecked() else self._off_color)
        painter.drawRoundedRect(track, radius, radius)

        thumb_diameter = track.height() - 4
        thumb_x = track.right() - thumb_diameter - 2 if self.isChecked() else track.left() + 2
        thumb_y = track.top() + 2
        painter.setBrush(self._thumb_color)
        painter.drawEllipse(thumb_x, thumb_y, thumb_diameter, thumb_diameter)


class InitializationVoltagePlot(QWidget):
    """Sent bipolar initialization values with a live time cursor."""

    axis_limit_v = 250.0
    axis_minimum: float | None = None
    trace_color = "#0050bd"

    def __init__(
        self,
        stages_v: tuple[float, ...] = Board.initialization_stages_v,
        stage_duration_s: float = Board.initialization_stage_duration_s,
        phase_interval_s: float = Board.initialization_phase_interval_s,
    ) -> None:
        super().__init__()
        self._stages_v = tuple(float(value) for value in stages_v)
        self._stage_duration_s = float(stage_duration_s)
        self._phase_interval_s = float(phase_interval_s)
        self._total_s = self._stage_duration_s * len(self._stages_v)
        self._elapsed_s = 0.0
        self._samples: list[tuple[float, float]] = []
        self._markers: list[tuple[float, str]] = []
        self.setMinimumHeight(205)
        self.setAccessibleName("Sent initialization drive voltage")
        self.setAccessibleDescription(
            "Sent values from the one hertz bipolar initialization drive, with "
            "a cursor showing elapsed progress and limits of minus 250 to plus "
            "250 volts."
        )

    @property
    def elapsed_s(self) -> float:
        return self._elapsed_s

    @property
    def visible_range_s(self) -> tuple[float, float]:
        window_s = min(30.0, self._total_s)
        if self._elapsed_s <= window_s:
            return 0.0, window_s
        return self._elapsed_s - window_s, self._elapsed_s

    @property
    def samples(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._samples)

    @property
    def markers(self) -> tuple[tuple[float, str], ...]:
        return tuple(self._markers)

    def reset(self, total_s: float | None = None) -> None:
        self._samples.clear()
        self._markers.clear()
        self.set_progress(0.0, total_s)

    def add_marker(self, elapsed_s: float, label: str) -> None:
        marker = (max(0.0, float(elapsed_s)), str(label))
        if marker not in self._markers:
            self._markers.append(marker)
        visible_start_s, _visible_end_s = self.visible_range_s
        self._markers = [value for value in self._markers if value[0] >= visible_start_s]
        self.update()

    def add_sent_value(
        self, elapsed_s: float, voltage: float, total_s: float | None = None
    ) -> None:
        self.set_progress(elapsed_s, total_s)
        sample = (self._elapsed_s, float(voltage))
        if self._samples and sample[0] < self._samples[-1][0]:
            self._samples.clear()
        if self._samples and sample[0] == self._samples[-1][0]:
            self._samples[-1] = sample
        else:
            self._samples.append(sample)
        visible_start_s, _visible_end_s = self.visible_range_s
        while len(self._samples) > 1 and self._samples[1][0] < visible_start_s:
            self._samples.pop(0)
        self.update()

    def set_progress(self, elapsed_s: float, total_s: float | None = None) -> None:
        if total_s is not None and total_s > 0:
            self._total_s = float(total_s)
        self._elapsed_s = max(0.0, min(float(elapsed_s), self._total_s))
        self.update()

    def _axis_limit(self) -> float:
        return self.axis_limit_v

    def _axis_bounds(self) -> tuple[float, float]:
        limit = self._axis_limit()
        minimum = -limit if self.axis_minimum is None else self.axis_minimum
        return minimum, limit

    def _normalized_y(self, value: float) -> float:
        """Map an axis value to 0 at the top and 1 at the bottom."""
        minimum, maximum = self._axis_bounds()
        clipped_value = max(minimum, min(float(value), maximum))
        return (maximum - clipped_value) / max(maximum - minimum, 1e-12)

    def _format_cursor_value(self, value: float) -> str:
        return f"{value:+.0f} V" if value else "0 V"

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        # Keep a label band above the graph so the live value never competes
        # with the waveform itself.
        left, right, top, bottom = 48, 14, 30, 30
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        min_voltage, max_voltage = self._axis_bounds()
        voltage_range = max(max_voltage - min_voltage, 1e-12)
        visible_start_s, visible_end_s = self.visible_range_s
        visible_duration_s = max(visible_end_s - visible_start_s, 1.0)

        def x_for(seconds: float) -> float:
            return left + width * (seconds - visible_start_s) / visible_duration_s

        def y_for(voltage: float) -> float:
            return top + height * self._normalized_y(voltage)

        grid_pen = QPen(QColor("#dfe3ea"), 1)
        painter.setPen(grid_pen)
        for index in range(5):
            voltage = min_voltage + voltage_range * index / 4
            y = y_for(voltage)
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
            painter.setPen(QColor("#5d6c7b"))
            painter.drawText(0, round(y) - 8, left - 7, 16, Qt.AlignRight | Qt.AlignVCenter, f"{voltage:g}")
            painter.setPen(grid_pen)

        tick_values = [
            visible_start_s + visible_duration_s * index / 3
            for index in range(4)
        ]
        for seconds in tick_values:
            tick_x = x_for(seconds)
            painter.drawLine(
                QPointF(tick_x, top), QPointF(tick_x, top + height)
            )

        marker_pen = QPen(QColor("#7c3aed"), 1.5)
        marker_pen.setStyle(Qt.DashLine)
        painter.setPen(marker_pen)
        for marker_time_s, marker_label in self._markers:
            if not visible_start_s <= marker_time_s <= visible_end_s:
                continue
            marker_x = x_for(marker_time_s)
            painter.drawLine(
                QPointF(marker_x, top), QPointF(marker_x, top + height)
            )
            label_width = painter.fontMetrics().horizontalAdvance(marker_label) + 8
            label_left = max(
                left,
                min(round(marker_x) + 4, round(left + width) - label_width),
            )
            painter.fillRect(label_left, 3, label_width, 18, QColor("#ffffff"))
            painter.setPen(QColor("#7c3aed"))
            painter.drawText(
                label_left,
                3,
                label_width,
                18,
                Qt.AlignLeft | Qt.AlignVCenter,
                marker_label,
            )
            painter.setPen(marker_pen)

        waveform_pen = QPen(QColor(self.trace_color), 1.5)
        painter.setPen(waveform_pen)
        history_end_s = min(self._elapsed_s, visible_end_s)
        active_voltage: float | None = None
        active_since_s = visible_start_s
        for sample_time_s, sample_voltage in self._samples:
            if sample_time_s <= visible_start_s:
                active_voltage = sample_voltage
                active_since_s = visible_start_s
                continue
            if sample_time_s > history_end_s:
                break
            if active_voltage is not None:
                painter.drawLine(
                    QPointF(x_for(active_since_s), y_for(active_voltage)),
                    QPointF(x_for(sample_time_s), y_for(active_voltage)),
                )
                painter.drawLine(
                    QPointF(x_for(sample_time_s), y_for(active_voltage)),
                    QPointF(x_for(sample_time_s), y_for(sample_voltage)),
                )
            active_voltage = sample_voltage
            active_since_s = sample_time_s
        if active_voltage is not None and history_end_s >= active_since_s:
            painter.drawLine(
                QPointF(x_for(active_since_s), y_for(active_voltage)),
                QPointF(x_for(history_end_s), y_for(active_voltage)),
            )

        if not self._samples:
            painter.setPen(QColor("#7a8797"))
            painter.drawText(
                left,
                top,
                width,
                height,
                Qt.AlignCenter,
                "Waiting for initialization to start",
            )

        painter.setPen(QColor("#5d6c7b"))
        for index, seconds in enumerate(tick_values):
            tick_x = x_for(seconds)
            alignment = (
                Qt.AlignLeft
                if index == 0
                else Qt.AlignRight
                if index == len(tick_values) - 1
                else Qt.AlignHCenter
            )
            painter.drawText(
                round(tick_x) - 28,
                top + height + 6,
                56,
                18,
                alignment | Qt.AlignTop,
                f"{seconds:.0f} s",
            )

        progress_x = x_for(self._elapsed_s)
        painter.setPen(QPen(QColor("#ee2c24"), 2))
        painter.drawLine(QPointF(progress_x, top), QPointF(progress_x, top + height))

        if active_voltage is not None:
            displayed_voltage = max(
                min_voltage, min(active_voltage, max_voltage)
            )
            cursor_y = y_for(displayed_voltage)
            value_text = self._format_cursor_value(active_voltage)
            value_width = painter.fontMetrics().horizontalAdvance(value_text) + 10
            value_left = max(0, round(progress_x) - value_width - 7)
            # Position the label above its data line. The enlarged top margin
            # keeps even maximum-scale values fully visible.
            value_top = max(2, round(cursor_y) - 23)
            painter.fillRect(
                value_left, value_top, value_width, 18, QColor("#ffffff")
            )
            painter.setPen(QColor(self.trace_color))
            painter.drawText(
                value_left,
                value_top,
                value_width,
                18,
                Qt.AlignRight | Qt.AlignVCenter,
                value_text,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#ee2c24"))
            painter.drawEllipse(QPointF(progress_x, cursor_y), 3.5, 3.5)


class FastInitializationVoltagePlot(InitializationVoltagePlot):
    """Positive Fast Init drive values on a fixed zero-to-250-volt scale."""

    axis_minimum = 0.0

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Positive Fast Init drive voltage")
        self.setAccessibleDescription(
            "Positive drive values used by Fast Init during the rolling "
            "thirty-second view, with limits of zero to 250 volts."
        )

    def _format_cursor_value(self, value: float) -> str:
        return f"{value:.0f} V"


class InitializationCurrentDeltaPlot(InitializationVoltagePlot):
    """Rolling current delta synchronized with initialization voltage changes."""

    # This is a class-level invariant inherited by every current-delta plot:
    # zero is the lower axis boundary and therefore the bottom of the graph.
    axis_minimum = 0.0
    trace_color = "#16834b"

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(165)
        self.setAccessibleName("Initialization current delta")
        self.setAccessibleDescription(
            "Current change from the initialization baseline for each voltage "
            "sent during the rolling thirty-second view, shown on a nonnegative "
            "current scale."
        )

    @property
    def axis_limit_ma(self) -> float:
        maximum = max((abs(value) for _time, value in self._samples), default=0.0)
        for limit in (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0):
            if maximum <= limit:
                return limit
        return maximum * 1.1

    def _axis_limit(self) -> float:
        return self.axis_limit_ma

    def _format_cursor_value(self, value: float) -> str:
        return f"{value:+.2f} mA"

    def add_current_delta(
        self, elapsed_s: float, delta_ma: float, total_s: float | None = None
    ) -> None:
        self.add_sent_value(elapsed_s, abs(float(delta_ma)), total_s)


class DiagnosisVoltageCurrentPlot(QWidget):
    """Live 0-200 V diagnostic sweep with current on the vertical axis."""

    LOWER_CURVE_START_MA = 0.3
    LOWER_CURVE_END_MA = 1.5
    UPPER_CURVE_START_MA = 0.6
    UPPER_CURVE_END_MA = 3.0
    CURVE_EASE_EXPONENT = 2.0

    def __init__(self) -> None:
        super().__init__()
        self._samples: list[tuple[float, float]] = []
        plot_policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        plot_policy.setHeightForWidth(True)
        self.setSizePolicy(plot_policy)
        self.setMinimumSize(360, 270)
        self.setAccessibleName("Diagnostic voltage-current plot")
        self.setAccessibleDescription(
            "A line plot with voltage from zero to 200 volts on the horizontal "
            "axis and current from zero to 5 milliamps on the vertical axis."
        )

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return round(width * 0.75)

    def sizeHint(self) -> QSize:
        return QSize(500, 375)

    @classmethod
    def _reference_current(
        cls, voltage_v: float, start_ma: float, end_ma: float
    ) -> float:
        normalized_voltage = max(0.0, min(float(voltage_v), 200.0)) / 200.0
        curved_progress = 1.0 - (1.0 - normalized_voltage) ** cls.CURVE_EASE_EXPONENT
        return start_ma + (end_ma - start_ma) * curved_progress

    @classmethod
    def lower_reference_current(cls, voltage_v: float) -> float:
        return cls._reference_current(
            voltage_v, cls.LOWER_CURVE_START_MA, cls.LOWER_CURVE_END_MA
        )

    @classmethod
    def upper_reference_current(cls, voltage_v: float) -> float:
        return cls._reference_current(
            voltage_v, cls.UPPER_CURVE_START_MA, cls.UPPER_CURVE_END_MA
        )

    @classmethod
    def current_zone(cls, voltage_v: float, current_ma: float) -> str:
        """Return the plot zone containing a diagnostic current sample."""
        current_ma = abs(float(current_ma))
        if current_ma <= cls.lower_reference_current(voltage_v):
            return "green"
        if current_ma > cls.upper_reference_current(voltage_v):
            return "red"
        return "yellow"

    @classmethod
    def health_summary(cls, samples: list[tuple[float, float]]) -> str:
        """Summarize actuator condition using the same limits drawn on the plot."""
        if not samples:
            return "The diagnostic did not collect enough data to assess the actuator."

        zones = [cls.current_zone(voltage, current) for voltage, current in samples]
        if zones[-1] == "red":
            return (
                "Your actuator is not currently operating within the acceptable "
                "current range. Run the Recover routine before using it."
            )
        if all(zone == "green" for zone in zones):
            return (
                "Your actuator is in great shape. Its current stayed within the "
                "healthy range throughout the diagnostic sweep."
            )
        return (
            "Your actuator is functional, but its current entered the caution range "
            "during the diagnostic sweep. Run one or more initialization routines "
            "before regular use."
        )

    @property
    def samples(self) -> tuple[tuple[float, float], ...]:
        return tuple(self._samples)

    @property
    def current_axis_limit_ma(self) -> float:
        return 5.0

    def reset(self) -> None:
        self._samples.clear()
        self.update()

    def add_sample(self, voltage_v: float, current_ma: float) -> None:
        sample = (
            max(0.0, min(float(voltage_v), 200.0)),
            abs(float(current_ma)),
        )
        if self._samples and sample[0] <= self._samples[-1][0]:
            self._samples = [value for value in self._samples if value[0] < sample[0]]
        self._samples.append(sample)
        self.update()

    @staticmethod
    def format_current_value(current_ma: float) -> str:
        return f"{abs(float(current_ma)):.2f} mA"

    def paintEvent(self, event: Any) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))

        left, right, top, bottom = 55, 18, 18, 42
        width = max(1, self.width() - left - right)
        height = max(1, self.height() - top - bottom)
        current_limit = self.current_axis_limit_ma

        def x_for(voltage: float) -> float:
            return left + width * voltage / 200.0

        def y_for(current: float) -> float:
            displayed_current = max(0.0, min(current, current_limit))
            return top + height * (current_limit - displayed_current) / current_limit

        plot_bottom = top + height
        plot_right = left + width
        reference_voltages = [float(voltage) for voltage in range(0, 201, 2)]
        lower_curve = [
            QPointF(x_for(voltage), y_for(self.lower_reference_current(voltage)))
            for voltage in reference_voltages
        ]
        upper_curve = [
            QPointF(x_for(voltage), y_for(self.upper_reference_current(voltage)))
            for voltage in reference_voltages
        ]

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#e4f5e8"))
        painter.drawPolygon(
            QPolygonF(
                lower_curve
                + [QPointF(plot_right, plot_bottom), QPointF(left, plot_bottom)]
            )
        )
        painter.setBrush(QColor("#fff0d6"))
        painter.drawPolygon(
            QPolygonF(upper_curve + list(reversed(lower_curve)))
        )
        painter.setBrush(QColor("#fde3e3"))
        painter.drawPolygon(
            QPolygonF(
                [QPointF(left, top), QPointF(plot_right, top)]
                + list(reversed(upper_curve))
            )
        )

        grid_pen = QPen(QColor("#dfe3ea"), 1)
        painter.setPen(grid_pen)
        for voltage in (0.0, 50.0, 100.0, 150.0, 200.0):
            x = x_for(voltage)
            painter.drawLine(QPointF(x, top), QPointF(x, top + height))
            painter.setPen(QColor("#5d6c7b"))
            painter.drawText(
                round(x) - 28,
                top + height + 5,
                56,
                18,
                Qt.AlignHCenter | Qt.AlignTop,
                f"{voltage:.0f}",
            )
            painter.setPen(grid_pen)

        for index in range(5):
            current = current_limit * index / 4
            y = y_for(current)
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
            painter.setPen(QColor("#5d6c7b"))
            painter.drawText(
                0,
                round(y) - 8,
                left - 7,
                16,
                Qt.AlignRight | Qt.AlignVCenter,
                f"{current:g}",
            )
            painter.setPen(grid_pen)

        painter.setPen(QPen(QColor("#55a963"), 2))
        painter.drawPolyline(QPolygonF(lower_curve))
        painter.setPen(QPen(QColor("#e28a1a"), 2))
        painter.drawPolyline(QPolygonF(upper_curve))

        if len(self._samples) > 1:
            painter.setPen(QPen(QColor("#075dcc"), 2))
            for previous, current in zip(self._samples, self._samples[1:]):
                painter.drawLine(
                    QPointF(x_for(previous[0]), y_for(previous[1])),
                    QPointF(x_for(current[0]), y_for(current[1])),
                )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#075dcc"))
        for voltage, current in self._samples:
            painter.drawEllipse(QPointF(x_for(voltage), y_for(current)), 3.0, 3.0)

        if self._samples:
            latest_voltage, latest_current = self._samples[-1]
            latest_x = x_for(latest_voltage)
            latest_y = y_for(latest_current)
            value_text = self.format_current_value(latest_current)
            value_width = painter.fontMetrics().horizontalAdvance(value_text) + 10
            if latest_x - value_width - 7 >= left:
                value_left = round(latest_x) - value_width - 7
            else:
                value_left = min(
                    round(latest_x) + 7, round(plot_right) - value_width
                )
            value_top = max(2, round(latest_y) - 24)
            painter.fillRect(
                value_left, value_top, value_width, 18, QColor("#ffffff")
            )
            painter.setPen(QColor("#075dcc"))
            painter.drawText(
                value_left,
                value_top,
                value_width,
                18,
                Qt.AlignRight | Qt.AlignVCenter,
                value_text,
            )

        if not self._samples:
            painter.setPen(QColor("#7a8797"))
            painter.drawText(
                left,
                top,
                width,
                height,
                Qt.AlignCenter,
                "Waiting for the voltage sweep",
            )

        painter.setPen(QColor("#5d6c7b"))
        painter.drawText(
            left,
            top + height + 24,
            width,
            16,
            Qt.AlignHCenter | Qt.AlignVCenter,
            "Voltage (V)",
        )
        painter.save()
        painter.translate(13, top + height / 2)
        painter.rotate(-90)
        painter.drawText(
            -round(height / 2),
            -8,
            round(height),
            16,
            Qt.AlignHCenter | Qt.AlignVCenter,
            "Current (mA)",
        )
        painter.restore()


class ToggleMetricCard(QFrame):
    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        on_text: str,
        off_text: str,
        *,
        on_color: str = "#ee2c24",
    ) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        self.setFixedHeight(58)
        self._on_text = on_text
        self._off_text = off_text

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("MetricTitle")

        self.value_label = QLabel(off_text)
        self.value_label.setObjectName("MetricValue")
        self.toggle = SwitchToggle(on_color=on_color)
        self.toggle.toggled.connect(self._on_toggled)
        layout.addWidget(self.title_label)
        layout.addStretch()
        layout.addWidget(self.value_label)
        layout.addWidget(self.toggle)

    def _on_toggled(self, checked: bool) -> None:
        self.value_label.setText(self._on_text if checked else self._off_text)
        self.toggled.emit(checked)

    def set_state(self, checked: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(checked)
        self.toggle.update()
        self.toggle.blockSignals(False)
        self.value_label.setText(self._on_text if checked else self._off_text)


class ToolDialog(QDialog):
    """Non-blocking popup that owns one actuator tool's controls and progress."""

    def __init__(
        self,
        title: str,
        description: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle(title)
        self.setModal(False)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("DialogTitle")
        detail = QLabel(description)
        detail.setObjectName("DialogSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)
        self.content = QVBoxLayout()
        self.content.setSpacing(10)
        layout.addLayout(self.content)
        self.footer = QHBoxLayout()
        self.footer.setSpacing(8)
        self.footer.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        self.footer.addWidget(self.close_button)
        layout.addLayout(self.footer)

    def add_action_button(self, button: QPushButton) -> None:
        """Place a tool action immediately before Close in the footer."""

        self.footer.insertWidget(self.footer.count() - 1, button)


class BoardSettingsDialog(QDialog):
    """Edit the four settings exposed by the firmware CFG command."""

    save_requested = Signal(dict)

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        detection_supported: bool = False,
        parent: QWidget | None = None,
        *,
        vt_supported: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Board Settings")
        self.setModal(False)
        self.setFixedWidth(440)
        self._detection_supported = detection_supported
        self._vt_supported = vt_supported
        self._loaded_vt_limit_vs: int | None = None
        self._vt_limit_modified = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Board Settings")
        heading.setObjectName("DialogTitle")
        detail = QLabel(
            "Configure the actuator VT budget, manual-output safety, and firmware logging."
            if vt_supported
            else "Configure actuator timing, manual-output safety, and firmware logging."
        )
        detail.setObjectName("DialogSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.vt_limit = QSpinBox()
        self.vt_limit.setRange(1, 4_294_967)
        self.vt_limit.setSuffix(" V·s")
        self.vt_limit.setAccessibleName("VT budget")
        form.addRow(form_label("VT budget"), self.vt_limit)

        self.max_active = QSpinBox()
        self.max_active.setRange(0, 2_147_483_647)
        self.max_active.setSuffix(" ms")
        self.max_active.setAccessibleName("Maximum active time")
        form.addRow(form_label("Maximum active time"), self.max_active)

        self.discharge = QSpinBox()
        self.discharge.setRange(0, 2_147_483_647)
        self.discharge.setSuffix(" ms")
        self.discharge.setAccessibleName("Maximum discharge time")
        form.addRow(form_label("Maximum discharge time"), self.discharge)
        form.setRowVisible(self.vt_limit, vt_supported)
        form.setRowVisible(self.max_active, not vt_supported)
        form.setRowVisible(self.discharge, not vt_supported)

        self.safety = LabeledToggle("Safety")
        self.safety.setToolTip("When enabled, the firmware blocks raw manual output commands.")
        form.addRow(form_label("Manual output"), self.safety)

        self.debug = LabeledToggle("Debug messages")
        self.debug.setToolTip("Include firmware debug messages in the event log.")
        form.addRow(form_label("Firmware logging"), self.debug)

        self.detection_current = QDoubleSpinBox()
        self.detection_current.setRange(0.01, 1000.0)
        self.detection_current.setDecimals(2)
        self.detection_current.setSingleStep(0.01)
        self.detection_current.setSuffix(" mA")
        self.detection_current.setAccessibleName("Detection current threshold")
        form.addRow(form_label("Detection current"), self.detection_current)

        self.dt0_error = QDoubleSpinBox()
        self.dt0_error.setRange(0.01, 1000.0)
        self.dt0_error.setDecimals(2)
        self.dt0_error.setSingleStep(0.10)
        self.dt0_error.setSuffix(" mA")
        self.dt0_error.setAccessibleName("DT0 error threshold")
        form.addRow(form_label("DT0 error threshold"), self.dt0_error)

        self.dt1_error = QDoubleSpinBox()
        self.dt1_error.setRange(0.01, 1000.0)
        self.dt1_error.setDecimals(2)
        self.dt1_error.setSingleStep(0.10)
        self.dt1_error.setSuffix(" mA")
        self.dt1_error.setAccessibleName("DT1 error threshold")
        form.addRow(form_label("DT1 error threshold"), self.dt1_error)
        self._detection_controls = (
            self.detection_current,
            self.dt0_error,
            self.dt1_error,
        )
        for row in range(5, 8):
            form.setRowVisible(row, detection_supported)
        layout.addLayout(form)

        self.status_label = QLabel("Reading settings from the board…")
        self.status_label.setObjectName("DialogSubtitle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        self.save_button = self.buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save settings")
        self.save_button.clicked.connect(self._submit)
        self.buttons.rejected.connect(self.close)
        layout.addWidget(self.buttons)

        self.set_config(config or {})
        self.set_loading(True)

    def set_config(self, config: dict[str, Any]) -> None:
        if "vt_limit_vs" in config:
            self.vt_limit.setValue(int(config["vt_limit_vs"]))
            self._loaded_vt_limit_vs = int(config["vt_limit_vs"])
            self._vt_limit_modified = bool(config.get("vt_limit_modified", False))
        if "max_active_ms" in config:
            self.max_active.setValue(int(config["max_active_ms"]))
        if "discharge_ms" in config:
            self.discharge.setValue(int(config["discharge_ms"]))
        if "safe" in config:
            self.safety.setChecked(bool(config["safe"]))
        if "debug" in config:
            self.debug.setChecked(bool(config["debug"]))
        if "detection_current_limit_ma" in config:
            self.detection_current.setValue(float(config["detection_current_limit_ma"]))
        if "dt0_error_threshold_ma" in config:
            self.dt0_error.setValue(float(config["dt0_error_threshold_ma"]))
        if "dt1_error_threshold_ma" in config:
            self.dt1_error.setValue(float(config["dt1_error_threshold_ma"]))

    def values(self) -> dict[str, Any]:
        values = {
            "safe": self.safety.isChecked(),
            "debug": self.debug.isChecked(),
        }
        if self._vt_supported:
            values["vt_limit_vs"] = self.vt_limit.value()
        else:
            values.update(
                max_active_ms=self.max_active.value(),
                discharge_ms=self.discharge.value(),
            )
        if self._detection_supported:
            values.update(
                detection_current_limit_ma=self.detection_current.value(),
                dt0_error_threshold_ma=self.dt0_error.value(),
                dt1_error_threshold_ma=self.dt1_error.value(),
            )
        return values

    def set_loading(self, loading: bool, message: str | None = None) -> None:
        for control in (
            self.vt_limit,
            self.max_active,
            self.discharge,
            self.safety,
            self.debug,
            *self._detection_controls,
        ):
            control.setEnabled(not loading)
        self.save_button.setEnabled(not loading)
        if message is not None:
            self.status_label.setText(message)
        elif not loading:
            if self._vt_supported:
                modified = "User modified" if self._vt_limit_modified else "Factory default"
                self.status_label.setText(
                    f"VT budget is retained after reboot. Configuration history: {modified}."
                )
            else:
                self.status_label.setText(
                    "MAX and DIS are retained after reboot; SAFE and DEBUG reset to their defaults."
                )

    def show_error(self, message: str) -> None:
        self.set_loading(False, message)
        self.status_label.setStyleSheet("color: #b42318;")

    def _submit(self) -> None:
        if self._detection_supported and (
            self.detection_current.value() >= self.dt0_error.value()
            or self.detection_current.value() >= self.dt1_error.value()
        ):
            self.show_error(
                "Detection current must be lower than both DT0 and DT1 error thresholds."
            )
            return
        if (
            self._vt_supported
            and self._loaded_vt_limit_vs is not None
            and self.vt_limit.value() != self._loaded_vt_limit_vs
        ):
            warning = QMessageBox(self)
            warning.setWindowTitle("Change VT budget")
            warning.setIcon(QMessageBox.Warning)
            warning.setText(
                f"Change the VT budget from {self._loaded_vt_limit_vs:,} V·s "
                f"to {self.vt_limit.value():,} V·s?"
            )
            warning.setInformativeText(
                "Changing this value alters the electrical exposure allowed for each "
                "actuator. An incorrect value could permanently damage the actuators "
                "or board electronics. Use only a limit validated for this hardware."
            )
            change_button = warning.addButton("Change VT budget", QMessageBox.AcceptRole)
            warning.addButton(QMessageBox.Cancel)
            warning.exec()
            if warning.clickedButton() is not change_button:
                return
        self.status_label.setStyleSheet("")
        self.set_loading(True, "Saving settings…")
        self.save_requested.emit(self.values())


class FirmwareUpdateDialog(QDialog):
    update_requested = Signal(str)
    abort_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Update Firmware")
        self.setModal(False)
        self.setFixedWidth(560)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        heading = QLabel("Update Firmware")
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        file_row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select a firmware .bin file")
        self.path_edit.textChanged.connect(self._update_button_state)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_row.addWidget(self.path_edit, 1)
        file_row.addWidget(browse)
        layout.addWidget(form_label("Firmware image"))
        layout.addLayout(file_row)

        self.progress = QProgressBar()
        self.progress.setObjectName("InitProgress")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("The board's outputs will be switched off before installation.")
        self.status_label.setObjectName("DialogSubtitle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        footer = QHBoxLayout()
        footer.addStretch()
        self.update_button = QPushButton("Update")
        self.update_button.setObjectName("PrimaryButton")
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self._start)
        self.abort_button = QPushButton("Abort")
        self.abort_button.setVisible(False)
        self.abort_button.clicked.connect(self._request_abort)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.update_button)
        footer.addWidget(self.abort_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        layout.addLayout(footer)

    def _browse(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Select firmware image",
            str(Path.home()),
            "Firmware image (*.bin);;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _update_button_state(self) -> None:
        self.update_button.setEnabled(
            not self._updating and Path(self.path_edit.text().strip()).is_file()
        )

    def _start(self) -> None:
        path = self.path_edit.text().strip()
        if not Path(path).is_file():
            self.show_error("Select an existing firmware .bin file.")
            return
        self.set_updating(True)
        self.update_requested.emit(path)

    def _request_abort(self) -> None:
        self.abort_button.setEnabled(False)
        self.abort_button.setText("Cancelling…")
        self.status_label.setText("Cancelling after the current frame…")
        self.abort_requested.emit()

    def set_updating(self, updating: bool) -> None:
        self._updating = updating
        self.path_edit.setEnabled(not updating)
        self.update_button.setVisible(not updating)
        self.abort_button.setVisible(updating)
        self.abort_button.setEnabled(updating)
        self.abort_button.setText("Abort")
        self.close_button.setEnabled(not updating)
        if updating:
            self.status_label.setStyleSheet("")
            self.status_label.setText("Preparing and transferring firmware…")
            self.progress.setValue(0)
        self._update_button_state()

    def set_progress(self, written: int, total: int) -> None:
        fraction = 0 if total <= 0 else min(1.0, written / total)
        self.progress.setValue(round(fraction * 1000))
        self.status_label.setText(
            f"Transferring firmware… {fraction * 100:.1f}% "
            f"({written:,} / {total:,} bytes)"
        )

    def show_complete(self, _sha256: str, version: str) -> None:
        self.set_updating(False)
        self.progress.setValue(1000)
        self.status_label.setStyleSheet("color: #0d6b3c;")
        self.status_label.setText(
            f"Update verified. The board rebooted and reconnected with firmware {version}."
        )

    def show_error(self, message: str) -> None:
        self.set_updating(False)
        self.status_label.setStyleSheet("color: #b42318;")
        self.status_label.setText(message)

    def closeEvent(self, event: Any) -> None:
        if self._updating:
            event.ignore()
            return
        super().closeEvent(event)


class FactoryResetDialog(QDialog):
    """Dashboard-styled confirmation for the destructive factory reset."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Factory Reset")
        self.setModal(True)
        self.setFixedWidth(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Restore factory settings?")
        heading.setObjectName("DialogTitle")
        detail = QLabel(
            "The board will reboot and the dashboard will reconnect automatically "
            "over USB serial."
        )
        detail.setObjectName("DialogSubtitle")
        detail.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(detail)

        self.warning_label = QLabel(
            "This permanently erases network, security, Bluetooth, actuator, and "
            "runtime configuration. This action cannot be undone."
        )
        self.warning_label.setObjectName("ConnectionError")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch()
        self.reset_button = QPushButton("Factory Reset")
        self.reset_button.setObjectName("dangerButton")
        self.reset_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        self.cancel_button.setDefault(True)
        footer.addWidget(self.reset_button)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)


class BluetoothConfigDialog(QDialog):
    """Configure the board's persisted Bluetooth settings."""

    save_requested = Signal(dict)
    clear_bonds_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Bluetooth Config")
        self.setModal(True)
        self.setFixedWidth(500)
        self._loading = True
        self._enabled_dirty = False
        self._name_dirty = False
        self._security_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        title = QLabel("Bluetooth Config")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.enabled = LabeledToggle("Bluetooth enabled")
        self.enabled.toggled.connect(self._enabled_changed)
        layout.addWidget(self.enabled)

        name_row = QHBoxLayout()
        self.name_prefix = QLabel("FR-")
        self.name_prefix.setObjectName("FormLabel")
        self.name = QLineEdit()
        self.name.setMaxLength(28)
        self.name.setPlaceholderText("board-name")
        self.name.setAccessibleName("Bluetooth name after FR-")
        self.name.textChanged.connect(self._name_changed)
        name_row.addWidget(form_label("Name"))
        name_row.addWidget(self.name_prefix)
        name_row.addWidget(self.name, 1)
        layout.addLayout(name_row)

        self.secure = LabeledToggle("Secure pairing + access token")
        self.secure.toggled.connect(self._security_changed)
        layout.addWidget(self.secure)

        self.status_label = QLabel()
        self.status_label.setObjectName("DialogSubtitle")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        layout.addWidget(self.status_label)

        footer = QHBoxLayout()
        self.clear_bonds_button = QPushButton("Forget paired devices")
        self.clear_bonds_button.setObjectName("quietButton")
        self.clear_bonds_button.clicked.connect(self._clear_bonds)
        footer.addWidget(self.clear_bonds_button)
        footer.addStretch()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("PrimaryButton")
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.apply_button)
        close_button = QPushButton("Close")
        close_button.setObjectName("quietButton")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)
        self._update_actions()

    def set_values(self, fields: dict[str, str]) -> None:
        self._loading = True
        self.enabled.setChecked(fields.get("STATE", "OFF").upper() == "ON")
        advertised_name = fields.get("NAME", "").strip()
        if advertised_name.upper().startswith("FR-"):
            suffix = advertised_name[3:]
            legacy_name = False
        elif advertised_name.lower().startswith(("rockford-", "lansing-")):
            suffix = advertised_name.split("-", 1)[1]
            legacy_name = True
        else:
            suffix = advertised_name
            legacy_name = bool(advertised_name)
        self.name.setText(suffix[:28])
        self.secure.setChecked(fields.get("SEC", "OFF").upper() == "ON")
        self._enabled_dirty = False
        self._name_dirty = legacy_name
        self._security_dirty = False
        self._loading = False
        self.status_label.hide()
        self._update_actions()

    def show_saved(self, fields: dict[str, str]) -> None:
        self.set_values(fields)
        self.status_label.setText("Saved. Restart the board to apply Bluetooth changes.")
        self.status_label.setStyleSheet("color: #0d6b3c;")
        self.status_label.show()

    def show_bonds_cleared(self, fields: dict[str, str]) -> None:
        self.set_values(fields)
        self.status_label.setText("Paired devices forgotten.")
        self.status_label.setStyleSheet("color: #0d6b3c;")
        self.status_label.show()

    def show_error(self, message: str) -> None:
        self._loading = False
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #b42318;")
        self.status_label.show()
        self._update_actions()

    def _enabled_changed(self, _enabled: bool) -> None:
        if not self._loading:
            self._enabled_dirty = True
        self._update_actions()

    def _name_changed(self, _name: str) -> None:
        if not self._loading:
            self._name_dirty = True
        self._update_actions()

    def _security_changed(self, _enabled: bool) -> None:
        if not self._loading:
            self._security_dirty = True
        self._update_actions()

    def _update_actions(self) -> None:
        dirty = self._enabled_dirty or self._name_dirty or self._security_dirty
        self.apply_button.setEnabled(not self._loading and dirty)
        self.clear_bonds_button.setEnabled(not self._loading)

    def _apply(self) -> None:
        suffix = self.name.text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,28}", suffix):
            self.show_error(
                "Enter 1–28 letters, numbers, hyphens, or underscores after FR-."
            )
            return
        name = f"FR-{suffix}"
        self._loading = True
        self.status_label.setText("Saving…")
        self.status_label.setStyleSheet("")
        self.status_label.show()
        self._update_actions()
        self.save_requested.emit(
            {
                "enabled": self.enabled.isChecked(),
                "enabled_dirty": self._enabled_dirty,
                "name": suffix,
                "name_dirty": self._name_dirty,
                "secure": self.secure.isChecked(),
                "security_dirty": self._security_dirty,
            }
        )

    def _clear_bonds(self) -> None:
        answer = QMessageBox.question(
            self,
            "Forget paired devices",
            "Forget all devices paired with this board?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._loading = True
        self.status_label.setText("Forgetting paired devices…")
        self.status_label.setStyleSheet("")
        self.status_label.show()
        self._update_actions()
        self.clear_bonds_requested.emit()


class WifiConfigDialog(QDialog):
    """Dashboard-owned copy of Network Setup's Wi-Fi configuration page."""

    wifi_enabled_requested = Signal(bool)
    scan_requested = Signal()
    join_requested = Signal(object, str)
    disconnect_requested = Signal()
    mode_requested = Signal(str)
    access_point_config_requested = Signal(str, str, int)

    def __init__(
        self, parent: QWidget | None = None, *, access_point_supported: bool = False
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Wi-Fi Config")
        # Block controls in the dashboard while this window is open. In
        # particular, closing this dialog must never click through to the
        # dashboard's board-level Disconnect button.
        self.setModal(True)
        self.resize(790, 520)
        self.setMinimumSize(680, 440)
        self._busy = False
        self._hardware_safe = True
        self._auto_scan_started = False
        self._saved_ssid = ""
        self._connected_bssid = ""
        self._wifi_state = "UNKNOWN"
        self._last_status_fields: dict[str, str] = {}
        self._access_point_supported = access_point_supported
        self._wifi_mode = "CLIENT"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("Wi-Fi Config")
        heading.setObjectName("DialogTitle")
        hint = QLabel("Choose a nearby network. A lock means a password is required.")
        hint.setObjectName("DialogSubtitle")
        layout.addWidget(heading)
        layout.addWidget(hint)

        controls = QHBoxLayout()
        self.wifi_enabled = LabeledToggle("Wi-Fi enabled")
        self.wifi_enabled.toggled.connect(
            self.wifi_enabled_requested.emit
        )
        self.scan_button = QPushButton()
        configure_refresh_button(self.scan_button, "Refresh networks")
        self.scan_button.clicked.connect(self._request_scan)
        controls.addWidget(self.wifi_enabled)
        controls.addStretch()
        controls.addWidget(self.scan_button)
        layout.addLayout(controls)

        mode_row = QHBoxLayout()
        self.mode_label = form_label("Mode")
        self.mode = QComboBox()
        self.mode.addItem("Client", "CLIENT")
        self.mode.addItem("Access point", "ACCESS_POINT")
        self.mode.activated.connect(self._mode_selected)
        mode_row.addWidget(self.mode_label)
        mode_row.addWidget(self.mode, 1)
        layout.addLayout(mode_row)
        self.mode_label.setVisible(access_point_supported)
        self.mode.setVisible(access_point_supported)

        self.access_point_panel = QFrame()
        self.access_point_panel.setObjectName("Inset")
        access_point_layout = QVBoxLayout(self.access_point_panel)
        access_point_layout.setContentsMargins(14, 12, 14, 12)
        access_point_form = QFormLayout()
        access_point_form.setHorizontalSpacing(14)
        access_point_form.setVerticalSpacing(9)
        self.access_point_ssid = QLineEdit()
        self.access_point_ssid.setPlaceholderText("Access point network name")
        self.access_point_password = QLineEdit()
        self.access_point_password.setPlaceholderText("Empty creates an open network")
        self.access_point_password_visibility = add_secret_visibility(
            self.access_point_password, secret_name="access point password"
        )
        self.access_point_channel = QSpinBox()
        self.access_point_channel.setRange(1, 13)
        self.access_point_channel.setValue(1)
        access_point_form.addRow(form_label("Network name"), self.access_point_ssid)
        access_point_form.addRow(form_label("Password"), self.access_point_password)
        access_point_form.addRow(form_label("Channel"), self.access_point_channel)
        access_point_layout.addLayout(access_point_form)
        access_point_footer = QHBoxLayout()
        self.access_point_status = QLabel("Access point settings not read")
        self.access_point_status.setObjectName("ConnectionHint")
        self.apply_access_point = QPushButton("Apply")
        self.apply_access_point.clicked.connect(self._apply_access_point)
        access_point_footer.addWidget(self.access_point_status, 1)
        access_point_footer.addWidget(self.apply_access_point)
        access_point_layout.addLayout(access_point_footer)
        layout.addWidget(self.access_point_panel)
        self.access_point_panel.hide()

        self.status_label = QLabel("Reading Wi-Fi status…")
        self.status_label.setObjectName("WifiStatus")
        self.status_label.setProperty("kind", "neutral")
        layout.addWidget(self.status_label)
        self.safety_warning = QLabel(
            "Turn off the power supply before scanning or changing Wi-Fi settings."
        )
        self.safety_warning.setObjectName("ConnectionError")
        self.safety_warning.setWordWrap(True)
        self.safety_warning.hide()
        layout.addWidget(self.safety_warning)

        self.network_list = QTreeWidget()
        self.network_list.setHeaderLabels(
            ["Network", "Signal", "Security", "Channels", "Access point MAC"]
        )
        self.network_list.setRootIsDecorated(False)
        self.network_list.setAlternatingRowColors(True)
        self.network_list.setSelectionMode(QTreeWidget.SingleSelection)
        self.network_list.setColumnWidth(0, 250)
        self.network_list.setColumnWidth(1, 90)
        self.network_list.setColumnWidth(2, 115)
        self.network_list.setColumnWidth(3, 70)
        self.network_list.setColumnWidth(4, 145)
        self.network_list.itemSelectionChanged.connect(self._network_selected)
        self.network_list.itemDoubleClicked.connect(
            lambda _item, _column: self._activate_selected()
        )
        layout.addWidget(self.network_list, 1)

        join_row = QHBoxLayout()
        self.network_password = QLineEdit()
        self.network_password.setPlaceholderText("Password")
        self.network_password_visibility = add_secret_visibility(
            self.network_password, secret_name="Wi-Fi password"
        )
        self.network_password.returnPressed.connect(self._join_selected)
        self.connection_button = QPushButton("Connect")
        self.connection_button.clicked.connect(self._activate_selected)
        join_row.addWidget(self.network_password, 1)
        join_row.addWidget(self.connection_button)
        layout.addLayout(join_row)

        footer = QHBoxLayout()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        footer.addStretch()
        footer.addWidget(close_button)
        layout.addLayout(footer)
        self._network_selected()

    def _request_scan(self) -> None:
        if self._wifi_mode != "CLIENT":
            return
        self.network_list.clear()
        self.network_password.clear()
        self._network_selected()
        self.set_busy(True, "Refreshing networks…")
        self.scan_requested.emit()

    def start_automatic_scan(self) -> None:
        if self._auto_scan_started or not self._hardware_safe or self._wifi_mode != "CLIENT":
            return
        self._auto_scan_started = True
        self._request_scan()

    def _mode_selected(self) -> None:
        mode = str(self.mode.currentData())
        if mode == self._wifi_mode:
            return
        self._wifi_mode = mode
        self._update_mode_visibility()
        self.set_busy(True, "Switching Wi-Fi mode…")
        self.mode_requested.emit(mode)

    def _update_mode_visibility(self) -> None:
        access_point = self._access_point_supported and self._wifi_mode == "ACCESS_POINT"
        self.access_point_panel.setVisible(access_point)
        self.network_list.setVisible(not access_point)
        self.network_password.setVisible(not access_point)
        self.connection_button.setVisible(not access_point)
        self._update_action_availability()

    def _apply_access_point(self) -> None:
        ssid = self.access_point_ssid.text().strip()
        password = self.access_point_password.text()
        if not ssid or len(ssid.encode("utf-8")) > 32:
            QMessageBox.information(self, "Access point", "Enter a network name up to 32 bytes.")
            return
        if password and not 8 <= len(password.encode("utf-8")) <= 63:
            QMessageBox.information(
                self, "Access point", "The password must contain 8 to 63 bytes, or be empty."
            )
            return
        self.set_busy(True, "Applying access point settings…")
        self.access_point_config_requested.emit(
            ssid, password, self.access_point_channel.value()
        )

    def _selected_network(self) -> WifiNetwork | None:
        items = self.network_list.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def _network_is_connected(self, network: WifiNetwork | None) -> bool:
        if network is None or self._wifi_state != "CONNECTED":
            return False
        # Rows represent an SSID and may combine several access points. The
        # displayed MAC belongs to the strongest AP, which is not necessarily
        # the AP currently serving the board.
        return network.ssid == self._saved_ssid

    def _network_selected(self) -> None:
        network = self._selected_network()
        connected = self._network_is_connected(network)
        secure = (
            network is not None
            and network.security.upper() != "OPEN"
            and not connected
            and network.ssid != self._saved_ssid
        )
        self.network_password.setVisible(secure)
        self.connection_button.setText("Disconnect" if connected else "Connect")
        self._update_action_availability()

    def _activate_selected(self) -> None:
        network = self._selected_network()
        if self._network_is_connected(network):
            self.set_busy(True, f"Disconnecting from {network.ssid}…")
            self.disconnect_requested.emit()
            return
        if network is not None and network.ssid == self._saved_ssid:
            self.set_busy(True, f"Connecting to {network.ssid}…")
            self.wifi_enabled_requested.emit(True)
            return
        self._join_selected()

    def _join_selected(self) -> None:
        network = self._selected_network()
        if network is None:
            QMessageBox.information(self, "Wi-Fi", "Select a network first.")
            return
        password = self.network_password.text()
        if network.security.upper() != "OPEN" and not password:
            QMessageBox.information(self, "Wi-Fi", "Enter the network password.")
            return
        self.set_busy(True, f"Connecting to {network.ssid}…")
        self.join_requested.emit(network, password)

    def set_status(self, fields: dict[str, str]) -> None:
        self._last_status_fields = dict(fields)
        self._wifi_state = fields.get("STATE", "UNKNOWN").upper()
        ssid64 = fields.get("SSID64", "")
        try:
            ssid = base64.b64decode(ssid64, validate=True).decode("utf-8") if ssid64 else ""
        except (ValueError, UnicodeError):
            ssid = ""
        self._saved_ssid = ssid
        self._wifi_mode = fields.get("WIFI_MODE", "CLIENT").upper()
        mode_index = self.mode.findData(self._wifi_mode)
        if mode_index >= 0:
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(mode_index)
            self.mode.blockSignals(False)
        self._connected_bssid = fields.get("CURRENT_BSSID", "NONE")
        self.wifi_enabled.blockSignals(True)
        self.wifi_enabled.setChecked(fields.get("WIFI", "OFF").upper() == "ON")
        self.wifi_enabled.blockSignals(False)
        self._render_connection_status()
        self._decorate_network_rows()
        self._update_mode_visibility()
        self.set_busy(self._wifi_state == "CONNECTING")

    def set_access_point_status(self, fields: dict[str, str]) -> None:
        encoded_ssid = fields.get("SSID64", "")
        try:
            ssid = base64.b64decode(encoded_ssid, validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            ssid = ""
        self.access_point_ssid.setText(ssid)
        if fields.get("CHANNEL"):
            self.access_point_channel.setValue(int(fields["CHANNEL"]))
        state = fields.get("STATE", "INACTIVE").replace("_", " ").title()
        clients = fields.get("CLIENTS", "0")
        address = fields.get("IP", "192.168.24.1")
        self.access_point_status.setText(
            f"{state} · {address} · {clients} client{'s' if clients != '1' else ''}"
        )
        self.set_busy(False)

    def _render_connection_status(self) -> None:
        fields = self._last_status_fields
        if self._wifi_state == "CONNECTED":
            status_text = f"Connected · {self._saved_ssid or 'Unknown network'}"
            kind = "ok"
        elif fields.get("WIFI", "OFF").upper() != "ON":
            status_text = "Wi-Fi off" + (
                f" · Saved: {self._saved_ssid}" if self._saved_ssid else ""
            )
            kind = "neutral"
        elif self._wifi_state == "CONNECTING":
            status_text = f"Connecting · {self._saved_ssid or 'Unknown network'}"
            kind = "active"
        else:
            status_text = "Disconnected" + (
                f" · Saved: {self._saved_ssid}" if self._saved_ssid else ""
            )
            kind = "warn"
        self.status_label.setText(status_text)
        self.status_label.setProperty("kind", kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_networks(self, networks: list[WifiNetwork]) -> None:
        self.network_list.clear()
        grouped: dict[str, tuple[WifiNetwork, list[int]]] = {}
        for network in networks:
            key = network.ssid
            if key not in grouped:
                grouped[key] = (network, [network.channel])
                continue
            best, channels = grouped[key]
            if network.channel not in channels:
                channels.append(network.channel)
            if network.rssi > best.rssi:
                best = network
            grouped[key] = (best, channels)

        ordered_groups = sorted(
            grouped.values(), key=lambda entry: entry[0].rssi, reverse=True
        )
        for network, channels in ordered_groups:
            secure = network.security.upper() != "OPEN"
            item = QTreeWidgetItem(
                [
                    network.ssid or "<hidden>",
                    f"{network.rssi} dBm",
                    network.security.replace("_", " "),
                    ",".join(str(channel) for channel in channels),
                    network.bssid or "—",
                ]
            )
            item.setData(0, Qt.UserRole, network)
            item.setIcon(1, signal_icon(network.rssi))
            item.setIcon(2, security_icon(secure))
            self.network_list.addTopLevelItem(item)
        self._decorate_network_rows()
        preferred_item = None
        for index in range(self.network_list.topLevelItemCount()):
            item = self.network_list.topLevelItem(index)
            network = item.data(0, Qt.UserRole)
            if self._network_is_connected(network):
                preferred_item = item
                break
            if preferred_item is None and network.ssid == self._saved_ssid:
                preferred_item = item
        if preferred_item is None and self.network_list.topLevelItemCount():
            preferred_item = self.network_list.topLevelItem(0)
        if preferred_item is not None:
            self.network_list.setCurrentItem(preferred_item)
        self.set_busy(False)
        if self._last_status_fields:
            self._render_connection_status()

    def _decorate_network_rows(self) -> None:
        connected_icon = (
            QIcon(str(CONNECTED_ICON_PATH))
            if CONNECTED_ICON_PATH.is_file()
            else self.style().standardIcon(QStyle.SP_DialogApplyButton)
        )
        for index in range(self.network_list.topLevelItemCount()):
            item = self.network_list.topLevelItem(index)
            network = item.data(0, Qt.UserRole)
            connected = self._network_is_connected(network)
            item.setIcon(0, connected_icon if connected else QIcon())
            item.setToolTip(0, "Currently connected" if connected else "")
        self._network_selected()

    def set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        self._update_action_availability()
        if message is not None:
            self.status_label.setText(message)
            self.status_label.setProperty("kind", "active" if busy else "neutral")
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)

    def set_hardware_safe(self, safe: bool) -> None:
        self._hardware_safe = bool(safe)
        self.safety_warning.setVisible(not self._hardware_safe)
        if self._hardware_safe and self.status_label.text().startswith("Turn off"):
            self.status_label.setText("Ready to scan for nearby networks.")
            self.status_label.setStyleSheet("")
        self._update_action_availability()

    def _update_action_availability(self) -> None:
        enabled = not self._busy and self._hardware_safe
        self.wifi_enabled.setEnabled(enabled)
        self.mode.setEnabled(enabled)
        client_mode = self._wifi_mode == "CLIENT"
        self.scan_button.setEnabled(enabled and client_mode)
        self.connection_button.setEnabled(
            enabled and client_mode and bool(self.network_list.selectedItems())
        )
        self.apply_access_point.setEnabled(enabled and not client_mode)

    def show_error(self, message: str) -> None:
        if "OUTPUT_ACTIVE" in message.upper():
            self.set_hardware_safe(False)
            message = "Turn off the power supply before scanning or changing Wi-Fi settings."
        self.set_busy(False, message)
        self.status_label.setStyleSheet("color: #b42318;")


class SecurityEncryptionDialog(QDialog):
    """Manage the board access token and TLS credentials."""

    apply_requested = Signal(dict)

    def __init__(
        self,
        *,
        auth_supported: bool,
        tls_supported: bool,
        server_name: str = "rockford.local",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Security & Encryption")
        self.setModal(True)
        self.resize(720, 540)
        self.setMinimumWidth(620)
        self._auth_supported = auth_supported
        self._tls_supported = tls_supported
        self._server_name = server_name
        self._hardware_safe = True
        self._loading = True
        self._token_dirty = False
        self._auth_dirty = False
        self._tls_dirty = False
        self._credentials_dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("Security & Encryption")
        title.setObjectName("DialogTitle")
        root.addWidget(title)

        self.access_panel = QFrame()
        self.access_panel.setObjectName("Inset")
        access_layout = QVBoxLayout(self.access_panel)
        access_layout.setContentsMargins(16, 14, 16, 14)
        access_layout.setSpacing(10)
        access_title = QLabel("Access token")
        access_title.setObjectName("SectionTitle")
        access_layout.addWidget(access_title)
        access_settings = QHBoxLayout()
        self.use_access_token = LabeledToggle("Use access token")
        self.use_access_token.toggled.connect(self._on_auth_toggled)
        access_settings.addWidget(self.use_access_token)
        access_settings.addStretch()
        access_layout.addLayout(access_settings)
        token_row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setReadOnly(True)
        self.token.setPlaceholderText("No access token configured")
        self.token_visibility = add_secret_visibility(
            self.token, secret_name="access token"
        )
        self.copy_token_btn = QPushButton()
        self.copy_token_btn.setObjectName("quietButton")
        self.copy_token_btn.setAccessibleName("Copy access token")
        self.copy_token_btn.setToolTip("Copy access token")
        self.copy_token_btn.setIcon(QIcon(str(COPY_ICON_PATH)))
        self.copy_token_btn.setIconSize(QSize(20, 20))
        self.copy_token_btn.setFixedSize(42, 38)
        self.copy_token_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.token.text())
        )
        self.generate_token_btn = QPushButton()
        configure_refresh_button(
            self.generate_token_btn, "Generate new access token"
        )
        self.generate_token_btn.clicked.connect(self._generate_token)
        token_row.addWidget(self.token, 1)
        token_row.addWidget(self.copy_token_btn)
        token_row.addWidget(self.generate_token_btn)
        access_layout.addLayout(token_row)
        root.addWidget(self.access_panel)

        self.encryption_panel = QFrame()
        self.encryption_panel.setObjectName("Inset")
        encryption_layout = QVBoxLayout(self.encryption_panel)
        encryption_layout.setContentsMargins(16, 14, 16, 14)
        encryption_layout.setSpacing(10)
        encryption_title = QLabel("Encryption")
        encryption_title.setObjectName("SectionTitle")
        encryption_layout.addWidget(encryption_title)
        encryption_settings = QHBoxLayout()
        self.tls_enabled = LabeledToggle("Enable TLS encryption")
        self.tls_enabled.toggled.connect(self._on_tls_toggled)
        encryption_settings.addWidget(self.tls_enabled)
        encryption_settings.addStretch()
        encryption_layout.addLayout(encryption_settings)

        self.tls_credentials_panel = QFrame()
        self.tls_credentials_panel.setObjectName("ConnectionOptions")
        credentials_layout = QVBoxLayout(self.tls_credentials_panel)
        credentials_layout.setContentsMargins(16, 14, 16, 14)
        credentials_layout.setSpacing(10)
        credentials_title = QLabel("Certificate and private key")
        credentials_title.setObjectName("SectionTitle")
        credentials_layout.addWidget(credentials_title)
        credentials_form = QFormLayout()
        credentials_form.setHorizontalSpacing(14)
        credentials_form.setVerticalSpacing(10)

        certificate_row = QHBoxLayout()
        self.tls_certificate_path = QLineEdit()
        self.tls_certificate_path.setPlaceholderText("Select a .pem or .crt file")
        self.tls_certificate_path.textChanged.connect(self._credentials_changed)
        self.certificate_browse = QPushButton("Browse…")
        self.certificate_browse.setObjectName("quietButton")
        self.certificate_browse.clicked.connect(
            lambda: self._choose_tls_file(
                self.tls_certificate_path, "Select server certificate"
            )
        )
        certificate_row.addWidget(self.tls_certificate_path, 1)
        certificate_row.addWidget(self.certificate_browse)
        credentials_form.addRow(form_label("Server certificate"), certificate_row)

        key_row = QHBoxLayout()
        self.tls_key_path = QLineEdit()
        self.tls_key_path.setPlaceholderText("Select the matching .pem or .key file")
        self.tls_key_path.textChanged.connect(self._credentials_changed)
        self.key_browse = QPushButton("Browse…")
        self.key_browse.setObjectName("quietButton")
        self.key_browse.clicked.connect(
            lambda: self._choose_tls_file(self.tls_key_path, "Select private key")
        )
        key_row.addWidget(self.tls_key_path, 1)
        key_row.addWidget(self.key_browse)
        credentials_form.addRow(form_label("Private key"), key_row)

        self.tls_key_password = QLineEdit()
        self.tls_key_password.setPlaceholderText(
            "Only needed if the private key is encrypted"
        )
        self.tls_key_password_visibility = add_secret_visibility(
            self.tls_key_password, secret_name="private-key password"
        )
        self.tls_key_password.textChanged.connect(self._credentials_changed)
        credentials_form.addRow(form_label("Key password"), self.tls_key_password)
        credentials_layout.addLayout(credentials_form)

        install_row = QHBoxLayout()
        self.tls_create_btn = QPushButton("Create certificate…")
        self.tls_create_btn.setObjectName("quietButton")
        self.tls_create_btn.clicked.connect(self._create_tls_credentials)
        install_row.addWidget(self.tls_create_btn)
        install_row.addStretch()
        credentials_layout.addLayout(install_row)

        encryption_layout.addWidget(self.tls_credentials_panel)
        root.addWidget(self.encryption_panel)

        self.status_label = QLabel("Reading security settings…")
        self.status_label.setObjectName("DialogSubtitle")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        self.safety_warning = QLabel(
            "Turn off the power supply before changing security settings."
        )
        self.safety_warning.setObjectName("ConnectionError")
        self.safety_warning.setWordWrap(True)
        self.safety_warning.hide()
        root.addWidget(self.safety_warning)

        footer = QHBoxLayout()
        footer.addStretch()
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._apply)
        footer.addWidget(self.apply_button)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("quietButton")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        root.addLayout(footer)

        self.access_panel.setVisible(auth_supported)
        self.encryption_panel.setVisible(tls_supported)
        self._action_widgets = (
            self.copy_token_btn,
            self.generate_token_btn,
            self.apply_button,
            self.use_access_token,
            self.tls_enabled,
            self.certificate_browse,
            self.key_browse,
            self.tls_create_btn,
        )
        self._update_actions()

    def set_hardware_safe(self, safe: bool) -> None:
        self._hardware_safe = bool(safe)
        self.safety_warning.setVisible(not safe)
        self._update_actions()

    def set_values(self, token: str, tls: dict[str, str]) -> None:
        self._loading = True
        self.token.setText(token)
        self._token_dirty = False
        self.use_access_token.setChecked(
            tls.get("AUTH_STATE", "ON" if token else "OFF").upper() == "ON"
        )
        self._auth_dirty = False
        self.tls_enabled.setChecked(
            tls.get("STATE", tls.get("TLS", "OFF")).upper() == "ON"
        )
        if not self.tls_enabled.isChecked():
            self.tls_certificate_path.clear()
            self.tls_key_path.clear()
            self.tls_key_password.clear()
        self._tls_dirty = False
        self._credentials_dirty = False
        ready = tls.get("READY", tls.get("TLS_READY", "NO")).upper() in {
            "YES",
            "ON",
            "1",
        }
        state = "enabled" if self.tls_enabled.isChecked() else "disabled"
        authentication = "enabled" if self.use_access_token.isChecked() else "disabled"
        credentials = "installed" if ready else "not installed"
        self.status_label.setText(
            f"Security settings loaded · Access token {authentication} · "
            f"TLS {state} · Credentials {credentials}."
        )
        self.status_label.setStyleSheet("")
        self._loading = False
        self._update_actions()

    def show_error(self, message: str) -> None:
        self._loading = False
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #b42318;")
        self._update_actions()

    def _update_actions(self) -> None:
        enabled = not self._loading and self._hardware_safe
        for widget in self._action_widgets:
            widget.setEnabled(enabled)
        self.apply_button.setEnabled(
            enabled
            and (
                self._token_dirty
                or self._auth_dirty
                or self._tls_dirty
                or self._credentials_dirty
            )
        )
        token_controls_enabled = enabled and self.use_access_token.isChecked()
        self.token.setEnabled(token_controls_enabled)
        self.copy_token_btn.setEnabled(token_controls_enabled and bool(self.token.text()))
        self.generate_token_btn.setEnabled(token_controls_enabled)
        self._update_tls_credentials_enabled()

    def _update_tls_credentials_enabled(self) -> None:
        self.tls_credentials_panel.setEnabled(
            not self._loading and self._hardware_safe and self.tls_enabled.isChecked()
        )

    def _generate_token(self) -> None:
        self.token.setText(secrets.token_hex(16))
        self._token_dirty = True
        self.status_label.setText("New access token prepared. Apply to save it.")
        self._update_actions()

    def _on_auth_toggled(self, _enabled: bool) -> None:
        if not self._loading:
            self._auth_dirty = True
            self.status_label.setText(
                "Access-token setting changed. Apply to save it."
            )
        self._update_actions()

    def _on_tls_toggled(self, _enabled: bool) -> None:
        if not self._loading:
            self._tls_dirty = True
            self.status_label.setText("Encryption setting changed. Apply to save it.")
        self._update_actions()

    def _credentials_changed(self, _value: str) -> None:
        if not self._loading:
            self._credentials_dirty = True
            self.status_label.setText("TLS credentials changed. Apply to install them.")
        self._update_actions()

    def _apply(self) -> None:
        if (
            not self._token_dirty
            and not self._auth_dirty
            and not self._tls_dirty
            and not self._credentials_dirty
        ):
            return
        settings = {
            "token": self.token.text(),
            "token_dirty": self._token_dirty,
            "auth_enabled": self.use_access_token.isChecked(),
            "auth_dirty": self._auth_dirty,
            "tls_enabled": self.tls_enabled.isChecked(),
            "tls_dirty": self._tls_dirty,
            "credentials_dirty": self._credentials_dirty,
        }
        if self._credentials_dirty:
            credentials = self._read_tls_credentials()
            if credentials is None:
                return
            certificate, private_key = credentials
            settings.update(
                certificate=certificate,
                private_key=private_key,
                key_password=self.tls_key_password.text(),
            )
        self._loading = True
        self.status_label.setText("Applying security settings…")
        self._update_actions()
        self.apply_requested.emit(settings)

    def _choose_tls_file(self, field: QLineEdit, title: str) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, title, "", "PEM files (*.pem *.crt *.cer *.key);;All files (*)"
        )
        if path:
            field.setText(path)

    def _create_tls_credentials(self) -> None:
        dialog = TlsCertificateDialog(parent=self)
        if dialog.exec() != QDialog.Accepted or dialog.generated_files is None:
            return
        self.tls_certificate_path.setText(str(dialog.generated_files.certificate))
        self.tls_key_path.setText(str(dialog.generated_files.private_key))
        self.tls_key_password.setText(dialog.key_password.text())

    def _read_tls_credentials(self) -> tuple[bytes, bytes] | None:
        certificate_path = Path(self.tls_certificate_path.text().strip())
        key_path = Path(self.tls_key_path.text().strip())
        if not certificate_path.is_file() or not key_path.is_file():
            QMessageBox.information(
                self,
                "TLS credentials",
                "Select an existing certificate and private-key file.",
            )
            return None
        try:
            certificate = certificate_path.read_bytes()
            private_key = key_path.read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "TLS credentials", str(exc))
            return None
        if b"-----BEGIN CERTIFICATE-----" not in certificate:
            QMessageBox.information(
                self, "TLS certificate", "The certificate must be PEM encoded."
            )
            return None
        if b"-----BEGIN" not in private_key or b"PRIVATE KEY-----" not in private_key:
            QMessageBox.information(
                self, "TLS private key", "The private key must be PEM encoded."
            )
            return None
        return certificate, private_key

class NetworkConfigDialog(QDialog):
    """Edit the board's IP addressing and TCP server configuration."""

    save_requested = Signal(dict)

    def __init__(
        self,
        *,
        connected_over_network: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Network Config")
        self.setModal(True)
        self.resize(700, 680)
        self.setMinimumSize(620, 580)
        self._connected_over_network = connected_over_network
        self._hardware_safe = True
        self._scoped = False
        self._access_point_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Network Config")
        title.setObjectName("DialogTitle")
        subtitle = QLabel("Configure the board's network address and TCP server.")
        subtitle.setObjectName("DialogSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.ip_panel = QFrame()
        self.ip_panel.setObjectName("Inset")
        ip_layout = QVBoxLayout(self.ip_panel)
        ip_layout.setContentsMargins(16, 14, 16, 14)
        ip_layout.setSpacing(10)
        ip_title = QLabel("IP addressing")
        ip_title.setObjectName("SectionTitle")
        ip_layout.addWidget(ip_title)
        form = QFormLayout()
        self.ip_form = form
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        self.ip_interface = QComboBox()
        form.addRow(form_label("Interface"), self.ip_interface)
        self.use_dhcp = LabeledToggle("Automatically obtain an IP address (DHCP)")
        self.use_dhcp.setChecked(True)
        self.use_dhcp.toggled.connect(self._update_ip_enabled)
        self.ip_assignment = QWidget()
        assignment_layout = QVBoxLayout(self.ip_assignment)
        assignment_layout.setContentsMargins(0, 0, 0, 0)
        assignment_layout.setSpacing(0)
        assignment_layout.addWidget(self.use_dhcp)
        self.ap_assignment = QLabel("Fixed by access-point mode")
        self.ap_assignment.setObjectName("DialogSubtitle")
        self.ap_assignment.hide()
        assignment_layout.addWidget(self.ap_assignment)
        form.addRow(form_label("IP assignment"), self.ip_assignment)
        self.address = QLineEdit("192.168.1.64")
        self.subnet = QLineEdit("255.255.255.0")
        self.gateway = QLineEdit("192.168.1.1")
        self.dns1 = QLineEdit("1.1.1.1")
        self.dns2 = QLineEdit("8.8.8.8")
        self.hostname = QLineEdit("fluid-reality")
        for label, widget in (
            ("IP address", self.address),
            ("Subnet mask", self.subnet),
            ("Gateway", self.gateway),
            ("Primary DNS", self.dns1),
            ("Secondary DNS", self.dns2),
            ("Hostname", self.hostname),
        ):
            form.addRow(form_label(label), widget)
        self._static_fields = (
            self.address,
            self.subnet,
            self.gateway,
            self.dns1,
            self.dns2,
        )
        ip_layout.addLayout(form)
        layout.addWidget(self.ip_panel)

        self.tcp_panel = QFrame()
        self.tcp_panel.setObjectName("Inset")
        tcp_layout = QVBoxLayout(self.tcp_panel)
        tcp_layout.setContentsMargins(16, 14, 16, 14)
        tcp_layout.setSpacing(10)
        tcp_title = QLabel("TCP server")
        tcp_title.setObjectName("SectionTitle")
        tcp_layout.addWidget(tcp_title)
        self.tcp_enabled = LabeledToggle("Enable TCP server")
        self.tcp_enabled.setChecked(True)
        tcp_layout.addWidget(self.tcp_enabled)
        tcp_row = QHBoxLayout()
        tcp_row.setSpacing(10)
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1, 65535)
        self.tcp_port.setValue(49765)
        self.tcp_port.setFixedWidth(110)
        self.tcp_bind = QComboBox()
        self.tcp_bind.setMinimumWidth(220)
        tcp_row.addWidget(form_label("Port"))
        tcp_row.addWidget(self.tcp_port)
        tcp_row.addSpacing(16)
        tcp_row.addWidget(form_label("Interface"))
        tcp_row.addWidget(self.tcp_bind, 1)
        tcp_layout.addLayout(tcp_row)
        layout.addWidget(self.tcp_panel)

        self.status_label = QLabel("Reading network settings…")
        self.status_label.setObjectName("DialogSubtitle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.safety_warning = QLabel(
            "Turn off the power supply before changing network settings."
        )
        self.safety_warning.setObjectName("ConnectionError")
        self.safety_warning.setWordWrap(True)
        self.safety_warning.hide()
        layout.addWidget(self.safety_warning)

        layout.addStretch(1)
        self.footer = QHBoxLayout()
        footer = self.footer
        footer.addStretch()
        self.save_button = QPushButton("Apply")
        self.save_button.clicked.connect(self._submit)
        footer.addWidget(self.save_button)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("quietButton")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self._update_ip_enabled()
        self.set_loading(True)

    def set_hardware_safe(self, safe: bool) -> None:
        self._hardware_safe = bool(safe)
        self.safety_warning.setVisible(not safe)
        self._update_actions()

    def set_loading(self, loading: bool, message: str | None = None) -> None:
        if message is not None:
            self.status_label.setText(message)
        self._loading = bool(loading)
        self._update_actions()

    def _update_actions(self) -> None:
        enabled = not getattr(self, "_loading", False) and self._hardware_safe
        for widget in (
            self.ip_interface,
            self.use_dhcp,
            self.address,
            self.subnet,
            self.gateway,
            self.dns1,
            self.dns2,
            self.hostname,
            self.tcp_enabled,
            self.tcp_port,
            self.tcp_bind,
            self.save_button,
        ):
            widget.setEnabled(enabled)
        self.ip_interface.setEnabled(enabled and self._scoped)
        self.tcp_bind.setEnabled(enabled and self._scoped)
        self._update_ip_enabled()

    def _update_ip_enabled(self) -> None:
        editable = not getattr(self, "_loading", False) and self._hardware_safe
        if self._access_point_mode:
            self.address.setEnabled(editable)
            self.subnet.setEnabled(editable)
            for widget in (self.gateway, self.dns1, self.dns2):
                widget.setEnabled(False)
        else:
            enabled = editable and not self.use_dhcp.isChecked()
            for widget in self._static_fields:
                widget.setEnabled(enabled)

    def _update_ip_mode_display(self) -> None:
        self.use_dhcp.setVisible(not self._access_point_mode)
        self.ap_assignment.setVisible(self._access_point_mode)
        for widget in (self.gateway, self.dns1, self.dns2):
            self.ip_form.setRowVisible(widget, not self._access_point_mode)
        self._update_ip_enabled()

    @staticmethod
    def _interface_label(interface: str) -> str:
        return {"WIFI": "Wi-Fi", "ETH": "Ethernet"}.get(
            interface, interface.replace("_", " ").title()
        )

    def set_values(
        self, fields: dict[str, str], interfaces: tuple[str, ...], scoped: bool
    ) -> None:
        self._scoped = bool(scoped)
        self._access_point_mode = (
            fields.get("WIFI_MODE", "CLIENT").upper() == "ACCESS_POINT"
        )
        selected = self.ip_interface.currentData()
        self.ip_interface.clear()
        self.tcp_bind.clear()
        self.tcp_bind.addItem("Any available interface", "ANY")
        for interface in interfaces:
            label = self._interface_label(interface)
            self.ip_interface.addItem(label, interface)
            self.tcp_bind.addItem(label, interface)
        if selected:
            index = self.ip_interface.findData(selected)
            if index >= 0:
                self.ip_interface.setCurrentIndex(index)
        self.use_dhcp.setChecked(fields.get("MODE", "DHCP").upper() == "DHCP")
        self.hostname.setText(fields.get("HOST", self.hostname.text()))
        self.tcp_enabled.setChecked(fields.get("TCP", "OFF").upper() == "ON")
        if fields.get("PORT"):
            self.tcp_port.setValue(int(fields["PORT"]))
        bind = fields.get("BIND", "ANY")
        bind_index = self.tcp_bind.findData(bind)
        if bind_index >= 0:
            self.tcp_bind.setCurrentIndex(bind_index)
        for key, widget in (
            ("IP", self.address),
            ("MASK", self.subnet),
            ("GW", self.gateway),
            ("DNS1", self.dns1),
            ("DNS2", self.dns2),
        ):
            value = fields.get(key)
            if value and value != "0.0.0.0":
                widget.setText(value)
        self._update_ip_mode_display()
        self.status_label.setText("Network settings loaded.")
        self.status_label.setStyleSheet("")
        self.set_loading(False)

    def values(self) -> dict[str, Any]:
        return {
            "apply_ip": True,
            "access_point_mode": self._access_point_mode,
            "dhcp": self.use_dhcp.isChecked(),
            "address": self.address.text().strip(),
            "subnet": self.subnet.text().strip(),
            "gateway": self.gateway.text().strip(),
            "dns1": self.dns1.text().strip(),
            "dns2": self.dns2.text().strip(),
            "hostname": self.hostname.text().strip(),
            "tcp_enabled": self.tcp_enabled.isChecked(),
            "tcp_port": self.tcp_port.value(),
            "interface": self.ip_interface.currentData(),
            "tcp_bind": self.tcp_bind.currentData(),
            "scoped": self._scoped,
        }

    def _submit(self) -> None:
        hostname = self.hostname.text().strip()
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", hostname):
            QMessageBox.information(self, "Hostname", "Enter a valid hostname.")
            return
        if self._access_point_mode:
            try:
                address = ipaddress.IPv4Address(self.address.text().strip())
                subnet = ipaddress.IPv4Network(
                    f"0.0.0.0/{self.subnet.text().strip()}"
                ).netmask
                if address.is_unspecified or subnet == ipaddress.IPv4Address("0.0.0.0"):
                    raise ValueError
            except ValueError:
                QMessageBox.information(
                    self, "IP addressing", "Enter a valid AP address and subnet mask."
                )
                return
        elif not self.use_dhcp.isChecked():
            try:
                for value in (
                    self.address.text(),
                    self.subnet.text(),
                    self.gateway.text(),
                    self.dns1.text(),
                    self.dns2.text(),
                ):
                    ipaddress.IPv4Address(value.strip())
            except ipaddress.AddressValueError:
                QMessageBox.information(
                    self, "IP addressing", "Enter valid IPv4 addresses in every field."
                )
                return
        if self._connected_over_network:
            answer = QMessageBox.warning(
                self,
                "Network connection will close",
                "Applying network settings will close the current network connection. "
                "You will need to reconnect using the new settings.\n\n"
                "Do you want to continue?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        self.set_loading(True, "Applying network settings…")
        self.save_requested.emit(self.values())

    def show_applied_status(self, fields: dict[str, str]) -> None:
        state = fields.get("STATE", "UNKNOWN").replace("_", " ").title()
        address = fields.get("IP") or "0.0.0.0"
        self.status_label.setText(
            f"Network status: {state} · IP address: {address}"
        )
        color = "#067647" if state in {"Connected", "Active"} else "#b54708"
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")

    def show_error(self, message: str) -> None:
        self.set_loading(False, message)
        self.status_label.setStyleSheet("color: #b42318;")


class ActuatorCard(QFrame):
    focused = Signal(int)

    def __init__(self, actuator: int) -> None:
        super().__init__()
        self.actuator = actuator
        self.setObjectName("ActuatorCard")
        self.setProperty("state", "na")
        self.setProperty("selected", False)
        self._health: dict[str, Any] | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumWidth(0)
        self.setFixedHeight(54)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)

        header = QHBoxLayout()
        number = QLabel(f"{actuator:02d}")
        number.setObjectName("ActuatorNumber")
        self.state = StatusPill("N/A", "neutral")
        self.state.setObjectName("ActuatorStatePill")
        header.addWidget(number)
        header.addStretch()
        header.addWidget(self.state)

        self.value = QLabel("")
        self.value.setObjectName("ActuatorValue")
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.runtime = QLabel("")
        self.runtime.setObjectName("ActuatorDetail")
        self.runtime.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        details = QHBoxLayout()
        details.setSpacing(8)
        details.addWidget(self.value)
        details.addStretch()
        details.addWidget(self.runtime)

        layout.addLayout(header)
        layout.addLayout(details)

    def mousePressEvent(self, event: Any) -> None:
        self.focused.emit(self.actuator)
        super().mousePressEvent(event)

    def is_selected(self) -> bool:
        return self.property("selected") is True

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_health(self, health: dict[str, Any] | None) -> None:
        self._health = health
        self.setCursor(Qt.PointingHandCursor if self.is_selectable() else Qt.ForbiddenCursor)
        self._apply_health_style()

    def reset_detection(self) -> None:
        self._health = None
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("state", "na")
        self.state.set("N/A", "neutral")
        self.value.clear()
        self.runtime.clear()
        self.style().unpolish(self)
        self.style().polish(self)

    def is_available(self) -> bool:
        return bool(self._health) and self._health.get("state") == "idle"

    def is_selectable(self) -> bool:
        return not self._health or self._health.get("state") not in {"disconnected", "detecting"}

    def _apply_health_style(self) -> bool:
        if not self._health:
            return False
        state = self._health.get("state")
        if state == "idle":
            self.setProperty("state", "idle")
            self.state.set("Ready", "ok")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"current {delta:.2f} mA")
            self.runtime.clear()
        elif state == "detecting":
            self.setProperty("state", "detecting")
            self.state.set("Detecting", "active")
            self.value.setText("checking...")
            self.runtime.setText("diagnostic running")
        elif state == "present":
            self.setProperty("state", "detecting")
            self.state.set("Present", "active")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"current {delta:.2f} mA")
            self.runtime.clear()
        elif state == "fast_init":
            self.setProperty("state", "detecting")
            self.state.set("Fast Init", "active")
            delta = float(self._health.get("delta_ma", 0.0))
            voltage = float(self._health.get("target_voltage", 0.0))
            self.value.setText(f"current {delta:.2f} mA")
            self.runtime.setText(f"drive {voltage:.0f} V")
        elif state == "disconnected":
            self.setProperty("state", "disconnected")
            self.state.set("Not connected", "neutral")
            self.value.clear()
            self.runtime.clear()
        else:
            self.setProperty("state", "health_error")
            self.state.set("Error", "warn")
            delta = float(self._health.get("delta_ma", 0.0))
            self.value.setText(f"current {delta:.2f} mA")
            self.runtime.clear()
        self.style().unpolish(self)
        self.style().polish(self)
        return True

    def update_from_status(self, status: dict[str, Any]) -> None:
        if self._apply_health_style():
            return
        state_value = int(status["actuator_states"][self.actuator])
        if state_value == 0:
            self.reset_detection()
            return
        self.setProperty("state", state_class(state_value))
        self.style().unpolish(self)
        self.style().polish(self)

        label = STATE_NAMES.get(state_value, f"State {state_value}")
        pill_kind = "ok" if state_value == 0 else "active" if state_value == 1 else "warn"
        self.state.set(label, pill_kind)
        self.value.setText(f"value {status['actuator_values'][self.actuator]}")
        self.runtime.setText(f"runtime {format_ms(status['total_ms'][self.actuator])}")


class DashboardWindow(QMainWindow):
    def __init__(self, board_class: type[Board] = FluidRealityBoard) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality Dashboard")
        self.resize(856, 811)
        self.setMaximumWidth(876)
        self._cards: list[ActuatorCard] = []
        self._actuator_count = 0
        self._connected = False
        self._capabilities: dict[str, str] = {}
        self._board_detail = "Not connected"
        self._active_endpoint = ""
        self._status: dict[str, Any] | None = None
        self._selected_actuator = 0
        self._initialization_rows: list[dict[str, object]] = []
        self._fast_initialization_rows: list[dict[str, object]] = []
        self._diagnosis_rows: list[dict[str, object]] = []
        self._recovery_rows: list[dict[str, object]] = []
        self._recovery_marked_stages: set[int] = set()
        self._square_wave_rows: list[dict[str, object]] = []
        self._square_elapsed_s = 0.0
        self._active_wave_tool: str | None = None
        self._psu_on = False
        self._psc_on = False
        self._square_running = False
        self._connection_dialog: ConnectionDialog | None = None
        self._board_settings_dialog: BoardSettingsDialog | None = None
        self._bluetooth_config_dialog: BluetoothConfigDialog | None = None
        self._wifi_config_dialog: WifiConfigDialog | None = None
        self._network_config_dialog: NetworkConfigDialog | None = None
        self._security_config_dialog: SecurityEncryptionDialog | None = None
        self._firmware_update_dialog: FirmwareUpdateDialog | None = None

        self.worker = BoardWorker(board_class)
        self.worker.connected_changed.connect(self._on_connected_changed)
        self.worker.capabilities_ready.connect(self._on_capabilities_ready)
        self.worker.actuator_count_ready.connect(self._on_actuator_count_ready)
        self.worker.status_ready.connect(self._on_status_ready)
        self.worker.diagnosis_ready.connect(self._on_diagnosis_ready)
        self.worker.diagnosis_progress.connect(self._on_diagnosis_progress)
        self.worker.diagnosis_finished.connect(self._on_diagnosis_finished)
        self.worker.initialization_progress.connect(self._on_initialization_progress)
        self.worker.fast_init_progress.connect(self._on_fast_init_progress)
        self.worker.fast_init_ready.connect(self._on_fast_init_ready)
        self.worker.recovery_ready.connect(self._on_recovery_ready)
        self.worker.recovery_progress.connect(self._on_recovery_progress)
        self.worker.health_ready.connect(self._on_health_ready)
        self.worker.square_changed.connect(self._on_square_changed)
        self.worker.square_progress.connect(self._on_square_progress)
        self.worker.actuator_tool_finished.connect(self._on_actuator_tool_finished)
        self.worker.board_config_ready.connect(self._on_board_config_ready)
        self.worker.board_config_saved.connect(self._on_board_config_saved)
        self.worker.board_config_failed.connect(self._on_board_config_failed)
        self.worker.bluetooth_config_ready.connect(self._on_bluetooth_config_ready)
        self.worker.bluetooth_config_saved.connect(self._on_bluetooth_config_saved)
        self.worker.bluetooth_bonds_cleared.connect(self._on_bluetooth_bonds_cleared)
        self.worker.bluetooth_config_failed.connect(self._on_bluetooth_config_failed)
        self.worker.wifi_status_ready.connect(self._on_wifi_status_ready)
        self.worker.wifi_access_point_ready.connect(
            self._on_wifi_access_point_ready
        )
        self.worker.wifi_networks_ready.connect(self._on_wifi_networks_ready)
        self.worker.wifi_operation_failed.connect(self._on_wifi_operation_failed)
        self.worker.network_config_ready.connect(self._on_network_config_ready)
        self.worker.network_config_saved.connect(self._on_network_config_saved)
        self.worker.network_config_failed.connect(self._on_network_config_failed)
        self.worker.security_config_ready.connect(self._on_security_config_ready)
        self.worker.security_config_failed.connect(self._on_security_config_failed)
        self.worker.firmware_update_progress.connect(self._on_firmware_update_progress)
        self.worker.firmware_update_finished.connect(self._on_firmware_update_finished)
        self.worker.firmware_update_failed.connect(self._on_firmware_update_failed)
        self.worker.factory_reset_finished.connect(self._on_factory_reset_finished)
        self.worker.factory_reset_failed.connect(self._on_factory_reset_failed)
        self.worker.busy_changed.connect(self._on_busy_changed)
        self.worker.message.connect(self._log)
        self.worker.start()

        self._build_ui()

    def closeEvent(self, event: Any) -> None:
        self.worker.enqueue("disconnect")
        self.worker.requestInterruption()
        self.worker.wait(2000)
        super().closeEvent(event)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(10)

        self.dashboard_content = QWidget()
        self.dashboard_content.setObjectName("DashboardContent")
        self.dashboard_content.setMaximumWidth(840)
        self.dashboard_content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content_layout = QVBoxLayout(self.dashboard_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_stack = QVBoxLayout()
        brand_row = QHBoxLayout()
        self.logo_label = QLabel()
        self.logo_label.setObjectName("Logo")
        if LOGO_PATH.exists():
            pixmap = QPixmap(str(LOGO_PATH))
            self.logo_label.setPixmap(pixmap.scaledToWidth(180, Qt.SmoothTransformation))
        else:
            self.logo_label.setText("Fluid Reality")
            self.logo_label.setObjectName("AppTitle")

        brand_text = QVBoxLayout()
        title = QLabel("Dashboard")
        title.setObjectName("AppTitle")
        brand_text.addWidget(title)
        brand_row.addWidget(self.logo_label)
        brand_row.addSpacing(18)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        title_stack.addLayout(brand_row)

        title_row.addLayout(title_stack)
        title_row.addStretch()
        content_layout.addLayout(title_row)

        content_layout.addWidget(self._build_connection_bar())
        self.metrics_bar = self._build_metrics_bar()
        content_layout.addWidget(self.metrics_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.actuator_panel = self._build_actuator_panel()
        self.side_panel = self._build_side_panel()
        splitter.addWidget(self.actuator_panel)
        splitter.addWidget(self.side_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 616])
        content_layout.addWidget(splitter, 1)

        outer.addWidget(self.dashboard_content, 1, Qt.AlignTop)

        self.setStyleSheet(APP_STYLES)
        self._set_board_controls_enabled(False)

    def _build_connection_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar.setFixedHeight(54)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 7, 12, 7)

        heading = QLabel("Board connection")
        heading.setObjectName("ToolTitle")
        self.connection_hint = QLabel("Connect by Serial, Bluetooth, TCP, or TLS")
        self.connection_hint.setObjectName("ConnectionHint")
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("quietButton")
        self.connection_label = QLabel("Not connected")
        self.connection_label.setProperty("kind", "neutral")

        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn.clicked.connect(lambda: self.worker.enqueue("disconnect"))
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.hide()

        layout.addWidget(heading)
        layout.addSpacing(8)
        layout.addWidget(self.connection_hint)
        layout.addStretch()
        layout.addWidget(self.connect_btn)
        layout.addWidget(self.disconnect_btn)
        layout.addWidget(self.connection_label)
        return bar

    def _build_metrics_bar(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.psc_card = ToggleMetricCard(
            "Power",
            "ON",
            "OFF",
            on_color="#0050bd",
        )
        self.psc_card.toggled.connect(lambda checked: self.worker.enqueue("psc", checked))
        self.voltage_card = MetricCard("Voltage", "-", "V")
        self.current_card = MetricCard("Current", "-", "mA")

        for card in (
            self.psc_card,
            self.voltage_card,
            self.current_card,
        ):
            card.setMinimumWidth(0)
            card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            layout.addWidget(card, 1)
        return row

    def _build_actuator_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(210)
        panel.setMaximumWidth(220)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        label = QLabel("Actuators")
        label.setObjectName("SectionTitle")
        self.group_label = form_label("Group")
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.redetect_all_btn = QPushButton()
        configure_refresh_button(self.redetect_all_btn, "Redetect all actuators")
        self.redetect_all_btn.clicked.connect(
            lambda: self.worker.enqueue("detect_all")
        )
        self.group_label.setVisible(False)
        self.group_combo.setVisible(False)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(self.group_label)
        header.addWidget(self.group_combo)
        header.addWidget(self.redetect_all_btn)

        scroll = QScrollArea()
        self.actuator_scroll = scroll
        scroll.setObjectName("ActuatorScroll")
        scroll.viewport().setObjectName("ActuatorViewport")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        grid_host = QWidget()
        grid_host.setObjectName("ActuatorGridHost")
        self.actuator_grid = QGridLayout(grid_host)
        self.actuator_grid.setContentsMargins(0, 0, 0, 0)
        self.actuator_grid.setSpacing(7)
        self.actuator_grid.setColumnStretch(0, 1)

        scroll.setWidget(grid_host)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        return panel

    def _build_board_controls_panel(self) -> QWidget:
        controls = QFrame()
        controls.setObjectName("ControlsPanel")
        controls.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        controls.setMaximumWidth(285)
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        heading = QHBoxLayout()
        self.actuator_tools_title = QLabel("Actuator Tools")
        self.actuator_tools_title.setObjectName("SectionTitle")
        heading.addWidget(self.actuator_tools_title)
        heading.addStretch()
        layout.addLayout(heading)

        self.init_btn = QPushButton("Initialize")
        self.fast_init_btn = QPushButton("Fast Init")
        self.diag_btn = QPushButton("Diagnose")
        self.recover_btn = QPushButton("Recover")
        self.square_target_btn = QPushButton("Square Wave")
        for button in (
            self.init_btn,
            self.fast_init_btn,
            self.diag_btn,
            self.recover_btn,
            self.square_target_btn,
        ):
            button.setObjectName("ToolButton")
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            button.setMaximumWidth(255)
            button.setFixedHeight(40)
            layout.addWidget(button)
        layout.addStretch()

        self.tool_dialogs: list[ToolDialog] = []

        self.init_dialog = ToolDialog(
            "Initialize actuator",
            "A baseline current is measured first. The actuator is then driven with "
            "a 1 Hz bipolar square wave for 30 seconds at each voltage: ±25 V, "
            "±50 V, ±100 V, and ±200 V. Current is measured after every voltage "
            "change, but only positive-output readings are used for the current "
            "delta plot. The output is then "
            "switched off, safety is restored, and the actuator is diagnosed.",
            self,
        )
        self.init_dialog.setMinimumWidth(650)
        init_plot_title = QLabel("Drive voltage — rolling 30-second view")
        init_plot_title.setObjectName("ToolTitle")
        self.init_voltage_plot = InitializationVoltagePlot()
        init_current_plot_title = QLabel("Current delta — rolling 30-second view")
        init_current_plot_title.setObjectName("ToolTitle")
        self.init_current_delta_plot = InitializationCurrentDeltaPlot()
        self.init_progress = QProgressBar()
        self.init_progress.setRange(0, 240)
        self.init_progress.setValue(0)
        self.init_progress.setTextVisible(False)
        self.init_progress.setObjectName("InitProgress")
        self.init_elapsed_label = QLabel("Elapsed 0 / 120 s")
        self.init_elapsed_label.setObjectName("Diagnosis")
        self.init_run_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaPlay, "Start initialization"
        )
        self.init_run_btn.clicked.connect(self._start_initialization)
        self.init_stop_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaStop, "Stop initialization"
        )
        self.init_stop_btn.setEnabled(False)
        self.init_stop_btn.clicked.connect(lambda: self._stop_wave_tool("init"))
        self.init_save_csv_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_DialogSaveButton, "Save initialization data"
        )
        self.init_save_csv_btn.setEnabled(False)
        self.init_save_csv_btn.clicked.connect(self._save_initialization_csv)
        self.init_dialog.content.addLayout(
            tool_action_bar(
                self.init_run_btn, self.init_stop_btn, self.init_save_csv_btn
            )
        )
        self.init_dialog.content.addWidget(init_plot_title)
        self.init_dialog.content.addWidget(self.init_voltage_plot)
        self.init_dialog.content.addWidget(init_current_plot_title)
        self.init_dialog.content.addWidget(self.init_current_delta_plot)
        self.init_dialog.content.addWidget(self.init_progress)
        self.init_dialog.content.addWidget(self.init_elapsed_label)

        self.fast_init_dialog = ToolDialog(
            "Fast Init",
            "Fast Init measures a zero-output baseline, applies positive and "
            "negative voltage, and compares the positive-output current with the "
            "baseline. It adjusts the next voltage toward the selected current "
            "delta and repeats for up to 60 seconds before switching the output "
            "off and diagnosing the actuator.",
            self,
        )
        self.fast_init_dialog.setMinimumWidth(650)
        self.fast_init_target_spin = QDoubleSpinBox()
        self.fast_init_target_spin.setRange(0.01, Board.error_delta_ma - 0.01)
        self.fast_init_target_spin.setDecimals(2)
        self.fast_init_target_spin.setSingleStep(0.10)
        self.fast_init_target_spin.setValue(2.00)
        self.fast_init_target_spin.setSuffix(" mA")
        self.fast_init_target_spin.setFixedWidth(100)
        fast_voltage_plot_title = QLabel(
            "Positive drive voltage — rolling 30-second view"
        )
        fast_voltage_plot_title.setObjectName("ToolTitle")
        self.fast_init_voltage_plot = FastInitializationVoltagePlot()
        fast_current_plot_title = QLabel("Current delta — rolling 30-second view")
        fast_current_plot_title.setObjectName("ToolTitle")
        self.fast_init_current_delta_plot = InitializationCurrentDeltaPlot()
        self.fast_init_progress = QProgressBar()
        self.fast_init_progress.setRange(0, 60)
        self.fast_init_progress.setValue(0)
        self.fast_init_progress.setTextVisible(False)
        self.fast_init_progress.setObjectName("InitProgress")
        self.fast_init_status_label = QLabel("Target 2.00 mA / max 60 s")
        self.fast_init_status_label.setObjectName("Diagnosis")
        self.fast_init_status_label.setWordWrap(True)
        self.fast_init_run_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaPlay, "Start Fast Init"
        )
        self.fast_init_run_btn.clicked.connect(self._start_fast_initialization)
        self.fast_init_stop_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaStop, "Stop Fast Init"
        )
        self.fast_init_stop_btn.setEnabled(False)
        self.fast_init_stop_btn.clicked.connect(
            lambda: self._stop_wave_tool("fast_init")
        )
        self.fast_init_save_csv_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_DialogSaveButton, "Save Fast Init data"
        )
        self.fast_init_save_csv_btn.setEnabled(False)
        self.fast_init_save_csv_btn.clicked.connect(self._save_fast_initialization_csv)
        fast_controls = QHBoxLayout()
        fast_controls.setSpacing(8)
        fast_controls.addWidget(form_label("Target current"))
        fast_controls.addWidget(self.fast_init_target_spin)
        fast_controls.addStretch()
        fast_controls.addWidget(self.fast_init_run_btn)
        fast_controls.addWidget(self.fast_init_stop_btn)
        fast_controls.addWidget(self.fast_init_save_csv_btn)
        self.fast_init_dialog.content.addLayout(fast_controls)
        self.fast_init_dialog.content.addWidget(fast_voltage_plot_title)
        self.fast_init_dialog.content.addWidget(self.fast_init_voltage_plot)
        self.fast_init_dialog.content.addWidget(fast_current_plot_title)
        self.fast_init_dialog.content.addWidget(self.fast_init_current_delta_plot)
        self.fast_init_dialog.content.addWidget(self.fast_init_progress)
        self.fast_init_dialog.content.addWidget(self.fast_init_status_label)

        self.diag_dialog = ToolDialog(
            "Diagnose actuator",
            "Diagnose first runs three warmup cycles at full voltage: one second "
            "forward and one second reverse. It then increases positive voltage "
            "from 0 to 200 V in 10 V steps and plots the measured current.",
            self,
        )
        self.diag_dialog.setMinimumWidth(650)
        diagnosis_plot_title = QLabel("Voltage-current sweep")
        diagnosis_plot_title.setObjectName("ToolTitle")
        self.diagnosis_plot = DiagnosisVoltageCurrentPlot()
        self.diagnosis_progress_bar = QProgressBar()
        self.diagnosis_progress_bar.setRange(0, 27)
        self.diagnosis_progress_bar.setValue(0)
        self.diagnosis_progress_bar.setTextVisible(False)
        self.diagnosis_progress_bar.setObjectName("InitProgress")
        self.diagnosis_label = QLabel("No diagnosis yet")
        self.diagnosis_label.setObjectName("Diagnosis")
        self.diagnosis_label.setWordWrap(True)
        self.diag_run_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaPlay, "Start diagnosis"
        )
        self.diag_run_btn.clicked.connect(self._start_diagnosis)
        self.diag_stop_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaStop, "Stop diagnosis"
        )
        self.diag_stop_btn.setEnabled(False)
        self.diag_stop_btn.clicked.connect(lambda: self._stop_wave_tool("diagnose"))
        self.diag_save_csv_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_DialogSaveButton, "Save diagnosis data"
        )
        self.diag_save_csv_btn.setEnabled(False)
        self.diag_save_csv_btn.clicked.connect(self._save_diagnosis_csv)
        self.diag_dialog.content.addLayout(
            tool_action_bar(
                self.diag_run_btn, self.diag_stop_btn, self.diag_save_csv_btn
            )
        )
        self.diag_dialog.content.addWidget(diagnosis_plot_title)
        self.diag_dialog.content.addWidget(self.diagnosis_plot)
        self.diag_dialog.content.addWidget(self.diagnosis_progress_bar)
        self.diag_dialog.content.addWidget(self.diagnosis_label)

        self.recover_dialog = ToolDialog(
            "Recover actuator",
            "Recover uses the same bipolar voltage stages as Initialize. It stays "
            "at each voltage until the positive current delta remains at or below "
            "90% of the upper diagnostic error-threshold curve for three "
            "consecutive seconds, then advances automatically.",
            self,
        )
        self.recover_dialog.setMinimumWidth(650)
        recover_voltage_title = QLabel("Drive voltage — rolling 30-second view")
        recover_voltage_title.setObjectName("ToolTitle")
        self.recover_voltage_plot = InitializationVoltagePlot()
        recover_current_title = QLabel("Current delta — rolling 30-second view")
        recover_current_title.setObjectName("ToolTitle")
        self.recover_current_delta_plot = InitializationCurrentDeltaPlot()
        self.recover_progress = QProgressBar()
        self.recover_progress.setRange(0, len(Board.initialization_stages_v))
        self.recover_progress.setValue(0)
        self.recover_progress.setTextVisible(False)
        self.recover_progress.setObjectName("InitProgress")
        self.recovery_status_label = QLabel("Not started")
        self.recovery_status_label.setObjectName("Diagnosis")
        self.recovery_status_label.setWordWrap(True)
        self.recover_run_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaPlay, "Start recovery"
        )
        self.recover_run_btn.clicked.connect(self._start_recovery)
        self.recover_stop_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaStop, "Stop recovery"
        )
        self.recover_stop_btn.setEnabled(False)
        self.recover_stop_btn.clicked.connect(lambda: self._stop_wave_tool("recover"))
        self.recover_save_csv_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_DialogSaveButton, "Save recovery data"
        )
        self.recover_save_csv_btn.setEnabled(False)
        self.recover_save_csv_btn.clicked.connect(self._save_recovery_csv)
        self.recover_dialog.content.addLayout(
            tool_action_bar(
                self.recover_run_btn,
                self.recover_stop_btn,
                self.recover_save_csv_btn,
            )
        )
        self.recover_dialog.content.addWidget(recover_voltage_title)
        self.recover_dialog.content.addWidget(self.recover_voltage_plot)
        self.recover_dialog.content.addWidget(recover_current_title)
        self.recover_dialog.content.addWidget(self.recover_current_delta_plot)
        self.recover_dialog.content.addWidget(self.recover_progress)
        self.recover_dialog.content.addWidget(self.recovery_status_label)

        self.square_dialog = ToolDialog(
            "Square Wave",
            "Square Wave measures one zero-output current baseline, applies full "
            "positive output for one second, measures the positive-output current "
            "delta, then applies full reverse voltage for exactly one second. These "
            "equal positive and reverse phases repeat until Stop is pressed.",
            self,
        )
        self.square_dialog.setMinimumWidth(650)
        square_voltage_plot_title = QLabel("Drive voltage — rolling 30-second view")
        square_voltage_plot_title.setObjectName("ToolTitle")
        self.square_voltage_plot = InitializationVoltagePlot()
        square_current_plot_title = QLabel("Current delta — rolling 30-second view")
        square_current_plot_title.setObjectName("ToolTitle")
        self.square_current_delta_plot = InitializationCurrentDeltaPlot()
        self.square_progress_bar = QProgressBar()
        self.square_progress_bar.setRange(0, 1)
        self.square_progress_bar.setValue(0)
        self.square_progress_bar.setTextVisible(False)
        self.square_progress_bar.setObjectName("InitProgress")
        self.square_status_label = QLabel("Not started")
        self.square_status_label.setObjectName("Diagnosis")
        self.square_status_label.setWordWrap(True)
        self.square_save_csv_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_DialogSaveButton, "Save Square Wave data"
        )
        self.square_save_csv_btn.setEnabled(False)
        self.square_save_csv_btn.clicked.connect(self._save_square_wave_csv)
        self.square_start_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaPlay, "Start Square Wave"
        )
        self.square_stop_btn = configure_tool_action_button(
            QPushButton(), QStyle.SP_MediaStop, "Stop Square Wave"
        )
        self.square_stop_btn.setEnabled(False)
        self.square_start_btn.clicked.connect(self._start_square_wave)
        self.square_stop_btn.clicked.connect(lambda: self._stop_wave_tool("square"))
        self.square_dialog.content.addLayout(
            tool_action_bar(
                self.square_start_btn,
                self.square_stop_btn,
                self.square_save_csv_btn,
            )
        )
        self.square_dialog.content.addWidget(square_voltage_plot_title)
        self.square_dialog.content.addWidget(self.square_voltage_plot)
        self.square_dialog.content.addWidget(square_current_plot_title)
        self.square_dialog.content.addWidget(self.square_current_delta_plot)
        self.square_dialog.content.addWidget(self.square_progress_bar)
        self.square_dialog.content.addWidget(self.square_status_label)

        self.tool_dialogs.extend(
            [
                self.init_dialog,
                self.fast_init_dialog,
                self.diag_dialog,
                self.recover_dialog,
                self.square_dialog,
            ]
        )
        self.init_btn.clicked.connect(lambda: self._show_tool_dialog(self.init_dialog))
        self.fast_init_btn.clicked.connect(
            lambda: self._show_tool_dialog(self.fast_init_dialog)
        )
        self.diag_btn.clicked.connect(lambda: self._show_tool_dialog(self.diag_dialog))
        self.recover_btn.clicked.connect(
            lambda: self._show_tool_dialog(self.recover_dialog)
        )
        self.square_target_btn.clicked.connect(
            lambda: self._show_tool_dialog(self.square_dialog)
        )
        return controls

    def _show_tool_dialog(self, dialog: ToolDialog) -> None:
        dialog.setWindowTitle(
            f"{dialog.windowTitle().split(' — ', 1)[0]} — Actuator {self._selected_actuator}"
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _set_active_wave_tool(self, tool: str | None) -> None:
        self._active_wave_tool = tool
        self._apply_wave_tool_button_state()

    def _apply_wave_tool_button_state(self) -> None:
        controls = {
            "init": (
                self.init_run_btn,
                self.init_stop_btn,
                self.init_save_csv_btn,
                bool(self._initialization_rows),
            ),
            "fast_init": (
                self.fast_init_run_btn,
                self.fast_init_stop_btn,
                self.fast_init_save_csv_btn,
                bool(self._fast_initialization_rows),
            ),
            "diagnose": (
                self.diag_run_btn,
                self.diag_stop_btn,
                self.diag_save_csv_btn,
                bool(self._diagnosis_rows),
            ),
            "recover": (
                self.recover_run_btn,
                self.recover_stop_btn,
                self.recover_save_csv_btn,
                bool(self._recovery_rows),
            ),
            "square": (
                self.square_start_btn,
                self.square_stop_btn,
                self.square_save_csv_btn,
                bool(self._square_wave_rows),
            ),
        }
        if self._active_wave_tool is not None:
            for key, (play, stop, save, _has_data) in controls.items():
                play.setEnabled(False)
                stop.setEnabled(key == self._active_wave_tool)
                save.setEnabled(False)
            self.fast_init_target_spin.setEnabled(False)
            for key, dialog in (
                ("init", self.init_dialog),
                ("fast_init", self.fast_init_dialog),
                ("diagnose", self.diag_dialog),
                ("recover", self.recover_dialog),
                ("square", self.square_dialog),
            ):
                dialog.close_button.setEnabled(key != self._active_wave_tool)
            return

        self.fast_init_target_spin.setEnabled(True)
        for dialog in (
            self.init_dialog,
            self.fast_init_dialog,
            self.diag_dialog,
            self.recover_dialog,
            self.square_dialog,
        ):
            dialog.close_button.setEnabled(True)
        for _key, (_play, stop, save, has_data) in controls.items():
            stop.setEnabled(False)
            save.setEnabled(has_data)

    def _stop_wave_tool(self, tool: str) -> None:
        if self._active_wave_tool != tool:
            return
        if tool == "square":
            self.square_status_label.setText("Stopping after the current phase…")
            self.worker.enqueue("square_stop")
            return
        if tool == "init":
            self.init_elapsed_label.setText("Stopping after the current value…")
        elif tool == "diagnose":
            self.diagnosis_label.setText("Stopping after the current value…")
        elif tool == "recover":
            self.recovery_status_label.setText("Stopping after the current value…")
        else:
            self.fast_init_status_label.setText("Stopping after the current value…")
        self.worker.request_actuator_tool_stop()

    def _start_diagnosis(self) -> None:
        self.diagnosis_plot.reset()
        self.diagnosis_progress_bar.setRange(0, 27)
        self.diagnosis_progress_bar.setValue(0)
        self._diagnosis_rows.clear()
        self.diagnosis_label.setText("Starting warmup — cycle 1 of 3")
        self.worker.prepare_actuator_tool()
        self._set_active_wave_tool("diagnose")
        self.worker.enqueue("diagnose", self._selected_actuator)

    def _start_recovery(self) -> None:
        self.recover_progress.setRange(0, len(Board.initialization_stages_v))
        self.recover_progress.setValue(0)
        self.recover_voltage_plot.reset(30.0)
        self.recover_current_delta_plot.reset(30.0)
        self._recovery_rows.clear()
        self._recovery_marked_stages.clear()
        self.recovery_status_label.setText("Preparing automatic recovery")
        self.worker.prepare_actuator_tool()
        self._set_active_wave_tool("recover")
        self.worker.enqueue("recover", self._selected_actuator)

    def _save_recovery_csv(self) -> None:
        if not self._recovery_rows:
            return
        actuator = int(self._recovery_rows[0]["actuator"])
        default_name = (
            f"actuator-{actuator:02d}-recovery-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Recovery Data",
            str(Path.home() / default_name),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            self._write_recovery_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not save the recovery data:\n{exc}",
            )
            return
        self._log(f"Recovery data saved to {path}", "ok")

    def _write_recovery_csv(self, path: Path) -> None:
        fieldnames = (
            "actuator",
            "elapsed_s",
            "stage",
            "phase",
            "voltage_v",
            "current_delta_ma",
            "target_current_delta_ma",
        )
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._recovery_rows)

    def _save_diagnosis_csv(self) -> None:
        if not self._diagnosis_rows:
            return
        actuator = int(self._diagnosis_rows[0]["actuator"])
        default_name = (
            f"actuator-{actuator:02d}-diagnosis-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Diagnosis Data",
            str(Path.home() / default_name),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            self._write_diagnosis_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not save the diagnosis data:\n{exc}",
            )
            return
        self._log(f"Diagnosis data saved to {path}", "ok")

    def _write_diagnosis_csv(self, path: Path) -> None:
        fieldnames = ("actuator", "voltage_v", "current_ma")
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._diagnosis_rows)

    def _start_initialization(self) -> None:
        total_s = int(
            Board.initialization_stage_duration_s
            * len(Board.initialization_stages_v)
        )
        self.init_progress.setRange(0, total_s)
        self.init_progress.setValue(0)
        self.init_voltage_plot.reset(total_s)
        self.init_current_delta_plot.reset(total_s)
        self._initialization_rows.clear()
        self.init_elapsed_label.setText(f"Elapsed 0 / {total_s} s — preparing")
        self.worker.prepare_actuator_tool()
        self._set_active_wave_tool("init")
        self.worker.enqueue("init", self._selected_actuator)

    def _save_initialization_csv(self) -> None:
        if not self._initialization_rows:
            return
        actuator = int(self._initialization_rows[0]["actuator"])
        default_name = (
            f"actuator-{actuator:02d}-initialization-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Initialization Data",
            str(Path.home() / default_name),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            self._write_initialization_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not save the initialization data:\n{exc}",
            )
            return
        self._log(f"Initialization data saved to {path}", "ok")

    def _write_initialization_csv(self, path: Path) -> None:
        fieldnames = (
            "actuator",
            "elapsed_s",
            "stage",
            "phase",
            "voltage_v",
            "current_delta_ma",
        )
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._initialization_rows)

    def _start_fast_initialization(self) -> None:
        duration_s = 60
        self.fast_init_progress.setRange(0, duration_s)
        self.fast_init_progress.setValue(0)
        self.fast_init_voltage_plot.reset(duration_s)
        self.fast_init_current_delta_plot.reset(duration_s)
        self._fast_initialization_rows.clear()
        target_delta_ma = self.fast_init_target_spin.value()
        self.fast_init_status_label.setText(
            f"Preparing — target {target_delta_ma:.2f} mA — max {duration_s} s"
        )
        self.worker.prepare_actuator_tool()
        self._set_active_wave_tool("fast_init")
        self.worker.enqueue(
            "fast_init",
            self._selected_actuator,
            target_delta_ma,
        )

    def _save_fast_initialization_csv(self) -> None:
        if not self._fast_initialization_rows:
            return
        actuator = int(self._fast_initialization_rows[0]["actuator"])
        default_name = (
            f"actuator-{actuator:02d}-fast-init-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Fast Init Data",
            str(Path.home() / default_name),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            self._write_fast_initialization_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not save the Fast Init data:\n{exc}",
            )
            return
        self._log(f"Fast Init data saved to {path}", "ok")

    def _write_fast_initialization_csv(self, path: Path) -> None:
        fieldnames = (
            "actuator",
            "elapsed_s",
            "phase",
            "voltage_v",
            "current_delta_ma",
            "target_delta_ma",
            "next_voltage_v",
            "status",
        )
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._fast_initialization_rows)

    def _start_square_wave(self) -> None:
        self._square_elapsed_s = 0.0
        self._square_wave_rows.clear()
        self.square_voltage_plot.reset(30.0)
        self.square_current_delta_plot.reset(30.0)
        self.square_progress_bar.setRange(0, 0)
        self.square_status_label.setText("Preparing")
        self._set_active_wave_tool("square")
        self.worker.enqueue("square_start", [self._selected_actuator])

    def _save_square_wave_csv(self) -> None:
        if not self._square_wave_rows:
            return
        actuator = int(self._square_wave_rows[0]["actuator"])
        default_name = (
            f"actuator-{actuator:02d}-square-wave-"
            f"{time.strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Square Wave Data",
            str(Path.home() / default_name),
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            self._write_square_wave_csv(Path(path))
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not save the Square Wave data:\n{exc}",
            )
            return
        self._log(f"Square Wave data saved to {path}", "ok")

    def _write_square_wave_csv(self, path: Path) -> None:
        fieldnames = (
            "actuator",
            "elapsed_s",
            "phase",
            "voltage_v",
            "current_delta_ma",
            "status",
        )
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self._square_wave_rows)

    def _build_side_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.tool_sections = QHBoxLayout()
        self.tool_sections.setSpacing(12)
        self.actuator_tools_panel = self._build_board_controls_panel()
        self.board_tools_panel = self._build_board_tools_panel()
        common_tool_height = max(
            self.actuator_tools_panel.sizeHint().height(),
            self.board_tools_panel.sizeHint().height(),
        )
        for tool_panel in (self.actuator_tools_panel, self.board_tools_panel):
            tool_panel.setFixedSize(285, common_tool_height)
        self.tool_sections.addWidget(self.actuator_tools_panel, 1, Qt.AlignTop)
        self.tool_sections.addWidget(self.board_tools_panel, 1, Qt.AlignTop)
        self.tool_sections.addStretch()
        layout.addLayout(self.tool_sections)

        log_header = QHBoxLayout()
        log_title = QLabel("Event Log")
        log_title.setObjectName("SectionTitle")
        self.verbose_log_checkbox = LabeledToggle("Verbose")
        self.verbose_log_checkbox.setChecked(False)
        self.save_log_btn = QPushButton("Save Log")
        self.save_log_btn.clicked.connect(self._save_event_log)
        log_header.addWidget(log_title)
        log_header.addStretch()
        log_header.addWidget(self.verbose_log_checkbox)
        log_header.addWidget(self.save_log_btn)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)

        layout.addLayout(log_header)
        layout.addWidget(self.log, 1)
        return panel

    def _build_board_tools_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ControlsPanel")
        panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        panel.setMaximumWidth(285)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("Board Tools")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        self.board_settings_btn = QPushButton("Board Settings")
        self.bluetooth_config_btn = QPushButton("Bluetooth Config")
        self.wifi_config_btn = QPushButton("Wi-Fi Config")
        self.network_config_btn = QPushButton("Network Config")
        # Qt uses a single ampersand as a mnemonic marker; double it so the
        # visible button label contains the literal character.
        self.security_config_btn = QPushButton("Security && Encryption")
        self.security_config_btn.setAccessibleName("Security & Encryption")
        self.fluid_mesh_btn = QPushButton("Fluid Mesh")
        self.firmware_update_btn = QPushButton("Update Firmware")
        self.factory_reset_btn = QPushButton("Factory Reset")
        for button in (
            self.board_settings_btn,
            self.bluetooth_config_btn,
            self.wifi_config_btn,
            self.network_config_btn,
            self.security_config_btn,
            self.fluid_mesh_btn,
            self.firmware_update_btn,
            self.factory_reset_btn,
        ):
            button.setObjectName("ToolButton")
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            button.setMaximumWidth(255)
            button.setFixedHeight(40)
            layout.addWidget(button)
        layout.addStretch()

        self.board_settings_btn.clicked.connect(self._show_board_settings)
        self.bluetooth_config_btn.clicked.connect(self._show_bluetooth_config)
        self.wifi_config_btn.clicked.connect(self._show_wifi_config)
        self.network_config_btn.clicked.connect(self._show_network_config)
        self.security_config_btn.clicked.connect(self._show_security_config)
        self.fluid_mesh_btn.clicked.connect(self._show_fluid_mesh)
        self.fluid_mesh_btn.setEnabled(False)
        self.fluid_mesh_btn.setToolTip(
            "Connect a board that reports Fluid Mesh support."
        )
        self.firmware_update_btn.clicked.connect(self._show_firmware_update)
        self.firmware_update_btn.setEnabled(False)
        self.firmware_update_btn.setToolTip(
            "Connect a board that reports firmware-update support."
        )
        self.factory_reset_btn.clicked.connect(self._confirm_factory_reset)
        self.factory_reset_btn.setEnabled(False)
        self.factory_reset_btn.setToolTip(
            "Connect over USB serial to restore factory settings."
        )
        return panel

    def _confirm_factory_reset(self) -> None:
        if not self._connected or not self._active_endpoint or "://" in self._active_endpoint:
            QMessageBox.information(
                self,
                "Factory Reset",
                "Connect to the board over USB serial to restore factory settings.",
            )
            return

        if not self._show_factory_reset_warning():
            return

        self.factory_reset_btn.setEnabled(False)
        self.worker.enqueue("factory_reset")

    def _show_factory_reset_warning(self) -> bool:
        return FactoryResetDialog(self).exec() == QDialog.Accepted

    def _show_board_settings(self) -> None:
        if not self._connected:
            QMessageBox.information(self, "Board Settings", "Connect to a board first.")
            return
        if self._board_settings_dialog is not None:
            self._board_settings_dialog.show()
            self._board_settings_dialog.raise_()
            self._board_settings_dialog.activateWindow()
            return

        initial = dict(self._status.get("config", {})) if self._status else {}
        detection_supported = (
            "DET" in self._capabilities and self._capabilities["DET"] != "0"
        )
        vt_supported = self._capabilities.get("VT") == "1"
        dialog = BoardSettingsDialog(
            initial, detection_supported, self, vt_supported=vt_supported
        )
        self._board_settings_dialog = dialog
        dialog.finished.connect(lambda _result: self._clear_board_settings_dialog(dialog))
        dialog.save_requested.connect(
            lambda values: self.worker.enqueue("write_board_config", values)
        )
        dialog.show()
        self.worker.enqueue("read_board_config")

    def _clear_board_settings_dialog(self, dialog: BoardSettingsDialog) -> None:
        if self._board_settings_dialog is dialog:
            self._board_settings_dialog = None

    def _on_board_config_ready(self, config: dict[str, Any]) -> None:
        dialog = self._board_settings_dialog
        if dialog is None:
            return
        dialog.set_config(config)
        dialog.status_label.setStyleSheet("")
        dialog.set_loading(False)

    def _on_board_config_saved(self, config: dict[str, Any]) -> None:
        dialog = self._board_settings_dialog
        if dialog is not None:
            dialog.set_config(config)
            dialog.accept()

    def _on_board_config_failed(self, message: str) -> None:
        dialog = self._board_settings_dialog
        if dialog is not None:
            dialog.show_error(f"Could not update board settings: {message}")

    def _show_bluetooth_config(self) -> None:
        if not self._connected:
            QMessageBox.information(
                self, "Bluetooth Config", "Connect to a board first."
            )
            return
        if self._capabilities.get("BLT") in {None, "0"}:
            QMessageBox.information(
                self,
                "Bluetooth Config",
                "The connected board does not report Bluetooth configuration support.",
            )
            return
        if self._bluetooth_config_dialog is not None:
            self._bluetooth_config_dialog.show()
            self._bluetooth_config_dialog.raise_()
            self._bluetooth_config_dialog.activateWindow()
            return

        dialog = BluetoothConfigDialog(self)
        self._bluetooth_config_dialog = dialog
        dialog.finished.connect(
            lambda _result: self._clear_bluetooth_config_dialog(dialog)
        )
        dialog.save_requested.connect(
            lambda values: self.worker.enqueue("write_bluetooth_config", values)
        )
        dialog.clear_bonds_requested.connect(
            lambda: self.worker.enqueue("clear_bluetooth_bonds")
        )
        dialog.show()
        self.worker.enqueue("read_bluetooth_config")

    def _clear_bluetooth_config_dialog(
        self, dialog: BluetoothConfigDialog
    ) -> None:
        if self._bluetooth_config_dialog is dialog:
            self._bluetooth_config_dialog = None

    def _on_bluetooth_config_ready(self, fields: dict[str, str]) -> None:
        if self._bluetooth_config_dialog is not None:
            self._bluetooth_config_dialog.set_values(fields)

    def _on_bluetooth_config_saved(self, fields: dict[str, str]) -> None:
        if self._bluetooth_config_dialog is not None:
            self._bluetooth_config_dialog.show_saved(fields)

    def _on_bluetooth_bonds_cleared(self, fields: dict[str, str]) -> None:
        if self._bluetooth_config_dialog is not None:
            self._bluetooth_config_dialog.show_bonds_cleared(fields)

    def _on_bluetooth_config_failed(self, message: str) -> None:
        if self._bluetooth_config_dialog is not None:
            self._bluetooth_config_dialog.show_error(message)

    def _show_firmware_update(self) -> None:
        if not self._connected or self._capabilities.get("FWU") != "1":
            QMessageBox.information(
                self,
                "Update Firmware",
                "Firmware update is not supported by the connected board.",
            )
            return
        if self._active_endpoint.lower().startswith("ble://"):
            QMessageBox.information(
                self,
                "Update Firmware",
                "Connect over USB serial or TCP/TLS to update firmware.",
            )
            return
        if self._firmware_update_dialog is not None:
            self._firmware_update_dialog.show()
            self._firmware_update_dialog.raise_()
            self._firmware_update_dialog.activateWindow()
            return
        dialog = FirmwareUpdateDialog(self)
        self._firmware_update_dialog = dialog
        dialog.finished.connect(lambda _result: self._clear_firmware_update_dialog(dialog))
        dialog.update_requested.connect(
            lambda path: self.worker.enqueue("firmware_update", path)
        )
        dialog.abort_requested.connect(self.worker.request_firmware_update_abort)
        dialog.show()

    def _clear_firmware_update_dialog(self, dialog: FirmwareUpdateDialog) -> None:
        if self._firmware_update_dialog is dialog:
            self._firmware_update_dialog = None

    def _on_firmware_update_progress(self, written: int, total: int) -> None:
        if self._firmware_update_dialog is not None:
            self._firmware_update_dialog.set_progress(written, total)

    def _on_firmware_update_finished(self, sha256: str, version: str) -> None:
        if self._firmware_update_dialog is not None:
            self._firmware_update_dialog.show_complete(sha256, version)

    def _on_firmware_update_failed(self, message: str) -> None:
        if self._firmware_update_dialog is not None:
            self._firmware_update_dialog.show_error(message)

    def _on_factory_reset_finished(self) -> None:
        self._on_capabilities_ready(self._capabilities)

    def _on_factory_reset_failed(self, message: str) -> None:
        self._on_capabilities_ready(self._capabilities)
        QMessageBox.critical(self, "Factory Reset", message)

    def _show_wifi_config(self) -> None:
        if not self._connected:
            QMessageBox.information(self, "Wi-Fi Config", "Connect to a board first.")
            return
        if self._capabilities.get("WIFI") in {None, "0"}:
            QMessageBox.information(
                self, "Wi-Fi Config", "The connected board does not report Wi-Fi support."
            )
            return
        if self._wifi_config_dialog is not None:
            self._wifi_config_dialog.show()
            self._wifi_config_dialog.raise_()
            self._wifi_config_dialog.activateWindow()
            return

        dialog = WifiConfigDialog(
            self,
            access_point_supported=self._capabilities.get("AP") not in {None, "0"},
        )
        self._wifi_config_dialog = dialog
        dialog.set_hardware_safe(self._network_changes_are_safe())
        dialog.finished.connect(lambda _result: self._clear_wifi_config_dialog(dialog))
        dialog.wifi_enabled_requested.connect(
            lambda enabled: self.worker.enqueue("wifi_enable", enabled)
        )
        dialog.scan_requested.connect(lambda: self.worker.enqueue("wifi_scan"))
        dialog.join_requested.connect(
            lambda network, password: self.worker.enqueue(
                "wifi_join", network, password
            )
        )
        dialog.disconnect_requested.connect(
            lambda: self.worker.enqueue("wifi_disconnect")
        )
        dialog.mode_requested.connect(
            lambda mode: self.worker.enqueue("wifi_mode", mode)
        )
        dialog.access_point_config_requested.connect(
            lambda ssid, password, channel: self.worker.enqueue(
                "wifi_ap_config", ssid, password, channel
            )
        )
        dialog.show()
        self.worker.enqueue("wifi_status")
        if self._capabilities.get("AP") not in {None, "0"}:
            self.worker.enqueue("wifi_ap_status")
        dialog.start_automatic_scan()

    def _clear_wifi_config_dialog(self, dialog: WifiConfigDialog) -> None:
        if self._wifi_config_dialog is dialog:
            self._wifi_config_dialog = None

    def _on_wifi_status_ready(self, fields: dict[str, str]) -> None:
        dialog = self._wifi_config_dialog
        if dialog is not None:
            dialog.status_label.setStyleSheet("")
            dialog.set_status(fields)

    def _on_wifi_networks_ready(self, networks: list[WifiNetwork]) -> None:
        dialog = self._wifi_config_dialog
        if dialog is not None:
            dialog.status_label.setStyleSheet("")
            dialog.set_networks(networks)

    def _on_wifi_access_point_ready(self, fields: dict[str, str]) -> None:
        dialog = self._wifi_config_dialog
        if dialog is not None:
            dialog.status_label.setStyleSheet("")
            dialog.set_access_point_status(fields)

    def _on_wifi_operation_failed(self, message: str) -> None:
        dialog = self._wifi_config_dialog
        if dialog is not None:
            dialog.show_error(message)

    def _network_changes_are_safe(self) -> bool:
        if self._psu_on:
            return False
        if not self._status:
            return True
        return all(int(state) == 0 for state in self._status.get("actuator_states", ()))

    def _show_network_config(self) -> None:
        if not self._connected:
            QMessageBox.information(self, "Network Config", "Connect to a board first.")
            return
        if self._capabilities.get("NET") in {None, "0"}:
            QMessageBox.information(
                self,
                "Network Config",
                "The connected board does not report network configuration support.",
            )
            return
        if self._network_config_dialog is not None:
            self._network_config_dialog.show()
            self._network_config_dialog.raise_()
            self._network_config_dialog.activateWindow()
            return

        connected_over_network = self._active_endpoint.lower().startswith(
            ("tcp://", "tls://")
        )
        dialog = NetworkConfigDialog(
            connected_over_network=connected_over_network, parent=self
        )
        self._network_config_dialog = dialog
        dialog.set_hardware_safe(self._network_changes_are_safe())
        dialog.finished.connect(
            lambda _result: self._clear_network_config_dialog(dialog)
        )
        dialog.save_requested.connect(
            lambda values: self.worker.enqueue("write_network_config", values)
        )
        dialog.show()
        self.worker.enqueue("read_network_config")

    def _clear_network_config_dialog(self, dialog: NetworkConfigDialog) -> None:
        if self._network_config_dialog is dialog:
            self._network_config_dialog = None

    def _on_network_config_ready(
        self, fields: dict[str, str], interfaces: tuple[str, ...], scoped: bool
    ) -> None:
        dialog = self._network_config_dialog
        if dialog is not None:
            dialog.set_values(fields, interfaces, scoped)

    def _on_network_config_saved(
        self, fields: dict[str, str], interfaces: tuple[str, ...], scoped: bool
    ) -> None:
        dialog = self._network_config_dialog
        if dialog is not None:
            dialog.set_values(fields, interfaces, scoped)
            dialog.show_applied_status(fields)

    def _on_network_config_failed(self, message: str) -> None:
        dialog = self._network_config_dialog
        if dialog is not None:
            dialog.show_error(f"Could not update network settings: {message}")

    def _show_security_config(self) -> None:
        if not self._connected:
            QMessageBox.information(
                self, "Security & Encryption", "Connect to a board first."
            )
            return
        auth_supported = self._capabilities.get("AUTH") not in {None, "0"}
        tls_supported = self._capabilities.get("TLS") not in {None, "0"}
        if not auth_supported and not tls_supported:
            QMessageBox.information(
                self,
                "Security & Encryption",
                "The connected board does not report access-token or TLS support.",
            )
            return
        if self._security_config_dialog is not None:
            self._security_config_dialog.show()
            self._security_config_dialog.raise_()
            self._security_config_dialog.activateWindow()
            return
        dialog = SecurityEncryptionDialog(
            auth_supported=auth_supported,
            tls_supported=tls_supported,
            parent=self,
        )
        self._security_config_dialog = dialog
        dialog.set_hardware_safe(self._network_changes_are_safe())
        dialog.finished.connect(
            lambda _result: self._clear_security_config_dialog(dialog)
        )
        dialog.apply_requested.connect(
            lambda settings: self.worker.enqueue("write_security_config", settings)
        )
        dialog.show()
        self.worker.enqueue("read_security_config")

    def _clear_security_config_dialog(
        self, dialog: SecurityEncryptionDialog
    ) -> None:
        if self._security_config_dialog is dialog:
            self._security_config_dialog = None

    def _on_security_config_ready(
        self, token: str, tls: dict[str, str]
    ) -> None:
        dialog = self._security_config_dialog
        if dialog is not None:
            dialog.set_values(token, tls)

    def _on_security_config_failed(self, message: str) -> None:
        dialog = self._security_config_dialog
        if dialog is not None:
            dialog.show_error(f"Could not update security settings: {message}")

    def _open_network_setup(self) -> None:
        app_path = APPS_ROOT / "network_config" / "app.py"
        if not app_path.is_file():
            QMessageBox.critical(
                self,
                "Network Setup unavailable",
                f"Could not find the Network Setup application:\n{app_path}",
            )
            return
        started = QProcess.startDetached(sys.executable, [str(app_path)])
        success = started[0] if isinstance(started, tuple) else bool(started)
        if not success:
            QMessageBox.critical(
                self,
                "Network Setup unavailable",
                "Could not start the Network Setup application.",
            )

    def _show_fluid_mesh(self) -> None:
        QMessageBox.information(
            self,
            "Fluid Mesh",
            "Fluid Mesh configuration is not implemented in this dashboard yet.",
        )

    def _connect(self) -> None:
        dialog = ConnectionDialog(self)
        self._connection_dialog = dialog
        dialog.attempt_requested.connect(self._request_connection)
        dialog.exec()
        if self._connection_dialog is dialog:
            self._connection_dialog = None

    def _request_connection(self, endpoint: str, options: dict[str, Any]) -> None:
        self._active_endpoint = endpoint
        connection_options = dict(options)
        if endpoint.lower().startswith(("tcp://", "tls://")):
            connection_options.setdefault("connect_timeout", NETWORK_CONNECT_TIMEOUT_S)
        self.worker.enqueue("connect", endpoint, connection_options)

    def _on_connected_changed(self, connected: bool, detail: str) -> None:
        self._connected = connected
        if not connected:
            self._active_wave_tool = None
            self._active_endpoint = ""
        self._board_detail = detail if connected else "Not connected"
        dialog = self._connection_dialog
        if dialog is not None:
            if connected:
                dialog.connection_succeeded()
            else:
                dialog.connection_failed(detail)
        self.connection_label.setText(detail if connected else "Not connected")
        self.connection_label.setProperty("kind", "ok" if connected else "neutral")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self._set_board_controls_enabled(connected)
        if connected:
            self._on_capabilities_ready(self._capabilities)
        self.disconnect_btn.setEnabled(connected)
        self.disconnect_btn.setVisible(connected)
        self.connect_btn.setEnabled(not connected)
        if not connected:
            if self._board_settings_dialog is not None:
                self._board_settings_dialog.close()
            if self._bluetooth_config_dialog is not None:
                self._bluetooth_config_dialog.close()
            if self._wifi_config_dialog is not None:
                self._wifi_config_dialog.close()
            if self._network_config_dialog is not None:
                self._network_config_dialog.close()
            if self._security_config_dialog is not None:
                self._security_config_dialog.close()
            self._on_capabilities_ready({})
            self._psu_on = False
            self._psc_on = False
            self.psc_card.set_state(False)
            self.voltage_card.set_value("-", "V")
            self.current_card.set_value("-", "mA")
            if hasattr(self, "init_progress"):
                self.init_progress.setValue(0)
                self.init_voltage_plot.reset(120)
                self.init_current_delta_plot.reset(120)
                self.init_elapsed_label.setText("Elapsed 0 / 120 s")
                self.fast_init_progress.setValue(0)
                self.fast_init_voltage_plot.reset(60)
                self.fast_init_current_delta_plot.reset(60)
                self.fast_init_status_label.setText("Target 2.00 mA / max 60 s")
                self._square_elapsed_s = 0.0
                self._square_wave_rows.clear()
                self.square_voltage_plot.reset(30)
                self.square_current_delta_plot.reset(30)
                self.square_progress_bar.setRange(0, 1)
                self.square_progress_bar.setValue(0)
                self.square_status_label.setText("Not started")
                self.square_save_csv_btn.setEnabled(False)
                self.diagnosis_plot.reset()
                self._diagnosis_rows.clear()
                self.diagnosis_progress_bar.setRange(0, 27)
                self.diagnosis_progress_bar.setValue(0)
                self.diagnosis_label.setText("No diagnosis yet")
                self.diag_save_csv_btn.setEnabled(False)
                self.recover_progress.setValue(0)
                self.recovery_status_label.setText("Not started")
                self.recover_voltage_plot.reset(30)
                self.recover_current_delta_plot.reset(30)
                self._recovery_rows.clear()
                self._recovery_marked_stages.clear()
                self.recover_save_csv_btn.setEnabled(False)
                for dialog in self.tool_dialogs:
                    dialog.close()
            self._status = None
            self._clear_actuator_cards()

    def _on_capabilities_ready(self, capabilities: dict[str, Any]) -> None:
        self._capabilities = {str(key): str(value) for key, value in capabilities.items()}
        firmware_update_supported = self._capabilities.get("FWU") == "1"
        firmware_update_transport_supported = not self._active_endpoint.lower().startswith(
            "ble://"
        )
        if hasattr(self, "firmware_update_btn"):
            self.firmware_update_btn.setEnabled(
                self._connected
                and firmware_update_supported
                and firmware_update_transport_supported
            )
            self.firmware_update_btn.setToolTip(
                "Update this board's firmware."
                if firmware_update_supported and firmware_update_transport_supported
                else (
                    "Connect over USB serial or TCP/TLS to update firmware."
                    if firmware_update_supported
                    else "The connected board does not support self-updating firmware."
                )
            )
        if hasattr(self, "factory_reset_btn"):
            factory_reset_supported = self._capabilities.get("FCR") == "1"
            serial_connection = bool(self._active_endpoint) and "://" not in self._active_endpoint
            self.factory_reset_btn.setEnabled(
                self._connected and factory_reset_supported and serial_connection
            )
            self.factory_reset_btn.setToolTip(
                "Restore all board settings to factory defaults."
                if factory_reset_supported and serial_connection
                else "Connect over USB serial to a board with factory-reset support."
            )
        if hasattr(self, "wifi_config_btn"):
            wifi_supported = self._capabilities.get("WIFI") not in {None, "0"}
            self.wifi_config_btn.setEnabled(self._connected and wifi_supported)
            self.wifi_config_btn.setToolTip(
                "Configure the board's Wi-Fi connection."
                if wifi_supported
                else "The connected board does not report Wi-Fi support."
            )
        if hasattr(self, "bluetooth_config_btn"):
            bluetooth_supported = self._capabilities.get("BLT") not in {None, "0"}
            self.bluetooth_config_btn.setEnabled(
                self._connected and bluetooth_supported
            )
            self.bluetooth_config_btn.setToolTip(
                "Configure Bluetooth."
                if bluetooth_supported
                else "The connected board does not report Bluetooth configuration support."
            )
        if hasattr(self, "network_config_btn"):
            network_supported = self._capabilities.get("NET") not in {None, "0"}
            self.network_config_btn.setEnabled(self._connected and network_supported)
            self.network_config_btn.setToolTip(
                "Configure IP addressing and the TCP server."
                if network_supported
                else "The connected board does not report network configuration support."
            )
        if hasattr(self, "security_config_btn"):
            security_supported = any(
                self._capabilities.get(name) not in {None, "0"}
                for name in ("AUTH", "TLS")
            )
            self.security_config_btn.setEnabled(
                self._connected and security_supported
            )
            self.security_config_btn.setToolTip(
                "Configure access-token authentication and TLS encryption."
                if security_supported
                else "The connected board does not report security configuration support."
            )
        if hasattr(self, "fluid_mesh_btn"):
            mesh_supported = self._capabilities.get("MESH") not in {None, "0"}
            self.fluid_mesh_btn.setEnabled(self._connected and mesh_supported)
            self.fluid_mesh_btn.setToolTip(
                "Configure Fluid Mesh."
                if mesh_supported
                else "The connected board does not report Fluid Mesh support."
            )

    def _on_actuator_count_ready(self, actuator_count: int) -> None:
        self._configure_actuator_cards(int(actuator_count))

    def _configure_actuator_cards(self, actuator_count: int) -> None:
        if actuator_count < 1:
            raise ValueError("actuator_count must be at least 1")
        if actuator_count == self._actuator_count and len(self._cards) == actuator_count:
            return

        self._clear_actuator_cards()

        self._actuator_count = actuator_count
        for actuator in range(actuator_count):
            card = ActuatorCard(actuator)
            card.focused.connect(self._select_actuator)
            self._cards.append(card)
            self.actuator_grid.addWidget(card, actuator % 8, 0)

        group_count = (actuator_count + 7) // 8
        self.group_combo.blockSignals(True)
        for group in range(group_count):
            start = group * 8
            end = min(start + 7, actuator_count - 1)
            self.group_combo.addItem(f"Group {group} ({start}-{end})")
        self.group_combo.setCurrentIndex(0)
        self.group_combo.blockSignals(False)
        show_groups = group_count > 1
        self.group_label.setVisible(show_groups)
        self.group_combo.setVisible(show_groups)
        self._selected_actuator = 0
        self._show_group(0)
        self._select_actuator(0)

    def _clear_actuator_cards(self) -> None:
        """Remove all topology that belonged to the previous board connection."""

        for card in self._cards:
            self.actuator_grid.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._actuator_count = 0
        self._selected_actuator = 0
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.blockSignals(False)
        self.group_label.hide()
        self.group_combo.hide()

    def _on_busy_changed(self, text: str) -> None:
        # Connection state is intentionally the only status displayed in the
        # header, matching Network Setup. Operation progress is shown in its
        # corresponding control and the event log.
        return None

    def _set_board_controls_enabled(self, enabled: bool) -> None:
        if hasattr(self, "metrics_bar"):
            self.metrics_bar.setEnabled(enabled)
        if hasattr(self, "actuator_panel"):
            self.actuator_panel.setEnabled(enabled)
        if hasattr(self, "side_panel"):
            self.side_panel.setEnabled(enabled)
        if enabled:
            self._update_action_availability()
        elif hasattr(self, "init_btn"):
            for button in (
                self.init_btn,
                self.fast_init_btn,
                self.diag_btn,
                self.recover_btn,
                self.square_target_btn,
                self.init_run_btn,
                self.init_stop_btn,
                self.init_save_csv_btn,
                self.fast_init_run_btn,
                self.fast_init_stop_btn,
                self.fast_init_save_csv_btn,
                self.diag_run_btn,
                self.diag_stop_btn,
                self.diag_save_csv_btn,
                self.recover_run_btn,
                self.recover_stop_btn,
                self.recover_save_csv_btn,
                self.square_start_btn,
                self.square_stop_btn,
                self.square_save_csv_btn,
            ):
                button.setEnabled(False)

    def _on_status_ready(self, status: dict[str, Any]) -> None:
        actuator_count = int(status.get("actuator_count", len(status["actuator_values"])))
        if actuator_count != self._actuator_count:
            self._configure_actuator_cards(actuator_count)
        self._status = status
        psu_on = str(status["psu"]).upper() == "ON"
        psc_state = str(status["psc"]).upper()
        psc_supported = psc_state != "NONE"
        psc_on = power_connection_is_ready(psc_state)
        became_ready = (not self._psu_on or not self._psc_on) and psu_on and psc_on
        self._psu_on = psu_on
        self._psc_on = psc_on
        if self._wifi_config_dialog is not None:
            self._wifi_config_dialog.set_hardware_safe(
                self._network_changes_are_safe()
            )
            self._wifi_config_dialog.start_automatic_scan()
        if self._network_config_dialog is not None:
            self._network_config_dialog.set_hardware_safe(
                self._network_changes_are_safe()
            )
        if self._security_config_dialog is not None:
            self._security_config_dialog.set_hardware_safe(
                self._network_changes_are_safe()
            )
        self.psc_card.setVisible(True)
        self.psc_card.set_state(psu_on and (psc_on if psc_supported else True))
        self.voltage_card.set_value(f"{float(status['voltage']):.2f}", "V")
        self.current_card.set_value(f"{float(status['current']):.2f}", "mA")
        for card in self._cards:
            card.update_from_status(status)
        if became_ready:
            self._request_group_detection()

    def _on_diagnosis_ready(self, result: dict[str, Any]) -> None:
        if "sample_count" in result:
            self.diagnosis_label.setText(result["health_summary"])
            return
        self.diagnosis_label.setText(
            "Actuator {actuator}: baseline {baseline_ma:.2f} mA, "
            "forward {forward_ma:.2f} mA, discharge {discharge_ma:.2f} mA".format(**result)
        )

    def _on_diagnosis_progress(self, result: dict[str, Any]) -> None:
        completed = int(result["completed_steps"])
        total = int(result["total_steps"])
        self.diagnosis_progress_bar.setRange(0, total)
        self.diagnosis_progress_bar.setValue(completed)
        if result["phase"] == "warmup":
            direction = "Forward" if float(result["sent_voltage"]) >= 0 else "Reverse"
            self.diagnosis_label.setText(
                f"Warmup — cycle {int(result['warmup_cycle'])} of 3 — {direction}"
            )
            return
        voltage_v = float(result["voltage_v"])
        current_ma = abs(float(result["current_ma"]))
        self.diagnosis_plot.add_sample(voltage_v, current_ma)
        self._diagnosis_rows.append(
            {
                "actuator": int(result["actuator"]),
                "voltage_v": voltage_v,
                "current_ma": current_ma,
            }
        )
        self.diagnosis_label.setText(
            f"Testing — {voltage_v:.0f} V — {current_ma:.2f} mA"
        )

    def _on_diagnosis_finished(self, completed: bool, status: str) -> None:
        if self._active_wave_tool == "diagnose":
            self._active_wave_tool = None
        if not completed:
            self.diagnosis_label.setText(status)
        self._update_action_availability()

    def _on_initialization_progress(self, result: dict[str, Any]) -> None:
        elapsed_value = float(result["elapsed_s"])
        total_value = float(result["total_s"])
        phase_interval_s = float(
            result.get("phase_interval_s", Board.initialization_phase_interval_s)
        )
        total_steps = max(1, round(total_value / phase_interval_s))
        elapsed_steps = min(total_steps, round(elapsed_value / phase_interval_s))
        self.init_progress.setRange(0, total_steps)
        self.init_progress.setValue(elapsed_steps)
        if "sent_voltage" in result:
            sent_voltage = float(result["sent_voltage"])
            self.init_voltage_plot.add_sent_value(
                elapsed_value, sent_voltage, total_value
            )
        else:
            self.init_voltage_plot.set_progress(elapsed_value, total_value)
        if "delta_ma" in result:
            delta_ma = float(result["delta_ma"])
            self.init_current_delta_plot.add_current_delta(
                elapsed_value, delta_ma, total_value
            )
        else:
            self.init_current_delta_plot.set_progress(elapsed_value, total_value)
        self._initialization_rows.append(
            {
                "actuator": int(result["actuator"]),
                "elapsed_s": elapsed_value,
                "stage": int(result["stage_index"]),
                "phase": str(result.get("phase", "")),
                "voltage_v": (
                    float(result["sent_voltage"])
                    if "sent_voltage" in result
                    else ""
                ),
                "current_delta_ma": (
                    float(result["delta_ma"]) if "delta_ma" in result else ""
                ),
            }
        )
        self._apply_wave_tool_button_state()
        self.init_elapsed_label.setText(
            "Elapsed {elapsed_s:.1f} / {total_s:.0f} s — stage {stage_index}/{stage_count}, "
            "±{stage_voltage:.0f} V".format(
                elapsed_s=elapsed_value,
                total_s=total_value,
                stage_index=int(result["stage_index"]),
                stage_count=int(result["stage_count"]),
                stage_voltage=float(result["stage_voltage"]),
            )
        )

    def _on_fast_init_progress(self, result: dict[str, Any]) -> None:
        elapsed_value = float(result["elapsed_s"])
        elapsed_s = int(elapsed_value)
        duration_s = int(result["duration_s"])
        self.fast_init_progress.setRange(0, duration_s)
        self.fast_init_progress.setValue(elapsed_s)
        sent_voltage = float(result.get("sent_voltage", 0.0))
        if str(result.get("phase", "")) == "positive":
            self.fast_init_voltage_plot.add_sent_value(
                elapsed_value, sent_voltage, duration_s
            )
        else:
            self.fast_init_voltage_plot.set_progress(elapsed_value, duration_s)
        if "delta_ma" in result:
            delta_ma = float(result["delta_ma"])
            self.fast_init_current_delta_plot.add_current_delta(
                elapsed_value, delta_ma, duration_s
            )
        else:
            delta_ma = None
            self.fast_init_current_delta_plot.set_progress(
                elapsed_value, duration_s
            )
        self.fast_init_status_label.setText(
            "{elapsed_s} / {duration_s} s — target {target_delta_ma:.2f} mA — "
            "{phase} — {status}".format(
                elapsed_s=elapsed_s,
                duration_s=duration_s,
                target_delta_ma=float(result["target_delta_ma"]),
                phase=str(result.get("phase", "")).replace("_", " ").title(),
                status=str(result["status"]),
            )
        )
        self._fast_initialization_rows.append(
            {
                "actuator": int(result["actuator"]),
                "elapsed_s": elapsed_value,
                "phase": str(result.get("phase", "")),
                "voltage_v": sent_voltage,
                "current_delta_ma": delta_ma if delta_ma is not None else "",
                "target_delta_ma": float(result["target_delta_ma"]),
                "next_voltage_v": float(result["next_voltage"]),
                "status": str(result["status"]),
            }
        )
        self._apply_wave_tool_button_state()
        actuator = int(result["actuator"])
        if delta_ma is not None:
            self._cards[actuator].set_health(
                {
                    "state": "fast_init",
                    "delta_ma": delta_ma,
                    "target_voltage": float(result["target_voltage"]),
                }
            )

    def _on_fast_init_ready(self, result: dict[str, Any]) -> None:
        elapsed_s = int(float(result["elapsed_s"]))
        duration_s = int(float(result["duration_s"]))
        self.fast_init_progress.setRange(0, duration_s)
        self.fast_init_progress.setValue(elapsed_s)
        outcome = "Success" if result.get("success") else "Failed"
        self.fast_init_status_label.setText(
            "{outcome} — SDK state {final_state}".format(
                outcome=outcome,
                final_state=str(result["final_state"]),
            )
        )

    def _on_actuator_tool_finished(
        self, tool: str, completed: bool, status: str
    ) -> None:
        if self._active_wave_tool == tool:
            self._active_wave_tool = None
        if not completed:
            if tool == "init":
                self.init_elapsed_label.setText(status)
            elif tool == "fast_init":
                self.fast_init_status_label.setText(status)
            elif tool == "recover":
                self.recovery_status_label.setText(status)
        self._update_action_availability()

    def _on_recovery_ready(self, result: dict[str, Any]) -> None:
        self.recover_progress.setRange(0, int(result["stage_count"]))
        self.recover_progress.setValue(int(result["completed_stages"]))
        self.recovery_status_label.setText(
            "Completed — {completed_stages}/{stage_count} voltage stages in "
            "{elapsed_s:.1f} s".format(**result)
        )

    def _on_recovery_progress(self, result: dict[str, Any]) -> None:
        elapsed_s = float(result["elapsed_s"])
        stage_index = int(result["stage_index"])
        stage_count = int(result["stage_count"])
        phase = str(result["phase"])
        self.recover_progress.setRange(0, stage_count)
        if phase == "baseline":
            self.recover_progress.setValue(0)
            self.recover_voltage_plot.add_sent_value(elapsed_s, 0.0)
            self.recover_current_delta_plot.set_progress(elapsed_s)
            self.recovery_status_label.setText("Measuring baseline current")
            return

        voltage_v = float(result["sent_voltage"])
        target_delta_ma = float(result["target_delta_ma"])
        delta_ma = (
            abs(float(result["delta_ma"])) if "delta_ma" in result else None
        )
        self.recover_voltage_plot.add_sent_value(elapsed_s, voltage_v)
        if phase == "positive" and stage_index not in self._recovery_marked_stages:
            marker_label = f"↑ {float(result['stage_voltage']):.0f} V"
            self.recover_voltage_plot.add_marker(elapsed_s, marker_label)
            self.recover_current_delta_plot.add_marker(elapsed_s, marker_label)
            self._recovery_marked_stages.add(stage_index)
        if delta_ma is not None and phase == "positive":
            self.recover_current_delta_plot.add_current_delta(elapsed_s, delta_ma)
        else:
            self.recover_current_delta_plot.set_progress(elapsed_s)
        stage_complete = bool(result.get("stage_complete"))
        self.recover_progress.setValue(
            stage_index if stage_complete and phase == "negative" else stage_index - 1
        )
        self._recovery_rows.append(
            {
                "actuator": int(result["actuator"]),
                "elapsed_s": round(elapsed_s, 3),
                "stage": stage_index,
                "phase": phase,
                "voltage_v": voltage_v,
                "current_delta_ma": delta_ma if phase == "positive" else "",
                "target_current_delta_ma": target_delta_ma,
            }
        )
        measured_text = (
            f"{delta_ma:.2f} mA / " if delta_ma is not None and phase == "positive" else ""
        )
        qualified_s = float(result.get("qualified_s", 0.0))
        qualification_s = float(result.get("qualification_duration_s", 3.0))
        qualification_text = (
            f" — below limit {qualified_s:.0f}/{qualification_s:.0f} s"
            if phase == "positive"
            else ""
        )
        direction = "Forward" if phase == "positive" else "Reverse"
        self.recovery_status_label.setText(
            f"Stage {stage_index}/{stage_count} — {direction} "
            f"{abs(voltage_v):.0f} V — {measured_text}target {target_delta_ma:.2f} mA"
            f"{qualification_text}"
        )

    def _on_health_ready(self, group: int, results: dict[int, dict[str, Any]]) -> None:
        for actuator, health in results.items():
            self._cards[actuator].set_health(health)
        has_detecting = any(health.get("state") == "detecting" for health in results.values())
        if not has_detecting and self._selected_actuator // 8 == group:
            self._select_first_available_in_group(group)
        self._update_action_availability()

    def _request_group_detection(self) -> None:
        if self._connected and self._psu_on and self._psc_on:
            self.worker.enqueue("detect_group", self.group_combo.currentIndex())

    def _on_square_changed(self, running: bool, actuators: list[int], phase: str) -> None:
        self._square_running = running
        if running:
            self._active_wave_tool = "square"
            self.square_progress_bar.setRange(0, 0)
            self.square_status_label.setText(
                f"{self._square_elapsed_s:.1f} s — "
                f"{phase.replace('_', ' ').title()} — actuator "
                + ", ".join(str(actuator) for actuator in actuators)
            )
        else:
            if self._active_wave_tool == "square":
                self._active_wave_tool = None
            self.square_progress_bar.setRange(0, 1)
            self.square_progress_bar.setValue(1 if self._square_elapsed_s else 0)
            self.square_status_label.setText(
                f"Stopped after {self._square_elapsed_s:.1f} s"
                if self._square_elapsed_s
                else "Stopped"
            )
        self._update_action_availability()

    def _on_square_progress(self, result: dict[str, Any]) -> None:
        elapsed_s = float(result["elapsed_s"])
        self._square_elapsed_s = elapsed_s
        plot_total_s = max(30.0, elapsed_s)
        if "sent_voltage" in result:
            voltage_v = float(result["sent_voltage"])
            self.square_voltage_plot.add_sent_value(
                elapsed_s, voltage_v, plot_total_s
            )
        else:
            voltage_v = None
            self.square_voltage_plot.set_progress(elapsed_s, plot_total_s)

        if "current_delta_ma" in result:
            current_delta_ma = abs(float(result["current_delta_ma"]))
            self.square_current_delta_plot.add_current_delta(
                elapsed_s, current_delta_ma, plot_total_s
            )
        else:
            current_delta_ma = None
            self.square_current_delta_plot.set_progress(elapsed_s, plot_total_s)

        phase = str(result.get("phase", ""))
        status = str(result.get("status", phase.replace("_", " ").title()))
        actuators = [int(value) for value in result.get("actuators", [])]
        self.square_status_label.setText(
            f"{elapsed_s:.1f} s — {status}"
            + (
                " — actuator " + ", ".join(str(value) for value in actuators)
                if actuators
                else ""
            )
        )

        if voltage_v is not None or current_delta_ma is not None:
            actuator = actuators[0] if actuators else self._selected_actuator
            self._square_wave_rows.append(
                {
                    "actuator": actuator,
                    "elapsed_s": elapsed_s,
                    "phase": phase,
                    "voltage_v": voltage_v if voltage_v is not None else "",
                    "current_delta_ma": (
                        current_delta_ma if current_delta_ma is not None else ""
                    ),
                    "status": status,
                }
            )
            self._apply_wave_tool_button_state()

    def _show_group(self, group: int) -> None:
        if group < 0:
            return
        for card in self._cards:
            card.setVisible(card.actuator // 8 == group)

    def _on_group_changed(self, group: int) -> None:
        if group < 0 or not self._cards:
            return
        self._show_group(group)
        selected = self._selected_actuator
        if selected // 8 != group:
            self._select_actuator(group * 8)
        self._request_group_detection()

    def _select_actuator(self, actuator: int) -> None:
        actuator = int(actuator)
        if not 0 <= actuator < len(self._cards):
            return
        group = actuator // 8
        if hasattr(self, "_cards") and self._cards and not self._cards[actuator].is_selectable():
            health = self._cards[actuator]._health or {}
            state = health.get("state", "blocked")
            self._log(f"Actuator {actuator} is {state}; actions are disabled.", "warn")
            return
        if hasattr(self, "group_combo") and self.group_combo.currentIndex() != group:
            self.group_combo.blockSignals(True)
            self.group_combo.setCurrentIndex(group)
            self.group_combo.blockSignals(False)
            self._show_group(group)
        self._selected_actuator = actuator
        if hasattr(self, "selected_actuator_label"):
            self.selected_actuator_label.setText(f"Actuator {actuator}")
        for card in self._cards:
            card.set_selected(card.actuator == actuator)
        self._update_action_availability()

    def _select_first_available_in_group(self, group: int) -> None:
        if not self._cards:
            return
        current = self._cards[self._selected_actuator]
        if current.actuator // 8 == group and current.is_selectable():
            return
        for actuator in range(group * 8, min(group * 8 + 8, len(self._cards))):
            if self._cards[actuator].is_selectable():
                self._select_actuator(actuator)
                return
        self._update_action_availability()

    def _update_action_availability(self) -> None:
        if not hasattr(self, "init_btn") or not self._cards:
            return
        card = self._cards[self._selected_actuator]
        health = card._health or {}
        state = health.get("state", "na")
        ready = state == "idle"
        error = state == "error"
        present = ready or error
        self.init_btn.setEnabled(present)
        self.fast_init_btn.setEnabled(present)
        self.diag_btn.setEnabled(present)
        self.recover_btn.setEnabled(error)
        self.square_target_btn.setEnabled(ready)
        self.init_run_btn.setEnabled(present)
        self.fast_init_run_btn.setEnabled(present)
        self.diag_run_btn.setEnabled(present)
        self.recover_run_btn.setEnabled(error)
        self.recover_stop_btn.setEnabled(False)
        self.square_start_btn.setEnabled(ready and not self._square_running)
        self.square_stop_btn.setEnabled(self._square_running)
        self._apply_wave_tool_button_state()

    def _log(self, text: str, level: str = "info") -> None:
        if level == "debug" and (
            not hasattr(self, "verbose_log_checkbox")
            or not self.verbose_log_checkbox.isChecked()
        ):
            return
        color = {
            "ok": "#43b97f",
            "warn": "#d8a22c",
            "error": "#e65f5c",
            "debug": "#6f7f95",
            "info": "#8fa3bf",
        }.get(level, "#8fa3bf")
        timestamp = time.strftime("%H:%M:%S")
        safe_text = html.escape(text)
        self.log.append(f'<span style="color:{color}">[{timestamp}] {safe_text}</span>')

    def _save_event_log(self) -> None:
        default_name = f"fluidreality-dashboard-log-{time.strftime('%Y%m%d-%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Event Log",
            str(Path.home() / default_name),
            "Text Files (*.txt);;Log Files (*.log);;All Files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log.toPlainText() + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not save the event log:\n{exc}")
            return
        self._log(f"Event log saved to {path}", "ok")


APP_STYLES = """
QWidget#Root {
    background: #f7f7fc;
    color: #1a1b1f;
    font-size: 13px;
}
QWidget:disabled {
    color: #7a8797;
}
QLabel:disabled {
    color: #7a8797;
}
QLabel#Logo {
    background: transparent;
}
QLabel#AppTitle {
    color: #1a1b1f;
    font-size: 36px;
    font-weight: 700;
}
QLabel#AppSubtitle {
    color: #5d6c7b;
    font-size: 13px;
}
QLabel#ConnectionHint {
    color: #5d6c7b;
    font-size: 12px;
}
QLabel#FormLabel {
    color: #344454;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
    border: none;
}
QLabel#FormLabel:disabled {
    color: #7a8797;
}
QDialog#ConnectionDialog {
    background: #f7f7fc;
    color: #1a1b1f;
}
QDialog#ToolDialog {
    background: #f7f7fc;
    color: #1a1b1f;
}
QDialog#ToolDialog QLabel {
    background: transparent;
}
QDialog#ToolDialog QLabel#WifiStatus[kind="neutral"] {
    color: #1a1b1f;
    background: #f2f2f3;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    padding: 4px 10px;
}
QDialog#ToolDialog QLabel#WifiStatus[kind="ok"] {
    color: #0d4d2c;
    background: #eaf8f1;
    border: 1px solid #69c695;
    border-radius: 8px;
    padding: 4px 10px;
}
QDialog#ToolDialog QLabel#WifiStatus[kind="active"] {
    color: #0050bd;
    background: transparent;
    border: none;
    padding: 2px 0;
    font-weight: 700;
}
QDialog#ToolDialog QLabel#WifiStatus[kind="warn"] {
    color: #721012;
    background: #ffeff0;
    border: 1px solid #ff5a65;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel#DialogTitle {
    color: #1a1b1f;
    font-size: 22px;
    font-weight: 700;
}
QLabel#DialogSubtitle {
    color: #5d6c7b;
    font-size: 13px;
}
QLabel#ConnectionError {
    color: #721012;
    background: #ffeff0;
    border: 1px solid #ff9ca3;
    border-radius: 7px;
    padding: 9px 11px;
}
QFrame#TopBar,
QFrame#Panel,
QFrame#MetricCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ControlsPanel {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ConnectionOptions {
    background: #f0f7ff;
    border: 1px solid #b8d5ff;
    border-radius: 7px;
}
QTabWidget#ControlTabs::pane {
    background: #ffffff;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    top: -1px;
}
QTabWidget#ConnectionTabs::pane {
    background: #ffffff;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    top: -1px;
}
QTabWidget#ConnectionTabs QTabBar::tab {
    background: #f3f4f7;
    color: #4f5f70;
    border: 1px solid #dcddeb;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 9px 24px;
    margin-right: 4px;
    font-weight: 700;
}
QTabWidget#ConnectionTabs QTabBar::tab:selected {
    background: #0050bd;
    color: #ffffff;
    border-color: #0050bd;
}
QTabWidget#ControlTabs QTabBar::tab {
    background: #f3f4f7;
    color: #4f5f70;
    border: 1px solid #dcddeb;
    border-bottom-color: #dcddeb;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    padding: 7px 14px;
    margin-right: 4px;
    font-weight: 700;
}
QTabWidget#ControlTabs QTabBar::tab:selected {
    background: #0050bd;
    color: #ffffff;
    border-color: #0050bd;
}
QTabWidget#ControlTabs QTabBar::tab:hover:!selected {
    background: #eaf2ff;
    color: #1a1b1f;
}
QProgressBar#InitProgress {
    background: #eef1f6;
    border: 1px solid #d6dae3;
    border-radius: 7px;
    min-height: 16px;
}
QProgressBar#InitProgress::chunk {
    background: #0050bd;
    border-radius: 6px;
}
QFrame#Inset {
    background: #fafafa;
    border: 1px solid #dcddeb;
    border-radius: 8px;
}
QLabel#SectionTitle {
    color: #1a1b1f;
    font-size: 17px;
    font-weight: 700;
}
QLabel#ToolTitle {
    color: #1a1b1f;
    font-size: 13px;
    font-weight: 700;
}
QLabel#MetricTitle {
    color: #5d6c7b;
    font-size: 11px;
    text-transform: uppercase;
}
QLabel#MetricValue {
    color: #1a1b1f;
    font-size: 16px;
    font-weight: 700;
}
QLabel#MetricUnit {
    color: #5d6c7b;
    font-size: 11px;
}
QFrame#ActuatorCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ActuatorCard[state="forward"] {
    background: #f0f7ff;
    border-color: #0050bd;
}
QFrame#ActuatorCard[state="discharge"] {
    background: #ffeff0;
    border-color: #ee2c24;
}
QFrame#ActuatorCard[state="disconnected"] {
    background: #f2f2f3;
    border-color: #c8c8c8;
}
QFrame#ActuatorCard[state="na"] {
    background: #fafafa;
    border-color: #dedfe3;
}
QFrame#ActuatorCard[state="detecting"] {
    background: #f0f7ff;
    border-color: #0050bd;
}
QFrame#ActuatorCard[state="health_error"] {
    background: #ffeff0;
    border-color: #ee2c24;
}
QFrame#ActuatorCard[selected="true"] {
    border: 2px solid #0050bd;
}
QPushButton#ToolButton {
    text-align: left;
    padding: 9px 12px;
}
QLabel#ActuatorNumber {
    color: #1a1b1f;
    font-size: 16px;
    font-weight: 700;
}
QLabel#ActuatorValue {
    color: #1a1b1f;
    font-size: 12px;
    font-weight: 700;
}
QLabel#ActuatorDetail {
    color: #4f5f70;
    font-size: 11px;
    font-weight: 600;
}
QLabel#ActuatorStatePill {
    border-radius: 6px;
    padding: 2px 5px;
    font-size: 11px;
}
QLabel#ActuatorDetail:disabled {
    color: #5f6f80;
}
QLabel#Diagnosis {
    color: #5d6c7b;
}
QLabel#SelectedActuatorBadge {
    color: #ffffff;
    background: #0050bd;
    border: 1px solid #0050bd;
    border-radius: 7px;
    padding: 9px 14px;
    font-weight: 700;
    min-width: 96px;
}
QLabel#SelectedActuatorBadge:disabled {
    color: #ffffff;
    background: #8fb6ec;
    border-color: #8fb6ec;
}
QLabel[kind="neutral"] {
    color: #1a1b1f;
    background: #f2f2f3;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="ok"] {
    color: #0d4d2c;
    background: #eaf8f1;
    border: 1px solid #69c695;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="active"] {
    color: #ffffff;
    background: #0050bd;
    border: 1px solid #0050bd;
    border-radius: 8px;
    padding: 4px 10px;
}
QLabel[kind="warn"] {
    color: #721012;
    background: #ffeff0;
    border: 1px solid #ff5a65;
    border-radius: 8px;
    padding: 4px 10px;
}
QPushButton {
    background: #1a1b1f;
    color: #ffffff;
    border: 1px solid #1a1b1f;
    border-radius: 7px;
    padding: 8px 11px;
    font-weight: 700;
}
QPushButton:hover {
    background: #ee2c24;
    border-color: #ee2c24;
}
QPushButton:pressed {
    background: #721012;
    border-color: #721012;
}
QPushButton:disabled {
    background: #e6e8ee;
    color: #7a8797;
    border-color: #d6dae3;
}
QPushButton#SecondaryButton,
QPushButton#quietButton {
    color: #1a1b1f;
    background: #ffffff;
    border: 1px solid #c8c8c8;
}
QPushButton#SecondaryButton:hover,
QPushButton#quietButton:hover {
    color: #0050bd;
    background: #eaf2ff;
    border-color: #0050bd;
}
QPushButton#dangerButton {
    background: #ee2c24;
    color: #ffffff;
    border-color: #ee2c24;
}
QPushButton#dangerButton:hover {
    background: #721012;
    border-color: #721012;
}
QLineEdit,
QComboBox,
QSpinBox,
QDoubleSpinBox,
QTreeWidget {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 7px 9px;
}
QTreeWidget {
    alternate-background-color: #fafafa;
    padding: 0;
    selection-background-color: #eaf2ff;
    selection-color: #1a1b1f;
}
QHeaderView::section {
    background: #f3f4f7;
    color: #4f5f70;
    border: 0;
    border-bottom: 1px solid #dcddeb;
    padding: 8px;
    font-weight: 700;
}
QLineEdit:disabled,
QComboBox:disabled,
QSpinBox:disabled,
QDoubleSpinBox:disabled {
    background: #f3f4f7;
    color: #7a8797;
    border-color: #d6dae3;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 4px;
    selection-background-color: #eaf2ff;
    selection-color: #1a1b1f;
    outline: 0;
}
QTextEdit {
    background: #1a1b1f;
    color: #ffffff;
    border: 1px solid #26272c;
    border-radius: 8px;
    padding: 8px;
}
QScrollArea#ActuatorScroll,
QWidget#ActuatorViewport {
    background: #ffffff;
    border: 0;
}
QWidget#ActuatorGridHost {
    background: #ffffff;
}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    window = DashboardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
