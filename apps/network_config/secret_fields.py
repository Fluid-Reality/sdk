"""Consistent, browser-style secret visibility controls for Qt apps."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QLineEdit


def _eye_icon(*, crossed: bool) -> QIcon:
    pixmap = QPixmap(22, 22)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#667085"), 1.7, Qt.SolidLine, Qt.RoundCap))
    eye = QPainterPath(QPointF(2.5, 11))
    eye.cubicTo(6.0, 5.2, 16.0, 5.2, 19.5, 11.0)
    eye.cubicTo(16.0, 16.8, 6.0, 16.8, 2.5, 11.0)
    painter.drawPath(eye)
    painter.drawEllipse(QPointF(11, 11), 2.5, 2.5)
    if crossed:
        painter.setPen(QPen(QColor("#667085"), 2.0, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(3.5, 3.5), QPointF(18.5, 18.5))
    painter.end()
    return QIcon(pixmap)


_SHOW_ICON: QIcon | None = None
_HIDE_ICON: QIcon | None = None


def add_secret_visibility(field: QLineEdit, *, secret_name: str = "secret") -> QAction:
    """Add an eye action inside the trailing edge of a secret field."""

    global _SHOW_ICON, _HIDE_ICON
    if _SHOW_ICON is None:
        _SHOW_ICON = _eye_icon(crossed=False)
        _HIDE_ICON = _eye_icon(crossed=True)

    field.setEchoMode(QLineEdit.Password)
    action = QAction(field)
    action.setObjectName("secretVisibilityAction")
    action.setCheckable(True)

    def update(visible: bool) -> None:
        field.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)
        action.setIcon(_HIDE_ICON if visible else _SHOW_ICON)
        verb = "Hide" if visible else "Show"
        action.setToolTip(f"{verb} {secret_name}")
        action.setText(f"{verb} {secret_name}")

    action.toggled.connect(update)
    update(False)
    field.addAction(action, QLineEdit.TrailingPosition)
    return action


__all__ = ["add_secret_visibility"]
