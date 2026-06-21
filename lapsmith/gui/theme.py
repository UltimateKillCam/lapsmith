"""App-wide dark theme (one QSS stylesheet) for LapSmith's focusable surfaces.

Applied once to the QApplication so the main management window and the modal
dialogs share a cohesive dark palette. The live overlay runs in the SAME
QApplication, so it inherits the palette for consistency - but this sheet is
deliberately written to NOT set a background on a bare ``QWidget`` (only on named
widgets / specific classes), so the overlay's translucent, non-activating window
is left exactly as-is.

Palette (single source of truth - kept in sync with the overlay HUD colours):
  window #14181b | panel #1c2227 | elevated #232b31 | hairline #2c343b
  text #e6eaed | muted #8b97a1 | accent #2fb24c/#289c43/#237f37 | danger #e5544b
  field #11161a | zebra #1a2024 | mono = Cascadia Mono / Consolas
"""
from __future__ import annotations

# Monospace is used ONLY for telemetry numbers (stat chips) and the value sheet.
MONO = '"Cascadia Mono","Consolas",monospace'

QSS = """
/* ---- base ---------------------------------------------------------------- */
QWidget {
    color: #e6eaed;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog { background: #14181b; }
QWidget#central, QWidget#tabPage { background: #14181b; }
QLabel { background: transparent; }
QLabel#muted { color: #8b97a1; }
QToolTip { background: #232b31; color: #e6eaed; border: 1px solid #2c343b; }

/* ---- tabs: flat segmented bar, no 3D frame ------------------------------- */
QTabWidget::pane { border: none; background: #14181b; top: -1px; }
QTabBar { background: transparent; }
QTabBar::tab {
    background: transparent;
    color: #8b97a1;
    padding: 8px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 2px;
}
QTabBar::tab:selected { color: #e6eaed; border-bottom: 2px solid #2fb24c; }
QTabBar::tab:hover:!selected { background: #232b31; }

/* ---- cards / panels ------------------------------------------------------ */
QFrame#card { background: #1c2227; border: 1px solid #2c343b; border-radius: 10px; }
QFrame#statChip { background: #232b31; border: 1px solid #2c343b; border-radius: 8px; }

/* ---- first-run "getting started" note ------------------------------------ */
QFrame#noteCard {
    background: #1a2024;
    border: 1px solid #2c343b;
    border-left: 3px solid #2fb24c;
    border-radius: 8px;
}
QLabel#noteTitle { color: #2fb24c; font-weight: 700; font-size: 13px; }
QLabel#noteBody { color: #e6eaed; }

/* ---- dashboard stat chips ------------------------------------------------ */
QLabel#statNumber {
    color: #2fb24c;
    font-family: """ + MONO + """;
    font-size: 26px;
    font-weight: 800;
}
QLabel#statLabel { color: #8b97a1; font-size: 11px; }

/* ---- secondary buttons = outline ---------------------------------------- */
QPushButton {
    background: transparent;
    color: #e6eaed;
    border: 1px solid #2c343b;
    border-radius: 6px;
    padding: 7px 14px;
}
QPushButton:hover { background: #232b31; }
QPushButton:pressed { background: #1c2227; }

/* ---- primary button (START TUNING) -------------------------------------- */
QPushButton#primary {
    background: #2fb24c;
    color: #0c0e12;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 16px;
    font-weight: 800;
}
QPushButton#primary:hover { background: #289c43; }
QPushButton#primary:pressed { background: #237f37; }

/* ---- danger button (Forget selected) ------------------------------------ */
QPushButton#danger {
    background: transparent;
    color: #e5544b;
    border: 1px solid #e5544b;
}
QPushButton#danger:hover { background: rgba(229, 84, 75, 0.15); }

/* ---- text areas (Logs / Help / tune detail) ----------------------------- */
QTextEdit, QTextBrowser {
    background: #1c2227;
    border: 1px solid #2c343b;
    border-radius: 8px;
    padding: 12px;
    color: #e6eaed;
}
QTextEdit#valueSheet { font-family: """ + MONO + """; }

/* ---- tables / lists ------------------------------------------------------ */
QTableWidget, QListWidget {
    background: #1c2227;
    alternate-background-color: #1a2024;
    gridline-color: #2c343b;
    border: 1px solid #2c343b;
    border-radius: 8px;
    outline: none;
}
QTableWidget::item, QListWidget::item { padding: 4px 6px; }
QTableWidget::item:selected, QListWidget::item:selected {
    background: rgba(47, 178, 76, 0.28);
    color: #e6eaed;
}
QHeaderView::section {
    background: #232b31;
    color: #8b97a1;
    font-weight: bold;
    border: none;
    border-right: 1px solid #2c343b;
    border-bottom: 1px solid #2c343b;
    padding: 6px 8px;
}
QTableCornerButton::section { background: #232b31; border: none; }

/* ---- inputs (spinbox / combos) ------------------------------------------ */
QSpinBox, QComboBox {
    background: #11161a;
    border: 1px solid #2c343b;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e6eaed;
}
QSpinBox:focus, QComboBox:focus { border: 1px solid #2fb24c; }
/* visible dropdown affordance: a divided button + a small muted chevron */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border-left: 1px solid #2c343b;
}
QComboBox::down-arrow {
    width: 0; height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #8b97a1;   /* muted chevron pointing down */
}
QComboBox:hover::down-arrow,
QComboBox:focus::down-arrow,
QComboBox:on::down-arrow { border-top-color: #c2ccd4; }  /* slightly brighter */
/* the open popup list, matched to the theme */
QComboBox QAbstractItemView {
    background: #1c2227;
    border: 1px solid #2c343b;
    color: #e6eaed;
    selection-background-color: rgba(47, 178, 76, 0.30);
    selection-color: #e6eaed;
    outline: none;
}
QCheckBox { color: #e6eaed; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #2c343b; border-radius: 4px; background: #11161a;
}
QCheckBox::indicator:checked { background: #2fb24c; border-color: #2fb24c; }

/* ---- scrollbars ---------------------------------------------------------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2c343b; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #3a444d; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #2c343b; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #3a444d; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""


def apply_theme(app) -> None:
    """Apply the app-wide dark stylesheet to the QApplication."""
    app.setStyleSheet(QSS)
