"""GUI app entry: wires the headless Controller to the non-activating overlay,
global hotkeys, optional LAN web view, and the peak-load Heat capture.

Run:  python -m lapsmith.gui  [--port 5607] [--web]

Robustness (real-game lessons):
  * EVERY action handler is wrapped so a failure shows an error in the overlay and
    is logged with a full traceback - it never vanishes silently.
  * Logs go to %APPDATA%/LapSmith/app.log AND the console.
  * The Qt loop never exits when the setup dialog closes
    (quitOnLastWindowClosed=False) and the overlay is re-shown afterwards.
  * Hotkey callbacks run on the `keyboard` thread, so they only ENQUEUE actions;
    a QTimer drains them on the Qt thread.

Requires Forza in BORDERLESS WINDOWED.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import queue
import sys
import threading
import time
import traceback
from typing import Optional

from . import controller as C
from .hotkeys import HotkeyManager
from ..vision import capture
from .. import PRODUCT_NAME, resource_path

log = logging.getLogger("lapsmith.gui")

# Heat must be captured MID-CORNER, at peak LATERAL load - not on a launch/straight
# (longitudinal g), which heats the rear evenly across the width. Forza's lateral
# axis is AccelerationX; override with FH6_LATERAL_AXIS=z if a build differs.
LATERAL_AXIS = os.environ.get("FH6_LATERAL_AXIS", "x").lower()

# A real cornering peak is in a SANE band and SUSTAINED. Crashes show 15-18g
# spikes (|ax|~180), often with a huge |az| and a sudden speed drop - reject those.
MAX_CORNER_G = 4.0        # above this = crash/curb spike, not cornering
SUSTAIN_FRAMES = 3        # consecutive in-band frames required (~150ms at 20Hz)
SPEED_DROP_MS = 8.0       # speed loss in one frame this large = impact


def lateral_g(p) -> float:
    a = getattr(p, f"accel_{LATERAL_AXIS}", p.accel_x)
    return abs(a) / 9.80665


def is_cornering_peak(lat_g: float, lon_g: float, speed_drop_ms: float,
                      sustained_frames: int) -> bool:
    """True only for a believable sustained mid-corner load - filters 1-frame
    crash/curb spikes and longitudinal (launch/impact) frames."""
    if lat_g < C.LOAD_MIN_G or lat_g > MAX_CORNER_G:
        return False                      # no load, or an unrealistic spike
    if lon_g > MAX_CORNER_G:
        return False                      # longitudinal crash/launch, not a corner
    if speed_drop_ms > SPEED_DROP_MS:
        return False                      # sudden deceleration = impact
    return sustained_frames >= SUSTAIN_FRAMES


def _log_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, PRODUCT_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def _set_app_user_model_id() -> None:
    """Windows: give the process an explicit AppUserModelID so the taskbar groups
    LapSmith under OUR icon instead of the generic python.exe one. MUST run BEFORE
    the QApplication is created, otherwise Windows has already chosen the icon."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(PRODUCT_NAME)
    except Exception:
        log.debug("could not set AppUserModelID", exc_info=True)


def setup_logging() -> str:
    logfile = os.path.join(_log_dir(), "app.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in (logging.FileHandler(logfile, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        root.addHandler(h)

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("lapsmith").critical(
            "UNCAUGHT: %s", "".join(traceback.format_exception(exc_type, exc, tb)))
    sys.excepthook = _excepthook
    return logfile


class PeakHeatCapture:
    """Background: screenshot the Heat page at the highest lateral-g frame.
    Runs continuously; `reset_and_get()` returns the best frame since the last
    reset and starts a fresh one (used per-lap in auto mode). The app's overlay is
    excluded from captures (WDA_EXCLUDEFROMCAPTURE) so frames are game-only."""
    def __init__(self, listener, tag: int = 0):
        self.listener, self.tag = listener, tag
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.best_g = 0.0
        self.best_path: Optional[str] = None
        self.best_udp: Optional[dict] = None      # UDP TireTemp (C) at the captured frame
        self._th: Optional[threading.Thread] = None
        self._can = capture.backend_available()

    def start(self):
        if not self._can:
            log.warning("no screenshot backend - Heat capture disabled (manual fallback)")
            return
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        consec = 0
        prev_speed = None
        while not self._stop.is_set():
            try:
                s = self.listener.snapshot() if self.listener else None
                if s is not None:
                    g = lateral_g(s)                       # LATERAL only, not launch
                    lon_g = abs(s.accel_z) / 9.80665       # longitudinal
                    drop = (prev_speed - s.speed) if prev_speed is not None else 0.0
                    prev_speed = s.speed
                    # count consecutive in-band cornering frames
                    consec = consec + 1 if (C.LOAD_MIN_G <= g <= MAX_CORNER_G) else 0
                    cornering = is_cornering_peak(g, lon_g, drop, consec)
                    with self._lock:
                        new_peak = cornering and g > self.best_g + 0.03
                        tag = self.tag
                    if new_peak:
                        path = capture.grab("tyre_temps", monotonic_tag=tag)
                        udp = {"FL": s.tire_temp_fl, "FR": s.tire_temp_fr,
                               "RL": s.tire_temp_rl, "RR": s.tire_temp_rr}  # Celsius
                        with self._lock:
                            self.best_path, self.best_g, self.best_udp = path, g, udp
                        log.info("Heat capture @ lateral %.2fg lon %.2fg (ax=%.1f az=%.1f "
                                 "%.0fmph) udp=%s -> %s", g, lon_g, s.accel_x, s.accel_z,
                                 s.speed_mph, {k: round(v) for k, v in udp.items()}, path)
            except Exception:
                log.exception("Heat capture frame failed")
            time.sleep(0.05)

    def reset_and_get(self):
        with self._lock:
            path, g, udp = self.best_path, self.best_g, self.best_udp
            self.best_path, self.best_g, self.best_udp = None, 0.0, None
            self.tag += 1
        return path, g, udp

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=1.5)
        return self.reset_and_get()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lapsmith.gui",
                                 description=f"{PRODUCT_NAME} overlay (never steals game focus)")
    ap.add_argument("--port", type=int, default=5607)
    ap.add_argument("--web", action="store_true", help="also serve the LAN view")
    ap.add_argument("--web-port", type=int, default=8077)
    args = ap.parse_args(argv)

    logfile = setup_logging()
    log.info("%s GUI starting (port %s). Log: %s", PRODUCT_NAME, args.port, logfile)

    # Diagnostic: LAPSMITH_OCR_SELFCHECK=1 forces the OFFLINE OCR engine to fully
    # initialise (loads the bundled PP-OCR .onnx models), prints a result, and
    # exits - no window, no socket. Lets a packaged build prove the OCR path still
    # works after dependency trimming, without driving the game.
    if os.environ.get("LAPSMITH_OCR_SELFCHECK"):
        from ..vision import read_tyres
        try:
            read_tyres._get_rapid_engine()      # constructs RapidOCR() -> loads models
            log.info("OCR self-check: RapidOCR initialised OK")
            print("OCR_SELFCHECK_OK")
            return 0
        except Exception as e:
            log.exception("OCR self-check FAILED")
            print(f"OCR_SELFCHECK_FAIL: {e}")
            return 1

    # Diagnostic: LAPSMITH_IMPORT_SELFCHECK=<file> parses a car-name file (the same
    # utf-8-sig path the Import dialog uses) and prints the counts, then exits. Lets
    # a packaged build confirm the Nexus JSON / semicolon-CSV import path works.
    chk = os.environ.get("LAPSMITH_IMPORT_SELFCHECK")
    if chk:
        from .. import car_import
        try:
            with open(chk, "r", encoding="utf-8-sig") as f:
                text = f.read()
            mapping, malformed = car_import.parse_text(text, chk)
            print(f"IMPORT_SELFCHECK_OK parsed={len(mapping)} malformed={malformed}")
            return 0
        except Exception as e:
            log.exception("import self-check FAILED")
            print(f"IMPORT_SELFCHECK_FAIL: {e}")
            return 1

    # Persist user-assigned car names + saved tunes under the app data dir.
    from .. import ordinals
    from ..state import store, prefs
    data_dir = _log_dir()
    n = ordinals.set_store_path(os.path.join(data_dir, "car_names.json"))
    store.set_sessions_dir(os.path.join(data_dir, "sessions"))
    prefs.set_store_path(os.path.join(data_dir, "prefs.json"))
    capture.CAPTURE_DIR = os.path.join(data_dir, "captures")   # Heat frames under the data dir
    log.info("loaded %d saved car name(s); tunes -> %s", n, store.SESSIONS_DIR)

    ctrl = C.Controller(port=args.port,
                        started_iso=_dt.datetime.now().isoformat(timespec="seconds"))
    ctrl.time_budget_min = prefs.time_budget_min()   # persisted ceiling (default 20)
    ctrl.start()
    ctrl.log(f"Listening on 127.0.0.1:{args.port}. Enable Data Out + borderless windowed.")
    ctrl.log(f"Log file: {logfile}")

    # build overlay (raises a helpful RuntimeError if PySide6 is missing).
    # TWO surfaces: this non-activating overlay is the LIVE-tuning HUD; the focusable
    # main window (built below) is the between-session management surface.
    from .overlay import build_overlay
    from . import setup_form, temps_dialog, name_dialog, main_window
    # MUST precede QApplication creation (inside build_overlay) so Windows uses our
    # taskbar icon, not the generic Python one.
    _set_app_user_model_id()
    app, overlay = build_overlay(ctrl.status, hotkey_help="",
                                 capturable_fn=lambda: ctrl.overlay_capturable)
    from PySide6 import QtCore, QtWidgets, QtGui   # safe now - build_overlay succeeded

    # Cohesive dark theme, applied app-wide. The overlay shares this QApplication
    # so it picks up the same palette; its own inline styles + non-activating,
    # translucent window are untouched (the sheet sets no bare-QWidget background).
    from .theme import apply_theme
    apply_theme(app)

    # One shared app icon (resolves from source AND from a PyInstaller build) set on
    # the application, the live overlay, the main window, and the tray.
    app_icon = QtGui.QIcon(resource_path("assets/lapsmith.ico"))
    app.setWindowIcon(app_icon)
    overlay.setWindowIcon(app_icon)

    # naming an unknown car: a Qt prompt, saved to car_names.json. Wrapped so a
    # dialog failure surfaces rather than crashing the confirm step.
    def prompt_car_name(identity):
        try:
            return name_dialog.show_name_dialog(identity.ordinal, detail=identity.summary())
        except Exception as e:
            log.exception("car-name dialog failed")
            ctrl.fail(f"name dialog: {e}")
            return None
    ctrl.car_name_prompt_fn = prompt_car_name

    # manual tyre-temp entry happens in a Qt dialog showing the captured frame -
    # NEVER console input(). Wrapped so a dialog failure surfaces, not crashes.
    def manual_temps(path):
        try:
            return temps_dialog.show_temps_dialog(path)
        except Exception as e:
            log.exception("manual temp dialog failed")
            ctrl.fail(f"temp dialog: {e}")
            return None
    ctrl.manual_temp_fn = manual_temps
    # CRITICAL: closing the main window / setup dialog must NOT quit the app - it
    # hides to the tray; telemetry + overlay stay alive. Only tray Quit exits.
    app.setQuitOnLastWindowClosed(False)

    actions: "queue.Queue[str]" = queue.Queue()
    capture_box = {"cap": None, "tag": 0}     # MANUAL transient capture
    auto_box = {"cap": None}                  # AUTO continuous per-lap capture
    busy = {"flag": False}     # reentrancy guard (the setup dialog runs a nested loop)
    done_box = {"bundled": False}             # write the support zip once on completion

    def current_frames():
        frames = []
        for box in (auto_box.get("cap"), capture_box.get("cap")):
            if box and getattr(box, "best_path", None):
                frames.append(box.best_path)
        return frames

    shutdown = {"done": False}

    def _shutdown():
        """Release every OS resource we hold - the UDP socket above all - so a
        relaunch can immediately re-bind port 5607. Idempotent: runs once whether
        triggered by tray Quit, the quit hotkey, or the post-loop cleanup."""
        if shutdown["done"]:
            return
        shutdown["done"] = True
        log.info("shutting down - releasing telemetry + hotkeys")
        try:
            if tray is not None:
                tray.hide()        # remove the tray icon immediately on exit
        except Exception:
            log.exception("tray hide failed")
        try:
            hk.stop()
        except Exception:
            log.exception("hotkey stop failed")
        for box in (auto_box, capture_box):
            cap = box.get("cap")
            if cap:
                try:
                    cap.stop()
                except Exception:
                    log.exception("capture stop failed")
                box["cap"] = None
        try:
            ctrl.stop()        # closes the UDP socket -> frees port 5607 now
        except Exception:
            log.exception("controller stop failed")

    def real_quit():
        log.info("tray Quit - exiting")
        _shutdown()            # release UDP 5607 NOW, before the loop unwinds
        app.quit()

    # forward declaration so hooks can reference start_tuning before it's defined
    state = {"start_tuning": None}

    hooks = {
        "start_tuning": lambda: state["start_tuning"] and state["start_tuning"](),
        "support_bundle": lambda: ctrl.write_support_bundle(
            app_log=logfile, heat_frames=current_frames()),
        "captures_dir": lambda: capture.captures_dir(),
        "app_log": logfile,
        # re-apply the overlay's capture display-affinity when the Settings
        # checkbox changes, so it takes effect immediately on the live overlay.
        "apply_overlay_capture": lambda: overlay.apply_capture_affinity(),
        "quit": real_quit,
    }
    window = main_window.build_main_window(ctrl, hooks)
    window.setWindowIcon(app_icon)

    def show_window():
        window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    # The non-activating overlay carries its own Exit / Main-window buttons (the
    # global hotkeys may not be registered without admin). Exit runs the SAME clean
    # shutdown + quit as the tray; Main window restores the management window.
    overlay.on_exit = real_quit
    overlay.on_show_main = show_window

    # system tray: the app lives here when the window is closed/hidden.
    tray = None
    if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        tray = QtWidgets.QSystemTrayIcon(app_icon, app)
        tray.setToolTip(PRODUCT_NAME)
        menu = QtWidgets.QMenu()
        menu.addAction("Open").triggered.connect(show_window)
        menu.addAction("Start Tuning").triggered.connect(lambda: hooks["start_tuning"]())
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(real_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: show_window()
            if reason == QtWidgets.QSystemTrayIcon.DoubleClick else None)
        tray.show()

    def enqueue(action):
        # log at the PRESS point (keyboard thread). If app.log shows nothing on a
        # press, it's a hotkey-registration/focus/elevation issue, not state.
        log.info("[hotkey] %s pressed (phase=%s mode=%s)", action, ctrl.phase, ctrl.mode)
        actions.put(action)

    hk = HotkeyManager({
        "advance": lambda: enqueue("advance"),
        "end_test": lambda: enqueue("end_test"),
        "mark_start": lambda: enqueue("mark_start"),
        "mark_end": lambda: enqueue("mark_end"),
        "view_mode": lambda: enqueue("view_mode"),
        "support_bundle": lambda: enqueue("support_bundle"),
        "quit": lambda: enqueue("quit"),
    })
    overlay._hotkey_help = hk.help_text()

    def begin_test():
        capture_box["tag"] += 1
        cap = PeakHeatCapture(ctrl.listener, capture_box["tag"])
        cap.start()
        capture_box["cap"] = cap
        ctrl.begin_test()

    def end_test():
        cap = capture_box["cap"]
        path, g, udp = (cap.stop() if cap else (None, 0.0, None))
        capture_box["cap"] = None
        ctrl.end_test(heat_path=path, peak_g=g, udp_temps=udp)

    def start_tuning():
        """START TUNING (from the main window or tray). Runs the SETUP steps here
        in the focusable window (car detect/name + discipline + bounds) - not
        driving yet, so focus is fine - then HANDS OFF to the overlay for the drive:
        hide the window, show the non-activating HUD. Wrapped so failures show."""
        if busy["flag"]:
            return
        try:
            ctrl.poll_identity()                       # need a live, detected car
            if ctrl.identity is None:
                QtWidgets.QMessageBox.information(
                    window, "Start tuning",
                    "No car detected yet. In FH6: enable Data Out, set borderless "
                    "windowed, and drive briefly - then press START TUNING again.")
                return
            ctrl.reset_session()                       # clean slate for a new car/run
            ctrl.confirm_car()                         # prompts for a name if unknown
            res = setup_form.show_setup_dialog(ctrl.identity.summary(),
                                               ctrl.identity.class_letter,
                                               time_budget_default=prefs.time_budget_min())
            if not res:
                ctrl.log("Setup cancelled.")
                ctrl.phase = C.WAIT_TELEMETRY
                return
            if res.get("time_budget_min") is not None:    # share with the main-window control
                prefs.set("time_budget_min", float(res["time_budget_min"]))
            ctrl.apply_setup(res["discipline"], res["limits"], res["front_weight"],
                             changes_per_test=res["changes_per_test"],
                             laps_per_test=res["laps_per_test"], lap_agg=res["lap_agg"],
                             temp_mode=res.get("temp_mode"),
                             use_vision_api=res.get("use_vision_api"),
                             target_class=res.get("target_class"),
                             aggressiveness=res.get("aggressiveness"),
                             rigour=res.get("rigour"),
                             time_budget_min=res.get("time_budget_min"))
            done_box["bundled"] = False
            log.info("setup applied: %s -> phase=%s",
                     {k: v for k, v in res.items() if k != "limits"}, ctrl.phase)
            # HAND OFF: hide the window so it can't steal focus; show the HUD.
            window.hide()
            overlay.show()
            overlay.raise_()
        except Exception as e:
            log.exception("start_tuning failed")
            ctrl.fail(f"start tuning: {e}")
    state["start_tuning"] = start_tuning

    def do_advance():
        ph = ctrl.phase
        if ph in (C.CONFIRM_CAR, C.WAIT_TELEMETRY, C.SETUP):
            start_tuning()
        elif ph == C.APPLY_BASELINE:
            ctrl.baseline_applied()        # detects auto vs manual mode
        elif ph == C.TEST:                 # manual only
            begin_test()
        elif ph == C.SHOW_CHANGE:
            ctrl.change_applied()
        elif ph == C.DONE:
            folder = (ctrl.export or {}).get("folder")
            if folder and os.path.isdir(folder):
                try:
                    os.startfile(folder)        # Windows: open the tunes folder
                except Exception:
                    ctrl.log(f"Tunes saved in: {folder}")
        elif ph == C.DRIVE_AUTO:
            if ctrl.mode is None:
                ctrl.log("Still detecting laps - drive a lap (or press F9 for a manual segment).")
            else:
                ctrl.log("Auto-lap mode: laps are captured automatically - just keep driving.")

    def dispatch(action: str):
        log.info("dispatch %s (phase=%s mode=%s)", action, ctrl.phase, ctrl.mode)
        if action == "advance":
            do_advance()
        elif action == "end_test":
            if ctrl.phase == C.TEST and capture_box["cap"]:
                end_test()
        elif action == "mark_start":
            ctrl.mark_segment_start()
        elif action == "mark_end":
            ctrl.mark_segment_end()
        elif action == "view_mode":
            ctrl.toggle_view_mode()
        elif action == "support_bundle":
            frames = []
            for box in (auto_box.get("cap"), capture_box.get("cap")):
                if box and getattr(box, "best_path", None):
                    frames.append(box.best_path)
            ctrl.write_support_bundle(app_log=logfile, heat_frames=frames)
        elif action == "quit":
            real_quit()

    def pump():
        # never let an exception escape the timer slot (that can kill the loop)
        try:
            if ctrl.phase == C.WAIT_TELEMETRY:
                ctrl.poll_identity()
            else:
                ctrl.refresh_identity()   # re-read DrivetrainType etc. every tick
            # AUTO-LAP: while DRIVING (detecting OR auto) start the continuous
            # per-lap Heat capture and run the lap detector each tick. (The bug:
            # tick() only ran once mode was AUTO, but mode only flips INSIDE tick -
            # so it never engaged. tick() must run while detecting too.)
            if ctrl.phase == C.DRIVE_AUTO:
                if auto_box["cap"] is None:
                    cap = PeakHeatCapture(ctrl.listener, tag=1000)
                    cap.start()
                    auto_box["cap"] = cap
                    ctrl.lap_heat_fn = cap.reset_and_get
                ctrl.tick()
            # on completion: write the support zip once, copy the tune to the
            # clipboard, then RETURN to the management window (refreshed so the new
            # tune shows in Previous Tunes + Dashboard) and drop the live overlay.
            if ctrl.phase == C.DONE and not done_box["bundled"]:
                done_box["bundled"] = True
                ctrl.write_support_bundle(app_log=logfile, heat_frames=current_frames())
                exp = ctrl.export or {}
                if exp.get("share_text"):
                    try:
                        app.clipboard().setText(exp["share_text"])
                        ctrl.log("Tune copied to clipboard.")
                    except Exception:
                        log.exception("clipboard copy failed")
                if auto_box["cap"]:
                    auto_box["cap"].stop()
                    auto_box["cap"] = None
                overlay.hide()
                window.refresh()
                window.show()
                window.raise_()
                window.activateWindow()
            if busy["flag"]:
                return
            busy["flag"] = True
            try:
                while True:
                    try:
                        action = actions.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        dispatch(action)
                    except Exception as e:
                        log.exception("action '%s' failed", action)
                        ctrl.fail(f"{action}: {e}  (see app.log)")
            finally:
                busy["flag"] = False
        except Exception as e:
            log.exception("pump failed")
            try:
                ctrl.fail(f"pump: {e}")
            except Exception:
                pass

    timer = QtCore.QTimer()
    timer.timeout.connect(pump)
    timer.start(120)

    if not hk.start():
        ctrl.log("[hotkeys] keyboard lib unavailable - install `keyboard` (admin on Windows).")
        log.warning("global hotkeys not registered (keyboard lib missing or no admin)")

    if args.web:
        from . import web
        wt = web.serve(ctrl.status, port=args.web_port)
        ctrl.log(f"[web] LAN view on http://<this-pc>:{args.web_port}"
                 if wt else "[web] fastapi/uvicorn not installed.")

    # Start on the MANAGEMENT window (focusable). The overlay appears only when a
    # session begins driving (START TUNING). FH6 runs borderless, so the window
    # sitting over it during setup is fine.
    window.show()
    log.info("main window shown; entering Qt loop")

    # Diagnostic: LAPSMITH_SELFTEST_EXIT=<ms> fires the overlay's Exit action after
    # the loop starts, to verify the clean-shutdown path (UDP 5607 release + tray
    # hide + quit) in a packaged build. Exercises the EXACT Exit-button callback.
    _exit_ms = os.environ.get("LAPSMITH_SELFTEST_EXIT")
    if _exit_ms:
        try:
            _ms = int(_exit_ms)
        except ValueError:
            _ms = 1500
        def _selftest_exit():
            log.info("SELFTEST_EXIT: invoking overlay Exit callback")
            if callable(overlay.on_exit):
                overlay.on_exit()
        QtCore.QTimer.singleShot(_ms, _selftest_exit)

    try:
        rc = app.exec()
    finally:
        _shutdown()            # idempotent - no-op if tray Quit already ran it
        log.info("shutdown complete")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
