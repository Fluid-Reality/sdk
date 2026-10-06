"""Blueprint style hand visualization for the demo window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

from fluid_reality import ActuatorState


TIP_COORDS = ((57, 258), (110, 143), (214, 67), (352, 90), (462, 255))
ASSET = Path(__file__).resolve().parent / "assets" / "hand_outline.svg"


class HandView(QWidget):
    """Draw a mapped hand with output dependent red and blue fingertip targets."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(480, 420)
        self._hand = QSvgRenderer(str(ASSET), self)
        if not self._hand.isValid():
            raise RuntimeError(f"Unable to load hand artwork: {ASSET}")
        self._actuators = list(range(5))
        self._values: dict[int, float] = {}
        self._detection_states: dict[int, ActuatorState | str] = {}

    def set_actuators(self, actuators: list[int]) -> None:
        self._actuators = list(actuators)
        self.update()

    def set_values(self, values: dict[int, float]) -> None:
        self._values = dict(values)
        self.update()

    def set_detection_state(self, actuator: int, state: ActuatorState | str) -> None:
        self._detection_states[actuator] = state
        self.update()

    def clear_detection_states(self) -> None:
        self._detection_states.clear()
        self.update()

    @staticmethod
    def _mix(base: QColor, active: QColor, weight: float) -> QColor:
        weight = max(0.0, min(1.0, weight))
        return QColor(
            round(base.red() + (active.red() - base.red()) * weight),
            round(base.green() + (active.green() - base.green()) * weight),
            round(base.blue() + (active.blue() - base.blue()) * weight),
        )

    @classmethod
    def _activation_style(
        cls,
        value: float,
        base_fill: QColor | None = None,
        base_outline: QColor | None = None,
    ) -> tuple[QColor, QColor, QColor, float]:
        intensity = min(1.0, abs(value) / 255.0)
        active = QColor("#ff4d45") if value > 0 else QColor("#42a5ff")
        neutral_fill = base_fill if base_fill is not None else QColor("#173b53")
        neutral_outline = base_outline if base_outline is not None else QColor("#50a6bd")
        return (
            cls._mix(neutral_fill, active, intensity),
            cls._mix(neutral_outline, active, intensity),
            active,
            intensity,
        )

    @staticmethod
    def _detection_style(state: ActuatorState | str) -> tuple[QColor, QColor]:
        if state == "Detecting" or state == ActuatorState.PRESENT:
            return QColor("#e1aa51"), QColor("#ffe0a3")
        if state == ActuatorState.READY:
            return QColor("#e9f5f7"), QColor("#b8e9f0")
        if state == ActuatorState.NOT_CONNECTED:
            return QColor("#687684"), QColor("#a2afba")
        if state == ActuatorState.ERROR:
            return QColor("#ad4f58"), QColor("#ff9ca5")
        return QColor("#173b53"), QColor("#50a6bd")

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        painter.fillRect(self.rect(), QColor("#0c2940"))

        # The faint grid and rings give the hand a schematic frame without
        # competing with the active finger markers.
        painter.setPen(QPen(QColor("#20445b"), 1))
        for x in range(0, width, 26):
            painter.drawLine(x, 0, x, height)
        for y in range(0, height, 26):
            painter.drawLine(0, y, width, y)
        painter.setPen(QPen(QColor("#2b6576"), 1))
        radius = min(width * 0.36, height * 0.44)
        for factor in (1.0, 0.73):
            rr = radius * factor
            painter.drawEllipse(QRectF(width / 2 - rr, height / 2 - rr, rr * 2, rr * 2))

        size = min(width * 0.64, height * 0.88)
        left = (width - size) / 2
        top = (height - size) / 2 - 6
        self._hand.render(painter, QRectF(left, top, size, size))

        font = QFont("Consolas", 15)
        font.setBold(True)
        painter.setFont(font)
        scale = size / 512
        for finger, (tx, ty) in enumerate(TIP_COORDS):
            number = self._actuators[finger]
            value = self._values.get(number, 0.0)
            state = self._detection_states.get(number, ActuatorState.UNKNOWN)
            base_fill, base_outline = self._detection_style(state)
            fill, outline, active, intensity = self._activation_style(
                value, base_fill, base_outline
            )
            x, y = left + tx * scale, top + ty * scale
            if intensity > 0 or state == "Detecting":
                halo = QColor(active if intensity > 0 else base_fill)
                halo.setAlpha(round(80 * intensity) if intensity > 0 else 65)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(halo)
                painter.drawEllipse(QRectF(x - 34, y - 34, 68, 68))
            painter.setPen(QPen(outline, 2))
            painter.setBrush(fill)
            painter.drawEllipse(QRectF(x - 24, y - 24, 48, 48))
            luminance = 0.2126 * fill.red() + 0.7152 * fill.green() + 0.0722 * fill.blue()
            painter.setPen(QColor("#102b42") if luminance > 170 else QColor("#ffffff"))
            painter.drawText(QRectF(x - 23, y - 22, 46, 44), Qt.AlignmentFlag.AlignCenter, str(number))

        painter.setPen(QColor("#80bfcc"))
        painter.setFont(QFont("Consolas", 10))
        painter.drawText(22, height - 20, "VIEW 01 / FRONT")
        legend_y = height - 24
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ff6f68"))
        painter.drawEllipse(QRectF(width - 236, legend_y - 7, 10, 10))
        painter.setBrush(QColor("#55baff"))
        painter.drawEllipse(QRectF(width - 116, legend_y - 7, 10, 10))
        painter.setPen(QColor("#80bfcc"))
        painter.drawText(width - 219, legend_y + 2, "+ OUTPUT")
        painter.drawText(width - 99, legend_y + 2, "- OUTPUT")
        painter.end()
