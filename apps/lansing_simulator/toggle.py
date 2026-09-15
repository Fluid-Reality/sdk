"""Branded toggle control for the Lansing Simulator."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QWidget


class LabeledToggle(QCheckBox):
    """A keyboard-accessible switch with a consistently visible text label."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(28)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(52 + metrics.horizontalAdvance(self.text()), 28)

    def hitButton(self, position) -> bool:  # type: ignore[no-untyped-def]
        return self.rect().contains(position)

    def paintEvent(self, _event) -> None:  # type: ignore[no-untyped-def]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track_width = 40.0
        track_height = 22.0
        track_top = (self.height() - track_height) / 2.0
        track = QRectF(0.5, track_top, track_width, track_height)
        if self.isEnabled():
            track_color = QColor("#0050bd") if self.isChecked() else QColor("#d6dae3")
            border_color = QColor("#0050bd") if self.isChecked() else QColor("#9aa5b1")
            text_color = QColor("#1a1b1f")
        else:
            track_color = QColor("#e6e8ee")
            border_color = QColor("#c8cdd6")
            text_color = QColor("#7a8797")

        painter.setPen(QPen(border_color, 1.0))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 11.0, 11.0)

        thumb_size = 16.0
        thumb_x = 21.5 if self.isChecked() else 3.0
        thumb = QRectF(thumb_x, track_top + 3.0, thumb_size, thumb_size)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(thumb)

        if self.hasFocus():
            painter.setPen(QPen(QColor("#7db1ff"), 1.0))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 13.0, 13.0)

        painter.setPen(text_color)
        painter.drawText(
            QRectF(50.0, 0.0, max(0.0, self.width() - 50.0), float(self.height())),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.text(),
        )
