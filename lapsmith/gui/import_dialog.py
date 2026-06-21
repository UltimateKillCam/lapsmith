"""Qt dialog: import a car-name database the user downloaded from Nexus Mods.

Shows a clickable link to the "Forza Horizon 6 Car ID List" page (we ship NO
third-party data - the user downloads it), credits the author, then lets them pick
the CSV/TSV/JSON and merges it (gaps only; their own names win). Returns the import
summary dict, or None if nothing was imported.
"""
from __future__ import annotations

from typing import Optional

from .. import PRODUCT_NAME, car_import


def show_import_dialog(parent=None) -> Optional[dict]:
    try:
        from PySide6 import QtWidgets
        from PySide6.QtCore import Qt
    except Exception as e:  # pragma: no cover - optional dep
        raise RuntimeError("PySide6 required for the import dialog. pip install PySide6") from e

    dlg = QtWidgets.QDialog(parent)
    dlg.setWindowTitle(f"{PRODUCT_NAME} - import car names")
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    lay = QtWidgets.QVBoxLayout(dlg)

    lay.addWidget(QtWidgets.QLabel(
        f"{PRODUCT_NAME} doesn't bundle a car list. Download the CSV or JSON from "
        "this page, then import it here:"))

    # CLICKABLE link to the Nexus page (opens in the browser).
    link = QtWidgets.QLabel(
        f'<a href="{car_import.NEXUS_CAR_LIST_URL}">{car_import.NEXUS_CAR_LIST_TITLE}'
        f'</a>  -  Nexus Mods')
    link.setTextFormat(Qt.RichText)
    link.setTextInteractionFlags(Qt.TextBrowserInteraction)
    link.setOpenExternalLinks(True)
    link.setStyleSheet("font-size:14px;font-weight:700;")
    lay.addWidget(link)

    author = car_import.NEXUS_CAR_LIST_AUTHOR.strip()
    by = f"by {author} on Nexus Mods" if author else "by its author on Nexus Mods"
    credit = (f"“{car_import.NEXUS_CAR_LIST_TITLE}” {by} — please keep their credit. "
              "Not bundled with LapSmith; you download and import it yourself.")
    cl = QtWidgets.QLabel(credit)
    cl.setWordWrap(True)
    cl.setStyleSheet("color:#888;font-size:11px;")
    lay.addWidget(cl)

    lay.addWidget(QtWidgets.QLabel(
        "Accepted: CSV/TSV (name,ordinal or ordinal,name) and JSON. Your own names "
        "are always kept - import only fills the gaps."))

    result = QtWidgets.QLabel("")
    result.setWordWrap(True)
    result.setStyleSheet("font-weight:700;")

    def choose():
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            dlg, "Choose the downloaded car list", "",
            "Car lists (*.csv *.tsv *.json);;All files (*.*)")
        if not path:
            return
        try:
            s = car_import.import_file(path)
        except OSError as e:
            result.setText(f"Could not read the file: {e}")
            return
        dlg._summary = s
        if s["parsed"] == 0:
            result.setText("No car names recognised - is this the CSV/JSON from the "
                           "Nexus page?")
        else:
            result.setText(
                f"Imported {s['imported']} new name(s); kept {s['already']} you "
                f"already named; {s['malformed']} malformed/skipped.")

    btns = QtWidgets.QHBoxLayout()
    choose_btn = QtWidgets.QPushButton("Choose file && import...")
    choose_btn.clicked.connect(choose)
    close_btn = QtWidgets.QPushButton("Close")
    close_btn.clicked.connect(dlg.accept)
    btns.addWidget(choose_btn)
    btns.addStretch(1)
    btns.addWidget(close_btn)
    lay.addLayout(btns)
    lay.addWidget(result)

    dlg._summary = None
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    dlg.exec()
    return getattr(dlg, "_summary", None)
