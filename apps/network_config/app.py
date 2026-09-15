"""Simple network configuration utility for every Fluid Reality NetworkBoard."""

from __future__ import annotations

import base64
import html
import ipaddress
import queue
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

APP_ROOT = Path(__file__).resolve().parent
APPS_ROOT = APP_ROOT.parent
LOGO_PATH = APP_ROOT / "assets" / "fluid_reality_logo_transparent.png"
APP_ICON_PATH = APP_ROOT / "assets" / "fluid-reality-icon.png"
COPY_ICON_PATH = APP_ROOT / "assets" / "copy.svg"
NEW_FILE_ICON_PATH = APP_ROOT / "assets" / "new-file.svg"
if str(APPS_ROOT) not in sys.path:
    sys.path.insert(0, str(APPS_ROOT))

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fluid_reality import (
    BluetoothDevice,
    ConnectionProfile,
    ConfigurableNetworkBoard,
    EthernetBoard,
    FirmwareError,
    NetworkBoard,
    WifiBoard,
    discover_bluetooth_boards,
    list_ports,
)
from network_config.tls_files import (
    GeneratedTlsFiles,
    certificate_server_name,
    create_self_signed_tls_files,
)
from network_config.icon_buttons import configure_refresh_button
from network_config.secret_fields import add_secret_visibility
from network_config.toggle import LabeledToggle


def form_label(text: str) -> QLabel:
    """Create a consistently visible label for compact forms."""

    label = QLabel(text)
    label.setObjectName("FormLabel")
    return label


def build_network_endpoint(scheme: str, host: str, port: int) -> str:
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


class BluetoothScanWorker(QThread):
    """Discover Fluid Reality BLE boards without blocking the UI."""

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
    """Collect a serial, network, or Bluetooth endpoint."""

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
        self.connection_tabs.addTab(self._serial_tab(), "Serial")
        self.connection_tabs.addTab(self._network_tab(), "Network")
        self.connection_tabs.addTab(self._bluetooth_tab(), "Bluetooth")
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
        self._update_protocol()
        self.refresh_serial_ports()

    def _serial_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(10)
        hint = QLabel("Select the USB serial port connected to the board.")
        hint.setObjectName("ConnectionHint")
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        self.serial_port.setMinimumWidth(300)
        self.refresh_ports_button = configure_refresh_button(
            QPushButton(), "Refresh serial ports"
        )
        self.refresh_ports_button.clicked.connect(self.refresh_serial_ports)
        self.serial_port_label = form_label("Serial port")
        row.addWidget(self.serial_port_label)
        row.addWidget(self.serial_port, 1)
        row.addWidget(self.refresh_ports_button)
        layout.addLayout(row)
        layout.addStretch()
        return tab

    def _network_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(12)
        hint = QLabel("Use TCP on a trusted network, or TLS for encrypted traffic.")
        hint.setObjectName("ConnectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.network_encryption = LabeledToggle("Encryption")
        self.network_encryption.toggled.connect(self._update_protocol)
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
        self.network_host_label = form_label("Host")
        self.network_port_label = form_label("Port")
        self.network_token_label = form_label("Access token")
        form.addRow(self.network_host_label, self.network_host)
        form.addRow(self.network_port_label, self.network_port)
        form.addRow(self.network_token_label, self.network_token)
        form.addRow(self.network_encryption)
        layout.addLayout(form)

        layout.addStretch()
        return tab

    def _bluetooth_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(12)
        hint = QLabel(
            "Scan for nearby Fluid Reality boards, then select the board to configure."
        )
        hint.setObjectName("ConnectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        device_row = QHBoxLayout()
        self.bluetooth_device = QComboBox()
        self.bluetooth_device.setMinimumWidth(300)
        self.refresh_bluetooth_button = configure_refresh_button(
            QPushButton(), "Scan for Bluetooth boards"
        )
        self.refresh_bluetooth_button.clicked.connect(self.refresh_bluetooth_devices)
        device_row.addWidget(form_label("Board"))
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
            ports = list_ports()
        except Exception:
            ports = []
        if current and current not in ports:
            ports.insert(0, current)
        self.serial_port.clear()
        self.serial_port.addItems(ports)
        if current:
            self.serial_port.setCurrentText(current)

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

    def _update_protocol(self) -> None:
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


class TlsCertificateDialog(QDialog):
    """Create local TLS files suitable for a Fluid Reality board."""

    def __init__(self, server_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ToolDialog")
        self.setWindowTitle("Create TLS certificate")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.generated_files: GeneratedTlsFiles | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        title = QLabel("Create certificate files")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.server_name = QLineEdit(server_name)
        self.server_name.setPlaceholderText("Optional hostname or IP address")
        form.addRow(form_label("Server name (optional)"), self.server_name)

        self.validity_days = QSpinBox()
        self.validity_days.setRange(1, 3650)
        self.validity_days.setValue(825)
        self.validity_days.setSuffix(" days")
        form.addRow(form_label("Valid for"), self.validity_days)

        self.private_key_file_row = QWidget()
        private_key_file_layout = QHBoxLayout(self.private_key_file_row)
        private_key_file_layout.setContentsMargins(0, 0, 0, 0)
        private_key_file_layout.setSpacing(7)
        self.private_key_file = QLineEdit()
        self.private_key_file.setPlaceholderText("Select an existing key or create a new one")
        private_key_browse = QPushButton("Browse…")
        private_key_browse.setObjectName("quietButton")
        private_key_browse.clicked.connect(self._choose_private_key)
        self.new_private_key_button = QPushButton()
        self.new_private_key_button.setObjectName("quietButton")
        self.new_private_key_button.setAccessibleName("Choose a new private-key file")
        self.new_private_key_button.setToolTip("Choose a new private-key file")
        self.new_private_key_button.setIcon(QIcon(str(NEW_FILE_ICON_PATH)))
        self.new_private_key_button.setIconSize(QSize(20, 20))
        self.new_private_key_button.setFixedSize(42, 38)
        self.new_private_key_button.clicked.connect(self._choose_new_private_key)
        private_key_file_layout.addWidget(self.private_key_file, 1)
        private_key_file_layout.addWidget(private_key_browse)
        private_key_file_layout.addWidget(self.new_private_key_button)
        form.addRow(form_label("Private key"), self.private_key_file_row)
        self._private_key_is_new = False

        self.key_password = QLineEdit()
        self.key_password.setPlaceholderText("Optional; leave empty for an unencrypted key")
        self.key_password_visibility = add_secret_visibility(
            self.key_password, secret_name="private-key password"
        )
        form.addRow(form_label("Key password"), self.key_password)

        layout.addLayout(form)

        note = QLabel(
            "Keep the private key private. Share only the certificate with clients that "
            "need to verify the board. You will be asked before existing files are overwritten."
        )
        note.setObjectName("help")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.error = QLabel()
        self.error.setObjectName("ConnectionError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Create")
        self.buttons.accepted.connect(self._create)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _choose_private_key(self) -> None:
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Select an existing private key",
            "",
            "Private-key files (*.pem *.key);;All files (*)",
        )
        if selected:
            self.private_key_file.setText(selected)
            self._private_key_is_new = False

    def _choose_new_private_key(self) -> None:
        name = self.server_name.text().strip()
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
        stem = stem or "fluid-reality-board"
        documents = Path.home() / "Documents"
        initial_directory = documents if documents.is_dir() else Path.home()
        selected, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save new private key",
            str(initial_directory / f"{stem}-private-key.pem"),
            "PEM private key (*.pem);;All files (*)",
        )
        if selected:
            path = Path(selected)
            if not path.suffix:
                path = path.with_suffix(".pem")
            self.private_key_file.setText(str(path))
            self._private_key_is_new = True

    def _create(self) -> None:
        name = self.server_name.text().strip()
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
        stem = stem or "fluid-reality-board"
        documents = Path.home() / "Documents"
        initial_directory = documents if documents.is_dir() else Path.home()
        selected, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save TLS certificate",
            str(initial_directory / f"{stem}-certificate.pem"),
            "PEM certificate (*.pem);;All files (*)",
            options=QFileDialog.DontConfirmOverwrite,
        )
        if not selected:
            return
        certificate_path = Path(selected)
        if not certificate_path.suffix:
            certificate_path = certificate_path.with_suffix(".pem")
        private_key_text = self.private_key_file.text().strip()
        existing_private_key = bool(private_key_text and not self._private_key_is_new)
        if private_key_text and self._private_key_is_new:
            new_private_key_path = Path(private_key_text)
        elif not private_key_text:
            certificate_stem = certificate_path.stem
            if certificate_stem.lower().endswith("-certificate"):
                certificate_stem = certificate_stem[: -len("-certificate")]
            new_private_key_path = certificate_path.with_name(
                f"{certificate_stem or stem}-private-key.pem"
            )
        else:
            new_private_key_path = None

        conflicts = [certificate_path] if certificate_path.exists() else []
        if new_private_key_path is not None and new_private_key_path.exists():
            conflicts.append(new_private_key_path)
        overwrite = False
        if conflicts:
            names = "\n".join(f"• {path.name}" for path in conflicts)
            answer = QMessageBox.question(
                self,
                "Overwrite existing files?",
                f"The following file{'s' if len(conflicts) != 1 else ''} already "
                f"exist{'s' if len(conflicts) == 1 else ''}:\n\n{names}\n\nOverwrite?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
            overwrite = True
        try:
            self.generated_files = create_self_signed_tls_files(
                certificate_path.parent,
                self.server_name.text(),
                validity_days=self.validity_days.value(),
                password=self.key_password.text(),
                private_key_file=(
                    private_key_text
                    if existing_private_key
                    else None
                ),
                certificate_file=certificate_path,
                private_key_output_file=(
                    private_key_text
                    if private_key_text and self._private_key_is_new
                    else None
                ),
                overwrite=overwrite,
            )
        except Exception as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


def decode_ssid(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode(
            "utf-8", errors="replace"
        )
    except (ValueError, UnicodeError):
        return "<invalid SSID>"


def signal_level(rssi: int) -> int:
    """Map RSSI to 0-4 signal bars."""

    if rssi >= -50:
        return 4
    if rssi >= -60:
        return 3
    if rssi >= -70:
        return 2
    if rssi >= -80:
        return 1
    return 0


def signal_icon(rssi: int) -> QIcon:
    """Draw a four-bar signal icon without external image assets."""

    pixmap = QPixmap(30, 20)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    active = signal_level(rssi)
    for index, height in enumerate((4, 7, 11, 15)):
        color = QColor("#0050bd") if index < active else QColor("#d6dae3")
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(3 + index * 6, 17 - height, 4, height), 1, 1)
    painter.end()
    return QIcon(pixmap)


def security_icon(secure: bool) -> QIcon:
    """Draw a closed or open padlock icon."""

    pixmap = QPixmap(24, 20)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    color = QColor("#1a1b1f") if secure else QColor("#7a8797")
    painter.setPen(QPen(color, 2))
    painter.setBrush(Qt.NoBrush)
    arc = QRectF(7 if secure else 9, 2, 10, 11)
    painter.drawArc(arc, 0 if secure else 30 * 16, 180 * 16)
    painter.setBrush(color)
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(QRectF(5, 9, 14, 9), 2, 2)
    painter.setBrush(QColor("white"))
    painter.drawEllipse(QPointF(12, 13), 1.2, 1.2)
    painter.end()
    return QIcon(pixmap)


class NetworkWorker(QThread):
    connected_changed = Signal(bool, str)
    transport_ready = Signal(bool)
    configuration_ready = Signal(bool)
    features_ready = Signal(tuple)
    interfaces_ready = Signal(tuple, bool)
    status_ready = Signal(dict)
    networks_ready = Signal(list)
    access_point_ready = Signal(dict)
    token_ready = Signal(str)
    diagnostics_ready = Signal(dict)
    ethernet_ready = Signal(dict)
    tls_ready = Signal(dict)
    message = Signal(str, str)

    def __init__(self, board_class: type[NetworkBoard] = WifiBoard) -> None:
        super().__init__()
        if not issubclass(board_class, NetworkBoard):
            raise TypeError("board_class must inherit from NetworkBoard")
        self._board_class = board_class
        self._board: NetworkBoard | None = None
        self._commands: queue.Queue[tuple[str, tuple[Any, ...]]] = queue.Queue()

    def enqueue(self, command: str, *args: Any) -> None:
        self._commands.put((command, args))

    def run(self) -> None:
        try:
            while not self.isInterruptionRequested():
                try:
                    command, args = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                self._execute(command, args)
        finally:
            self._close_board()

    def _execute(self, command: str, args: tuple[Any, ...]) -> None:
        try:
            if command == "connect":
                options = dict(args[1]) if len(args) > 1 else {}
                self._connect(str(args[0]), options)
            elif command == "disconnect":
                self._disconnect()
            elif command == "refresh":
                self._emit_status()
            elif command == "scan":
                self._scan()
            elif command == "join":
                network, password = args
                self._require_wifi_board().join_wifi(
                    int(network.index),
                    None if str(network.security).upper() == "OPEN" else str(password),
                )
                self.message.emit(f"Connecting to {network.ssid}", "info")
                self._wait_for_wifi(str(network.ssid), int(network.rssi))
            elif command == "join_hidden":
                self._require_wifi_board().join_hidden_wifi(str(args[0]), str(args[1]))
                self.message.emit(f"Connecting to hidden network {args[0]}", "info")
                self._wait_for_wifi(str(args[0]))
            elif command == "wifi":
                self._require_wifi_board().set_wifi_enabled(bool(args[0]))
                self._emit_status()
            elif command == "wifi_mode":
                self._require_wifi_board().set_wifi_mode(str(args[0]))
                self._emit_status()
                self._emit_access_point_status()
            elif command == "access_point_status":
                self._emit_access_point_status()
            elif command == "access_point_config":
                fields = self._require_wifi_board().configure_access_point(
                    str(args[0]), str(args[1]) or None, channel=int(args[2])
                )
                self.access_point_ready.emit(fields)
                self._emit_status()
            elif command == "ethernet":
                fields = self._require_ethernet_board().set_ethernet_enabled(
                    bool(args[0])
                )
                self.ethernet_ready.emit(fields)
                self._emit_status()
            elif command == "ethernet_status":
                self.ethernet_ready.emit(
                    self._require_ethernet_board().ethernet_status()
                )
            elif command == "disconnect_wifi":
                self._require_wifi_board().disconnect_wifi()
                self._emit_status()
            elif command == "forget_wifi":
                self._require_wifi_board().forget_wifi()
                self._emit_status()
            elif command == "apply":
                self._apply(dict(args[0]))
            elif command == "diagnostics":
                self._emit_diagnostics()
            elif command == "tls_status":
                self._emit_tls_status()
            elif command == "tls_provision":
                certificate, private_key, password, enabled = args
                fields = self._require_configurable_board().provision_tls(
                    certificate,
                    private_key,
                    private_key_password=str(password) or None,
                    enable=bool(enabled),
                )
                self.tls_ready.emit(fields)
                self.message.emit(
                    "TLS credentials installed and saved permanently", "ok"
                )
                self._emit_status()
            elif command == "tls_enable":
                fields = self._require_configurable_board().configure_tls(bool(args[0]))
                self.tls_ready.emit(fields)
                self.message.emit(
                    "TLS enabled" if args[0] else "TLS disabled", "ok"
                )
                self._emit_status()
            elif command == "tls_clear":
                fields = self._require_configurable_board().clear_tls()
                self.tls_ready.emit(fields)
                self.message.emit("TLS credentials erased", "ok")
                self._emit_status()
            else:
                raise ValueError(f"Unknown worker command: {command}")
        except Exception as exc:
            fields = getattr(exc, "fields", {})
            error_message = str(exc)
            redirected_connection_lost = (
                command != "connect"
                and self._board is not None
                and bool(getattr(self._board.transport, "redirected", False))
                and any(
                    marker in error_message.lower()
                    for marker in ("closed", "reset", "broken pipe", "forcibly")
                )
            )
            if command == "connect":
                options = dict(args[1]) if len(args) > 1 else {}
                endpoint = str(args[0]).lower() if args else ""
                connection_closed = any(
                    marker in error_message.lower()
                    for marker in ("closed", "reset", "forcibly")
                )
                if endpoint.startswith("tcp://") and connection_closed:
                    error_message = (
                        "The endpoint rejected a plain TCP connection. If TLS is enabled "
                        "on the board, select TLS and provide its certificate."
                    )
                elif (
                    endpoint.startswith("tls://")
                    and not options.get("network_token")
                    and connection_closed
                ):
                    error_message = (
                        "The network endpoint closed the connection. It may have access-token "
                        "authentication enabled; enter its token and try again."
                    )
            elif redirected_connection_lost:
                error_message = (
                    "The network connection closed, likely because the TCP/TLS setting "
                    "changed. Reconnect using the board's current protocol."
                )
            if command in {"join", "join_hidden"} and fields.get("REASON") == "INDEX":
                self.message.emit("The network list expired. Scan again, then reconnect.", "error")
            else:
                self.message.emit(error_message, "error")
            if command == "connect":
                self._close_board()
                self.connected_changed.emit(False, error_message)
            elif redirected_connection_lost:
                self._close_board()
                self.connected_changed.emit(False, error_message)
        finally:
            if command in {"join", "join_hidden"}:
                self.networks_ready.emit([])

    def _connect(self, port: str, options: dict[str, Any] | None = None) -> None:
        self._close_board()
        self._board = self._board_class(port, **(options or {}))
        self._board.force_text_mode()
        version = self._board.firmware_version()
        self.transport_ready.emit(
            bool(getattr(getattr(self._board, "transport", None), "redirected", False))
        )
        self.connected_changed.emit(
            True, f"{version.firmware} {version.version} on {port}"
        )
        self.message.emit(f"Connected to {version.firmware} {version.version}", "ok")
        configurable = self._board.supports_network_configuration()
        self.configuration_ready.emit(configurable)
        if not configurable:
            self.interfaces_ready.emit((), False)
            self.message.emit(
                "This board uses an externally managed network; only the app's "
                "TCP/TLS connection settings apply.",
                "info",
            )
            return
        board = self._require_configurable_board()
        status = board.network_status()
        interfaces = board.network_interfaces()
        self.status_ready.emit(status)
        self.interfaces_ready.emit(
            interfaces, board.network_interface_commands_supported
        )
        features = board.network_configuration_features
        self.features_ready.emit(features)
        if "AP" in features and isinstance(board, WifiBoard):
            self.access_point_ready.emit(board.access_point_status())
        if "AUTH" in features:
            redirected = bool(getattr(board.transport, "redirected", False))
            token = (
                str((options or {}).get("network_token", ""))
                if redirected
                else board.network_key()
            )
            self.token_ready.emit(token)
        if "ETH" in interfaces and isinstance(board, EthernetBoard):
            self.ethernet_ready.emit(board.ethernet_status())
        self._emit_tls_status()

    def _disconnect(self) -> None:
        self._close_board()
        self.connected_changed.emit(False, "Not connected")
        self.message.emit("Board disconnected", "info")

    def _close_board(self) -> None:
        if self._board is not None:
            try:
                self._board.close()
            finally:
                self._board = None

    def _require_board(self) -> NetworkBoard:
        if self._board is None:
            raise RuntimeError("Connect a board first")
        return self._board

    def _require_configurable_board(self) -> ConfigurableNetworkBoard:
        board = self._require_board()
        if not isinstance(board, ConfigurableNetworkBoard):
            raise RuntimeError(
                "This board's network is managed externally and cannot be configured"
            )
        return board

    def _require_wifi_board(self) -> WifiBoard:
        board = self._require_board()
        if not isinstance(board, WifiBoard):
            raise RuntimeError("This board does not provide a Wi-Fi interface")
        return board

    def _require_ethernet_board(self) -> EthernetBoard:
        board = self._require_board()
        if not isinstance(board, EthernetBoard):
            raise RuntimeError("This board does not provide an Ethernet interface")
        return board

    def _emit_status(self) -> None:
        self.status_ready.emit(self._require_configurable_board().network_status())

    def _emit_access_point_status(self) -> None:
        self.access_point_ready.emit(self._require_wifi_board().access_point_status())

    def _scan(self) -> None:
        board = self._require_wifi_board()
        board.start_wifi_scan()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            summary = board.raw_command("NET", "LIST")[0].fields
            if "COUNT" in summary:
                networks = board.wifi_networks()
                self.networks_ready.emit(networks)
                self.message.emit(
                    f"Found {len(networks)} network{'s' if len(networks) != 1 else ''}",
                    "ok" if networks else "info",
                )
                return
            time.sleep(0.25)
        raise TimeoutError("Wi-Fi scan timed out")

    def _wait_for_wifi(
        self, ssid: str, rssi: int | None = None, timeout_s: float = 35.0
    ) -> None:
        # The caller has already required WifiBoard before beginning a join.
        # Keeping this polling helper transport-shaped also makes it easy to test.
        board = self._require_board()
        deadline = time.monotonic() + timeout_s
        last_state = ""
        failure_states = {
            "AUTH_FAILED": "Authentication failed. Check the Wi-Fi password.",
            "NO_SSID": "The selected network is no longer available.",
            "CONNECTION_LOST": "The Wi-Fi connection was lost.",
        }
        reason_messages = {
            "AUTH_FAIL": "The access point rejected the password.",
            "4WAY_HANDSHAKE_TIMEOUT": "The WPA password handshake timed out.",
            "HANDSHAKE_TIMEOUT": "The WPA password handshake timed out.",
            "802_1X_AUTH_FAILED": "The network requires unsupported enterprise authentication.",
            "NO_AP_FOUND_W_COMPATIBLE_SECURITY": "The access point security mode is incompatible.",
            "ASSOC_FAIL": "The access point rejected the association request.",
            "NO_AP_FOUND": "The access point could not be found.",
        }
        while time.monotonic() < deadline:
            status = board.network_status()
            self.status_ready.emit(status)
            state = status.get("STATE", "").upper()
            reason = status.get("FAIL", "NONE").upper()
            if state and state != last_state:
                self.message.emit(f"Wi-Fi state: {state.replace('_', ' ').title()}", "info")
                last_state = state
            if state == "CONNECTED":
                address = status.get("IP", "0.0.0.0")
                self.message.emit(f"Connected to {ssid} — IP {address}", "ok")
                return
            if reason in reason_messages:
                self._emit_diagnostics()
                raise RuntimeError(reason_messages[reason])
            if state in failure_states:
                self._emit_diagnostics()
                raise RuntimeError(failure_states[state])
            time.sleep(0.5)
        if rssi is not None and rssi >= -70:
            self._emit_diagnostics()
            raise TimeoutError(
                f"Could not authenticate with {ssid}. Signal is strong ({rssi} dBm); "
                "re-enter the Wi-Fi password."
            )
        self._emit_diagnostics()
        raise TimeoutError(f"Could not connect to {ssid}. Check the password and signal strength.")

    def _emit_diagnostics(self) -> None:
        board = self._require_configurable_board()
        try:
            diagnostics = board.network_diagnostics()
        except Exception as exc:
            self.message.emit(f"Diagnostics unavailable: {exc}", "error")
            return
        self.diagnostics_ready.emit(diagnostics)

    def _emit_tls_status(self) -> None:
        try:
            fields = self._require_configurable_board().tls_status()
        except FirmwareError as exc:
            reason = exc.fields.get("REASON", "").upper()
            if exc.code not in {"BAD_COMMAND", "UNKNOWN_COMMAND"} and reason not in {
                "UNSUPPORTED", "UNKNOWN_OPERATION"
            }:
                raise
            fields = {"SUPPORTED": "NO"}
        self.tls_ready.emit(fields)

    def _apply(self, settings: dict[str, Any]) -> None:
        board = self._require_configurable_board()
        features = set(settings.get("features", ("IP", "HOST", "TCP")))
        interface = settings.get("interface") if settings.get("scoped") else None
        if "IP" in features and settings.get("apply_ip", True):
            if settings.get("access_point_mode", False):
                self._require_wifi_board().configure_access_point_ipv4(
                    settings["address"], settings["subnet"]
                )
            elif settings["dhcp"]:
                board.use_dhcp(interface)
            else:
                board.set_static_ipv4(
                    settings["address"], settings["subnet"], settings["gateway"],
                    settings["dns1"], settings["dns2"],
                    interface=interface,
                )
        if "HOST" in features:
            board.network_hostname(settings["hostname"])
        if settings.get("token_dirty"):
            token = board.set_network_key(str(settings.get("network_token", "")))
            self.token_ready.emit(token)
            self.message.emit("Access token saved", "ok")
        if "TCP" in features:
            board.configure_tcp(
                enabled=settings["tcp_enabled"],
                port=settings["tcp_port"],
                bind=settings.get("tcp_bind") if settings.get("scoped") else None,
            )
        self.message.emit("Network settings saved", "ok")
        self.status_ready.emit(board.network_status())


class StatusCard(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("MetricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        caption = QLabel(title)
        caption.setObjectName("MetricTitle")
        self.value = QLabel("—")
        self.value.setObjectName("MetricValue")
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(caption)
        layout.addWidget(self.value)


class NetworkConfigWindow(QMainWindow):
    def __init__(self, board_class: type[NetworkBoard] = WifiBoard) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality — Network Setup")
        self.resize(1080, 680)
        self._connected = False
        self._connected_over_network = False
        self._active_endpoint = ""
        self._token_dirty = False
        self._connection_dialog: ConnectionDialog | None = None
        self._action_widgets: list[QWidget] = []
        self._last_diagnostics = "No diagnostics collected."
        self._last_status: dict[str, str] = {}
        self._interfaces: tuple[str, ...] = board_class.declared_network_interfaces()
        self._interface_scoped = False
        self._network_configurable = board_class.supports_network_configuration()
        self._network_features = tuple(
            getattr(board_class, "default_network_configuration_features", ())
        )
        self._access_point_mode = False

        self.worker = NetworkWorker(board_class)
        self.worker.connected_changed.connect(self._on_connected)
        self.worker.transport_ready.connect(self._on_transport_ready)
        self.worker.configuration_ready.connect(self._on_configuration)
        self.worker.features_ready.connect(self._on_features)
        self.worker.interfaces_ready.connect(self._on_interfaces)
        self.worker.status_ready.connect(self._on_status)
        self.worker.networks_ready.connect(self._on_networks)
        self.worker.access_point_ready.connect(self._on_access_point_status)
        self.worker.token_ready.connect(self._on_token)
        self.worker.diagnostics_ready.connect(self._on_diagnostics)
        self.worker.ethernet_ready.connect(self._on_ethernet_status)
        self.worker.tls_ready.connect(self._on_tls_status)
        self.worker.message.connect(self._log)
        self.worker.start()

        self._build_ui()
        self._set_actions_enabled(False)

    def closeEvent(self, event: Any) -> None:
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

        brand_header = QWidget()
        brand_header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        brand_header.setMaximumHeight(120)
        brand_row = QHBoxLayout(brand_header)
        brand_row.setContentsMargins(0, 0, 0, 0)
        logo = QLabel()
        logo.setObjectName("Logo")
        if LOGO_PATH.exists():
            pixmap = QPixmap(str(LOGO_PATH))
            logo.setPixmap(pixmap.scaledToWidth(220, Qt.SmoothTransformation))
        else:
            logo.setText("Fluid Reality")
            logo.setObjectName("AppTitle")
        brand_text = QVBoxLayout()
        heading = QLabel("Fluid Reality Network Setup")
        heading.setObjectName("AppTitle")
        subtitle = QLabel(
            "Connect over serial, TCP, or TLS, and configure networking when the board supports it."
        )
        subtitle.setObjectName("AppSubtitle")
        brand_text.addWidget(heading)
        brand_text.addWidget(subtitle)
        brand_row.addWidget(logo)
        brand_row.addSpacing(18)
        brand_row.addLayout(brand_text)
        brand_row.addStretch()
        outer.addWidget(brand_header)
        outer.addWidget(self._connection_bar())

        self.disconnected_space = QWidget()
        self.disconnected_space.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(self.disconnected_space, 1)

        self.board_details = QWidget()
        details_layout = QVBoxLayout(self.board_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(16)
        details_layout.addLayout(self._status_row())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("ControlTabs")
        self.connection_only_tab = self._connection_only_tab()
        self.wifi_tab = self._wifi_tab()
        self.ethernet_tab = self._ethernet_tab()
        self.ip_tcp_tab = self._network_tab()
        self.tabs.addTab(self.connection_only_tab, "Connection")
        self.tabs.addTab(self.wifi_tab, "Wi-Fi")
        self.tabs.addTab(self.ethernet_tab, "Ethernet")
        self.tabs.addTab(self.ip_tcp_tab, "IP & TCP")
        self._apply_interface_visibility()
        details_layout.addWidget(self.tabs, 1)
        outer.addWidget(self.board_details, 1)
        self.board_details.hide()

        self.activity_panel = QWidget()
        activity_layout = QVBoxLayout(self.activity_panel)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        activity_layout.setSpacing(8)
        log_header = QHBoxLayout()
        activity_title = QLabel("Activity")
        activity_title.setObjectName("ToolTitle")
        log_header.addWidget(activity_title)
        log_header.addStretch()
        diagnostics = QPushButton("Run diagnostics")
        diagnostics.setObjectName("quietButton")
        diagnostics.clicked.connect(lambda: self.worker.enqueue("diagnostics"))
        self.copy_diagnostics_btn = QPushButton("Copy diagnostics")
        self.copy_diagnostics_btn.setObjectName("quietButton")
        self.copy_diagnostics_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self._last_diagnostics)
        )
        clear = QPushButton("Clear")
        clear.setObjectName("quietButton")
        clear.clicked.connect(lambda: self.log.clear())
        log_header.addWidget(diagnostics)
        log_header.addWidget(self.copy_diagnostics_btn)
        log_header.addWidget(clear)
        self._action_widgets.extend([diagnostics, self.copy_diagnostics_btn])
        activity_layout.addLayout(log_header)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(100)
        activity_layout.addWidget(self.log)
        outer.addWidget(self.activity_panel)
        self.activity_panel.hide()
        self.setStyleSheet(STYLE)

    def _connection_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar.setFixedHeight(66)
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 12, 14, 12)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("quietButton")
        self.disconnect_btn.clicked.connect(lambda: self.worker.enqueue("disconnect"))
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.hide()
        self.connection_label = QLabel("Not connected")
        self.connection_label.setProperty("kind", "neutral")
        self.activity_toggle = LabeledToggle("Activity")
        self.activity_toggle.setChecked(False)
        self.activity_toggle.toggled.connect(self._update_activity_visibility)
        self.activity_toggle.hide()
        heading = QLabel("Board connection")
        heading.setObjectName("ToolTitle")
        hint = QLabel("Connect by Serial, TCP, or TLS")
        hint.setObjectName("ConnectionHint")
        row.addWidget(heading)
        row.addSpacing(8)
        row.addWidget(hint)
        row.addStretch()
        row.addWidget(self.connect_btn)
        row.addWidget(self.disconnect_btn)
        row.addWidget(self.activity_toggle)
        row.addWidget(self.connection_label)
        return bar

    def _status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.wifi_status = StatusCard("Network state")
        self.ssid_status = StatusCard("Network / link")
        self.ip_status = StatusCard("IP address")
        self.mac_status = StatusCard("Interface MAC")
        self.tcp_status = StatusCard("TCP server")
        self._status_cards = (
            self.wifi_status,
            self.ssid_status,
            self.ip_status,
            self.mac_status,
            self.tcp_status,
        )
        for card in self._status_cards:
            row.addWidget(card, 1)
        return row

    def _connection_only_tab(self) -> QWidget:
        tab, layout = self._tab(
            "This board reaches the network through another device. Its network "
            "addressing and security are managed externally, so this app will not "
            "send device-side NET configuration commands."
        )
        title = QLabel("Connection-only network")
        title.setObjectName("SectionTitle")
        self.connection_only_detail = QLabel(
            "Use Connect to enter the TCP or TLS host, port, access token, and certificate."
        )
        self.connection_only_detail.setObjectName("help")
        self.connection_only_detail.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.connection_only_detail)
        layout.addStretch()
        return tab

    def _tab(self, explanation: str) -> tuple[QWidget, QVBoxLayout]:
        tab = QScrollArea()
        tab.setObjectName("ConfigScroll")
        tab.setWidgetResizable(True)
        tab.setFrameShape(QFrame.NoFrame)
        tab.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("ConfigPage")
        tab.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        text = QLabel(explanation)
        text.setObjectName("help")
        text.setWordWrap(True)
        layout.addWidget(text)
        return tab, layout

    @staticmethod
    def _form_label(text: str) -> QLabel:
        return form_label(text)

    def _wifi_tab(self) -> QWidget:
        tab, layout = self._tab("Choose a nearby network. A lock means a password is required.")
        controls = QHBoxLayout()
        self.wifi_enabled = LabeledToggle("Wi-Fi enabled")
        apply_wifi = QPushButton("Apply")
        apply_wifi.setObjectName("quietButton")
        apply_wifi.clicked.connect(
            lambda: self.worker.enqueue("wifi", self.wifi_enabled.isChecked())
        )
        scan = QPushButton("Scan for networks")
        scan.clicked.connect(lambda: self.worker.enqueue("scan"))
        controls.addWidget(self.wifi_enabled)
        controls.addWidget(apply_wifi)
        controls.addStretch()
        controls.addWidget(scan)
        layout.addLayout(controls)
        self.wifi_scan_button = scan

        mode_form = QFormLayout()
        self.wifi_mode_label = form_label("Mode")
        self.wifi_mode = QComboBox()
        self.wifi_mode.addItem("Client", "CLIENT")
        self.wifi_mode.addItem("Access point", "ACCESS_POINT")
        self.wifi_mode.activated.connect(
            lambda: self.worker.enqueue("wifi_mode", self.wifi_mode.currentData())
        )
        mode_form.addRow(self.wifi_mode_label, self.wifi_mode)
        layout.addLayout(mode_form)

        self.access_point_panel = QFrame()
        self.access_point_panel.setObjectName("ConfigCard")
        access_point_layout = QVBoxLayout(self.access_point_panel)
        access_point_layout.setContentsMargins(14, 12, 14, 12)
        access_point_form = QFormLayout()
        self.access_point_ssid = QLineEdit()
        self.access_point_ssid.setPlaceholderText("Access point network name")
        self.access_point_password = QLineEdit()
        self.access_point_password.setPlaceholderText("Empty creates an open network")
        self.access_point_password_visibility = add_secret_visibility(
            self.access_point_password, secret_name="access point password"
        )
        self.access_point_channel = QSpinBox()
        self.access_point_channel.setRange(1, 13)
        access_point_form.addRow(form_label("Network name"), self.access_point_ssid)
        access_point_form.addRow(form_label("Password"), self.access_point_password)
        access_point_form.addRow(form_label("Channel"), self.access_point_channel)
        access_point_layout.addLayout(access_point_form)
        ap_footer = QHBoxLayout()
        self.access_point_state = QLabel("Access point settings not read")
        self.access_point_state.setObjectName("help")
        self.access_point_apply = QPushButton("Apply")
        self.access_point_apply.clicked.connect(self._apply_access_point)
        ap_footer.addWidget(self.access_point_state, 1)
        ap_footer.addWidget(self.access_point_apply)
        access_point_layout.addLayout(ap_footer)
        layout.addWidget(self.access_point_panel)
        self.access_point_panel.hide()

        self.network_list = QTreeWidget()
        self.network_list.setHeaderLabels(
            ["Network", "Signal", "Security", "Channel", "Access point MAC"]
        )
        self.network_list.setRootIsDecorated(False)
        self.network_list.setAlternatingRowColors(True)
        self.network_list.setSelectionMode(QTreeWidget.SingleSelection)
        self.network_list.setColumnWidth(0, 280)
        self.network_list.setColumnWidth(1, 105)
        self.network_list.setColumnWidth(2, 125)
        self.network_list.setColumnWidth(3, 75)
        self.network_list.setColumnWidth(4, 155)
        self.network_list.itemSelectionChanged.connect(self._network_selected)
        layout.addWidget(self.network_list, 1)

        join_row = QHBoxLayout()
        self.network_password = QLineEdit()
        self.network_password.setPlaceholderText("Password")
        self.network_password_visibility = add_secret_visibility(
            self.network_password, secret_name="Wi-Fi password"
        )
        self.network_password.returnPressed.connect(self._join_selected)
        self.join_btn = QPushButton("Connect to selected network")
        self.join_btn.clicked.connect(self._join_selected)
        join_row.addWidget(self.network_password, 1)
        join_row.addWidget(self.join_btn)
        layout.addLayout(join_row)

        self.hidden_toggle = LabeledToggle("The network is hidden")
        self.hidden_toggle.toggled.connect(self._show_hidden)
        layout.addWidget(self.hidden_toggle)
        self.hidden_row = QFrame()
        hidden_layout = QHBoxLayout(self.hidden_row)
        hidden_layout.setContentsMargins(0, 0, 0, 0)
        self.hidden_ssid = QLineEdit()
        self.hidden_ssid.setPlaceholderText("Network name")
        self.hidden_password = QLineEdit()
        self.hidden_password.setPlaceholderText("Password")
        self.hidden_password_visibility = add_secret_visibility(
            self.hidden_password, secret_name="Wi-Fi password"
        )
        hidden_join = QPushButton("Connect")
        hidden_join.clicked.connect(self._join_hidden)
        hidden_layout.addWidget(self.hidden_ssid)
        hidden_layout.addWidget(self.hidden_password)
        hidden_layout.addWidget(hidden_join)
        self.hidden_row.hide()
        layout.addWidget(self.hidden_row)

        footer = QHBoxLayout()
        disconnect = QPushButton("Disconnect Wi-Fi")
        disconnect.setObjectName("quietButton")
        disconnect.clicked.connect(lambda: self.worker.enqueue("disconnect_wifi"))
        forget = QPushButton("Forget saved network")
        forget.setObjectName("dangerButton")
        forget.clicked.connect(self._forget_wifi)
        footer.addStretch()
        footer.addWidget(disconnect)
        footer.addWidget(forget)
        layout.addLayout(footer)
        self._action_widgets.extend(
            [apply_wifi, scan, self.join_btn, hidden_join, disconnect, forget,
             self.wifi_mode, self.access_point_apply]
        )
        self._client_mode_widgets = [
            self.network_list, self.network_password, self.join_btn,
            self.hidden_toggle, disconnect, forget,
        ]
        self._network_selected()
        return tab

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
        self.worker.enqueue(
            "access_point_config", ssid, password, self.access_point_channel.value()
        )

    def _update_wifi_mode_visibility(self) -> None:
        access_point = self.wifi_mode.currentData() == "ACCESS_POINT"
        self.access_point_panel.setVisible(access_point)
        for widget in self._client_mode_widgets:
            widget.setVisible(not access_point)
        self.hidden_row.setVisible(not access_point and self.hidden_toggle.isChecked())
        self.wifi_scan_button.setEnabled(self._connected and not access_point)

    def _ethernet_tab(self) -> QWidget:
        tab, layout = self._tab(
            "Configure and inspect the wired Ethernet interface. Link information "
            "is reported independently from Wi-Fi."
        )
        form = QFormLayout()
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        self.ethernet_enabled = LabeledToggle("Ethernet enabled")
        self.ethernet_link = QLabel("Not read")
        self.ethernet_speed = QLabel("—")
        self.ethernet_duplex = QLabel("—")
        self.ethernet_mac = QLabel("—")
        for value in (
            self.ethernet_link,
            self.ethernet_speed,
            self.ethernet_duplex,
            self.ethernet_mac,
        ):
            value.setObjectName("FormValue")
        form.addRow(form_label("Interface"), self.ethernet_enabled)
        form.addRow(form_label("Link"), self.ethernet_link)
        form.addRow(form_label("Speed"), self.ethernet_speed)
        form.addRow(form_label("Duplex"), self.ethernet_duplex)
        form.addRow(form_label("MAC address"), self.ethernet_mac)
        layout.addLayout(form)

        controls = QHBoxLayout()
        ethernet_apply = QPushButton("Apply Ethernet on/off")
        ethernet_apply.clicked.connect(
            lambda: self.worker.enqueue(
                "ethernet", self.ethernet_enabled.isChecked()
            )
        )
        ethernet_refresh = configure_refresh_button(
            QPushButton(), "Refresh Ethernet link status"
        )
        ethernet_refresh.clicked.connect(
            lambda: self.worker.enqueue("ethernet_status")
        )
        controls.addWidget(ethernet_apply)
        controls.addWidget(ethernet_refresh)
        controls.addStretch()
        layout.addLayout(controls)
        layout.addStretch()
        self._action_widgets.extend([ethernet_apply, ethernet_refresh])
        return tab

    def _network_tab(self) -> QWidget:
        tab, layout = self._tab(
            "Configure the board's network address, TCP server, and client access."
        )

        self.ip_panel = QFrame()
        self.ip_panel.setObjectName("ConfigCard")
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
        form.addRow(self._form_label("Interface"), self.ip_interface)
        self.use_dhcp = LabeledToggle("Automatically obtain an IP address (DHCP)")
        self.use_dhcp.setChecked(True)
        self.use_dhcp.toggled.connect(self._update_ip_enabled)
        self.ip_assignment = QWidget()
        assignment_layout = QVBoxLayout(self.ip_assignment)
        assignment_layout.setContentsMargins(0, 0, 0, 0)
        assignment_layout.setSpacing(0)
        assignment_layout.addWidget(self.use_dhcp)
        self.ap_assignment = QLabel("Fixed by access-point mode")
        self.ap_assignment.setObjectName("Hint")
        self.ap_assignment.hide()
        assignment_layout.addWidget(self.ap_assignment)
        form.addRow(self._form_label("IP assignment"), self.ip_assignment)
        self.address = QLineEdit("192.168.1.64")
        self.subnet = QLineEdit("255.255.255.0")
        self.gateway = QLineEdit("192.168.1.1")
        self.dns1 = QLineEdit("1.1.1.1")
        self.dns2 = QLineEdit("8.8.8.8")
        self.hostname = QLineEdit("fluid-reality")
        for label, widget in (
            ("IP address", self.address), ("Subnet mask", self.subnet),
            ("Gateway", self.gateway), ("Primary DNS", self.dns1),
            ("Secondary DNS", self.dns2), ("Hostname", self.hostname),
        ):
            form.addRow(self._form_label(label), widget)
        self._static_fields = [self.address, self.subnet, self.gateway, self.dns1, self.dns2]
        ip_layout.addLayout(form)
        layout.addWidget(self.ip_panel)

        self.tcp_panel = QFrame()
        self.tcp_panel.setObjectName("ConfigCard")
        tcp_layout = QVBoxLayout(self.tcp_panel)
        tcp_layout.setContentsMargins(16, 14, 16, 14)
        tcp_layout.setSpacing(10)
        tcp_title = QLabel("TCP server")
        tcp_title.setObjectName("SectionTitle")
        tcp_layout.addWidget(tcp_title)
        self.tcp_enabled = LabeledToggle("Enable TCP server")
        self.tcp_enabled.setChecked(True)
        tcp_layout.addWidget(self.tcp_enabled)

        self.tcp_settings_row = QHBoxLayout()
        self.tcp_settings_row.setSpacing(10)
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1, 65535)
        self.tcp_port.setValue(49765)
        self.tcp_port.setFixedWidth(110)
        self.tcp_bind = QComboBox()
        self.tcp_bind.addItem("Any available interface", "ANY")
        self.tcp_bind.setMinimumWidth(220)
        self.tcp_settings_row.addWidget(self._form_label("Port"))
        self.tcp_settings_row.addWidget(self.tcp_port)
        self.tcp_settings_row.addSpacing(16)
        self.tcp_settings_row.addWidget(self._form_label("Interface"))
        self.tcp_settings_row.addWidget(self.tcp_bind, 1)
        tcp_layout.addLayout(self.tcp_settings_row)
        layout.addWidget(self.tcp_panel)

        self.access_panel = QFrame()
        self.access_panel.setObjectName("ConfigCard")
        access_layout = QVBoxLayout(self.access_panel)
        access_layout.setContentsMargins(16, 14, 16, 14)
        access_layout.setSpacing(10)
        access_title = QLabel("Access token")
        access_title.setObjectName("SectionTitle")
        access_layout.addWidget(access_title)
        token_row = QHBoxLayout()
        self.token = QLineEdit()
        self.token.setReadOnly(True)
        self.token_visibility = add_secret_visibility(
            self.token, secret_name="access token"
        )
        self.token.setPlaceholderText("No access token configured")
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
        self.generate_token_btn.clicked.connect(self._regenerate_token)
        token_row.addWidget(self.token, 1)
        token_row.addWidget(self.copy_token_btn)
        token_row.addWidget(self.generate_token_btn)
        access_layout.addLayout(token_row)
        layout.addWidget(self.access_panel)

        self.encryption_panel = self._tls_section()
        layout.addWidget(self.encryption_panel)

        layout.addStretch()
        save_row = QHBoxLayout()
        save_row.addStretch()
        self.create_connection_file_btn = QPushButton("Create connection file")
        self.create_connection_file_btn.setObjectName("quietButton")
        self.create_connection_file_btn.clicked.connect(self._create_connection_file)
        save_row.addWidget(self.create_connection_file_btn)
        self.save_network_settings = QPushButton("Save network settings")
        self.save_network_settings.clicked.connect(self._apply_settings)
        save_row.addWidget(self.save_network_settings)
        layout.addLayout(save_row)
        self._action_widgets.extend(
            [self.create_connection_file_btn, self.save_network_settings,
             self.copy_token_btn, self.generate_token_btn]
        )
        self._update_ip_enabled()
        return tab

    def _tls_section(self) -> QWidget:
        section = QFrame()
        section.setObjectName("ConfigCard")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        title = QLabel("Encryption")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        settings_row = QHBoxLayout()
        self.tls_enabled = LabeledToggle("Enable TLS encryption")
        self.tls_enabled.setChecked(False)
        self.tls_enabled.toggled.connect(self._update_tls_credentials_enabled)
        settings_row.addWidget(self.tls_enabled)
        settings_row.addStretch()
        self.tls_apply_btn = QPushButton("Save encryption setting")
        self.tls_apply_btn.setObjectName("quietButton")
        self.tls_apply_btn.clicked.connect(
            lambda: self.worker.enqueue("tls_enable", self.tls_enabled.isChecked())
        )
        settings_row.addWidget(self.tls_apply_btn)
        layout.addLayout(settings_row)

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
        certificate_browse = QPushButton("Browse…")
        certificate_browse.setObjectName("quietButton")
        certificate_browse.clicked.connect(
            lambda: self._choose_tls_file(self.tls_certificate_path, "Select server certificate")
        )
        certificate_row.addWidget(self.tls_certificate_path, 1)
        certificate_row.addWidget(certificate_browse)
        credentials_form.addRow(form_label("Server certificate"), certificate_row)

        key_row = QHBoxLayout()
        self.tls_key_path = QLineEdit()
        self.tls_key_path.setPlaceholderText("Select the matching .pem or .key file")
        key_browse = QPushButton("Browse…")
        key_browse.setObjectName("quietButton")
        key_browse.clicked.connect(
            lambda: self._choose_tls_file(self.tls_key_path, "Select private key")
        )
        key_row.addWidget(self.tls_key_path, 1)
        key_row.addWidget(key_browse)
        credentials_form.addRow(form_label("Private key"), key_row)

        self.tls_key_password = QLineEdit()
        self.tls_key_password.setPlaceholderText("Only needed if the private key is encrypted")
        self.tls_key_password_visibility = add_secret_visibility(
            self.tls_key_password, secret_name="private-key password"
        )
        credentials_form.addRow(form_label("Key password"), self.tls_key_password)
        credentials_layout.addLayout(credentials_form)

        install_row = QHBoxLayout()
        self.tls_create_btn = QPushButton("Create certificate…")
        self.tls_create_btn.setObjectName("quietButton")
        self.tls_create_btn.clicked.connect(self._create_tls_credentials)
        install_row.addWidget(self.tls_create_btn)
        install_row.addStretch()
        self.tls_install_btn = QPushButton("Install credentials")
        self.tls_install_btn.clicked.connect(self._install_tls)
        install_row.addWidget(self.tls_install_btn)
        credentials_layout.addLayout(install_row)

        footer = QHBoxLayout()
        footer.addStretch()
        self.tls_clear_btn = QPushButton("Erase TLS credentials")
        self.tls_clear_btn.setObjectName("dangerButton")
        self.tls_clear_btn.clicked.connect(self._clear_tls)
        footer.addWidget(self.tls_clear_btn)
        credentials_layout.addLayout(footer)
        layout.addWidget(self.tls_credentials_panel)
        self._tls_action_widgets = [
            self.tls_enabled, self.tls_create_btn, certificate_browse, key_browse,
            self.tls_install_btn, self.tls_apply_btn, self.tls_clear_btn,
        ]
        self._action_widgets.extend(self._tls_action_widgets)
        self._tls_supported = True
        self._update_tls_credentials_enabled()
        return section

    def _update_tls_credentials_enabled(self) -> None:
        if not hasattr(self, "tls_credentials_panel"):
            return
        self.tls_credentials_panel.setEnabled(
            self._connected
            and getattr(self, "_tls_supported", True)
            and self.tls_enabled.isChecked()
        )

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
        self._log(
            f"Created TLS certificate and private key in "
            f"{dialog.generated_files.certificate.parent}",
            "ok",
        )

    def _install_tls(self) -> None:
        certificate_path = Path(self.tls_certificate_path.text().strip())
        key_path = Path(self.tls_key_path.text().strip())
        if not certificate_path.is_file() or not key_path.is_file():
            QMessageBox.information(
                self, "TLS credentials", "Select an existing certificate and private-key file."
            )
            return
        try:
            certificate = certificate_path.read_bytes()
            private_key = key_path.read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "TLS credentials", str(exc))
            return
        if b"-----BEGIN CERTIFICATE-----" not in certificate:
            QMessageBox.information(self, "TLS certificate", "The certificate must be PEM encoded.")
            return
        if b"-----BEGIN" not in private_key or b"PRIVATE KEY-----" not in private_key:
            QMessageBox.information(self, "TLS private key", "The private key must be PEM encoded.")
            return
        self.worker.enqueue(
            "tls_provision", certificate, private_key,
            self.tls_key_password.text(), False,
        )

    def _clear_tls(self) -> None:
        if QMessageBox.question(
            self,
            "Erase TLS credentials",
            "Disable TLS and permanently erase the certificate and private key from the board?",
        ) == QMessageBox.Yes:
            self.worker.enqueue("tls_clear")

    def _connect(self) -> None:
        dialog = ConnectionDialog(self)
        self._connection_dialog = dialog
        dialog.attempt_requested.connect(self._request_connection)
        dialog.exec()
        if self._connection_dialog is dialog:
            self._connection_dialog = None

    def _request_connection(self, endpoint: str, options: dict[str, Any]) -> None:
        self._active_endpoint = endpoint
        self.worker.enqueue("connect", endpoint, options)

    def _network_selected(self) -> None:
        items = self.network_list.selectedItems()
        network = items[0].data(0, Qt.UserRole) if items else None
        secure = network is not None and network.security.upper() != "OPEN"
        self.network_password.setVisible(secure)
        self.join_btn.setEnabled(self._connected and network is not None)

    def _join_selected(self) -> None:
        items = self.network_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Wi-Fi", "Select a network first.")
            return
        network = items[0].data(0, Qt.UserRole)
        password = self.network_password.text()
        if network.security.upper() != "OPEN" and not password:
            QMessageBox.information(self, "Wi-Fi", "Enter the network password.")
            return
        self.worker.enqueue("join", network, password)

    def _show_hidden(self, visible: bool) -> None:
        self.hidden_row.setVisible(visible)

    def _join_hidden(self) -> None:
        ssid = self.hidden_ssid.text().strip()
        password = self.hidden_password.text()
        if not ssid or not password:
            QMessageBox.information(self, "Hidden network", "Enter the network name and password.")
            return
        self.worker.enqueue("join_hidden", ssid, password)

    def _forget_wifi(self) -> None:
        if QMessageBox.question(
            self, "Forget network", "Remove the saved Wi-Fi credentials from the board?"
        ) == QMessageBox.Yes:
            self.worker.enqueue("forget_wifi")

    def _apply_settings(self) -> None:
        if "HOST" in self._network_features and not self.hostname.text().strip():
            QMessageBox.information(self, "Hostname", "Enter a hostname.")
            return
        if "IP" in self._network_features and self._access_point_mode:
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
        if self._connected_over_network:
            answer = QMessageBox.warning(
                self,
                "Network connection will close",
                "Saving network settings will close the current network connection. "
                "You will need to reconnect using the new settings.\n\n"
                "Do you want to continue?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        self.worker.enqueue(
            "apply",
            {
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
                "scoped": self._interface_scoped,
                "features": self._network_features,
                "network_token": self.token.text(),
                "token_dirty": self._token_dirty,
            },
        )

    def _prepare_connection_file(self) -> tuple[ConnectionProfile, Path] | None:
        features = set(self._network_features)
        current_ip = self._last_status.get("IP", "").strip()
        hostname = self.hostname.text().strip()
        if "IP" in features and not self._access_point_mode and not self.use_dhcp.isChecked():
            host = self.address.text().strip()
        elif "HOST" in features and hostname:
            host = hostname
        else:
            host = current_ip or self.address.text().strip()
        if (not host or host == "0.0.0.0") and self._connected_over_network:
            parsed_endpoint = urlsplit(self._active_endpoint)
            if parsed_endpoint.scheme in {"tcp", "tls"}:
                host = parsed_endpoint.hostname or ""
        if not host or host == "0.0.0.0":
            host, accepted = QInputDialog.getText(
                self,
                "Connection address",
                "Enter the hostname or IP address clients should use to connect:",
            )
            host = host.strip()
            if not accepted or not host:
                return None

        encrypted = self.tls_enabled.isChecked() and "TLS" in features
        certificate_text: str | None = None
        certificate_file: str | None = None
        server_hostname: str | None = None
        certificate_path = Path(self.tls_certificate_path.text().strip())
        if encrypted:
            include_certificate = QMessageBox.question(
                self,
                "Include TLS certificate",
                "Include the public TLS certificate inside the connection file?\n\n"
                "Private keys are never included.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            ) == QMessageBox.Yes
            if include_certificate and not certificate_path.is_file():
                selected, _filter = QFileDialog.getOpenFileName(
                    self,
                    "Select the board TLS certificate",
                    "",
                    "Certificate files (*.pem *.crt *.cer);;All files (*)",
                )
                certificate_path = Path(selected) if selected else Path()
                if not selected:
                    return None
            if certificate_path.is_file():
                server_hostname = certificate_server_name(certificate_path) or hostname or host
                if include_certificate:
                    try:
                        certificate_text = certificate_path.read_text(encoding="utf-8")
                    except OSError as exc:
                        QMessageBox.warning(self, "Connection file", str(exc))
                        return None
                else:
                    certificate_file = str(certificate_path)
            else:
                server_hostname = hostname or host

        profile = ConnectionProfile(
            transport="tls" if encrypted else "tcp",
            host=host,
            port=self.tcp_port.value(),
            access_token=self.token.text() or None,
            tls_certificate=certificate_text,
            tls_certificate_file=certificate_file,
            tls_server_hostname=server_hostname,
        )
        safe_name = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in host
        ).strip("-") or "fluid-reality"
        documents = Path.home() / "Documents"
        initial = (documents if documents.is_dir() else Path.home()) / f"{safe_name}.connection.yaml"
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save connection file",
            str(initial),
            "YAML connection files (*.yaml *.yml)",
        )
        if not selected:
            return None
        destination = Path(selected)
        if destination.suffix.lower() not in {".yaml", ".yml"}:
            destination = destination.with_suffix(".yaml")
        return profile, destination

    def _create_connection_file(self) -> None:
        pending = self._prepare_connection_file()
        if pending is None:
            return
        profile, path = pending
        try:
            profile.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Connection file", str(exc))
            return
        self._log(f"Saved connection file: {path}", "ok")

    def _regenerate_token(self) -> None:
        self.token.setText(secrets.token_hex(16))
        self._token_dirty = True
        self._log(
            "New access token prepared. Save network settings to apply it.", "info"
        )

    def _on_connected(self, connected: bool, detail: str) -> None:
        self._connected = connected
        if not connected:
            self._connected_over_network = False
            self._active_endpoint = ""
            self._clear_board_details()
        self.disconnected_space.setVisible(not connected)
        self.board_details.setVisible(connected)
        self.activity_toggle.setVisible(connected)
        self._update_activity_visibility()
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
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        self.disconnect_btn.setVisible(connected)
        self._set_actions_enabled(connected and self._network_configurable)
        if connected and not self._network_configurable:
            self.connection_only_detail.setText(
                f"Connected: {detail}\n\nThe host, port, TCP/TLS mode, token, and "
                "certificate remain app connection settings."
            )
        self._network_selected()

    def _update_activity_visibility(self) -> None:
        if hasattr(self, "activity_panel"):
            self.activity_panel.setVisible(
                self._connected and self.activity_toggle.isChecked()
            )

    def _on_transport_ready(self, connected_over_network: bool) -> None:
        self._connected_over_network = bool(connected_over_network)

    def _clear_board_details(self) -> None:
        for card in getattr(self, "_status_cards", ()):
            card.value.setText("—")
        self.network_list.clear()
        self.network_password.clear()
        self.hidden_ssid.clear()
        self.hidden_password.clear()
        self.token.clear()
        self._token_dirty = False
        self.ethernet_link.setText("Not read")
        self.ethernet_speed.setText("—")
        self.ethernet_duplex.setText("—")
        self.ethernet_mac.setText("—")

    def _on_configuration(self, configurable: bool) -> None:
        self._network_configurable = bool(configurable)
        self._apply_interface_visibility()
        self._set_actions_enabled(self._connected and self._network_configurable)

    def _on_features(self, features: tuple[str, ...]) -> None:
        self._network_features = tuple(str(item).upper() for item in features)
        self._apply_interface_visibility()

    def _apply_interface_visibility(self) -> None:
        if not hasattr(self, "tabs"):
            return
        configurable = self._network_configurable
        features = set(self._network_features)
        has_wifi = configurable and "WIFI" in self._interfaces
        has_ethernet = configurable and "ETH" in self._interfaces
        self.tabs.setTabVisible(
            self.tabs.indexOf(self.connection_only_tab), not configurable
        )
        self.tabs.setTabVisible(self.tabs.indexOf(self.wifi_tab), has_wifi)
        access_point_supported = has_wifi and "AP" in features
        self.wifi_mode_label.setVisible(access_point_supported)
        self.wifi_mode.setVisible(access_point_supported)
        if not access_point_supported:
            self.wifi_mode.setCurrentIndex(0)
        self._update_wifi_mode_visibility()
        self.tabs.setTabVisible(self.tabs.indexOf(self.ethernet_tab), has_ethernet)
        self.tabs.setTabVisible(
            self.tabs.indexOf(self.ip_tcp_tab),
            configurable and bool(features & {"IP", "HOST", "TCP", "AUTH", "TLS"}),
        )
        visible_page_count = sum(
            self.tabs.isTabVisible(index) for index in range(self.tabs.count())
        )
        single_page = visible_page_count <= 1
        self.tabs.tabBar().setVisible(not single_page)
        if self.tabs.property("singlePage") != single_page:
            self.tabs.setProperty("singlePage", single_page)
            self.tabs.style().unpolish(self.tabs)
            self.tabs.style().polish(self.tabs)
        if hasattr(self, "ip_form"):
            for widget in (
                self.ip_interface, self.ip_assignment, self.address, self.subnet,
                self.gateway, self.dns1, self.dns2,
            ):
                self.ip_form.setRowVisible(widget, "IP" in features)
            self.ip_form.setRowVisible(self.hostname, "HOST" in features)
            self.ip_panel.setVisible(bool(features & {"IP", "HOST"}))
            self.tcp_panel.setVisible("TCP" in features)
            self._update_ip_mode_display()
            self.access_panel.setVisible("AUTH" in features)
            self.encryption_panel.setVisible("TLS" in features)
            self.save_network_settings.setVisible(bool(features & {"IP", "HOST", "TCP"}))
        for card in getattr(self, "_status_cards", ()):
            card.setVisible(configurable)

        selected = self.ip_interface.currentData()
        self.ip_interface.clear()
        labels = {"WIFI": "Wi-Fi", "ETH": "Ethernet", "HOST": "Host default"}
        display_name = lambda value: labels.get(value, value.replace("_", " ").title())
        for interface in self._interfaces:
            self.ip_interface.addItem(display_name(interface), interface)
        if selected:
            index = self.ip_interface.findData(selected)
            if index >= 0:
                self.ip_interface.setCurrentIndex(index)
        self.ip_interface.setEnabled(self._interface_scoped and bool(self._interfaces))

        selected_bind = self.tcp_bind.currentData()
        self.tcp_bind.clear()
        self.tcp_bind.addItem("Any available interface", "ANY")
        for interface in self._interfaces:
            self.tcp_bind.addItem(display_name(interface), interface)
        index = self.tcp_bind.findData(selected_bind)
        if index >= 0:
            self.tcp_bind.setCurrentIndex(index)
        self.tcp_bind.setEnabled(self._interface_scoped and bool(self._interfaces))

    def _on_interfaces(self, interfaces: tuple[str, ...], scoped: bool) -> None:
        self._interfaces = tuple(interfaces)
        self._interface_scoped = bool(scoped)
        self._apply_interface_visibility()
        if not self._network_configurable:
            self._log("Network configuration is managed externally", "info")
            return
        names = {"WIFI": "Wi-Fi", "ETH": "Ethernet"}
        description = ", ".join(names.get(item, item) for item in self._interfaces)
        self._log(
            f"Network interfaces: {description or 'none reported'}"
            + (" (interface-scoped configuration)" if scoped else " (legacy protocol)"),
            "info",
        )

    def _on_ethernet_status(self, fields: dict[str, str]) -> None:
        enabled = fields.get("ENABLED", fields.get("ETH", "ON")).upper() == "ON"
        link = fields.get("LINK", fields.get("STATE", "UNKNOWN"))
        self.ethernet_enabled.setChecked(enabled)
        self.ethernet_link.setText(link.replace("_", " ").title())
        speed = fields.get("SPEED", "—")
        self.ethernet_speed.setText(
            f"{speed} Mbps" if speed not in {"—", "UNKNOWN"} else speed
        )
        self.ethernet_duplex.setText(fields.get("DUPLEX", "—").title())
        self.ethernet_mac.setText(fields.get("MAC", "—"))

    def _on_status(self, fields: dict[str, str]) -> None:
        self._last_status = dict(fields)
        self._access_point_mode = (
            fields.get("WIFI_MODE", "CLIENT").upper() == "ACCESS_POINT"
        )
        state = fields.get("STATE", "UNKNOWN").replace("_", " ").title()
        if "WIFI" in self._interfaces:
            network_name = decode_ssid(fields.get("SSID64", "")) or "Not selected"
        else:
            network_name = fields.get("LINK", "Not connected").replace("_", " ").title()
        address = fields.get("IP", "0.0.0.0")
        transport = "TLS" if fields.get("TLS", "OFF").upper() == "ON" else "Plain"
        tcp = f"{'On' if fields.get('TCP', 'OFF').upper() == 'ON' else 'Off'} · {transport} · {fields.get('PORT', '—')}"
        self.wifi_status.value.setText(state)
        self.ssid_status.value.setText(network_name)
        self.ip_status.value.setText(address)
        self.mac_status.value.setText(fields.get("MAC", "—"))
        self.tcp_status.value.setText(tcp)
        if "WIFI" in self._interfaces:
            self.wifi_enabled.setChecked(fields.get("WIFI", "OFF").upper() == "ON")
            mode = fields.get("WIFI_MODE", "CLIENT").upper()
            mode_index = self.wifi_mode.findData(mode)
            if mode_index >= 0:
                self.wifi_mode.blockSignals(True)
                self.wifi_mode.setCurrentIndex(mode_index)
                self.wifi_mode.blockSignals(False)
            self._update_wifi_mode_visibility()
        self.use_dhcp.setChecked(fields.get("MODE", "DHCP").upper() == "DHCP")
        self.hostname.setText(fields.get("HOST", self.hostname.text()))
        self.tcp_enabled.setChecked(fields.get("TCP", "OFF").upper() == "ON")
        if fields.get("PORT"):
            self.tcp_port.setValue(int(fields["PORT"]))
        if hasattr(self, "tls_enabled"):
            self.tls_enabled.setChecked(fields.get("TLS", "OFF").upper() == "ON")
        for key, widget in (
            ("IP", self.address), ("MASK", self.subnet), ("GW", self.gateway),
            ("DNS1", self.dns1), ("DNS2", self.dns2),
        ):
            value = fields.get(key)
            if value and value != "0.0.0.0":
                widget.setText(value)
        self._update_ip_mode_display()

    def _on_networks(self, networks: list[Any]) -> None:
        self.network_list.clear()
        for network in sorted(networks, key=lambda item: item.rssi, reverse=True):
            secure = network.security.upper() != "OPEN"
            item = QTreeWidgetItem(
                [
                    network.ssid or "<hidden>",
                    f"{network.rssi} dBm",
                    network.security.replace("_", " "),
                    str(network.channel),
                    network.bssid or "—",
                ]
            )
            item.setData(0, Qt.UserRole, network)
            item.setToolTip(
                0,
                f"BSSID: {network.bssid or 'not reported'}\n"
                f"Channel: {network.channel}\nSecurity: {network.security}",
            )
            item.setIcon(1, signal_icon(network.rssi))
            item.setIcon(2, security_icon(secure))
            self.network_list.addTopLevelItem(item)
        if self.network_list.topLevelItemCount():
            self.network_list.setCurrentItem(self.network_list.topLevelItem(0))

    def _on_access_point_status(self, fields: dict[str, str]) -> None:
        self.access_point_ssid.setText(decode_ssid(fields.get("SSID64", "")))
        if fields.get("CHANNEL"):
            self.access_point_channel.setValue(int(fields["CHANNEL"]))
        state = fields.get("STATE", "INACTIVE").replace("_", " ").title()
        clients = fields.get("CLIENTS", "0")
        address = fields.get("IP", "192.168.24.1")
        self.access_point_state.setText(
            f"{state} · {address} · {clients} client{'s' if clients != '1' else ''}"
        )

    def _on_token(self, token: str) -> None:
        self.token.setText(token)
        self._token_dirty = False

    def _on_tls_status(self, fields: dict[str, str]) -> None:
        supported = fields.get("SUPPORTED", "YES") != "NO"
        self._tls_supported = supported
        for widget in getattr(self, "_tls_action_widgets", []):
            widget.setEnabled(self._connected and supported)
        if not supported:
            self._update_tls_credentials_enabled()
            return
        enabled = fields.get("STATE", fields.get("TLS", "OFF")).upper() == "ON"
        self.tls_enabled.setChecked(enabled)
        self._update_tls_credentials_enabled()

    def _on_diagnostics(self, fields: dict[str, str]) -> None:
        order = (
            "STATE", "SSID64", "SELECTED_BSSID", "SELECTED_CH", "SELECTED_AUTH",
            "CURRENT_BSSID", "CURRENT_CH", "MAC", "FAIL", "FAIL_CODE", "FAIL_AGE_MS",
            "ATTEMPTS", "ATTEMPT_AGE_MS", "SCAN", "FREE_HEAP",
        )
        parts: list[str] = []
        for key in order:
            value = fields.get(key)
            if key == "SSID64":
                value = decode_ssid(value or "")
                key = "SSID"
            if value is not None:
                parts.append(f"{key}={value}")
        self._last_diagnostics = " | ".join(parts)
        self._log(f"Diagnostics: {self._last_diagnostics}", "info")

    def _set_actions_enabled(self, enabled: bool) -> None:
        for widget in self._action_widgets:
            widget.setEnabled(enabled)
        self._update_tls_credentials_enabled()

    def _update_ip_enabled(self) -> None:
        if self._access_point_mode:
            self.address.setEnabled(True)
            self.subnet.setEnabled(True)
            for field in (self.gateway, self.dns1, self.dns2):
                field.setEnabled(False)
        else:
            for field in getattr(self, "_static_fields", []):
                field.setEnabled(not self.use_dhcp.isChecked())

    def _update_ip_mode_display(self) -> None:
        if not hasattr(self, "ap_assignment"):
            return
        ip_supported = "IP" in set(self._network_features)
        self.use_dhcp.setVisible(ip_supported and not self._access_point_mode)
        self.ap_assignment.setVisible(ip_supported and self._access_point_mode)
        for widget in (self.gateway, self.dns1, self.dns2):
            self.ip_form.setRowVisible(
                widget, ip_supported and not self._access_point_mode
            )
        self._update_ip_enabled()

    def _log(self, message: str, kind: str = "info") -> None:
        if not hasattr(self, "log"):
            return
        color = {"ok": "#43b97f", "error": "#e65f5c", "info": "#8fa3bf"}.get(kind, "#8fa3bf")
        self.log.append(
            f'<span style="color:#98a2b3">[{time.strftime("%H:%M:%S")}]</span> '
            f'<span style="color:{color}">{html.escape(message)}</span>'
        )


STYLE = """
QWidget#Root {
    background: #f7f7fc;
    color: #1a1b1f;
    font-size: 13px;
}
QWidget:disabled,
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
QLabel#AppSubtitle,
QLabel#help {
    color: #5d6c7b;
    font-size: 13px;
}
QLabel#ToolTitle {
    color: #1a1b1f;
    font-size: 13px;
    font-weight: 700;
}
QLabel#SectionTitle {
    color: #1a1b1f;
    font-size: 15px;
    font-weight: 700;
}
QLabel#FieldLabel {
    color: #5d6c7b;
    font-size: 11px;
    font-weight: 700;
}
QLabel#FormLabel {
    color: #344454;
    font-size: 12px;
    font-weight: 700;
    background: transparent;
    border: none;
}
QLabel#FormValue {
    color: #1a1b1f;
    font-size: 13px;
    background: transparent;
    border: none;
}
QLabel#ConnectionHint {
    color: #5d6c7b;
    font-size: 12px;
}
QDialog#ConnectionDialog,
QDialog#ToolDialog {
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
QFrame#MetricCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#TlsCard,
QFrame#ConfigCard {
    background: #ffffff;
    border: 1px solid #dedfe3;
    border-radius: 8px;
}
QFrame#ConnectionOptions {
    background: #f0f7ff;
    border: 1px solid #b8d5ff;
    border-radius: 7px;
}
QLabel#MetricTitle {
    color: #5d6c7b;
    font-size: 12px;
}
QLabel#MetricValue {
    color: #1a1b1f;
    font-size: 18px;
    font-weight: 700;
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
QTabWidget#ControlTabs::pane {
    background: #ffffff;
    border: 1px solid #dcddeb;
    border-radius: 8px;
    top: -1px;
}
QTabWidget#ControlTabs[singlePage="true"]::pane {
    border: none;
    border-radius: 0;
    top: 0;
}
QScrollArea#ConfigScroll,
QScrollArea#ConfigScroll > QWidget > QWidget,
QWidget#ConfigPage {
    background: #ffffff;
    border: none;
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
QLineEdit,
QComboBox,
QSpinBox,
QTreeWidget {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    border-radius: 7px;
    padding: 7px 9px;
}
QLineEdit:disabled,
QComboBox:disabled,
QSpinBox:disabled {
    background: #f3f4f7;
    color: #7a8797;
    border-color: #d6dae3;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
    selection-background-color: #eaf2ff;
    selection-color: #1a1b1f;
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
QPushButton#quietButton {
    background: #ffffff;
    color: #1a1b1f;
    border: 1px solid #c8c8c8;
}
QPushButton#quietButton:hover {
    background: #eaf2ff;
    border-color: #0050bd;
}
QPushButton#dangerButton {
    background: #ee2c24;
    color: #ffffff;
    border: 1px solid #ee2c24;
}
QPushButton#dangerButton:hover {
    background: #721012;
    border-color: #721012;
}
QTextEdit {
    background: #1a1b1f;
    color: #ffffff;
    border: 1px solid #26272c;
    border-radius: 8px;
    padding: 8px;
}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setStyle("Fusion")
    font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
    font.setPointSize(10)
    app.setFont(font)
    window = NetworkConfigWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
