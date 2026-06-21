"""Transparent, always-on-top, NON-ACTIVATING overlay (PySide6).

The whole point: it must NEVER steal focus from the game. On Windows we add the
WS_EX_NOACTIVATE extended style so clicking/showing the overlay can't pull focus
off Forza (run Forza in BORDERLESS WINDOWED). The overlay only displays - all
interaction is via global hotkeys (see hotkeys.py).

PySide6 is imported lazily so the rest of the package imports without it.
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from .. import PRODUCT_NAME


def _apply_no_activate(win_id: int):
    """Add WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW so the window never takes focus."""
    if not sys.platform.startswith("win"):
        return
    import ctypes
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_TOPMOST = 0x00000008
    user32 = ctypes.windll.user32
    hwnd = int(win_id)
    cur = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                          cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


def build_overlay(status_fn: Callable[[], dict], hotkey_help: str = "",
                  capturable_fn: Optional[Callable[[], bool]] = None):
    """Create (but do not exec) the overlay widget. Returns (app, widget).
    `capturable_fn` -> current "show overlay in recordings" setting (bool).
    Raises RuntimeError with guidance if PySide6 is missing."""
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
    except Exception as e:  # pragma: no cover - depends on optional dep
        raise RuntimeError(
            "PySide6 is required for the overlay. Install GUI extras:\n"
            "  pip install PySide6 keyboard\n"
            f"(import error: {e})")

    class Overlay(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.status_fn = status_fn
            self._capturable_fn = capturable_fn or (lambda: False)
            # app-level actions, set by app.py once they exist (so the always-on
            # overlay can quit / restore the main window without the global hotkeys).
            self.on_exit = None
            self.on_show_main = None
            self.setWindowFlags(
                QtCore.Qt.FramelessWindowHint
                | QtCore.Qt.WindowStaysOnTopHint
                | QtCore.Qt.Tool
                | QtCore.Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
            self.setWindowOpacity(0.92)

            # --- always-visible control row (Exit / Main window + quit hint) ----
            # The overlay is NON-ACTIVATING (WS_EX_NOACTIVATE) and translucent; this
            # bar paints an OPAQUE hit area so the buttons reliably receive clicks
            # without the overlay taking focus (even if it were click-through).
            bar = QtWidgets.QWidget(self)
            bar.setObjectName("ovbar")
            bar.setAttribute(QtCore.Qt.WA_NoMousePropagation, True)
            bar.setStyleSheet(
                "#ovbar{background:rgba(16,18,22,235);"
                "border:1px solid rgba(120,160,255,120);border-radius:8px;}"
                "QPushButton{background:#232b31;color:#e6eaed;border:1px solid #2c343b;"
                "border-radius:6px;padding:4px 12px;font-family:'Segoe UI';font-size:12px;}"
                "QPushButton:hover{background:#2c343b;}"
                "QPushButton#exit{color:#e5544b;border:1px solid #e5544b;font-weight:700;}"
                "QPushButton#exit:hover{background:rgba(229,84,75,0.18);}"
                "QLabel{color:#8b97a1;background:transparent;font-family:Consolas;font-size:11px;}")
            exit_btn = QtWidgets.QPushButton("✕ Exit", bar)
            exit_btn.setObjectName("exit")
            exit_btn.setCursor(QtCore.Qt.PointingHandCursor)
            exit_btn.clicked.connect(self._on_exit_clicked)
            main_btn = QtWidgets.QPushButton("Main window", bar)
            main_btn.setCursor(QtCore.Qt.PointingHandCursor)
            main_btn.clicked.connect(self._on_main_clicked)
            quit_hint = QtWidgets.QLabel("[Ctrl+F12] quit", bar)
            bh = QtWidgets.QHBoxLayout(bar)
            bh.setContentsMargins(8, 6, 8, 6)
            bh.setSpacing(8)
            bh.addWidget(exit_btn)
            bh.addWidget(main_btn)
            bh.addStretch(1)
            bh.addWidget(quit_hint)

            self._label = QtWidgets.QLabel(self)
            self._label.setTextFormat(QtCore.Qt.RichText)
            self._label.setWordWrap(True)
            self._label.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)
            self._label.setStyleSheet(
                "color:#eaeaea; background:rgba(16,18,22,205);"
                "border:1px solid rgba(120,160,255,120); border-radius:10px;"
                "padding:12px; font-family:Consolas,monospace; font-size:13px;")
            scroll = QtWidgets.QScrollArea(self)
            scroll.setWidget(self._label)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("background:transparent;")
            scroll.viewport().setAutoFillBackground(False)
            lay = QtWidgets.QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            lay.addWidget(bar)
            lay.addWidget(scroll)
            # small minimum so the layout never exceeds the window (no Qt min-size
            # warning); content that's taller (e.g. the baseline list) scrolls.
            self.setMinimumSize(300, 160)
            self.resize(470, 560)
            self.move(40, 40)
            self._hotkey_help = hotkey_help
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(200)

        def _on_exit_clicked(self):
            if callable(self.on_exit):
                self.on_exit()

        def _on_main_clicked(self):
            if callable(self.on_show_main):
                self.on_show_main()

        def apply_capture_affinity(self):
            """(Re)apply the capture display-affinity for the CURRENT setting -
            called on show and whenever the setting changes while the overlay is up
            (so toggling it in Settings takes effect on the live overlay)."""
            if not self.isVisible():
                return
            from ..vision import capture
            try:
                capture.set_window_capturable(int(self.winId()),
                                              bool(self._capturable_fn()))
            except Exception:
                pass

        def showEvent(self, ev):
            super().showEvent(ev)
            hwnd = int(self.winId())
            _apply_no_activate(hwnd)
            # By default keep the overlay OUT of the Heat-page screenshot (it was
            # obscuring the Front-Left readings and failing OCR); if the user opted
            # in (or the dev env var is set) it stays visible to capture instead.
            from ..vision import capture
            capture.set_window_capturable(hwnd, bool(self._capturable_fn()))

        def refresh(self):
            self._label.setText(_render(self.status_fn()) +
                                (f"<br><span style='color:#88a'>{self._hotkey_help}</span>"
                                 if self._hotkey_help else ""))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = Overlay()
    return app, w


# What the user should do at each phase (which hotkey advances it).
def _hint(st: dict) -> str:
    ph = st.get("phase", "")
    port = st.get("port", 5607)
    if ph == "drive_auto":
        if st.get("mode") is None:
            return ("Drive a lap with the Heat page up - AUTO-LAP engages when the lap "
                    "timer advances. (Free-roam, no timer? Press [F9] to mark a segment.)")
        return ("Drive clean laps with the Heat page up. After you apply a change "
                "([F8]), the NEXT full lap is the test.")
    return {
        "wait_telemetry": (f"Waiting for telemetry on 127.0.0.1:{port}. In FH6 enable "
                           "Data Out (Settings &rarr; HUD and Gameplay), set borderless "
                           "windowed, and drive."),
        "confirm_car": "Car detected above. Press [F8] to confirm and open setup.",
        "setup": "Fill the setup form (discipline + slider ranges).",
        "apply_baseline": "Enter the tune below in-game, then press [F8].",
        "baseline_time": "Set a reference time: [F9] at your segment START, [F10] at END.",
        "test": "Open the Heat page &amp; keep it visible. [F8] to begin the test, drive, [F11] when done.",
        "show_change": "Apply the change above in-game, then press [F8].",
        "change_time": "Re-time the SAME segment: [F9] START, [F10] END.",
        "drive_auto": "Keep the Heat page up and drive a lap. AUTO-LAP engages when "
                      "the lap timer advances; each completed lap is then captured "
                      "automatically (after [F8], the next FULL lap is the test). In "
                      "free-roam (no timer), press [F9]/[F10] for manual segments.",
        "done": "Converged - tune saved to sessions/.",
    }.get(ph, "")


# Plain-language, colour-coded state -> (label, colour).
def _state_badge(st: dict):
    ph = st.get("phase", "")
    mode = st.get("mode")
    if ph == "wait_telemetry":
        return ("Waiting for telemetry", "#ff6b6b")
    if ph == "confirm_car":
        return ("Car detected - confirm it", "#f2b134")
    if ph == "setup":
        return ("Fill the setup form", "#f2b134")
    if ph == "apply_baseline":
        return ("Apply the baseline tune", "#f2b134")
    if ph == "drive_auto":
        return ("Detecting laps - drive a lap", "#f2b134") if mode is None \
            else ("Auto-lap active", "#4fcc4f")
    if ph in ("baseline_time", "change_time"):
        return ("Timing your segment", "#5aa0ff")
    if ph == "test":
        return ("Driving the test", "#5aa0ff")
    if ph == "show_change":
        batch = st.get("batch") or []
        if len(batch) == 1:
            return (f"Testing: {batch[0].get('detail') or batch[0]['group']}", "#5aa0ff")
        if len(batch) > 1:
            return (f"Testing {len(batch)} changes this lap", "#5aa0ff")
        return ("Apply the change", "#5aa0ff")
    if ph == "done":
        return ("Converged - tune saved", "#4fcc4f")
    return (ph, "#cccccc")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The overlay is the LIVE-TUNING HUD only. The management surfaces (Dashboard /
# Previous Tunes / Logs / Settings / Help) live in the focusable main window; the
# overlay keeps WS_EX_NOACTIVATE so it never steals focus while driving.
def _render(st: dict) -> str:
    return "<div>" + _render_tune(st) + "</div>"


def _render_tune(st: dict) -> str:
    advanced = st.get("view_mode") == "advanced"
    P = []
    # 1. header: car + class + drivetrain + discipline + step counter
    car = st.get("car") or PRODUCT_NAME
    disc = st.get("discipline", "")
    step = st.get("step") or {}
    sc = (f"<span style='color:#9aa;font-weight:600'>Step {step.get('number','?')}/"
          f"{step.get('total',6)} - {step.get('title','')}</span>") if step else ""
    P.append("<div style='font-size:13px;font-weight:700;color:#cfe3ff'>"
             f"{_esc(car)}{(' &nbsp;|&nbsp; ' + _esc(disc)) if disc else ''}</div>")
    if sc:
        P.append(f"<div style='font-size:11px;margin:1px 0 3px'>{sc} "
                 f"<span style='color:#5a6273'>[F6] {st.get('view_mode','simple')}</span></div>")
    # 2. colour-coded plain-language STATE
    label, color = _state_badge(st)
    P.append(f"<div style='font-size:17px;font-weight:800;color:{color};margin:5px 0'>"
             f"&#9679; {_esc(label)}</div>")
    if st.get("error"):
        P.append("<div style='color:#ff6b6b;font-weight:700;margin:2px 0'>"
                 f"&#9888; ERROR: {_esc(str(st['error']))}</div>")
    # 3. the recommended change(s) for THIS LAP
    batch = st.get("batch") or []
    if batch:
        P.append("<div style='font-size:13px;font-weight:700;margin:3px 0'>THIS LAP - "
                 "set all in the Tune menu:</div>")
        for r in batch:
            tag = "" if r["kind"] == "evidence" else " <span style='color:#c9a'>(search)</span>"
            P.append(f"<div style='font-size:13px;color:#ffd479'>&bull; "
                     f"<b>{_esc(r['detail'] or r['group'])}</b>{tag}</div>")
            if advanced and r.get("reason"):     # WHY, in advanced view
                P.append(f"<div style='font-size:11px;color:#b9c;margin-left:10px'>"
                         f"{_esc(r['reason'])}</div>")
    # 4. ONE prominent WHAT TO DO NOW line (the guided action)
    hint = (step.get("action") if step else "") or _hint(st)
    if hint:
        P.append("<div style='font-size:13px;font-weight:700;color:#0c0e12;"
                 "background:#ffd479;border-radius:6px;padding:6px 8px;margin:6px 0'>"
                 f"&#9654; {_esc(hint)}</div>")
    # 5. progress + best / last / iteration
    tgt = st.get("test_target")
    if tgt and tgt > 1:
        done = st.get("test_laps_done", 0)
        tb = st.get("test_best")
        tb_s = f" - best {tb:.2f}s" if tb else ""
        P.append("<div style='font-size:13px;font-weight:700;color:#9cf'>"
                 f"lap {min(done + 1, tgt)}/{tgt}{tb_s}</div>")
    bits = [f"iteration {st.get('iteration', 0)}"]
    if st.get("best_segment_s"):
        bits.append(f"best {st['best_segment_s']:.2f}s")
    if st.get("last_lap_s"):
        bits.append(f"last {st['last_lap_s']:.2f}s")
    P.append(f"<div style='color:#cde;font-size:12px'>{' &nbsp;|&nbsp; '.join(bits)}</div>")
    # 6. DONE: where the shareable files are
    exp = st.get("export")
    if exp:
        P.append("<div style='font-size:12px;color:#4fcc4f;margin-top:4px'>"
                 f"Saved: {_esc(exp.get('folder',''))}</div>"
                 "<div style='font-size:11px;color:#9aa'>value sheet + JSON + optn.club "
                 "block (values to type in - not an in-game share code).</div>")
    # baseline checklist (only at apply_baseline)
    if st.get("checklist"):
        esc = _esc(st["checklist"]).replace("\n", "<br>")
        P.append(f"<pre style='color:#cde;font-size:11px;margin:4px 0'>{esc}</pre>")
    # --- ADVANCED extras: telemetry, temps+UDP, lap fields, history, reader ----
    if advanced:
        P.append(_render_advanced(st))
    # last message, dim
    msgs = st.get("messages", [])
    if msgs:
        P.append(f"<div style='color:#778; font-size:11px;margin-top:3px'>{_esc(msgs[-1])}</div>")
    return "".join(P)


def _render_advanced(st: dict) -> str:
    P = ["<hr style='border:0;border-top:1px solid #2a3140;margin:6px 0'>"]
    live = st.get("live")
    if live:
        P.append(f"<div style='color:#9cf;font-size:11px'>{live['speed_mph']:.0f}mph "
                 f"{live['rpm']:.0f}rpm gear {live['gear']} | "
                 f"lat {live['lat_g']:+.2f}g | {live['drivetrain']} "
                 f"(raw {live.get('drivetrain_raw','?')}) {live.get('num_cylinders','?')}cyl</div>")
    tr = st.get("tyre_reading")
    if tr:
        reader = st.get("last_reader") or "?"
        cells = []
        for k in ("FL", "FR", "RL", "RR"):
            z = tr.get(k) or {}
            if z:
                cells.append(f"{k} {z.get('inner',0):.0f}/{z.get('mid',0):.0f}/{z.get('outer',0):.0f}")
        P.append(f"<div style='color:#cea;font-size:11px'>tyre C (in/mid/out) via {_esc(reader)}: "
                 f"{' '.join(cells)}</div>")
    laps = st.get("laps")
    if laps is not None:
        P.append(f"<div style='color:#8a9;font-size:11px'>raceOn={laps['race_on']} "
                 f"lap={laps['lap']} cur={laps['cur']:.1f} last={laps['last']:.2f} "
                 f"| restarts {st.get('restart_count',0)}</div>")
    hist = st.get("history") or []
    if hist:
        P.append("<div style='color:#9aa;font-size:11px;margin-top:3px'>history:</div>")
        for h in hist[-5:]:
            col = {"kept": "#4fcc4f", "reverted": "#ff8a8a"}.get(h["verdict"], "#bbb")
            sets = ", ".join(f"{k}->{v}" for k, v in h["fields"].items())
            P.append(f"<div style='color:{col};font-size:10px'>&bull; {_esc(h['group'])}: "
                     f"{_esc(sets)} [{h['verdict']}]</div>")
    age = st.get("packet_age_s")
    age_s = "no telemetry" if age is None else (f"{age:.1f}s" + (" STALE" if age > 2 else ""))
    P.append(f"<div style='color:#5a6273;font-size:10px;margin-top:3px'>"
             f"{_esc(st.get('mode_label') or st.get('phase',''))} | age {age_s} | "
             f"port {st.get('port','?')}</div>")
    return "".join(P)
