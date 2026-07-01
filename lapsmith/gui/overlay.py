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
            self._saved_pos = None          # last position, so it reopens where left
            self._drag_offset = None        # set on a body press; cleared on release
            self.setObjectName("overlayRoot")
            self.setWindowFlags(
                QtCore.Qt.FramelessWindowHint
                | QtCore.Qt.WindowStaysOnTopHint
                | QtCore.Qt.Tool
                | QtCore.Qt.WindowDoesNotAcceptFocus)
            # NO WA_TranslucentBackground: a per-pixel-translucent window is
            # click-through on its transparent pixels, which is why only the buttons
            # used to be grabbable. We paint a FULL, OPAQUE panel (so the OS delivers
            # clicks across the ENTIRE overlay -> draggable anywhere) and use
            # windowOpacity for the see-through HUD look (uniform alpha is fully
            # hit-testable, unlike per-pixel alpha).
            self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
            self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
            self.setStyleSheet(
                "#overlayRoot{background:#14181b;"
                "border:1px solid rgba(120,160,255,140);border-radius:10px;}")
            self.setWindowOpacity(0.92)

            # --- always-visible control row (Exit / Main window + quit hint) ----
            # The buttons consume their own clicks (so a button press never starts a
            # drag); pressing the row BACKGROUND falls through to the overlay's drag
            # handlers like the rest of the body.
            bar = QtWidgets.QWidget(self)
            bar.setObjectName("ovbar")
            bar.setAttribute(QtCore.Qt.WA_StyledBackground, True)
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
            # transparent: the overlay root paints the panel now (one border, not two)
            self._label.setStyleSheet(
                "color:#eaeaea; background:transparent; border:0;"
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

        # --- click-and-drag to move, ANYWHERE on the overlay body ---------------
        # A left-press that isn't consumed by a child (the Exit/Main-window buttons
        # consume theirs) bubbles up here and starts a drag; the window then follows
        # the cursor. No focus needed - the overlay stays non-activating.
        def mousePressEvent(self, ev):
            if ev.button() == QtCore.Qt.LeftButton:
                self._drag_offset = (ev.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
                ev.accept()
            else:
                super().mousePressEvent(ev)

        def mouseMoveEvent(self, ev):
            if self._drag_offset is not None and (ev.buttons() & QtCore.Qt.LeftButton):
                self.move(ev.globalPosition().toPoint() - self._drag_offset)
                ev.accept()
            else:
                super().mouseMoveEvent(ev)

        def mouseReleaseEvent(self, ev):
            if self._drag_offset is not None:
                self._saved_pos = self.pos()     # remember where the user left it
                self._drag_offset = None
                ev.accept()
            else:
                super().mouseReleaseEvent(ev)

        def showEvent(self, ev):
            super().showEvent(ev)
            if self._saved_pos is not None:
                self.move(self._saved_pos)      # reopen where the user left it
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
    if st.get("error"):
        P.append("<div style='color:#ff6b6b;font-weight:700;margin:2px 0'>"
                 f"&#9888; ERROR: {_esc(str(st['error']))}</div>")
    # bound-but-no-telemetry: point the user at the firewall / Data Out, not "no car"
    if st.get("telemetry_diagnostic"):
        P.append("<div style='font-size:12px;font-weight:800;color:#0c0e12;background:#ffd479;"
                 "border-radius:5px;padding:5px 8px;margin:3px 0'>&#9888; NO TELEMETRY - "
                 f"{_esc(st['telemetry_diagnostic'])}</div>")
    # car detected but the stream paused (Forza lost focus): reassure, don't alarm -
    # the detection is retained, the user can keep reading the overlay out of focus.
    _det = st.get("detection") or {}
    if _det.get("state") == "car_detected" and _det.get("live") is False:
        P.append("<div style='font-size:11px;color:#9aa'>&#9208; Telemetry paused "
                 "(game out of focus) - detected car retained.</div>")
    # persistent notice: the in-game Heat SCREEN can't be read (telemetry is fine) so
    # camber/toe are limited to lap-time tuning on tarmac.
    if st.get("temp_blind"):
        P.append("<div style='font-size:12px;font-weight:800;color:#0c0e12;background:#ff8a4c;"
                 "border-radius:5px;padding:5px 8px;margin:3px 0'>&#9888; CAN'T READ HEAT "
                 "SCREEN - camber/toe limited (telemetry OK). Show the in-game tyre-temp "
                 "page on a cornering lap.</div>")
    # persistent console-mode notice: camber/toe less accurate (single tyre temp)
    if st.get("console_mode"):
        ipnote = (f" &nbsp;|&nbsp; console &#8594; this PC {_esc(st['lan_ip'])}:"
                  f"{st.get('port', 5607)}" if st.get("lan_ip") else "")
        P.append("<div style='font-size:11px;color:#0c0e12;background:#7fbfff;"
                 "border-radius:5px;padding:4px 7px;margin:3px 0'>&#127918; CONSOLE MODE - "
                 "camber/toe less accurate (single tyre temp, no 3-zone Heat reading)"
                 f"{ipnote}</div>")
    # 2+3+4. UNMISTAKABLE state: ACTION (edit the menu) vs DRIVE (touch nothing) vs DONE.
    # Colour + header verb + presence/absence of the checklist make a timed measuring
    # lap impossible to confuse with a go-to-the-menu prompt.
    ui = st.get("ui") or {}
    klass = ui.get("klass", "info")
    if klass == "action":
        # AMBER, bold header, then the exact field->value checklist, then "press F8".
        amber, ink = "#f2b134", "#0c0e12"
        P.append(f"<div style='background:{amber};color:{ink};border-radius:7px;"
                 "padding:7px 9px;margin:5px 0'>"
                 f"<div style='font-size:16px;font-weight:900'>&#9888; {_esc(ui.get('header',''))}</div>")
        cl = ui.get("checklist") or []
        for item in cl:
            P.append("<div style='font-size:14px;font-weight:700;margin:3px 0 0'>&bull; "
                     f"{_esc(item['label'])}: <span style='color:#7a1f1f'>{_esc(item['from'])}</span> "
                     f"&#8594; <b>{_esc(item['to'])}</b></div>")
        # one-line, telemetry-tied WHY for each proposed change (always shown)
        for reason in (ui.get("why") or []):
            P.append("<div style='font-size:11px;font-style:italic;color:#3a2f10;"
                     f"margin:1px 0 0 10px'>why: {_esc(reason)}</div>")
        foot = "&#9654; press F8 when applied"
        if ui.get("can_reject"):
            foot += " &nbsp;·&nbsp; [F10] reject (won't suggest again)"
        P.append(f"<div style='font-size:13px;font-weight:800;margin-top:5px'>{foot}</div></div>")
    elif klass == "drive":
        # GREEN/BLUE, passive wording, NO checklist - do not touch anything.
        green = "#4fcc4f"
        P.append(f"<div style='font-size:17px;font-weight:800;color:{green};margin:5px 0'>"
                 f"&#9679; {_esc(ui.get('header',''))}</div>")
        if ui.get("sub"):
            P.append(f"<div style='font-size:12px;color:#9fd9a0'>{_esc(ui['sub'])}</div>")
    elif klass == "done":
        col = "#f2b134" if ui.get("header", "").startswith("TIME BUDGET") else "#4fcc4f"
        P.append(f"<div style='font-size:17px;font-weight:800;color:{col};margin:5px 0'>"
                 f"&#9679; {_esc(ui.get('header',''))}</div>")
        if ui.get("sub"):
            P.append(f"<div style='font-size:12px;color:#cde'>{_esc(ui['sub'])}</div>")
    else:
        label, color = _state_badge(st)
        P.append(f"<div style='font-size:17px;font-weight:800;color:{color};margin:5px 0'>"
                 f"&#9679; {_esc(label)}</div>")
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
    # #5: fastest CLEAN lap actually driven this session - shown so the user is never told
    # their best is slower than a lap they just turned (it may be on a reverted change).
    fld = st.get("fastest_lap_driven_s")
    if fld:
        P.append("<div style='font-size:12px;color:#7fd3ff'>"
                 f"&#9201; Fastest lap driven: {fld:.2f}s</div>")
    # PROGRESS: confirmed gains, best-vs-start, and a plain-language trend so the user
    # can always tell if it's getting anywhere.
    pr = st.get("progress") or {}
    if pr.get("best_s") is not None:
        d = pr.get("delta_vs_start_s")
        dtxt = f" ({d:+.2f}s vs start)" if d is not None else ""
        trend = pr.get("trend", "")
        tcol = ("#4fcc4f" if trend in ("Improving", "Fine-tuning")
                else "#f2b134" if "finish soon" in trend else "#9cf")
        saved = pr.get("aba_saved", 0)
        savetxt = f" &nbsp;·&nbsp; {saved} re-test{'s' if saved != 1 else ''} saved" if saved else ""
        P.append("<div style='font-size:12px;font-weight:700;margin-top:2px'>"
                 f"Confirmed gains: {pr.get('confirmed_gains', 0)} &nbsp;·&nbsp; "
                 f"Best so far: {pr['best_s']:.2f}{dtxt}</div>"
                 f"<div style='font-size:12px;color:{tcol}'>&#9679; {_esc(trend)}{savetxt}</div>")
    # time budget countdown (real wall-clock from the first Rivals lap)
    rem = st.get("budget_remaining_s")
    if st.get("budget_expired"):
        P.append("<div style='font-size:12px;font-weight:700;color:#f2b134'>"
                 "Time budget reached - finishing current test</div>")
    elif rem is not None:
        col = "#f2b134" if rem <= 120 else "#9cf"
        P.append(f"<div style='font-size:12px;color:{col}'>time budget: "
                 f"{int(rem // 60)}:{int(rem % 60):02d} left</div>")
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
        speed = live.get("speed_text") or f"{live.get('speed_mph', 0):.0f} mph"
        P.append(f"<div style='color:#9cf;font-size:11px'>{_esc(speed)} "
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
