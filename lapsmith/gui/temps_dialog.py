"""Qt manual tyre-temp entry dialog (no console input in the GUI path).

Shown only when OCR of the captured PEAK-LOAD Heat screenshot fails. It DISPLAYS
that screenshot (captures/tyre_temps_N.png) so the user reads the loaded-tyre
data from the frame - not the live on-screen page, which goes dead-even after you
stop. 12 fields (inner/mid/outer x4) + a C/F selector; result normalized to C.

Top-most + activating like the setup form; hides on OK to return focus to the game.
"""
from __future__ import annotations

from typing import Optional, Dict

from ..vision import read_tyres
from .. import PRODUCT_NAME

_TYRES = ("FL", "FR", "RL", "RR")
_ZONES = ("inner", "mid", "outer")


def show_temps_dialog(image_path: str) -> Optional[Dict[str, Dict[str, float]]]:
    try:
        from PySide6 import QtWidgets, QtGui
        from PySide6.QtCore import Qt
    except Exception as e:  # pragma: no cover
        raise RuntimeError("PySide6 required for the temp dialog. pip install PySide6") from e

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(f"{PRODUCT_NAME} - read tyre temps from the captured frame")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    root = QtWidgets.QVBoxLayout(dlg)

    root.addWidget(QtWidgets.QLabel(
        "OCR couldn't read the Heat page. Read the temps from THIS captured "
        "peak-load frame (not the live page):"))

    # the captured screenshot
    pix = QtGui.QPixmap(image_path)
    img = QtWidgets.QLabel()
    if not pix.isNull():
        maxw = 900
        if pix.width() > maxw:
            pix = pix.scaledToWidth(maxw, Qt.SmoothTransformation)
        img.setPixmap(pix)
    else:
        img.setText(f"(could not load image: {image_path})")
    scroll = QtWidgets.QScrollArea()
    scroll.setWidget(img)
    scroll.setWidgetResizable(False)
    scroll.setMinimumHeight(280)
    root.addWidget(scroll)

    # unit selector
    unit_row = QtWidgets.QHBoxLayout()
    unit_row.addWidget(QtWidgets.QLabel("Unit shown on the page:"))
    unit = QtWidgets.QComboBox()
    unit.addItems(["C", "F"])
    unit_row.addWidget(unit)
    unit_row.addStretch(1)
    root.addLayout(unit_row)

    # 12 spin boxes in a grid: rows = tyres, cols = inner/mid/outer
    grid = QtWidgets.QGridLayout()
    grid.addWidget(QtWidgets.QLabel(""), 0, 0)
    for c, z in enumerate(_ZONES):
        grid.addWidget(QtWidgets.QLabel(z), 0, c + 1)
    spins: Dict[str, Dict[str, QtWidgets.QDoubleSpinBox]] = {}
    for r, tyre in enumerate(_TYRES):
        grid.addWidget(QtWidgets.QLabel(tyre), r + 1, 0)
        spins[tyre] = {}
        for c, z in enumerate(_ZONES):
            sb = QtWidgets.QDoubleSpinBox()
            sb.setRange(-50, 400)
            sb.setDecimals(1)
            grid.addWidget(sb, r + 1, c + 1)
            spins[tyre][z] = sb
    root.addLayout(grid)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)

    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    accepted = dlg.exec() == QtWidgets.QDialog.Accepted
    dlg.hide()
    if not accepted:
        return None

    raw = {"unit": unit.currentText()}
    for tyre in _TYRES:
        raw[tyre] = {z: spins[tyre][z].value() for z in _ZONES}
    return read_tyres._normalize(raw)     # -> Celsius
