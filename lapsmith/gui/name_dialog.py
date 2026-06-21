"""Qt prompt for naming a newly-detected (unknown-ordinal) car.

Shown once when the detected CarOrdinal has no friendly name yet. The typed name
is saved to car_names.json (ordinals.save_name) and shown everywhere afterwards
instead of "Car #N". Top-most + activating like the setup form; hides on close so
focus returns to the game.
"""
from __future__ import annotations

from typing import Optional

from .. import PRODUCT_NAME


def show_name_dialog(ordinal: int, suggested: str = "",
                     detail: str = "") -> Optional[str]:
    try:
        from PySide6 import QtWidgets
        from PySide6.QtCore import Qt
    except Exception as e:  # pragma: no cover - optional dep
        raise RuntimeError("PySide6 required for the name dialog. pip install PySide6") from e

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(f"{PRODUCT_NAME} - name this car")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    root = QtWidgets.QVBoxLayout(dlg)

    root.addWidget(QtWidgets.QLabel(
        f"New car detected (ordinal #{ordinal}). What car is this?"))
    if detail:
        lab = QtWidgets.QLabel(detail)
        lab.setStyleSheet("color:#888;")
        root.addWidget(lab)
    root.addWidget(QtWidgets.QLabel(
        "The name is only a label (tuning never depends on it). Saved for next time."))

    edit = QtWidgets.QLineEdit()
    edit.setPlaceholderText("e.g. 2020 Toyota Supra RZ")
    if suggested:
        edit.setText(suggested)
    edit.selectAll()
    root.addWidget(edit)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)

    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    edit.setFocus()
    accepted = dlg.exec() == QtWidgets.QDialog.Accepted
    dlg.hide()
    if not accepted:
        return None
    return edit.text().strip() or None
