"""Blueprint-style Fluid Reality Glove Demo."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from fluid_reality import list_ports
if __package__:
    from .hand_view import HandView
    from .worker import BoardThread, DemoConfig, FINGER_NAMES, PATTERNS
else:
    from hand_view import HandView
    from worker import BoardThread, DemoConfig, FINGER_NAMES, PATTERNS


STYLE = """
QWidget#Root { background: #071d30; color: #e5fcff; }
QLabel { color: #e5fcff; font-family: Consolas, 'Courier New', monospace; }
QLabel#Brand { color: #b8f3fb; font-size: 20px; font-weight: bold; }
QLabel#Port { color: #8ad5e1; font-size: 14px; font-weight: bold; }
QLabel#Title { color: #e5fcff; font-size: 28px; font-weight: bold; }
QLabel#Subtitle { color: #7eb6c5; font-size: 14px; }
QLabel#Section { color: #b8f3fb; font-size: 15px; font-weight: bold; }
QLabel#Hint { color: #7eb6c5; font-size: 13px; }
QLabel#Row { color: #e2f5f7; font-size: 16px; font-weight: bold; }
QLabel#RowNumber { color: #54b8ca; font-size: 13px; font-weight: bold; }
QLabel#Phase { color: #7eb6c5; font-size: 12px; }
QFrame#Rule, QFrame#RowRule { max-height: 1px; }
QFrame#Rule { background: #37677a; }
QFrame#RowRule { background: #2e5367; }
QFrame#Stage, QFrame#Side { background: #102b42; border: 1px solid #397184; border-radius: 15px; }
QComboBox, QLineEdit { background: #19394e; border: 1px solid #477083;
    border-radius: 6px; color: #d9f2f5; padding: 7px 10px; font: bold 15px Consolas; }
QComboBox:disabled { color: #70909d; background: #183043; }
QComboBox QAbstractItemView { background: #19394e; color: #e5fcff; selection-background-color: #397184; }
QPushButton { border: 1px solid #477083; border-radius: 7px; background: #19394e;
    color: #d9f2f5; padding: 8px 17px; font: bold 14px Consolas; }
QPushButton:hover { background: #24536a; }
QPushButton:disabled { background: #214457; color: #7a9da9; border-color: #315b6c; }
QPushButton#Run { background: #62d8e9; color: #0d2a3c; border: 0;
    font-size: 16px; min-height: 29px; }
QPushButton#Run:hover { background: #89e5f0; }
QPushButton#Run:disabled { background: #2d6675; color: #8baab2; }
QDialog { background: #102b42; }
"""

APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "fluid-reality-icon.png"


class ConnectionDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to a controller")
        self.setMinimumWidth(430)
        self.setStyleSheet(STYLE)
        form = QFormLayout(self)
        form.setSpacing(14)
        self.endpoint = QComboBox()
        self.endpoint.setEditable(True)
        self.endpoint.addItems(sorted(list_ports()))
        if self.endpoint.count():
            self.endpoint.setCurrentIndex(0)
        self.endpoint.lineEdit().setPlaceholderText("Select or enter a serial port")
        form.addRow("Serial port", self.endpoint)
        buttons = QDialogButtonBox()
        buttons.addButton("Connect", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(
            self._accept_if_valid
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept_if_valid(self) -> None:
        port = self.endpoint.currentText().strip()
        if not port or "://" in port:
            QMessageBox.warning(self, "Connection", "Select a serial port")
            return
        self.accept()

    def connection(self) -> str:
        return self.endpoint.currentText().strip()


class GloveDemo(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fluid Reality Glove Demo")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self._worker: BoardThread | None = None
        self._connected = False
        self._close_requested = False
        root = QWidget()
        root.setObjectName("Root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(38, 28, 38, 32)
        layout.setSpacing(16)

        header = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(QIcon(str(APP_ICON_PATH)).pixmap(32, 32))
        brand = QLabel("FLUID REALITY  /  GLOVE DEMO")
        brand.setObjectName("Brand")
        self.port_label = QLabel("BOARD DISCONNECTED")
        self.port_label.setObjectName("Port")
        self.connect_button = QPushButton("Connect…")
        self.connect_button.clicked.connect(self.connect_board)
        header.addWidget(brand_icon)
        header.addSpacing(6)
        header.addWidget(brand)
        header.addStretch()
        header.addWidget(self.port_label)
        header.addSpacing(18)
        header.addWidget(self.connect_button)
        layout.addLayout(header)
        layout.addWidget(self._rule())

        title = QLabel("GLOVE / ACTUATOR MAP")
        title.setObjectName("Title")
        subtitle = QLabel("Interactive glove schematic  /  five mapped outputs")
        subtitle.setObjectName("Subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        content = QHBoxLayout()
        content.setSpacing(24)
        stage = QFrame()
        stage.setObjectName("Stage")
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        self.hand = HandView()
        stage_layout.addWidget(self.hand)
        content.addWidget(stage, 1)

        side = QFrame()
        side.setObjectName("Side")
        side.setFixedWidth(390)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(27, 26, 27, 14)
        side_layout.setSpacing(10)
        heading = QLabel("ASSIGNMENTS")
        heading.setObjectName("Section")
        hint = QLabel("Select an actuator for each digit")
        hint.setObjectName("Hint")
        side_layout.addWidget(heading)
        side_layout.addWidget(hint)
        side_layout.addSpacing(14)
        self.finger_boxes: list[QComboBox] = []
        for index, finger in enumerate(FINGER_NAMES):
            row = QHBoxLayout()
            row.setSpacing(13)
            row_number = QLabel(f"{index:02}")
            row_number.setObjectName("RowNumber")
            row_number.setFixedWidth(30)
            finger_label = QLabel(finger.upper())
            finger_label.setObjectName("Row")
            box = QComboBox()
            box.addItems([str(value) for value in range(5)])
            box.setCurrentIndex(index)
            box.setFixedWidth(88)
            box.currentIndexChanged.connect(self._mapping_changed)
            row.addWidget(row_number)
            row.addWidget(finger_label)
            row.addStretch()
            row.addWidget(box)
            side_layout.addLayout(row)
            self.finger_boxes.append(box)
            side_layout.addWidget(self._rule(row=True))
            side_layout.addSpacing(3)
        side_layout.addStretch()
        pattern_label = QLabel("PATTERN")
        pattern_label.setObjectName("Section")
        side_layout.addWidget(pattern_label)
        self.pattern_box = QComboBox()
        self.pattern_box.addItems(PATTERNS)
        side_layout.addWidget(self.pattern_box)
        self.phase_label = QLabel("READY TO CONNECT")
        self.phase_label.setObjectName("Phase")
        side_layout.addWidget(self.phase_label)
        self.run_button = QPushButton("RUN DEMO")
        self.run_button.setObjectName("Run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.toggle_demo)
        side_layout.addWidget(self.run_button)
        content.addWidget(side)
        layout.addLayout(content, 1)

        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)
        self.setMinimumSize(1100, 720)
        self.resize(1280, 800)

    @staticmethod
    def _rule(*, row: bool = False) -> QFrame:
        rule = QFrame()
        rule.setObjectName("RowRule" if row else "Rule")
        rule.setFixedHeight(1)
        return rule

    def _mapping_changed(self) -> None:
        self.hand.set_actuators([box.currentIndex() for box in self.finger_boxes])

    def connect_board(self) -> None:
        if self._worker is not None:
            self.port_label.setText("DISCONNECTING")
            self.connect_button.setEnabled(False)
            self.run_button.setEnabled(False)
            self._worker.disconnect()
            return
        dialog = ConnectionDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        endpoint = dialog.connection()
        self.port_label.setText(f"CONNECTING  {endpoint}")
        self.phase_label.setText("IDENTIFYING BOARD")
        self.connect_button.setEnabled(False)
        worker = BoardThread(endpoint)
        self._worker = worker
        worker.connected.connect(self._connected_to_board)
        worker.failed.connect(self._worker_failed)
        worker.disconnected.connect(self._worker_disconnected)
        worker.values_changed.connect(self.hand.set_values)
        worker.running_changed.connect(self._set_running)
        worker.phase_changed.connect(self.phase_label.setText)
        worker.finished.connect(self._worker_finished)
        worker.start()

    def _connected_to_board(self, endpoint: str, model: str) -> None:
        if self._close_requested:
            return
        self._connected = True
        self.port_label.setText(f"{model.upper()}  /  {endpoint.upper()}")
        self.connect_button.setText("Disconnect")
        self.connect_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.phase_label.setText("READY")

    def _worker_failed(self, message: str) -> None:
        self._connected = False
        self.run_button.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.port_label.setText("BOARD ERROR")
        self.phase_label.setText("STOPPED AFTER ERROR")
        if not self._close_requested:
            QMessageBox.critical(self, "Board error", message)

    def _worker_disconnected(self) -> None:
        self._connected = False
        self.hand.set_values({})

    def _worker_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._connected = False
        self.port_label.setText("BOARD DISCONNECTED")
        self.phase_label.setText("READY TO CONNECT")
        self.connect_button.setText("Connect…")
        self.connect_button.setEnabled(not self._close_requested)
        self.run_button.setText("RUN DEMO")
        self.run_button.setEnabled(False)
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def toggle_demo(self) -> None:
        worker = self._worker
        if worker is None or not self._connected:
            return
        if self.run_button.text() == "STOP DEMO":
            self.run_button.setEnabled(False)
            self.phase_label.setText("STOPPING / DISCHARGING")
            worker.stop_demo()
            return
        config = DemoConfig(
            self.pattern_box.currentText(),
            tuple(box.currentIndex() for box in self.finger_boxes),
        )
        try:
            worker.start_demo(config)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Demo", str(exc))
            return
        self._set_running(True)
        self.phase_label.setText("STARTING DEMO")

    def _set_running(self, running: bool) -> None:
        for box in self.finger_boxes:
            box.setEnabled(not running)
        self.pattern_box.setEnabled(not running)
        self.connect_button.setEnabled(not running and self._connected)
        self.run_button.setText("STOP DEMO" if running else "RUN DEMO")
        self.run_button.setEnabled(self._connected)
        if not running and self._connected:
            self.phase_label.setText("READY")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker is not None and self._worker.isRunning():
            self._close_requested = True
            self.connect_button.setEnabled(False)
            self.run_button.setEnabled(False)
            self.phase_label.setText("CLOSING / FINISHING OUTPUT CLEANUP")
            self._worker.disconnect()
            event.ignore()
            return
        event.accept()


def main() -> None:
    application = QApplication(sys.argv)
    application.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    window = GloveDemo()
    window.show()
    raise SystemExit(application.exec())


if __name__ == "__main__":
    main()
