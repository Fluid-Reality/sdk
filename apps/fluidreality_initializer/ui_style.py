"""Visual tokens and the Qt stylesheet (matches the Fluid Reality Dashboard)."""

from __future__ import annotations

INK = "#1a1b1f"
INK_2 = "#4f5f70"
MUTED = "#7a8797"
SURFACE = "#ffffff"
CANVAS = "#f7f7fc"
LINE = "#dedfe3"
GRID = "#eceef3"
ACCENT = "#0050bd"
BRAND_RED = "#ee2c24"

# Drive polarity (diverging pair) and voltage levels (ordinal blue ramp).
POSITIVE = "#2a78d6"
NEGATIVE = "#e34948"
VOLTAGE_RAMP = ("#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b")

# Status colours: (text, background, border)
STATUS = {
    "idle": ("#4f5f70", "#f2f2f3", "#dcddeb"),
    "configured": ("#1a1b1f", "#f2f2f3", "#c8c8c8"),
    "detecting": ("#0050bd", "#f0f7ff", "#b8d5ff"),
    "ready": ("#0d4d2c", "#eaf8f1", "#69c695"),
    "error": ("#721012", "#ffeff0", "#ff5a65"),
    "not_connected": ("#5d6c7b", "#f2f2f3", "#c8c8c8"),
    "checking": ("#0050bd", "#f0f7ff", "#b8d5ff"),
    "running": ("#ffffff", "#0050bd", "#0050bd"),
    "measuring": ("#ffffff", "#5b2bbd", "#5b2bbd"),
    "held": ("#8a5a00", "#fff4dc", "#f0c060"),
    "paused": ("#8a5a00", "#fff4dc", "#f0c060"),
    "completed": ("#0d4d2c", "#eaf8f1", "#69c695"),
    "stopped": ("#8a5a00", "#fff4dc", "#f0c060"),
    "failed": ("#721012", "#ffeff0", "#ff5a65"),
}

APP_STYLES = """
QWidget#Root { background: #f7f7fc; color: #1a1b1f; font-size: 13px; }
QWidget:disabled, QLabel:disabled { color: #7a8797; }
QLabel { background: transparent; }
QLabel#AppTitle { color: #1a1b1f; font-size: 30px; font-weight: 700; }
QLabel#AppSubtitle { color: #5d6c7b; font-size: 13px; }
QLabel#SectionTitle { color: #1a1b1f; font-size: 17px; font-weight: 700; }
QLabel#BoardTitle { color: #1a1b1f; font-size: 16px; font-weight: 700; }
QLabel#BoardDetail { color: #5d6c7b; font-size: 12px; }
QLabel#DialogTitle { color: #1a1b1f; font-size: 22px; font-weight: 700; }
QLabel#DialogSubtitle { color: #5d6c7b; font-size: 13px; }
QLabel#FormLabel { color: #344454; font-size: 12px; font-weight: 700; }
QLabel#Hint { color: #5d6c7b; font-size: 12px; }
QLabel#ErrorText { color: #721012; background: #ffeff0; border: 1px solid #ff9ca3; border-radius: 7px; padding: 8px 10px; }
QLabel#MetricTitle { color: #5d6c7b; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }
QLabel#MetricValue { color: #1a1b1f; font-size: 20px; font-weight: 700; }
QLabel#MetricSub { color: #5d6c7b; font-size: 11px; }
QLabel#HeroValue { color: #1a1b1f; font-size: 30px; font-weight: 700; }
QLabel#TileChannel { color: #1a1b1f; font-size: 15px; font-weight: 700; }
QLabel#TileId { color: #1a1b1f; font-size: 13px; font-weight: 700; }
QLabel#TileVoltage { color: #1a1b1f; font-size: 22px; font-weight: 700; }
QLabel#TileDetail { color: #4f5f70; font-size: 11px; font-weight: 600; }
QLabel#TileMuted { color: #7a8797; font-size: 11px; }
QLabel#Pill { border-radius: 7px; padding: 2px 7px; font-size: 11px; font-weight: 700; }
QLabel#DetailTitle { color: #1a1b1f; font-size: 24px; font-weight: 700; }
QLabel#ChartTitle { color: #1a1b1f; font-size: 13px; font-weight: 700; }
QLabel#LegendText { color: #4f5f70; font-size: 11px; }
QFrame#Panel, QFrame#MetricCard, QFrame#TopBar { background: #ffffff; border: 1px solid #dedfe3; border-radius: 10px; }
QFrame#Inset { background: #fafafa; border: 1px solid #dcddeb; border-radius: 8px; }
QFrame#Divider { background: #e6e8ee; max-height: 1px; min-height: 1px; border: none; }
QFrame#ActuatorTile { background: #ffffff; border: 1px solid #dedfe3; border-radius: 10px; }
QFrame#ActuatorTile[state="running"] { background: #f5f9ff; border-color: #8fb6ec; }
QFrame#ActuatorTile[state="measuring"] { background: #f7f3ff; border-color: #b39ce8; }
QFrame#ActuatorTile[state="held"] { background: #fffaf0; border-color: #f0d9a0; }
QFrame#ActuatorTile[state="failed"], QFrame#ActuatorTile[state="error"] { background: #fff7f7; border-color: #ff9ca3; }
QFrame#ActuatorTile[state="completed"] { background: #f6fcf8; border-color: #9fd9b8; }
QFrame#ActuatorTile[state="not_connected"] { background: #f5f5f6; border-color: #d6d6d9; }
QFrame#ActuatorTile[selected="true"] { border: 2px solid #0050bd; }
QPushButton { background: #1a1b1f; color: #ffffff; border: 1px solid #1a1b1f; border-radius: 7px; padding: 7px 12px; font-weight: 700; }
QPushButton:hover { background: #ee2c24; border-color: #ee2c24; }
QPushButton:pressed { background: #721012; border-color: #721012; }
QPushButton:disabled { background: #e6e8ee; color: #7a8797; border-color: #d6dae3; }
QPushButton#SecondaryButton { color: #1a1b1f; background: #ffffff; border: 1px solid #c8c8c8; }
QPushButton#SecondaryButton:hover { color: #0050bd; background: #eaf2ff; border-color: #0050bd; }
QPushButton#SecondaryButton:disabled { background: #f3f4f7; color: #9aa5b1; border-color: #e0e2e8; }
QPushButton#DangerButton { background: #ffffff; color: #b42318; border: 1px solid #f3b4ae; }
QPushButton#DangerButton:hover { background: #ee2c24; color: #ffffff; border-color: #ee2c24; }
QPushButton#DangerButton:disabled { background: #f3f4f7; color: #c0a3a0; border-color: #e0e2e8; }
QPushButton#SmallButton { padding: 4px 9px; font-size: 11px; }
QPushButton#SmallSecondary { color: #1a1b1f; background: #ffffff; border: 1px solid #c8c8c8; padding: 4px 9px; font-size: 11px; }
QPushButton#SmallSecondary:hover { color: #0050bd; background: #eaf2ff; border-color: #0050bd; }
QPushButton#SmallSecondary:disabled { background: #f3f4f7; color: #9aa5b1; border-color: #e0e2e8; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
    background: #ffffff; color: #1a1b1f; border: 1px solid #c8c8c8; border-radius: 7px; padding: 6px 8px; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border-color: #0050bd; }
QLineEdit[invalid="true"] { border-color: #ee2c24; background: #fff7f7; }
QComboBox QAbstractItemView { background: #ffffff; color: #1a1b1f; border: 1px solid #c8c8c8; selection-background-color: #eaf2ff; selection-color: #1a1b1f; }
QCheckBox { spacing: 7px; }
QGroupBox { border: 1px solid #dcddeb; border-radius: 8px; margin-top: 14px; padding: 12px 10px 8px 10px; font-weight: 700; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #344454; }
QProgressBar { background: #eef1f6; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: #0050bd; border-radius: 4px; }
QProgressBar#BigProgress { min-height: 12px; max-height: 12px; border-radius: 6px; }
QProgressBar#BigProgress::chunk { border-radius: 6px; }
QTextEdit#EventLog { background: #1a1b1f; color: #e8e8ea; border: 1px solid #26272c; border-radius: 8px; padding: 6px 8px; font-family: Consolas, "Cascadia Mono", monospace; font-size: 12px; }
QScrollArea { background: transparent; border: none; }
QWidget#ScrollHost { background: transparent; }
QSplitter::handle { background: #e6e8ee; }
QSplitter::handle:horizontal { width: 4px; }
QSplitter::handle:vertical { height: 4px; }
QToolTip { color: #1a1b1f; background: #ffffff; border: 1px solid #c8c8c8; padding: 4px; }
"""
