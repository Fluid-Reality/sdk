"""Shared icon-button styling for Fluid Reality desktop apps."""

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QPushButton, QStyle


REFRESH_ICON_PATH = Path(__file__).resolve().parent / "assets" / "refresh.svg"


def configure_refresh_button(button: QPushButton, accessible_name: str) -> QPushButton:
    """Apply the standard quiet, icon-only refresh-button presentation."""

    button.setText("")
    button.setObjectName("quietButton")
    button.setAccessibleName(accessible_name)
    button.setToolTip(accessible_name)
    icon = (
        QIcon(str(REFRESH_ICON_PATH))
        if REFRESH_ICON_PATH.is_file()
        else button.style().standardIcon(QStyle.SP_BrowserReload)
    )
    button.setIcon(icon)
    button.setIconSize(QSize(20, 20))
    button.setFixedSize(40, 36)
    return button
