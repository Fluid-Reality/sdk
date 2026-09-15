"""Network-related widgets owned by the Fluid Reality Dashboard."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fluidreality_dashboard.secret_fields import add_secret_visibility
from fluidreality_dashboard.tls_files import (
    GeneratedTlsFiles,
    create_self_signed_tls_files,
)

NEW_FILE_ICON_PATH = Path(__file__).resolve().parent / "assets" / "new-file.svg"


def form_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("FormLabel")
    return label


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
