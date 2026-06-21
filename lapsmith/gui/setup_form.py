"""One-screen setup form (PySide6): discipline dropdown + all slider ranges in a
single dialog, instead of sequential prompts.

This dialog is the one place it's OK to take focus (you fill it before driving).
Lazy-imports PySide6. Returns (discipline, CarLimits, front_weight_pct) or None.
"""
from __future__ import annotations

from typing import Optional, Tuple

from ..state.tune_state import CarLimits
from .. import PRODUCT_NAME

_DISCIPLINES = ["road circuit", "touge", "dirt", "cross country", "top speed", "drag"]


def _val(spin) -> Optional[float]:
    v = spin.value()
    return None if v == 0 else float(v)


def show_setup_dialog(detected_summary: str = "") -> Optional[dict]:
    try:
        from PySide6 import QtWidgets
    except Exception as e:  # pragma: no cover
        raise RuntimeError("PySide6 required for the setup form. pip install PySide6") from e

    from PySide6.QtCore import Qt
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(f"{PRODUCT_NAME} - setup")
    # The setup dialog (unlike the driving overlay) SHOULD take focus and sit on
    # top of the borderless game so the user can type while parked.
    dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
    form = QtWidgets.QFormLayout(dlg)

    if detected_summary:
        form.addRow(QtWidgets.QLabel(f"<b>Detected:</b> {detected_summary}"))

    disc = QtWidgets.QComboBox()
    disc.addItems(_DISCIPLINES)
    form.addRow("Discipline", disc)

    fw = QtWidgets.QDoubleSpinBox()
    fw.setRange(0, 100)
    fw.setValue(50)
    fw.setSuffix(" %")
    form.addRow("Front weight", fw)

    cpt = QtWidgets.QComboBox()
    cpt.addItems(["1 (safe, one at a time)", "2", "3"])
    form.addRow("Search changes per lap\n(springs/ARB/damping)", cpt)
    form.addRow(QtWidgets.QLabel(
        "<i>Evidence-driven changes (camber, pressure, ride height, diff, aero) are "
        "always applied together and confirmed in one lap. Batching the handling "
        "cluster above trades attribution for fewer laps.</i>"))

    lpt = QtWidgets.QComboBox()
    lpt.addItems(["Adaptive (1 -> 2-3 as gains shrink)", "1", "2", "3"])
    form.addRow("Laps per test\n(noise robustness)", lpt)
    agg = QtWidgets.QComboBox()
    agg.addItems(["Best of N", "Median of N"])
    form.addRow("Lap aggregate", agg)

    tmode = QtWidgets.QComboBox()
    tmode.addItems(["Auto - local OCR (recommended)", "Manual entry each lap"])
    form.addRow("Tyre temps", tmode)
    form.addRow(QtWidgets.QLabel(
        "<i>Auto reads temps locally (bundled OCR, offline). If a lap can't be "
        "read it tunes camber by lap time instead - never blocks on typing.</i>"))
    vapi = QtWidgets.QCheckBox("Also use Anthropic vision API if ANTHROPIC_API_KEY is set")
    vapi.setChecked(False)
    form.addRow("Cloud reader (optional)", vapi)

    def pair(label, lo_default, hi_default, suffix=""):
        lo = QtWidgets.QDoubleSpinBox()
        hi = QtWidgets.QDoubleSpinBox()
        for s in (lo, hi):
            s.setRange(0, 100000)
            s.setSuffix(suffix)
        lo.setValue(lo_default)
        hi.setValue(hi_default)
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QtWidgets.QLabel("min"))
        h.addWidget(lo)
        h.addWidget(QtWidgets.QLabel("max"))
        h.addWidget(hi)
        form.addRow(label + " (0 = skip)", row)
        return lo, hi

    rhf = pair("Ride height FRONT", 0, 0, " cm")
    rhr = pair("Ride height REAR", 0, 0, " cm")
    sf = pair("Spring FRONT", 0, 0, " kgf/mm")
    sr = pair("Spring REAR", 0, 0, " kgf/mm")
    af = pair("Aero FRONT", 0, 0)
    ar = pair("Aero REAR", 0, 0)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)

    # Force it to the front and grab focus (borderless game would otherwise hide it).
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    accepted = dlg.exec() == QtWidgets.QDialog.Accepted
    dlg.hide()                      # returning focus to the game (the foreground app)
    if not accepted:
        return None

    lim = CarLimits(
        ride_height_front_min=_val(rhf[0]), ride_height_front_max=_val(rhf[1]),
        ride_height_rear_min=_val(rhr[0]), ride_height_rear_max=_val(rhr[1]),
        spring_front_min=_val(sf[0]), spring_front_max=_val(sf[1]),
        spring_rear_min=_val(sr[0]), spring_rear_max=_val(sr[1]),
        aero_front_min=_val(af[0]), aero_front_max=_val(af[1]),
        aero_rear_min=_val(ar[0]), aero_rear_max=_val(ar[1]),
    )
    # discard half-entered pairs
    for lo, hi in (("ride_height_front_min", "ride_height_front_max"),
                   ("ride_height_rear_min", "ride_height_rear_max"),
                   ("spring_front_min", "spring_front_max"),
                   ("spring_rear_min", "spring_rear_max"),
                   ("aero_front_min", "aero_front_max"),
                   ("aero_rear_min", "aero_rear_max")):
        if getattr(lim, lo) is None or getattr(lim, hi) is None:
            setattr(lim, lo, None)
            setattr(lim, hi, None)
    laps = "adaptive" if lpt.currentIndex() == 0 else lpt.currentIndex()   # 1/2/3
    return {"discipline": _DISCIPLINES[disc.currentIndex()], "limits": lim,
            "front_weight": float(fw.value()), "changes_per_test": cpt.currentIndex() + 1,
            "laps_per_test": laps, "lap_agg": "median" if agg.currentIndex() == 1 else "best",
            "temp_mode": "manual" if tmode.currentIndex() == 1 else "auto",
            "use_vision_api": bool(vapi.isChecked())}
