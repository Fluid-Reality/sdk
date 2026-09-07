"""Fluid Reality board dashboard.

Run from the SDK root with:

    python apps/fluidreality_dashboard/app.py
"""

from __future__ import annotations

import html
import queue
import sys
import time
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent
APPS_ROOT = APP_ROOT.parent
LOGO_PATH = APP_ROOT / "assets" / "fluid_reality_logo_transparent.png"
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QPainter, QPixmap
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
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from fluid_reality import (
    ActuatorState,
    BluetoothDevice,
    Board,
    FirmwareError,
    discover_bluetooth_boards,
    is_virtual_port,
    list_ports,
)
from shared.secret_fields import add_secret_visibility
from shared.toggle import LabeledToggle


STATE_NAMES = {
    0: "Idle",
    1: "Forward",
    2: "Discharge",
}


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
        subtitle = QLabel(
            "Connect directly over USB, Bluetooth, or the network."
        )
        subtitle.setObjectName("DialogSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

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
        self.refresh_ports_button = QPushButton("Refresh ports")
        self.refresh_ports_button.setObjectName("SecondaryButton")
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
        explanation = QLabel(
            "Use TCP on a trusted network, or TLS with a certificate for encrypted traffic."
        )
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
        self.network_port.setValue(8765)
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

        self.tls_options = QFrame()
        self.tls_options.setObjectName("ConnectionOptions")
        tls_form = QFormLayout(self.tls_options)
        tls_form.setContentsMargins(12, 12, 12, 12)
        tls_form.setHorizontalSpacing(14)
        tls_form.setVerticalSpacing(10)
        certificate_row = QWidget()
        certificate_layout = QHBoxLayout(certificate_row)
        certificate_layout.setContentsMargins(0, 0, 0, 0)
        certificate_layout.setSpacing(7)
        self.tls_ca_file = QLineEdit()
        self.tls_ca_file.setPlaceholderText("Board certificate or CA certificate (.pem/.crt)")
        self.browse_certificate_button = QPushButton("Browse…")
        self.browse_certificate_button.setObjectName("SecondaryButton")
        self.browse_certificate_button.clicked.connect(self._choose_tls_certificate)
        certificate_layout.addWidget(self.tls_ca_file, 1)
        certificate_layout.addWidget(self.browse_certificate_button)
        self.tls_server_hostname = QLineEdit()
        self.tls_server_hostname.setPlaceholderText("Optional certificate hostname override")
        tls_form.addRow(form_label("Certificate"), certificate_row)
        tls_form.addRow(form_label("Certificate name"), self.tls_server_hostname)
        layout.addWidget(self.tls_options)
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
        self.refresh_bluetooth_button = QPushButton("Scan for boards")
        self.refresh_bluetooth_button.setObjectName("SecondaryButton")
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
        self.tls_options.setVisible(self.network_encryption.isChecked())
        self.adjustSize()

    def _choose_tls_certificate(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Select board certificate or certificate authority",
            "",
            "Certificate files (*.pem *.crt *.cer);;All files (*)",
        )
        if path:
            self.tls_ca_file.setText(path)

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
                certificate = Path(self.tls_ca_file.text().strip())
                if not certificate.is_file():
                    self._show_error("Select an existing TLS certificate or CA file.")
                    return
                options["tls_ca_file"] = str(certificate)
                server_hostname = self.tls_server_hostname.text().strip()
                if server_hostname:
                    options["tls_server_hostname"] = server_hostname
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
    actuator_count_ready = Signal(int)
    status_ready = Signal(dict)
    diagnosis_ready = Signal(dict)
    initialization_progress = Signal(dict)
    fast_init_progress = Signal(dict)
    fast_init_ready = Signal(dict)
    recovery_ready = Signal(dict)
    recovery_progress = Signal(dict)
    health_ready = Signal(int, object)
    square_changed = Signal(bool, list, str)
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
        self._next_square_step = 0.0
        self._last_square_warning = 0.0
        self._square_restore_debug: bool | None = None
        self._last_status = 0.0
        self._actuator_health: dict[int, dict[str, Any]] = {}

    def enqueue(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

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
                elif command == "psu":
                    self._require_board().power_supply(bool(args[0]))
                    self.message.emit(f"Power supply {'on' if args[0] else 'off'}", "ok")
                    self._emit_status()
                elif command == "psc":
                    self._require_board().connect_power(bool(args[0]))
                    self.message.emit(f"Power connection {'closed' if args[0] else 'open'}", "ok")
                    self._emit_status()
                elif command == "init":
                    self._initialize_actuator(int(args[0]))
                elif command == "fast_init":
                    self._fast_initialize_actuator(int(args[0]), float(args[1]))
                elif command == "diagnose":
                    self._diagnose_actuator(int(args[0]))
                elif command == "recover":
                    self._recover_actuator(int(args[0]), float(args[1]), int(args[2]))
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
                else:
                    self.message.emit(f"Unknown worker command: {command}", "error")
            except Exception as exc:
                self.busy_changed.emit("")
                error_message = str(exc)
                if command == "connect":
                    options = dict(args[1]) if len(args) > 1 else {}
                    endpoint = str(args[0]).lower() if args else ""
                    if (
                        endpoint.startswith(("tcp://", "tls://"))
                        and not options.get("network_token")
                        and ("closed" in error_message.lower() or "reset" in error_message.lower())
                    ):
                        error_message = (
                            "The network endpoint closed the connection. It may have access-token "
                            "authentication enabled; enter its token and try again."
                        )
                self.message.emit(error_message, "error")
                if command == "connect":
                    self._close_board()
                    self.connected_changed.emit(False, error_message)
                if self._board is not None and command in {"psu", "psc"}:
                    try:
                        self._emit_status()
                    except Exception:
                        pass

    def _connect(self, port: str, options: dict[str, Any] | None = None) -> None:
        self._close_board()
        self.busy_changed.emit("Connecting")
        self._board = self._board_class(port, **(options or {}))
        self._board.set_debug_out(self._emit_debug)
        self._board.force_text_mode()
        version = self._board.firmware_version()
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
                self._board.set_actuator(actuator, 0)
        except Exception:
            pass
        self._restore_square_debug()
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
            f"Initializing actuator {actuator}: safety off, 1 Hz bipolar drive "
            "at +/-25 V, +/-50 V, +/-100 V, +/-200 V, then diagnose.",
            "warn",
        )
        state = board.initialize(
            actuator,
            progress_callback=self.initialization_progress.emit,
        )
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
        last_progress_second = -1
        last_result: dict[str, Any] | None = None
        success = False
        status_text = "failed"

        try:
            if previous_safety:
                board.safety(False)
            while True:
                elapsed_s = time.monotonic() - start
                if elapsed_s > max_duration_s:
                    status_text = "failed"
                    break

                drive_voltage = target_voltage
                output_value = self._voltage_to_output_allow_zero(drive_voltage, supply_voltage)
                board.set_manual_output(actuator, 0, 0)
                time.sleep(0.05)
                baseline_ma = board.current()

                board.set_manual_output(actuator, output_value, 0)
                time.sleep(0.5)
                forward_ma = board.current()
                delta_ma = abs(forward_ma - baseline_ma)

                board.set_manual_output(actuator, 0, output_value)
                time.sleep(0.5)
                reverse_ma = board.current()

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

                elapsed_s = time.monotonic() - start
                result = {
                    "actuator": actuator,
                    "elapsed_s": min(elapsed_s, max_duration_s),
                    "duration_s": max_duration_s,
                    "target_delta_ma": target_delta_ma,
                    "target_voltage": drive_voltage,
                    "next_voltage": target_voltage,
                    "supply_voltage": supply_voltage,
                    "baseline_ma": baseline_ma,
                    "forward_ma": forward_ma,
                    "reverse_ma": reverse_ma,
                    "delta_ma": delta_ma,
                    "error_ma": error_ma,
                    "step_v": step_v,
                    "status": status_text,
                }
                last_result = result
                whole_second = int(elapsed_s)
                if whole_second > last_progress_second or success:
                    self.fast_init_progress.emit(result)
                    self.message.emit(
                        "Fast Init {actuator}: {elapsed_s:.0f}/{duration_s:.0f}s, "
                        "delta {delta_ma:.2f} mA, target {target_delta_ma:.2f} mA, "
                        "drive {target_voltage:.0f} V, {status}.".format(
                            **result
                        ),
                        "ok" if success else "info",
                    )
                    last_progress_second = whole_second
                if success:
                    break
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(previous_safety)

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
        self._emit_status()

    def _diagnose_actuator(self, actuator: int) -> None:
        self._ensure_actuator_diagnosable(actuator)
        board = self._require_board()
        self.busy_changed.emit(f"Diagnosing actuator {actuator}")
        self.message.emit(f"Diagnosing actuator {actuator}", "info")
        result = board.diagnose_actuator(actuator)
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
            }
        )
        self.message.emit(f"Diagnosis complete for actuator {actuator}", "ok")
        self.message.emit(
            f"Actuator {actuator} classified as {health['state']} after diagnosis.",
            "ok" if health["state"] == "idle" else "warn",
        )
        self.busy_changed.emit("")
        self._emit_status()

    def _recover_actuator(self, actuator: int, target_voltage: float, duration_s: int) -> None:
        self._ensure_actuator_recoverable(actuator)
        if target_voltage <= 0:
            raise ValueError("Recovery voltage must be greater than 0.")
        if duration_s <= 0:
            raise ValueError("Recovery duration must be greater than 0.")
        board = self._require_board()
        supply_voltage = board.voltage()
        if supply_voltage <= 0:
            raise RuntimeError("Cannot recover: measured PSU voltage is 0 V.")
        output_value = self._voltage_to_output(target_voltage, supply_voltage)
        self.busy_changed.emit(f"Recovering actuator {actuator}")
        self.message.emit(
            f"Recovering actuator {actuator} at +/-{target_voltage:.1f} V "
            f"for {duration_s}s from {supply_voltage:.1f} V PSU.",
            "warn",
        )
        duration_s_float = float(duration_s)
        phase_interval_s = 0.5
        baseline_ma = board.current()
        previous_safety = board.safety()
        samples: list[float] = []
        try:
            if previous_safety:
                board.safety(False)
            start = time.monotonic()
            next_phase = start
            next_report_second = 1
            phase = 0
            while True:
                now = time.monotonic()
                elapsed_s = now - start
                if elapsed_s >= duration_s_float:
                    break

                if now >= next_phase:
                    if phase % 2 == 0:
                        board.set_manual_output(actuator, output_value, 0)
                    else:
                        board.set_manual_output(actuator, 0, output_value)
                    phase += 1
                    next_phase = now + phase_interval_s

                current_ma = board.current()
                samples.append(current_ma)
                delta_ma = abs(current_ma - baseline_ma)
                whole_second = int(elapsed_s)
                if whole_second >= next_report_second:
                    progress = {
                        "actuator": actuator,
                        "elapsed_s": min(whole_second, duration_s),
                        "duration_s": duration_s,
                        "baseline_ma": baseline_ma,
                        "current_ma": current_ma,
                        "delta_ma": delta_ma,
                        "target_voltage": target_voltage,
                        "supply_voltage": supply_voltage,
                        "output_value": output_value,
                    }
                    self.recovery_progress.emit(progress)
                    self.message.emit(
                        "Recovery {actuator}: {elapsed_s}/{duration_s}s, "
                        "delta {delta_ma:.2f} mA.".format(**progress),
                        "info",
                    )
                    next_report_second = whole_second + 1
                time.sleep(0.1)
            board.set_manual_output(actuator, 0, 0)
        finally:
            try:
                board.set_manual_output(actuator, 0, 0)
            finally:
                board.safety(previous_safety)

        recovery_ma = sum(samples) / len(samples) if samples else baseline_ma
        delta_ma = abs(recovery_ma - baseline_ma)
        result = {
            "actuator": actuator,
            "baseline_ma": baseline_ma,
            "recovery_ma": recovery_ma,
            "delta_ma": delta_ma,
            "duration_s": duration_s,
            "target_voltage": target_voltage,
            "supply_voltage": supply_voltage,
            "output_value": output_value,
        }
        self.recovery_ready.emit(result)
        self.message.emit(
            f"Recovery complete for actuator {actuator}: delta {delta_ma:.2f} mA.",
            "ok",
        )
        self.busy_changed.emit("")
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
        baseline_ma: float | None = None
        for actuator in actuators:
            actuator_group = actuator // 8
            self.health_ready.emit(actuator_group, {actuator: {"state": "detecting"}})
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
            entry = self._health_from_detection(detection)
            results[actuator] = entry
            self._actuator_health[actuator] = entry
            self.health_ready.emit(actuator_group, {actuator: entry})
            self.message.emit(
                f"Detection actuator {actuator}: baseline {detection.baseline_ma:.2f} mA, "
                f"initial forward {detection.initial_forward_ma:.2f} mA "
                f"(delta {detection.initial_delta_ma:.2f} mA), final forward "
                f"{detection.forward_ma:.2f} mA (delta {entry['delta_ma']:.2f} mA) -> "
                f"{'ready' if entry['state'] == 'idle' else entry['state']}.",
                "ok" if entry["state"] == "idle" else "warn",
            )

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
        self._square_restore_debug = board.firmware_debug()
        if not self._square_restore_debug:
            board.firmware_debug(True)
        board.flush_debug_lines()
        self._square_actuators = unique
        self._square_phase = "forward"
        self._next_square_step = 0.0
        self.square_changed.emit(True, unique, "arming")
        self.message.emit(
            "Square wave started for "
            + ", ".join(str(value) for value in unique)
            + "; waiting for firmware discharge debug before each reactivation",
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
            try:
                board.set_actuator(actuator, 0)
            except FirmwareError as exc:
                if exc.code != "ACT_FAILED":
                    raise
        self._restore_square_debug()
        self.square_changed.emit(False, [], "idle")
        self.message.emit("Square wave stopped", "ok")
        self._emit_status()

    def _restore_square_debug(self) -> None:
        if self._board is None or self._square_restore_debug is None:
            self._square_restore_debug = None
            return
        try:
            self._board.firmware_debug(self._square_restore_debug)
        finally:
            self._square_restore_debug = None

    @staticmethod
    def _health_from_detection(detection: Any) -> dict[str, Any]:
        state = str(detection.state.value if hasattr(detection.state, "value") else detection.state)
        if state == "Ready":
            health_state = "idle"
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
        now = time.monotonic()
        if now < self._next_square_step:
            return

        try:
            if self._square_phase == "forward":
                for actuator in list(self._square_actuators):
                    self._board.set_actuator(actuator, 255)
                self.square_changed.emit(True, list(self._square_actuators), "full on")
                self._square_phase = "off"
                self._next_square_step = now + 1.0
                return

            if self._square_phase == "off":
                for actuator in list(self._square_actuators):
                    self._board.set_actuator(actuator, 0)
                self.square_changed.emit(True, list(self._square_actuators), "discharge")
                self._square_phase = "wait_debug"
                self._next_square_step = now + 1.0
                return

            if self._square_phase == "wait_debug":
                status = self._board.status()
                self.status_ready.emit(status)
                self._last_status = now
                debug_lines = self._board.flush_debug_lines()
                if self._debug_confirms_discharge_complete(debug_lines):
                    self.square_changed.emit(True, list(self._square_actuators), "ready")
                    self._square_phase = "forward"
                    self._next_square_step = 0.0
                    return
                if now - self._last_square_warning > 2.0:
                    self.message.emit(
                        "Square wave waiting for DBG:DISCHARGE_STOP before reactivation.",
                        "warn",
                    )
                    self._last_square_warning = now
                self._next_square_step = now + 0.1
                return

            self._square_phase = "forward"
            self._next_square_step = 0.0
        except FirmwareError as exc:
            if exc.code == "ACT_FAILED":
                if now - self._last_square_warning > 2.0:
                    self.message.emit(
                        "Square wave waiting for DBG:DISCHARGE_STOP before reactivation.",
                        "warn",
                    )
                    self._last_square_warning = now
                self._square_phase = "wait_debug"
                self._next_square_step = now + 0.1
                return
            self.message.emit(f"Square wave stopped: {exc}", "error")
            self._square_actuators = []
            self._square_phase = "idle"
            self._restore_square_debug()
            self.square_changed.emit(False, [], "idle")
            return
        except Exception as exc:
            self.message.emit(f"Square wave stopped: {exc}", "error")
            self._square_actuators = []
            self._square_phase = "idle"
            self._restore_square_debug()
            self.square_changed.emit(False, [], "idle")
            return

    def _debug_confirms_discharge_complete(self, debug_lines: tuple[str, ...]) -> bool:
        stopped: set[int] = set()
        for line in debug_lines:
            if not line.startswith("DBG:DISCHARGE_STOP,ACT>"):
                continue
            payload = line.removeprefix("DBG:DISCHARGE_STOP,ACT>")
            actuator_text = payload.split(",", 1)[0]
            try:
                stopped.add(int(actuator_text))
            except ValueError:
                continue
        return all(actuator in stopped for actuator in self._square_actuators)

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")

        row = QHBoxLayout()
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.unit_label = QLabel(unit)
        self.unit_label.setObjectName("MetricUnit")
        row.addWidget(self.value_label)
        row.addWidget(self.unit_label, 0, Qt.AlignBottom)
        row.addStretch()

        layout.addWidget(title_label)
        layout.addLayout(row)

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
        self.setFixedSize(74, 38)

    def sizeHint(self) -> QSize:
        return QSize(74, 38)

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

        thumb_diameter = track.height() - 6
        thumb_x = track.right() - thumb_diameter - 3 if self.isChecked() else track.left() + 3
        thumb_y = track.top() + 3
        painter.setBrush(self._thumb_color)
        painter.drawEllipse(thumb_x, thumb_y, thumb_diameter, thumb_diameter)


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
        self._on_text = on_text
        self._off_text = off_text

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        title_label = QLabel(title)
        title_label.setObjectName("MetricTitle")

        row = QHBoxLayout()
        self.value_label = QLabel(off_text)
        self.value_label.setObjectName("MetricValue")
        self.toggle = SwitchToggle(on_color=on_color)
        self.toggle.toggled.connect(self._on_toggled)
        row.addWidget(self.value_label)
        row.addStretch()
        row.addWidget(self.toggle)

        layout.addWidget(title_label)
        layout.addLayout(row)

    def _on_toggled(self, checked: bool) -> None:
        self.value_label.setText(self._on_text if checked else self._off_text)
        self.toggled.emit(checked)

    def set_state(self, checked: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(checked)
        self.toggle.update()
        self.toggle.blockSignals(False)
        self.value_label.setText(self._on_text if checked else self._off_text)


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
        self.setMinimumWidth(180)
        self.setFixedHeight(158)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        number = QLabel(f"{actuator:02d}")
        number.setObjectName("ActuatorNumber")
        self.state = StatusPill("N/A", "neutral")
        header.addWidget(number)
        header.addStretch()
        header.addWidget(self.state)

        self.value = QLabel("not detected")
        self.value.setObjectName("ActuatorValue")
        self.runtime = QLabel("connect and detect")
        self.runtime.setObjectName("ActuatorDetail")

        layout.addLayout(header)
        layout.addWidget(self.value)
        layout.addStretch()
        layout.addWidget(self.runtime)

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
        self.value.setText("not detected")
        self.runtime.setText("connect and detect")
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
        self.resize(1480, 940)
        self._cards: list[ActuatorCard] = []
        self._actuator_count = 0
        self._connected = False
        self._status: dict[str, Any] | None = None
        self._selected_actuator = 0
        self._psu_on = False
        self._psc_on = False
        self._connection_dialog: ConnectionDialog | None = None

        self.worker = BoardWorker(board_class)
        self.worker.connected_changed.connect(self._on_connected_changed)
        self.worker.actuator_count_ready.connect(self._on_actuator_count_ready)
        self.worker.status_ready.connect(self._on_status_ready)
        self.worker.diagnosis_ready.connect(self._on_diagnosis_ready)
        self.worker.initialization_progress.connect(self._on_initialization_progress)
        self.worker.fast_init_progress.connect(self._on_fast_init_progress)
        self.worker.fast_init_ready.connect(self._on_fast_init_ready)
        self.worker.recovery_ready.connect(self._on_recovery_ready)
        self.worker.recovery_progress.connect(self._on_recovery_progress)
        self.worker.health_ready.connect(self._on_health_ready)
        self.worker.square_changed.connect(self._on_square_changed)
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
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(16)

        title_row = QHBoxLayout()
        title_stack = QVBoxLayout()
        brand_row = QHBoxLayout()
        self.logo_label = QLabel()
        self.logo_label.setObjectName("Logo")
        if LOGO_PATH.exists():
            pixmap = QPixmap(str(LOGO_PATH))
            self.logo_label.setPixmap(pixmap.scaledToWidth(250, Qt.SmoothTransformation))
        else:
            self.logo_label.setText("Fluid Reality")
            self.logo_label.setObjectName("AppTitle")

        brand_text = QVBoxLayout()
        title = QLabel("Fluid Reality Dashboard")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Board telemetry, actuator runtime, initialization, diagnostics, and square-wave drive.")
        subtitle.setObjectName("AppSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_row.addWidget(self.logo_label)
        brand_row.addSpacing(18)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        title_stack.addLayout(brand_row)

        title_row.addLayout(title_stack)
        title_row.addStretch()
        outer.addLayout(title_row)

        outer.addWidget(self._build_connection_bar())
        self.metrics_bar = self._build_metrics_bar()
        outer.addWidget(self.metrics_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.actuator_panel = self._build_actuator_panel()
        self.side_panel = self._build_side_panel()
        splitter.addWidget(self.actuator_panel)
        splitter.addWidget(self.side_panel)
        splitter.setSizes([900, 520])
        outer.addWidget(splitter, 1)

        self.setStyleSheet(APP_STYLES)
        self._set_board_controls_enabled(False)

    def _build_connection_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar.setFixedHeight(66)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 12, 14, 12)

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
        layout.setSpacing(12)

        self.psu_card = ToggleMetricCard("Power Supply", "On", "Off", on_color="#ee2c24")
        self.psu_card.toggled.connect(lambda checked: self.worker.enqueue("psu", checked))
        self.psc_card = ToggleMetricCard(
            "Output Connection",
            "Connected",
            "Open",
            on_color="#0050bd",
        )
        self.psc_card.toggled.connect(lambda checked: self.worker.enqueue("psc", checked))
        self.voltage_card = MetricCard("Voltage", "-", "V")
        self.current_card = MetricCard("Current", "-", "mA")
        self.config_card = MetricCard("Timing", "-")

        for card in (self.psu_card, self.psc_card, self.voltage_card, self.current_card, self.config_card):
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            layout.addWidget(card)
        return row

    def _build_actuator_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        label = QLabel("Actuators")
        label.setObjectName("SectionTitle")
        self.group_label = form_label("Group")
        self.group_combo = QComboBox()
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)
        self.redetect_all_btn = QPushButton("Redetect all")
        self.redetect_all_btn.setObjectName("SecondaryButton")
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
        scroll.setObjectName("ActuatorScroll")
        scroll.viewport().setObjectName("ActuatorViewport")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        grid_host = QWidget()
        grid_host.setObjectName("ActuatorGridHost")
        self.actuator_grid = QGridLayout(grid_host)
        self.actuator_grid.setContentsMargins(0, 0, 0, 0)
        self.actuator_grid.setSpacing(10)

        scroll.setWidget(grid_host)
        layout.addLayout(header)
        layout.addWidget(self._build_board_controls_panel())
        layout.addWidget(scroll, 1)
        return panel

    def _build_board_controls_panel(self) -> QWidget:
        controls = QFrame()
        controls.setObjectName("ControlsPanel")
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setObjectName("ControlTabs")

        init_tab = QWidget()
        init_layout = QHBoxLayout(init_tab)
        init_layout.setContentsMargins(10, 10, 10, 10)
        init_layout.setSpacing(10)
        self.init_btn = QPushButton("Initialize")
        self.init_btn.clicked.connect(lambda: self.worker.enqueue("init", self._selected_actuator))
        self.init_progress = QProgressBar()
        self.init_progress.setRange(0, 120)
        self.init_progress.setValue(0)
        self.init_progress.setTextVisible(False)
        self.init_progress.setObjectName("InitProgress")
        self.init_elapsed_label = QLabel("Elapsed 0 / 120 s")
        self.init_elapsed_label.setObjectName("Diagnosis")
        init_layout.addWidget(self.init_btn)
        init_layout.addWidget(self.init_progress, 1)
        init_layout.addWidget(self.init_elapsed_label)
        init_layout.addStretch()

        fast_init_tab = QWidget()
        fast_init_layout = QHBoxLayout(fast_init_tab)
        fast_init_layout.setContentsMargins(10, 10, 10, 10)
        fast_init_layout.setSpacing(10)
        self.fast_init_btn = QPushButton("Fast Init")
        self.fast_init_btn.clicked.connect(
            lambda: self.worker.enqueue(
                "fast_init",
                self._selected_actuator,
                self.fast_init_target_spin.value(),
            )
        )
        fast_init_layout.addWidget(form_label("Target"))
        self.fast_init_target_spin = QDoubleSpinBox()
        self.fast_init_target_spin.setRange(0.01, Board.error_delta_ma - 0.01)
        self.fast_init_target_spin.setDecimals(2)
        self.fast_init_target_spin.setSingleStep(0.10)
        self.fast_init_target_spin.setValue(2.00)
        self.fast_init_target_spin.setSuffix(" mA")
        fast_init_layout.addWidget(self.fast_init_target_spin)
        self.fast_init_progress = QProgressBar()
        self.fast_init_progress.setRange(0, 60)
        self.fast_init_progress.setValue(0)
        self.fast_init_progress.setTextVisible(False)
        self.fast_init_progress.setObjectName("InitProgress")
        self.fast_init_status_label = QLabel("Target 2.00 mA / max 60 s")
        self.fast_init_status_label.setObjectName("Diagnosis")
        self.fast_init_status_label.setWordWrap(True)
        fast_init_layout.addWidget(self.fast_init_btn)
        fast_init_layout.addWidget(self.fast_init_progress, 1)
        fast_init_layout.addWidget(self.fast_init_status_label, 2)

        diag_tab = QWidget()
        diag_layout = QHBoxLayout(diag_tab)
        diag_layout.setContentsMargins(10, 10, 10, 10)
        diag_layout.setSpacing(10)
        self.diag_btn = QPushButton("Diagnose")
        self.diag_btn.clicked.connect(lambda: self.worker.enqueue("diagnose", self._selected_actuator))
        self.diagnosis_label = QLabel("No diagnosis yet")
        self.diagnosis_label.setObjectName("Diagnosis")
        self.diagnosis_label.setWordWrap(True)
        diag_layout.addWidget(self.diag_btn)
        diag_layout.addWidget(self.diagnosis_label, 1)

        recover_tab = QWidget()
        recover_layout = QHBoxLayout(recover_tab)
        recover_layout.setContentsMargins(10, 10, 10, 10)
        recover_layout.setSpacing(10)
        self.recover_btn = QPushButton("Recover")
        self.recover_btn.clicked.connect(
            lambda: self.worker.enqueue(
                "recover",
                self._selected_actuator,
                self.recover_voltage_spin.value(),
                self.recover_duration_spin.value(),
            )
        )
        recover_layout.addWidget(form_label("Voltage"))
        self.recover_voltage_spin = QDoubleSpinBox()
        self.recover_voltage_spin.setRange(1.0, 500.0)
        self.recover_voltage_spin.setDecimals(1)
        self.recover_voltage_spin.setSingleStep(5.0)
        self.recover_voltage_spin.setValue(50.0)
        self.recover_voltage_spin.setSuffix(" V")
        recover_layout.addWidget(self.recover_voltage_spin)
        recover_layout.addWidget(form_label("Duration"))
        self.recover_duration_spin = QSpinBox()
        self.recover_duration_spin.setRange(1, 300)
        self.recover_duration_spin.setValue(60)
        self.recover_duration_spin.setSuffix(" s")
        recover_layout.addWidget(self.recover_duration_spin)
        recover_layout.addWidget(self.recover_btn)
        recover_layout.addStretch()

        wave_tab = QWidget()
        wave_layout = QHBoxLayout(wave_tab)
        wave_layout.setContentsMargins(10, 10, 10, 10)
        wave_layout.setSpacing(10)
        self.square_target_btn = QPushButton("Start Selected")
        square_stop = QPushButton("Stop")
        all_off = QPushButton("All Off")
        self.square_target_btn.clicked.connect(lambda: self.worker.enqueue("square_start", [self._selected_actuator]))
        square_stop.clicked.connect(lambda: self.worker.enqueue("square_stop"))
        all_off.clicked.connect(lambda: self.worker.enqueue("all_off"))
        self.square_pill = StatusPill("Stopped", "neutral")
        wave_layout.addWidget(self.square_target_btn)
        wave_layout.addWidget(square_stop)
        wave_layout.addWidget(all_off)
        wave_layout.addWidget(self.square_pill)
        wave_layout.addStretch()

        tabs.addTab(init_tab, "Initialize")
        tabs.addTab(fast_init_tab, "Fast Init")
        tabs.addTab(diag_tab, "Diagnose")
        tabs.addTab(recover_tab, "Recover")
        tabs.addTab(wave_tab, "Square Wave")
        layout.addWidget(tabs)

        return controls

    def _build_side_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

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

    def _connect(self) -> None:
        dialog = ConnectionDialog(self)
        self._connection_dialog = dialog
        dialog.attempt_requested.connect(
            lambda endpoint, options: self.worker.enqueue(
                "connect", endpoint, options
            )
        )
        dialog.exec()
        if self._connection_dialog is dialog:
            self._connection_dialog = None

    def _on_connected_changed(self, connected: bool, detail: str) -> None:
        self._connected = connected
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
        self.disconnect_btn.setEnabled(connected)
        self.disconnect_btn.setVisible(connected)
        self.connect_btn.setEnabled(not connected)
        if not connected:
            self._psu_on = False
            self._psc_on = False
            self.psu_card.set_state(False)
            self.psc_card.set_state(False)
            self.voltage_card.set_value("-", "V")
            self.current_card.set_value("-", "mA")
            self.config_card.set_value("-", "")
            if hasattr(self, "init_progress"):
                self.init_progress.setValue(0)
                self.init_elapsed_label.setText("Elapsed 0 / 120 s")
                self.fast_init_progress.setValue(0)
                self.fast_init_status_label.setText("Target 2.00 mA / max 60 s")
            self._status = None
            self._clear_actuator_cards()

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
            self.actuator_grid.addWidget(card, (actuator % 8) // 4, actuator % 4)

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
            self.init_btn.setEnabled(False)
            self.fast_init_btn.setEnabled(False)
            self.diag_btn.setEnabled(False)
            self.recover_btn.setEnabled(False)
            self.square_target_btn.setEnabled(False)

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
        self.psu_card.set_state(psu_on)
        self.psc_card.setVisible(psc_supported)
        if psc_supported:
            self.psc_card.set_state(psc_on)
        self.voltage_card.set_value(f"{float(status['voltage']):.2f}", "V")
        self.current_card.set_value(f"{float(status['current']):.2f}", "mA")
        config = status["config"]
        self.config_card.set_value(
            f"MAX {format_ms(config['max_active_ms'])} / DIS {format_ms(config['discharge_ms'])}",
            "",
        )
        for card in self._cards:
            card.update_from_status(status)
        if became_ready:
            self._request_group_detection()

    def _on_diagnosis_ready(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Actuator {actuator}: baseline {baseline_ma:.2f} mA, "
            "forward {forward_ma:.2f} mA, discharge {discharge_ma:.2f} mA".format(**result)
        )

    def _on_initialization_progress(self, result: dict[str, Any]) -> None:
        elapsed_s = int(result["elapsed_s"])
        total_s = int(result["total_s"])
        self.init_progress.setRange(0, total_s)
        self.init_progress.setValue(elapsed_s)
        self.init_elapsed_label.setText(
            "Elapsed {elapsed_s} / {total_s} s - stage {stage_index}/{stage_count}, "
            "+/-{stage_voltage:.0f} V".format(
                elapsed_s=elapsed_s,
                total_s=total_s,
                stage_index=int(result["stage_index"]),
                stage_count=int(result["stage_count"]),
                stage_voltage=float(result["stage_voltage"]),
            )
        )

    def _on_fast_init_progress(self, result: dict[str, Any]) -> None:
        elapsed_s = int(result["elapsed_s"])
        duration_s = int(result["duration_s"])
        self.fast_init_progress.setRange(0, duration_s)
        self.fast_init_progress.setValue(elapsed_s)
        self.fast_init_status_label.setText(
            "{elapsed_s} / {duration_s} s - delta {delta_ma:.2f} mA "
            "target {target_delta_ma:.2f} mA - drive {target_voltage:.0f} V "
            "- {status}".format(
                elapsed_s=elapsed_s,
                duration_s=duration_s,
                delta_ma=float(result["delta_ma"]),
                target_delta_ma=float(result["target_delta_ma"]),
                target_voltage=float(result["target_voltage"]),
                status=str(result["status"]),
            )
        )
        actuator = int(result["actuator"])
        self._cards[actuator].set_health(
            {
                "state": "fast_init",
                "delta_ma": float(result["delta_ma"]),
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
            "{outcome}: delta {delta_ma:.2f} mA at {target_voltage:.0f} V "
            "- SDK state {final_state}".format(
                outcome=outcome,
                delta_ma=float(result["delta_ma"]),
                target_voltage=float(result["target_voltage"]),
                final_state=str(result["final_state"]),
            )
        )

    def _on_recovery_ready(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Recovery {actuator} done after {duration_s}s: baseline {baseline_ma:.2f} mA, "
            "manual avg {recovery_ma:.2f} mA, delta {delta_ma:.2f} mA "
            "at {target_voltage:.1f} V target from {supply_voltage:.1f} V PSU".format(**result)
        )

    def _on_recovery_progress(self, result: dict[str, Any]) -> None:
        self.diagnosis_label.setText(
            "Recovery {actuator}: {elapsed_s}/{duration_s}s, baseline "
            "{baseline_ma:.2f} mA, current {current_ma:.2f} mA, "
            "delta {delta_ma:.2f} mA at {target_voltage:.1f} V".format(**result)
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
        if running:
            text = f"{phase}: " + ", ".join(str(actuator) for actuator in actuators)
            self.square_pill.set(text, "active")
        else:
            self.square_pill.set("Stopped", "neutral")

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
        available = state == "idle"
        error = state == "error"
        connected = state in {"idle", "error"}
        self.init_btn.setEnabled(available or error)
        self.fast_init_btn.setEnabled(available or error)
        self.diag_btn.setEnabled(available or error)
        self.recover_btn.setEnabled(connected)
        self.square_target_btn.setEnabled(available)

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
    font-size: 28px;
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
    font-size: 12px;
    text-transform: uppercase;
}
QLabel#MetricValue {
    color: #1a1b1f;
    font-size: 22px;
    font-weight: 700;
}
QLabel#MetricUnit {
    color: #5d6c7b;
    padding-bottom: 3px;
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
QLabel#ActuatorNumber {
    color: #1a1b1f;
    font-size: 20px;
    font-weight: 700;
}
QLabel#ActuatorValue {
    color: #1a1b1f;
    font-size: 16px;
    font-weight: 700;
}
QLabel#ActuatorDetail {
    color: #4f5f70;
    font-size: 13px;
    font-weight: 600;
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
QLineEdit,
QComboBox,
QSpinBox,
QDoubleSpinBox {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 7px 9px;
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
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    window = DashboardWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
