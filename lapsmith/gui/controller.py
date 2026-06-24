"""Headless controller: the GUI's brain, with NO Qt / input() dependency.

It owns the tuning state machine and reuses the validated core (identity
auto-detect, range-relative + clamped baseline, the analyzer with per-iteration
clamping and the ride-height no-progress fallback, the free-roam segment timer,
and the fitness gate). The PySide6 overlay, global hotkeys and optional web view
are thin layers that call these methods and render `status()` - so the decision
logic here is unit-testable without a display.

Advance is event-driven (hotkeys), never blocking: the game keeps focus the
whole time.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

_laplog = logging.getLogger("lapsmith.laps")
_rawlog = logging.getLogger("lapsmith.raw")     # high-frequency per-tick/per-packet dumps
_declog = logging.getLogger("lapsmith.session")  # user-facing decision narrative

from ..telemetry.listener import TelemetryListener
from ..telemetry.session import aggregate, TestStats, HIGH_G_THRESHOLD
from ..telemetry import segment
from ..identity import identify, is_live, CarIdentity
from .. import ordinals, PRODUCT_NAME
from ..knowledge.baseline import build_baseline, format_checklist, field_label, fmt_field
from ..knowledge import rules
from ..knowledge import fitness
from ..state.tune_state import Tune, TuneState, CarLimits
from ..state import store
from ..telemetry.laps import LapWatcher, LapResult, LAP_TIME_FLOOR

LOAD_MIN_G = HIGH_G_THRESHOLD
RIDE_IMPROVE_MARGIN = 0.02
ADAPTIVE_SMALL_DELTA = 0.3     # improvement below this -> escalate laps-per-test
ADAPTIVE_EVIDENCE_MARGIN = 0.3 # extra time a regression must clear to revert EVIDENCE


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

# phases (also drive what the overlay shows / which hotkeys are meaningful)
WAIT_TELEMETRY = "wait_telemetry"
CONFIRM_CAR = "confirm_car"
SETUP = "setup"
APPLY_BASELINE = "apply_baseline"
BASELINE_TIME = "baseline_time"      # MANUAL: mark start/end to set the reference time
TEST = "test"                        # MANUAL: drive the characterisation test (Heat up)
SHOW_CHANGE = "show_change"          # one exact change shown; apply it
CHANGE_TIME = "change_time"          # MANUAL: time the same segment after the change
DRIVE_AUTO = "drive_auto"            # AUTO-LAP: just keep lapping; laps captured automatically
DONE = "done"

MODE_AUTO = "auto"       # Rivals/circuit: lap fields live -> auto-lap
MODE_MANUAL = "manual"   # free-roam: F9/F10 segment markers


@dataclass
class Controller:
    port: int = 5607
    front_weight_pct: float = 50.0
    listener: Optional[TelemetryListener] = None

    phase: str = WAIT_TELEMETRY
    identity: Optional[CarIdentity] = None
    discipline: str = "road"
    target_class: Optional[str] = None    # user-chosen build target ("B 700" etc.)
    limits: CarLimits = field(default_factory=CarLimits)

    baseline: Optional[Tune] = None
    state: Optional[TuneState] = None
    batch: List = field(default_factory=list)        # the change(s) for this test lap
    _applied_records: List = field(default_factory=list)
    changes_per_test: int = 1                          # search-driven changes per lap

    @property
    def rec(self):
        """First change in the batch (back-compat for single-change call sites)."""
        return self.batch[0] if self.batch else None

    best_segment: Optional[float] = None
    _baseline_lap_s: Optional[float] = None      # the original baseline reference time
    ref_distance: Optional[float] = None
    _seg_start: Optional[segment.Mark] = None
    _pending_run: Optional[segment.SegmentRun] = None

    stats: Optional[TestStats] = None
    tyre_reading: Optional[dict] = None
    last_reader: Optional[str] = None     # which temp reader last succeeded (env info)
    _test_mark: int = 0

    # auto-lap (Rivals/circuit)
    mode: Optional[str] = None
    _watcher: LapWatcher = field(default_factory=LapWatcher)
    _tick_mark: int = 0
    _skip_laps: int = 0               # ignore N upcoming lap completions (warm-up/out-laps)
    _restart_count: int = 0           # event restarts detected (diagnostic)
    _awaiting_test: bool = False      # a change was applied; next full lap is its test
    # explicit WAITING_FOR_MEASURED_LAP sub-state after a change: None | "out_lap"
    # (waiting for the out-lap to pass) | "measuring" (timing the clean lap). A
    # reload/spurious lap or another F8 does NOT advance while this is set.
    _await_state: Optional[str] = None
    _pre_change_last_lap: Optional[float] = None   # carried-over LastLap to reject
    _await_dbg: tuple = ()            # dedup for the per-tick waiting log
    last_lap_s: Optional[float] = None
    lap_number: int = 0
    # multi-lap fitness (single laps are too noisy)
    laps_per_test: object = "adaptive"   # "adaptive" | 1 | 2 | 3
    lap_agg: str = "best"                # "best" (default) | "median"
    # Console mode: Forza runs on an Xbox/console streaming Data Out over the LAN.
    # No in-game Heat screen to OCR, so tyre temps fall back to the single per-corner
    # UDP TireTemp; camber/toe degrade to lap-time tuning (less accurate).
    console_mode: bool = False
    # Manual drivetrain override (safety net for a misdetected DrivetrainType): when
    # set to FWD/RWD/AWD it overrides the telemetry-detected drivetrain everywhere
    # (which diff fields the rules touch, the baseline checklist).
    drivetrain_override: Optional[str] = None
    _test_laps: List[float] = field(default_factory=list)
    _best_lap_time: Optional[float] = None
    _best_lap_stats: object = None
    _best_lap_reading: object = None
    _last_improvement: Optional[float] = None
    _lap_dbg: tuple = (-1, -1, -1.0, -1.0)   # last-logged (raceOn, lap, cur, last)
    _first_change_logged: bool = False
    # GUI-injected: () -> (best_heat_path, peak_g) for the lap just finished, and
    # RESET the peak tracker for the next lap. None in headless/tests.
    lap_heat_fn: Optional[Callable[[], tuple]] = None

    ride_locked: set = field(default_factory=set)
    _last_ride_change: Optional[dict] = None
    # anti-fixation: per-axle bottoming attempt counts, axles whose bottoming remedy
    # is LOCKED (cap hit, or the gate reverted it), and the same-symptom streak.
    _bottoming_attempts: dict = field(default_factory=dict)
    _bottoming_locked: set = field(default_factory=set)
    _cur_bottoming_axles: set = field(default_factory=set)   # this batch's bottoming axles
    _last_symptom: Optional[str] = None
    _symptom_streak: int = 0
    # GENERAL anti-fixation: per-lever-key ("field:dir") consecutive non-improving
    # count, the set of locked lever-keys, and each field's last value that actually
    # improved the best lap (to roll back to on lock).
    _noimprove: dict = field(default_factory=dict)
    _lever_locked: set = field(default_factory=set)
    _last_improving: dict = field(default_factory=dict)
    # --- drift-robust methodology (telemetry-primary fitness) -------------------
    rigour: str = "confirmed"          # "quick" (single pass) | "confirmed" (A/B/A)
    time_budget_min: float = 0.0       # 0 = unlimited; wall-clock from first lap
    _ref_telem: object = None          # accepted tune's binned telemetry (A)
    _baseline_telem: object = None     # ORIGINAL session baseline telemetry (final check)
    _test_packets: list = field(default_factory=list)   # packets across the measurement
    _warmup_seen: int = 0              # warm-up laps before the first baseline
    _iters_since_reanchor: int = 0
    _aba: object = None                # in-flight A/B/A confirmation, or None
    _aba_keep: object = None           # A/B/A-confirmed B, awaiting re-apply
    _reanchor_pending: bool = False    # next measurement re-anchors the baseline
    _final_check: bool = False         # in-flight honest final re-measure of the baseline
    _final_check_done: bool = False
    _budget_start: float = 0.0         # perf_counter at the first green lap (0=not started)
    _budget_expired: bool = False
    stop_reason: str = ""              # "converged" | "stopped: time budget (N min)"
    final_verdict: str = ""            # honest result text after the final check
    _on_car: dict = field(default_factory=dict)   # values PHYSICALLY entered in-game now
    # --- UX/methodology (v0.1.12) ---------------------------------------------
    _session_start_best: Optional[float] = None    # best lap when the baseline was set
    _confirmed_gains: int = 0          # changes that really lowered the best lap
    _recent_outcomes: list = field(default_factory=list)   # last few: gain/revert/discount/reject
    _rejected_fields: set = field(default_factory=set)      # user-rejected levers (whole session)
    _aba_saved: int = 0                # A/B/A re-tests SKIPPED thanks to driver-input discounting
    # The BEST CONFIRMED tune (what gets SAVED) - tracked separately from state.current,
    # which drifts during reverts/A-B-A. Guarantees the saved tune is never a mid-flight
    # reverted state (fixes "exited on baseline despite a faster confirmed tune").
    _best_tune: object = None          # Tune copy that achieved best_segment
    _best_tune_lap: Optional[float] = None
    _session_log: object = None        # per-session decision-log handler
    _saved: bool = False               # a final save has been written
    persist: bool = False              # app sets True; gates disk writes (off in tests)
    _temp_warned: bool = False         # logged the no-temp-reader warning once
    _channels_logged: bool = False     # logged which telemetry channels were live
    # change aggressiveness -> step multiplier (fine 0.5 / normal 1.0 / coarse 2.0).
    aggressiveness: str = "normal"
    stale: int = 0
    export: Optional[dict] = None       # paths to the shareable files (set at finish)
    started_iso: str = ""
    messages: List[str] = field(default_factory=list)
    error: Optional[str] = None       # shown prominently; set on any caught failure
    # GUI-injected: (image_path) -> reading dict in C, or None. Opens a Qt dialog
    # showing the captured screenshot. Headless/tests leave it None (no console I/O).
    manual_temp_fn: Optional[Callable[[str], Optional[dict]]] = None
    # Tyre-temp reading mode. "auto" = local OCR (RapidOCR primary, Tesseract
    # fallback) with NO blocking on manual; if no clean read, camber is tuned by
    # lap-time search instead. "manual" = open the Qt dialog every lap (opt-in).
    temp_mode: str = "auto"
    # OPT-IN only: use the Anthropic vision API as an extra reader BEFORE local
    # OCR. Off by default; the standalone build works fully offline with no key.
    use_vision_api: bool = False
    # GUI-injected: (CarIdentity) -> typed name or None. Opens a Qt prompt when an
    # unknown ordinal is detected. None in headless/tests.
    car_name_prompt_fn: Optional[Callable[[CarIdentity], Optional[str]]] = None
    # overlay view (presentation only): SIMPLE default; ADVANCED adds diagnostics.
    # The management TABS (Dashboard/Previous/Logs/Settings/Help) live in the main
    # window now, not the overlay - this is just the live HUD's detail level.
    view_mode: str = "simple"          # "simple" | "advanced"
    # Show the overlay in screen recordings/screenshots? OFF by default keeps it
    # hidden from capture (WDA_EXCLUDEFROMCAPTURE) so it can't obscure the Heat-page
    # tyre temps. The dev env var LAPSMITH_OVERLAY_CAPTURABLE force-enables it.
    overlay_capturable: bool = False

    # ---- lifecycle -------------------------------------------------------
    def _bind_host(self) -> str:
        # Console mode: the console streams from another LAN device, so listen on all
        # interfaces. PC mode stays on loopback (safer; no LAN exposure).
        return "0.0.0.0" if self.console_mode else "127.0.0.1"

    def start(self):
        if self.listener is None:
            self.listener = TelemetryListener(port=self.port, host=self._bind_host())
        self.listener.start()

    def set_console_mode(self, on: bool):
        """Toggle console mode. Rebinds the UDP listener (loopback <-> all interfaces)
        live so the change takes effect without a restart."""
        on = bool(on)
        if on == self.console_mode:
            return
        self.console_mode = on
        want = self._bind_host()
        if self.listener is not None and getattr(self.listener, "host", None) != want:
            running = self.listener._running.is_set()
            self.listener.stop()
            self.listener = TelemetryListener(port=self.port, host=want)
            if running:
                self.listener.start()
        _laplog.info("CONSOLE MODE %s; UDP listening on %s:%d.",
                     "ON" if on else "OFF", want, self.port)
        self.log(f"Console mode {'ON' if on else 'OFF'} - "
                 + ("listening for LAN telemetry; tyre temps use the single UDP value "
                    "(camber/toe less accurate)." if on
                    else "back to PC/loopback with Heat-screen OCR."))

    def stop(self):
        if self.listener:
            self.listener.stop()

    def log(self, msg: str):
        self.messages.append(msg)
        self.messages = self.messages[-50:]
        # Route the human narrative through the logging system so it lands in BOTH
        # app.log and the per-session DECISION log (a logging handler, see below).
        _declog.info(msg)

    def _open_session_log(self):
        """Attach a per-session DECISION log: a FileHandler on the `lapsmith` parent
        logger, so the WHOLE decision trail (proposed changes + their 'why', the
        eligible-vs-fired rules, drivetrain detection, A/B/A, re-anchors, fixation
        locks, the final check) is captured start-to-finish - NOT just self.log() and
        NOT the high-frequency raw telemetry (lapsmith.raw doesn't propagate here).
        Written incrementally + flushed per record, so it survives a crash."""
        self._close_session_log()
        try:
            m = self._meta()
            path = store.session_log_path(m["car"], self.discipline or "session")
            parent = logging.getLogger("lapsmith")
            parent.setLevel(logging.INFO)      # ensure INFO records reach the handler
                                               # (root may be unconfigured, e.g. in tests)
            # remove any stray session handler from a prior controller (no leaks)
            for old in [x for x in parent.handlers if getattr(x, "_lapsmith_session", False)]:
                parent.removeHandler(old)
                try:
                    old.close()
                except Exception:
                    pass
            h = logging.FileHandler(path, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            h.addFilter(lambda r: not r.name.startswith("lapsmith.raw"))   # belt-and-suspenders
            h._lapsmith_session = True
            parent.addHandler(h)
            self._session_log = h
            _laplog.info("===== SESSION START %s (%s, %s) - decision log: %s =====",
                         self.started_iso, m["car"], self.discipline, path)
        except Exception as e:
            self._session_log = None
            _laplog.exception("could not open session decision log")
            self.fail(f"session log could not be opened ({e}); check the sessions folder "
                      "is writable - see app.log.")

    def _close_session_log(self):
        h, self._session_log = self._session_log, None
        if h is not None:
            try:
                _laplog.info("===== SESSION LOG CLOSED =====")
                logging.getLogger("lapsmith").removeHandler(h)
                h.close()
            except Exception:
                pass

    # ---- telemetry helpers ----------------------------------------------
    def snapshot(self):
        return self.listener.snapshot() if self.listener else None

    def _best_moving_packet(self):
        best = None
        for p in (self.listener.drain_since(0) if self.listener else []):
            if p is None:
                continue
            if p.is_race_on or p.speed > 1.0:
                if best is None or p.speed > best.speed:
                    best = p
        return best

    # ---- phase 1: detect & confirm car ----------------------------------
    def poll_identity(self) -> Optional[CarIdentity]:
        pkt = self._best_moving_packet()
        if is_live(pkt):
            self.identity = identify(pkt)
            if self.phase == WAIT_TELEMETRY:
                self.phase = CONFIRM_CAR
            return self.identity
        return None

    def refresh_identity(self):
        """Re-read DrivetrainType (and the rest of the car-info block) from the
        LATEST live frame, so a swapped drivetrain (e.g. an AWD conversion) is
        reflected - it's not detected once at startup. Logs the raw value on any
        change so a misdetect is traceable."""
        snap = self.snapshot()
        if not is_live(snap) or self.identity is None:
            return
        if snap.drivetrain_type != self.identity.drivetrain_raw \
                or snap.car_ordinal != self.identity.ordinal:
            new = identify(snap)
            if new.drivetrain != self.identity.drivetrain:
                self.log(f"Drivetrain now {new.drivetrain} (raw {new.drivetrain_raw}, "
                         f"NumCylinders {new.num_cylinders}).")
            self.identity = new

    def needs_car_name(self) -> bool:
        """True when the detected ordinal has no friendly name yet (new/updated
        car). The GUI should prompt 'What car is this?' and save it."""
        return bool(self.identity) and self.identity.ordinal > 0 \
            and not ordinals.is_known(self.identity.ordinal)

    def set_car_name(self, name: str) -> bool:
        """Persist ordinal -> name and reflect it on the live identity immediately
        so it shows everywhere instead of 'Car #N'. Blank name is ignored."""
        if not self.identity or not (name or "").strip():
            return False
        ok = ordinals.save_name(self.identity.ordinal, name.strip())
        self.identity.name = ordinals.name_for(self.identity.ordinal)
        self.identity.known = ordinals.is_known(self.identity.ordinal)
        self.log(f"Car #{self.identity.ordinal} saved as '{self.identity.name}'.")
        return ok

    def reset_session(self):
        """Clear all per-session tuning state for a NEW run (the app is long-lived
        and tunes many cars). Keeps identity/listener; setup rebuilds the baseline.
        Does not change any saved file - only in-memory state."""
        self.best_segment = None
        self._baseline_lap_s = None
        self.mode = None
        self._watcher.reset()
        self._skip_laps = 0
        self._restart_count = 0
        self._awaiting_test = False
        self._await_state = None
        self._pre_change_last_lap = None
        self._await_dbg = ()
        self.stale = 0
        self.export = None
        self.batch = []
        self._applied_records = []
        self._reset_test()
        self.tyre_reading = None
        self.last_reader = None
        self.ride_locked = set()
        self._last_ride_change = None
        self._bottoming_attempts = {}
        self._bottoming_locked = set()
        self._cur_bottoming_axles = set()
        self._last_symptom = None
        self._symptom_streak = 0
        self._noimprove = {}
        self._lever_locked = set()
        self._last_improving = {}
        self._ref_telem = None
        self._baseline_telem = None
        self._test_packets = []
        self._warmup_seen = 0
        self._iters_since_reanchor = 0
        self._aba = None
        self._aba_keep = None
        self._reanchor_pending = False
        self._final_check = False
        self._final_check_done = False
        self._budget_start = 0.0
        self._budget_expired = False
        self.stop_reason = ""
        self.final_verdict = ""
        self._on_car = {}
        self._session_start_best = None
        self._confirmed_gains = 0
        self._recent_outcomes = []
        self._rejected_fields = set()
        self._aba_saved = 0
        self._best_tune = None
        self._best_tune_lap = None
        self._close_session_log()
        self._saved = False
        self._temp_warned = False
        self._channels_logged = False
        self.error = None
        self.started_iso = _dt.datetime.now().isoformat(timespec="seconds")

    def confirm_car(self):
        """Accept the detected car and move to the setup form. If the ordinal is
        unknown, prompt for a name first (via the GUI-injected prompt) and save it."""
        if self.identity is None:
            return
        if self.needs_car_name() and self.car_name_prompt_fn is not None:
            try:
                typed = self.car_name_prompt_fn(self.identity)
            except Exception as e:
                _laplog.exception("car-name prompt failed")
                typed = None
            if typed:
                self.set_car_name(typed)
        self.front_weight_pct = self.front_weight_pct or 50.0
        self.phase = SETUP

    # ---- phase 2: one-screen setup --------------------------------------
    def apply_setup(self, discipline: str, limits: CarLimits,
                    front_weight_pct: Optional[float] = None,
                    changes_per_test: Optional[int] = None,
                    laps_per_test: object = None, lap_agg: Optional[str] = None,
                    temp_mode: Optional[str] = None,
                    use_vision_api: Optional[bool] = None,
                    target_class: Optional[str] = None,
                    aggressiveness: Optional[str] = None,
                    rigour: Optional[str] = None,
                    time_budget_min: Optional[float] = None,
                    console_mode: Optional[bool] = None,
                    drivetrain: Optional[str] = None):
        if console_mode is not None:
            self.set_console_mode(bool(console_mode))   # rebinds the listener if needed
        if drivetrain in ("FWD", "RWD", "AWD"):
            self.drivetrain_override = drivetrain
        elif drivetrain in ("auto", "", None):
            self.drivetrain_override = None
        self.discipline = discipline
        if aggressiveness in ("fine", "normal", "coarse"):
            self.aggressiveness = aggressiveness
        if rigour in ("quick", "confirmed"):
            self.rigour = rigour
        if time_budget_min is not None:
            self.time_budget_min = max(0.0, float(time_budget_min))
        self.limits = limits or CarLimits()
        if temp_mode in ("auto", "manual"):
            self.temp_mode = temp_mode
        if use_vision_api is not None:
            self.use_vision_api = bool(use_vision_api)
        if front_weight_pct is not None:
            self.front_weight_pct = front_weight_pct
        if changes_per_test is not None:
            self.changes_per_test = max(1, min(3, int(changes_per_test)))
            if self.changes_per_test > 1:
                self.log(f"Search changes per lap = {self.changes_per_test}: batching "
                         "handling-cluster steps trades attribution for fewer laps.")
        if laps_per_test is not None:
            self.laps_per_test = laps_per_test
        if lap_agg in ("best", "median"):
            self.lap_agg = lap_agg
        # the user's chosen target class wins; fall back to the auto-detected one.
        self.target_class = (target_class
                             or (self.identity.target_class if self.identity else "S1 800"))
        target = self.target_class
        drivetrain = self.effective_drivetrain()        # manual override wins
        car = self.identity.name if self.identity else "Car"
        self.baseline = build_baseline(car, target, discipline,
                                       self.front_weight_pct, drivetrain,
                                       limits=self.limits)
        self.state = TuneState(self.baseline.copy())
        self.phase = APPLY_BASELINE
        # best CONFIRMED tune starts as the baseline (no confirmed gain yet)
        self._best_tune = self.baseline.copy()
        self._best_tune_lap = None
        self._saved = False
        src = ("manual override" if self.drivetrain_override
               else f"detected raw {self.identity.drivetrain_raw if self.identity else '?'}")
        _laplog.info("SETUP: drivetrain=%s (%s); discipline=%s; target=%s.",
                     drivetrain, src, discipline, target)
        # session meaningfully starts now -> open the incremental log + record a
        # provisional session row so it shows in history even on an abnormal exit.
        # (Gated by `persist` so unit tests don't write to disk on every apply_setup.)
        if self.persist:
            self._open_session_log()
            self.log(f"Baseline built for {car} ({target}, {drivetrain}).")
            self.save_progress("in_progress")
        else:
            self.log(f"Baseline built for {car} ({target}, {drivetrain}).")

    def baseline_checklist(self) -> str:
        if not self.baseline:
            return ""
        ident = self.identity
        target = self.target_class or (ident.target_class if ident else "S1 800")
        return format_checklist(self.baseline, ident.name if ident else "Car",
                                target,
                                self.discipline, self.front_weight_pct,
                                self.effective_drivetrain())

    def baseline_applied(self):
        """User entered the baseline. Mode is NOT locked here - it stays
        'detecting' and engages AUTO-LAP the moment the lap timer is seen
        advancing (even if the user enters the Rivals event later). Until then,
        [F9] commits to manual free-roam markers."""
        self.mode = None
        self._watcher.reset()
        self._tick_mark = self.listener.mark if self.listener else 0
        self.phase = DRIVE_AUTO
        # the car is now PHYSICALLY on the baseline values the user just entered
        self._on_car = self.state.current.as_dict() if self.state else {}
        self.log("Detecting lap timing... drive a lap (AUTO-LAP engages when the "
                 "timer advances), or press [F9] to mark a manual segment.")

    def _drive_phase(self) -> str:
        return DRIVE_AUTO if self.mode == MODE_AUTO else TEST

    # ---- AUTO-LAP: one iteration = one lap ------------------------------
    def arm_next_lap(self):
        """Ignore the next lap completion (a partial out-lap on a fresh tune)."""
        self._skip_laps = 1

    def _instrument(self, snap):
        """Raw lap fields (offset-verification) - HIGH FREQUENCY, kept off app.log."""
        if snap is None:
            return
        dbg = (int(snap.is_race_on), int(snap.lap_number),
               round(snap.current_lap, 1), round(snap.last_lap, 2))
        if dbg != self._lap_dbg:
            self._lap_dbg = dbg
            _rawlog.info("lap-fields RAW: IsRaceOn@0=%d LapNumber@312=%d "
                         "CurrentLap@304=%.2f LastLap@300=%.2f", *dbg)

    def tick(self):
        """Called frequently by the app while driving (DETECTING or AUTO). Feeds
        the persistent watcher, engages AUTO-LAP on first timer advance, and
        processes completed laps. Heavily logged so a stuck-detecting state is
        diagnosable; never swallows an exception silently."""
        if self.phase != DRIVE_AUTO or not self.listener:
            return
        try:
            mark0 = self._tick_mark
            pkts = self.listener.drain_since(mark0)
            self._tick_mark = self.listener.mark
            if pkts:
                self._instrument(pkts[-1])
            prev_lap = self._watcher.prev_lap_number
            prev_cur = self._watcher.prev_current_lap
            laps = self._watcher.feed(pkts)
            adv = self._watcher.advancing()
            if self.mode is None:
                last = pkts[-1] if pkts else None
                _rawlog.info("tick DETECT: n=%d prevLap=%s curLap=%s prevCur=%s "
                             "curCur=%s advancing=%s completed=%d",
                             len(pkts), prev_lap,
                             (last.lap_number if last else None), prev_cur,
                             (round(last.current_lap, 2) if last else None),
                             adv, len(laps))
            # engage AUTO-LAP the instant the lap timer is seen advancing
            if self.mode is None and adv:
                self.mode = MODE_AUTO
                # WARM-UP GUARD (D): skip cold/learning laps before the first baseline
                # so they don't set an artificially slow anchor.
                self._skip_laps = max(1, rules.WARMUP_LAPS) if self.best_segment is None else 1
                # TIME BUDGET (G): the clock starts at the FIRST Rivals lap and counts
                # continuous real wall-clock (incl. loads/menus/applying changes); never paused.
                if self.time_budget_min > 0 and self._budget_start == 0.0:
                    self._budget_start = time.perf_counter()
                    _laplog.info("TIME BUDGET: %.0f min clock STARTED at the first Rivals lap.",
                                 self.time_budget_min)
                    self.log(f"Tuning time budget: {int(self.time_budget_min)} min (real "
                             "wall-clock from now, including loads/menus).")
                _laplog.info("MODE TRANSITION detecting -> AUTO (lap timer advancing); "
                             "skip_laps=%d (warm-up=%d)", self._skip_laps, rules.WARMUP_LAPS)
                self.log("AUTO-LAP engaged - lap timer is live. Each completed lap is "
                         "captured automatically.")
            # TIME BUDGET expiry: don't hard-cut; flag it so the in-flight test/A-B-A
            # finishes cleanly, then the loop runs the honest final check and stops.
            if (self._budget_start > 0.0 and not self._budget_expired
                    and time.perf_counter() - self._budget_start >= self.time_budget_min * 60.0):
                self._budget_expired = True
                _laplog.info("TIME BUDGET reached (%.0f min) - finishing the in-flight test, "
                             "then stopping.", self.time_budget_min)
                self.log("Time budget reached - finishing current test.")
            # an event RESTART (race off->on, lap counter reset, or a reload's
            # CurrentLap-reset) re-arms the warm-up discard so lap 1 of the fresh
            # standing start is ignored - never mistaken for the measured lap.
            if self._watcher.pop_restarted() and self.mode == MODE_AUTO:
                self.arm_next_lap()
                self._reset_test()         # discard any partial measurement on this run
                self._restart_count += 1
                if self._await_state is not None:
                    self._await_state = "out_lap"   # back to waiting for a clean lap
                _laplog.info("RESTART/reload detected -> re-arming out-lap (skip_laps=%d, "
                             "await=%s). The reset is NOT counted as a measured lap.",
                             self._skip_laps, self._await_state)
                self.log("Event restart detected - discarding the warm-up lap; the "
                         "next FULL lap is measured.")
            if self.mode != MODE_AUTO:
                return
            # per-tick instrumentation while WAITING_FOR_MEASURED_LAP (deduped)
            if self._await_state is not None and pkts:
                last = pkts[-1]
                dbg = (last.lap_number, round(last.current_lap, 1), self._skip_laps,
                       self._await_state)
                if dbg != self._await_dbg:
                    self._await_dbg = dbg
                    _rawlog.info("WAITING_FOR_MEASURED_LAP[%s] tick: prevLap=%s curLap=%s "
                                 "curCur=%.1f skip_laps=%d completed=%d",
                                 self._await_state, prev_lap, last.lap_number,
                                 last.current_lap, self._skip_laps, len(laps))
            for lap in laps:
                self._on_lap(lap)
                if self.phase != DRIVE_AUTO:
                    break   # a change is now shown; stop consuming further laps
        except Exception as e:
            _laplog.exception("auto-lap tick failed")
            self.fail(f"auto-lap tick: {e}")

    def _on_lap(self, lap: "LapResult"):
        self.lap_number = lap.lap_number
        self.last_lap_s = lap.last_lap_s
        waiting = self._await_state is not None
        # 1) the out-lap on a fresh tune is never measured
        if self._skip_laps > 0:
            self._skip_laps -= 1
            if waiting and self._skip_laps == 0:
                self._await_state = "measuring"
            _laplog.info("WAITING_FOR_MEASURED_LAP[%s]: out-lap [lap %s, %.2fs] skipped; "
                         "skip_laps now %d - drive a clean lap.",
                         self._await_state, lap.lap_number, lap.last_lap_s, self._skip_laps)
            self.log(f"[lap {lap.lap_number}] warm-up/out-lap ignored "
                     f"({lap.last_lap_s:.2f}s) - drive the next full lap.")
            return
        # 2) reject an implausibly short "lap" - a reload/restart counter-reset, not
        #    a measured lap. Re-arm the out-lap; do NOT run the gate.
        if lap.last_lap_s <= LAP_TIME_FLOOR:
            _laplog.info("WAITING_FOR_MEASURED_LAP[%s]: REJECTED lap %s (LastLap %.2fs <= floor "
                         "%.1fs) - reload/glitch, not a measured lap; re-arming the out-lap.",
                         self._await_state, lap.lap_number, lap.last_lap_s, LAP_TIME_FLOOR)
            if waiting:
                self.arm_next_lap()
            return
        # 3) reject the carried-over pre-reload LastLap (not refreshed by a new lap)
        if waiting and self._pre_change_last_lap is not None \
                and abs(lap.last_lap_s - self._pre_change_last_lap) < 1e-3:
            _laplog.info("WAITING_FOR_MEASURED_LAP[%s]: REJECTED lap %s (LastLap %.2fs == "
                         "carried-over pre-reload value) - not a fresh lap; re-arming out-lap.",
                         self._await_state, lap.lap_number, lap.last_lap_s)
            self.arm_next_lap()
            return
        if waiting:
            _laplog.info("WAITING_FOR_MEASURED_LAP[measuring]: ACCEPTED measured lap %s = "
                         "%.2fs - collecting for the fitness gate.", lap.lap_number, lap.last_lap_s)
        self._collect_lap(lap)

    def _target_laps(self) -> int:
        """How many laps to average for THIS test. Adaptive: 1 early, escalate to
        2-3 once gains get small / stale (incl. a final confirmation pass)."""
        if isinstance(self.laps_per_test, int):
            return max(1, min(3, self.laps_per_test))
        it = self.state.iteration if self.state else 0
        if it < 2:
            return 1
        if self.stale >= 1:
            return 3
        if self._last_improvement is not None and abs(self._last_improvement) < ADAPTIVE_SMALL_DELTA:
            return 3
        return 2

    def _collect_lap(self, lap: "LapResult"):
        """Accumulate consecutive full laps; finalize once we have target laps.
        The BEST lap supplies the diagnostic telemetry + Heat reading."""
        t = lap.last_lap_s
        self._test_laps.append(t)
        self._test_packets.extend(lap.packets)   # for track-position telemetry binning
        stats = aggregate(lap.packets)
        self._read_lap_heat()                  # per-lap Heat (resets the capture)
        if self._best_lap_time is None or t < self._best_lap_time:
            self._best_lap_time = t
            self._best_lap_stats = stats
            self._best_lap_reading = self.tyre_reading
        target = self._target_laps()
        if len(self._test_laps) < target:
            self.log(f"[test] lap {len(self._test_laps)}/{target}: {t:.2f}s "
                     f"(best {min(self._test_laps):.2f})")
            return
        self._finalize_test()

    def progress_state(self) -> dict:
        """A plain-language read on whether the session is getting anywhere: best vs
        the session start, how many changes really stuck, and the recent trend."""
        best, start = self.best_segment, self._session_start_best
        delta = (best - start) if (best is not None and start is not None) else None
        recent = self._recent_outcomes[-4:]
        recent_gain = any(o == "gain" for o in recent)
        dry = len(recent) >= 3 and not recent_gain
        if best is None:
            trend = "Getting a baseline"
        elif self.phase == DONE:
            trend = "Done"
        elif self.stale >= 2 or dry:
            trend = "Not finding much - may finish soon"
        elif recent_gain and delta is not None and delta < -0.05:
            trend = "Improving"
        elif self._confirmed_gains > 0:
            trend = "Fine-tuning"
        else:
            trend = "Searching for gains"
        return {"confirmed_gains": self._confirmed_gains, "best_s": best,
                "start_s": start, "delta_vs_start_s": delta, "trend": trend,
                "aba_saved": self._aba_saved}

    def reject_change(self):
        """User rejects the proposed change ([F10]/overlay): DON'T apply it, LOCK those
        levers for the rest of the session (never propose them again), and move on. A
        rejected lever is treated like a locked one, so it can't block convergence."""
        if self.phase != SHOW_CHANGE or not self.batch:
            return
        grp = self.batch[0].group
        if grp in ("confirm_revert", "confirm_reapply", "final_baseline", "reanchor"):
            self.log("[reject] this step is a measurement, not a tuning change - nothing to reject.")
            return
        fields = []
        for rec in self.batch:
            for fld in rec.fields:
                self._rejected_fields.add(fld)
                fields.append(fld)
        labels = ", ".join(field_label(f) for f in fields)
        _laplog.info("REJECT: user rejected %s -> fields %s LOCKED for the session.",
                     [(r.group, r.fields) for r in self.batch], fields)
        self.log(f"[reject] Skipped {labels} - won't suggest that again this session.")
        self._record_outcome("reject")
        self.batch = []
        self._applied_records = []
        self._compute_batch()
        if self.phase == DONE and self._telemetry_mode and not self._final_check_done:
            self._begin_final_check(reason="converged")

    def effective_drivetrain(self) -> str:
        """Drivetrain the rules should tune for: the manual override if set, else the
        telemetry-detected one. Source is logged so a misdetect is obvious in the log."""
        if self.drivetrain_override in ("FWD", "RWD", "AWD"):
            return self.drivetrain_override
        return self.identity.drivetrain if self.identity else "AWD"

    TEMP_BLIND_NOTICE = ("No tyre-temp reading on a tarmac run - camber/toe are being "
                         "tuned BLIND (by lap time only) and will be unreliable. Bring up "
                         "the in-game Heat/tyre-temp page on a hard cornering lap so it "
                         "can read the 3-zone temps.")

    def temp_blind(self) -> bool:
        """True when tyre temps MATTER (tarmac) but no temp source is producing a
        reading - so camber/toe are being tuned blind. Console mode has its own
        single-temp notice; dirt/CC camber is lap-time-tuned by design."""
        if self.console_mode or not self.discipline:
            return False
        disc = self.discipline.lower()
        if "dirt" in disc or "cross" in disc or disc in ("cc", "drag"):
            return False
        return (self.tyre_reading is None
                and self.last_reader in (None, "", "none", "camber_search"))

    def _warn_temp_blind_once(self):
        """Log the no-temp-reader warning prominently ONCE per session (after we're
        actually driving), so it's plain in the decision log, not buried."""
        if self._temp_warned or not self.temp_blind() or self.best_segment is None:
            return
        self._temp_warned = True
        _laplog.warning("NO TEMP READER: tarmac run with no tyre-temp reading "
                        "(reader=%s) - camber/toe are being tuned BLIND by lap time. %s",
                        self.last_reader, self.TEMP_BLIND_NOTICE)
        self.log("[temps] " + self.TEMP_BLIND_NOTICE)

    CONSOLE_NOTICE = ("Console mode: only ONE tyre temperature per corner is available "
                      "(no in-game Heat screen to read the 3-zone inner/middle/outer "
                      "temps). Camber and toe suggestions are LESS ACCURATE and tuned by "
                      "lap time instead. Everything else works normally.")

    def lan_ip(self) -> str:
        """Best-guess LAN IP of this PC (what to enter as the Data Out target on the
        console). No packets are sent - the connect() just picks the outbound NIC."""
        import socket as _s
        try:
            sk = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
            try:
                sk.connect(("8.8.8.8", 80))     # no traffic; selects the default route
                return sk.getsockname()[0]
            finally:
                sk.close()
        except OSError:
            try:
                return _s.gethostbyname(_s.gethostname())
            except OSError:
                return "127.0.0.1"

    def budget_remaining_s(self) -> Optional[float]:
        """Seconds left on the wall-clock budget (None if unlimited or not started)."""
        if self.time_budget_min <= 0 or self._budget_start == 0.0:
            return None
        return max(0.0, self.time_budget_min * 60.0 - (time.perf_counter() - self._budget_start))

    def set_time_budget(self, minutes: float):
        """Change the wall-clock budget (main-window control). 0 = unlimited. If a run
        is already on the clock, the new limit applies to the running budget."""
        self.time_budget_min = max(0.0, float(minutes or 0.0))
        if self.time_budget_min > 0 and self._budget_start > 0.0:
            self._check_budget()        # a tighter limit may already be exceeded
        elif self.time_budget_min == 0:
            self._budget_expired = False
        _laplog.info("TIME BUDGET set to %s.",
                     "unlimited" if self.time_budget_min == 0 else f"{self.time_budget_min:.0f} min")

    # ---- exact "change these" checklists + unmistakable overlay state ---------
    def _target_tune_dict(self) -> dict:
        """What the car SHOULD read after the user performs the currently shown action.
        For the final check it is the ORIGINAL baseline; otherwise it is the current
        accepted tune with the shown batch (not yet applied) overlaid."""
        if not self.state:
            return {}
        if self.batch and self.batch[0].group == "final_baseline":
            return self.baseline.as_dict() if self.baseline else {}
        target = self.state.current.as_dict()
        for rec in (self.batch or []):
            target.update(rec.fields)
        return target

    def menu_checklist(self) -> list:
        """The EXACT fields to change in the tune menu right now: only those that differ
        between what's physically on the car (_on_car) and the target tune, each as a
        from->to with its menu label. Covers both reverting the last change and the new
        one in a single list, so the user never has to guess the old value."""
        target = self._target_tune_dict()
        out = []
        for fld, to in target.items():
            frm = self._on_car.get(fld, to)
            try:
                differs = abs(float(frm) - float(to)) > 1e-6
            except (TypeError, ValueError):
                differs = frm != to
            if differs:
                out.append({"field": fld, "label": field_label(fld),
                            "from": fmt_field(fld, frm), "to": fmt_field(fld, to)})
        return out

    def ui_state(self) -> dict:
        """Classify the overlay into ACTION (edit the menu now) vs DRIVE (touch nothing)
        vs DONE, with an unambiguous header and - for ACTION - the exact checklist, so a
        timed lap can never be confused with a go-to-the-menu prompt."""
        ph = self.phase
        if ph == DONE:
            budget = self.stop_reason.startswith("stopped")
            return {"klass": "done",
                    "header": "TIME BUDGET REACHED" if budget else "DONE - converged",
                    "sub": self.final_verdict or self.stop_reason or "converged",
                    "checklist": []}
        if ph == APPLY_BASELINE:
            # the FULL baseline is rendered by the separate baseline-checklist block;
            # no delta checklist here (the car isn't on a known tune yet).
            return {"klass": "action", "action": "baseline",
                    "header": "CHANGE THESE NOW - enter the baseline tune (full list below)",
                    "sub": "Enter every value below, then press F8 when applied.",
                    "checklist": []}
        if ph == SHOW_CHANGE:
            grp = self.batch[0].group if self.batch else ""
            cl = self.menu_checklist()
            if grp == "reanchor":
                return {"klass": "drive", "header": "RE-ANCHOR - drive current tune, no changes",
                        "sub": "No change to enter - drive a clean lap, press F8 when at the line.",
                        "checklist": []}
            if grp == "final_baseline" and not cl:
                return {"klass": "drive", "header": "Car is already on the baseline - just drive",
                        "sub": "Everything was reverted. Drive one clean lap for the honest "
                               "final check, then press F8.", "checklist": []}
            headers = {
                "final_baseline": "CHANGE THESE NOW - set the car back to the baseline",
                "confirm_revert": "DOUBLE-CHECK - re-drive the PREVIOUS setup",
                "confirm_reapply": "CONFIRMED REAL - re-apply the change",
            }
            subs = {
                "confirm_revert": "Double-checking that last change was really the tune, not "
                                  "you driving better - enter the PREVIOUS values, then F8.",
                "confirm_reapply": "It beat the re-driven baseline - a real gain. Re-enter "
                                   "these values, then F8.",
                "final_baseline": "Re-enter the original baseline values, then F8.",
            }
            # one-line, telemetry-tied WHY per proposed change (e.g. "On-power oversteer:
            # rear slip ratio 0.45 under throttle (> 0.3)").
            normal = grp not in ("final_baseline", "confirm_revert", "confirm_reapply")
            why = [r.reason for r in self.batch if getattr(r, "reason", "")] if normal else []
            return {"klass": "action", "action": grp or "change",
                    "header": headers.get(grp, "CHANGE THESE NOW"),
                    "sub": subs.get(grp, "Set each value below, then press F8 when applied."),
                    "why": why if normal else [],
                    "can_reject": normal,
                    "checklist": cl}
        if ph == DRIVE_AUTO:
            if self.mode is None:
                return {"klass": "drive", "header": "DETECTING - drive a lap",
                        "sub": "Auto-lap engages when the lap timer advances.", "checklist": []}
            skip = self._skip_laps
            if self._aba is not None:
                hdr = ("DOUBLE-CHECK out-lap - get back to the line" if skip > 0
                       else "DOUBLE-CHECKING - re-drive the PREVIOUS setup (was it the tune, not you?)")
            elif self._reanchor_pending:
                hdr = ("RE-ANCHOR out-lap - get back to the line" if skip > 0
                       else "RE-ANCHOR - drive the current tune (no changes)")
            elif self._final_check:
                hdr = ("FINAL CHECK out-lap - get back to the line" if skip > 0
                       else "FINAL CHECK - drive the baseline clean")
            elif self.best_segment is None and not self._awaiting_test:
                hdr = ("WARM-UP - just drive (not counted)" if skip > 0
                       else "BASELINE - drive a clean lap")
            elif skip > 0:
                hdr = "OUT-LAP - not counted, get back to the line"
            else:
                done = len(self._test_laps)
                tgt = max(1, self._target_laps())
                hdr = f"MEASURING - lap {min(done + 1, tgt)}/{tgt}, drive clean"
            return {"klass": "drive", "header": hdr,
                    "sub": "Do NOT touch the tune menu.", "checklist": []}
        return {"klass": "info", "header": "", "sub": "", "checklist": []}

    @property
    def _telemetry_mode(self) -> bool:
        """Telemetry-primary fitness is engaged only when the baseline anchor had
        live channels. Otherwise we degrade to the lap-time gate everywhere (and
        skip A/B/A, re-anchor and the final check)."""
        return self._baseline_telem is not None and getattr(self._baseline_telem, "live", False)

    def _finalize_test(self):
        laps = self._test_laps
        test_time = min(laps) if self.lap_agg != "median" else _median(laps)
        noise = (max(laps) - min(laps)) if len(laps) >= 2 else 0.0
        self.stats = self._best_lap_stats if self._best_lap_stats is not None else self.stats
        self.tyre_reading = self._best_lap_reading
        if not self._channels_logged and self.stats is not None:
            self._channels_logged = True
            ch = self.stats.channels_available()
            live = [k for k, v in ch.items() if v] or ["lap-time only"]
            _laplog.info("CHANNELS LIVE this session: %s (missing: %s).",
                         ", ".join(live), ", ".join(k for k, v in ch.items() if not v) or "none")
            self.log("[telemetry] richer channels live: " + ", ".join(live))
        cand = fitness.bin_lap(self._test_packets)
        self._consume_ride_progress()
        _laplog.info("measurement: laps=%s -> %s %.2fs noise %.2fs telem(live=%s src=%s n=%d)",
                     [round(x, 2) for x in laps], self.lap_agg, test_time, noise,
                     cand.live, cand.pos_src, cand.n_frames)
        self._reset_test()
        was_awaiting = self._awaiting_test
        self._awaiting_test = False
        self._await_state = None
        self._pre_change_last_lap = None

        if self._aba is not None:                       # this measurement is A' (re-test of A)
            self._resolve_aba(test_time, cand)
            return
        if self._final_check:                           # honest final re-measure of baseline (E)
            self._resolve_final_check(test_time, cand)
            return
        if self._reanchor_pending:                      # re-measure of the accepted tune (C)
            self._reanchor_pending = False
            self._applied_records = []
            if cand.live:
                self._ref_telem = cand
            if self.best_segment is None or test_time < self.best_segment:
                self.best_segment = test_time
            self._iters_since_reanchor = 0
            _laplog.info("RE-ANCHOR: accepted tune re-measured at %.2fs (judging later "
                         "changes against current driver pace).", test_time)
            self.log(f"[re-anchor] baseline re-measured at {test_time:.2f}s "
                     "(now comparing against your current pace).")
            self._next_step()
            return
        if not was_awaiting and self.best_segment is None:    # the first baseline anchor (after warm-up)
            self.best_segment = test_time
            self._baseline_lap_s = test_time
            self._session_start_best = test_time          # progress baseline (best-vs-start)
            self._ref_telem = cand
            self._baseline_telem = cand
            self.log(f"[baseline] reference {test_time:.2f}s set "
                     f"(telemetry-primary fitness {'ON' if cand.live else 'unavailable - lap-time only'}).")
            self._next_step()
            return
        # a measured change B
        if self._telemetry_mode and cand.live and self._ref_telem is not None \
                and getattr(self._ref_telem, "live", False):
            self._gate_change(test_time, noise, cand)
        else:
            _laplog.info("WAITING_FOR_MEASURED_LAP -> GATE: measured %.2fs vs best %s; "
                         "lap-time fitness (telemetry not live).", test_time,
                         f"{self.best_segment:.2f}" if self.best_segment else "n/a")
            self._apply_fitness_multi(test_time, noise)
            self._check_budget()
            if self._budget_expired and not self._final_check_done:
                self._begin_final_check(reason="budget")
            else:
                self._compute_batch()

    def _check_budget(self):
        """Latch budget expiry (belt-and-suspenders with tick: the deadline can pass
        DURING a measurement, with no tick in between)."""
        if (self._budget_start > 0.0 and not self._budget_expired and self.time_budget_min > 0
                and time.perf_counter() - self._budget_start >= self.time_budget_min * 60.0):
            self._budget_expired = True
            _laplog.info("TIME BUDGET reached (%.0f min) at a decision point - stopping after "
                         "the honest final check.", self.time_budget_min)

    def _next_step(self):
        """After a measurement decision: honour the time budget, schedule a periodic
        re-anchor, otherwise compute the next change. On convergence, run the honest
        final check before declaring a result."""
        self._check_budget()
        if self._budget_expired and not self._final_check_done:
            self._begin_final_check(reason="budget")
            return
        # Compute the next change FIRST: genuine convergence must win over a pending
        # re-anchor. The budget is a CEILING, not a target - if nothing is left to try
        # we stop and save immediately, even with time on the clock.
        self._compute_batch()
        if self.phase == DONE:
            if self._telemetry_mode and not self._final_check_done:
                self._begin_final_check(reason="converged")
            # else: already converged/degraded -> stop; the app saves on DONE.
            return
        # not converged: a periodic re-anchor may pre-empt the computed change (it is
        # recomputed after the re-anchor measurement, so nothing is lost).
        if self._telemetry_mode and self.best_segment is not None and not self._reanchor_pending:
            self._iters_since_reanchor += 1
            if self._iters_since_reanchor >= rules.BASELINE_REANCHOR_EVERY:
                self._begin_reanchor()

    # --- shared keep / revert mechanics (used by both gates) -------------------
    def _count_and_lock(self, keys, delta):
        """Per-lever no-improvement counting + cap-lock + roll-back to last improving."""
        for key, fld, _prev in keys:
            self._noimprove[key] = self._noimprove.get(key, 0) + 1
            _laplog.info("ANTI-FIXATION: lever '%s' no-improvement %d/%d (delta %+.2fs).",
                         key, self._noimprove[key], rules.LEVER_NOIMPROVE_CAP, delta)
            if self._noimprove[key] >= rules.LEVER_NOIMPROVE_CAP \
                    and key not in self._lever_locked:
                self._lever_locked.add(key)
                roll = self._last_improving.get(fld, self.state.current.get(fld))
                self.state.current.set(fld, roll)
                _laplog.info("ANTI-FIXATION: lever '%s' hit the no-improve cap (%d) - "
                             "LOCKED and ROLLED BACK %s to its last improving value %.2f.",
                             key, rules.LEVER_NOIMPROVE_CAP, fld, roll)
                self.log(f"[anti-fixation] {fld} ({key.split(':')[1]}) made no gain in "
                         f"{rules.LEVER_NOIMPROVE_CAP} tries - locked at {roll:.2f}; moving on.")

    def _lock_bottoming_if_no_improve(self, improved, delta):
        """A bottoming change that didn't clearly improve locks that axle's whole
        remedy (ride->bump->spring escalation)."""
        if not improved:
            for axle in self._cur_bottoming_axles:
                if axle not in self._bottoming_locked:
                    self._bottoming_locked.add(axle)
                    _laplog.info("ANTI-FIXATION: %s bottoming change did not improve "
                                 "(%+.2fs) - GATE locking the axle's whole remedy.", axle, delta)
                    self.log(f"[anti-fixation] {axle} bottoming change didn't help "
                             f"({delta:+.2f}s) - locked; moving to other levers.")
        self._cur_bottoming_axles = set()

    def _record_outcome(self, kind: str):
        """Track recent keep/revert/discount/reject outcomes for the progress state."""
        if kind == "gain":
            self._confirmed_gains += 1
        self._recent_outcomes.append(kind)
        self._recent_outcomes = self._recent_outcomes[-6:]

    def _keep_batch(self, test_time, cand=None):
        """Bank the applied batch as a real gain."""
        improved = self.best_segment is None or test_time < self.best_segment - 1e-9
        for r in self._applied_records:
            r.verdict = "kept"
        self.best_segment = test_time if self.best_segment is None else min(self.best_segment, test_time)
        self.stale = 0
        if cand is not None and getattr(cand, "live", False):
            self._ref_telem = cand
        for key, fld, _prev in self._batch_lever_keys():
            self._noimprove[key] = 0
            self._last_improving[fld] = self.state.current.get(fld)
        if improved:                       # snapshot the BEST CONFIRMED tune for saving
            self._best_tune = self.state.current.copy()
            self._best_tune_lap = self.best_segment
        self._record_outcome("gain" if improved else "flat")

    def _revert_batch(self):
        for r in reversed(self._applied_records):
            for k, v in r.previous.items():
                self.state.current.set(k, v)
            r.verdict = "reverted"

    # --- telemetry-primary gate (H) + A/B/A (B) --------------------------------
    def _gate_change(self, test_time, noise, cand):
        """Judge a measured change PRIMARILY on the binned-telemetry composite, with
        lap time as a guardrail. Apparent wins go through A/B/A (confirmed rigour) to
        separate a real tune gain from the driver simply improving over the session."""
        ref = self._ref_telem
        group = self._applied_records[0].lever_group if self._applied_records else ""
        comp = fitness.composite(cand, ref, self.discipline, group=group)
        lap_delta = test_time - self.best_segment if self.best_segment else 0.0
        keys = self._batch_lever_keys()
        for r in self._applied_records:
            r.seg_before_s = self.best_segment
            r.seg_after_s = test_time
        _laplog.info("FITNESS(telemetry): composite %+.3f [grip %+.3f exit %+.3f trac %+.3f "
                     "minspd %+.2f cleanspin %+.3f roll %+.3f ride %+.3f targeted(%s) %+.3f] "
                     "| lap %+.2fs | rigour=%s",
                     comp.delta, comp.grip, comp.exit, comp.traction, comp.minspeed,
                     comp.cleanspin, comp.bodyroll, comp.ride,
                     fitness.targeted_channel(group) or "-", comp.targeted, lap_delta, self.rigour)
        apparent_win = comp.delta > fitness.COMPOSITE_IMPROVE_EPS

        if apparent_win and lap_delta > fitness.LAPTIME_GUARDRAIL_S:
            # anti-Goodhart: composite up but lap time clearly worse -> distrust it
            _laplog.info("GUARDRAIL: composite improved but lap %+.2fs clearly worse "
                         "(> %.2fs) - composite/lap-time DISAGREE; not keeping.",
                         lap_delta, fitness.LAPTIME_GUARDRAIL_S)
            self.log(f"[fitness] composite better but lap {lap_delta:+.2f}s worse - distrusted; reverted.")
            self._revert_batch()
            self.stale += 1
            self._count_and_lock(keys, lap_delta)
            self._lock_bottoming_if_no_improve(False, lap_delta)
            self._record_outcome("revert")
            self._applied_records = []
            self.state.iteration += 1
            self._next_step()
            return

        if apparent_win:
            # DRIVER-INPUT DISCOUNTING: if the human drove notably differently this lap
            # (throttle/brake/steering changed), the apparent gain is likely the DRIVER,
            # not the tune - so DISCOUNT it WITHOUT a full A/B/A re-test. Only fall back
            # to A/B/A when the inputs look the SAME but the result moved (inconclusive).
            in_delta = fitness.input_difference(cand, ref)
            if 0.0 <= in_delta and in_delta > fitness.INPUT_DRIVER_THRESH:
                self._aba_saved += 1
                _laplog.info("INPUT-DISCOUNT: apparent gain but driver inputs differ a lot "
                             "(%.3f > %.3f) - crediting the DRIVER, not the tune; A/B/A skipped.",
                             in_delta, fitness.INPUT_DRIVER_THRESH)
                self.log("[fitness] that looked faster, but your inputs changed a lot this lap - "
                         "scoring it as YOU driving differently, not the tune (saved a re-test lap).")
                self._revert_batch()
                self.stale += 1
                self._count_and_lock(keys, lap_delta)
                self._lock_bottoming_if_no_improve(False, lap_delta)
                self._record_outcome("discount")
                self._applied_records = []
                self.state.iteration += 1
                self._next_step()
                return
            if self.rigour == "confirmed":
                if in_delta >= 0.0:
                    _laplog.info("INPUT-CHECK: inputs similar (%.3f <= %.3f) but the result "
                                 "moved - INCONCLUSIVE; running A/B/A to be sure.",
                                 in_delta, fitness.INPUT_DRIVER_THRESH)
                self._start_aba(test_time, cand, comp)   # drives its own A' measurement
                return
            # quick rigour: single-pass accept
            self._keep_batch(test_time, cand)
            self.log(f"[fitness] change KEPT - composite {comp.delta:+.3f} (telemetry-confirmed gain).")
            self._lock_bottoming_if_no_improve(True, lap_delta)
            self._applied_records = []
            self.state.iteration += 1
            self._next_step()
            return

        # not an apparent win -> don't bank driver drift; revert and count the lever(s)
        self._revert_batch()
        self.stale += 1
        self.log(f"[fitness] change NOT banked - composite {comp.delta:+.3f} <= "
                 f"{fitness.COMPOSITE_IMPROVE_EPS:.3f} (no telemetry gain); reverted.")
        self._count_and_lock(keys, lap_delta)
        self._lock_bottoming_if_no_improve(False, lap_delta)
        self._record_outcome("revert")
        self._applied_records = []
        self.state.iteration += 1
        self._next_step()

    def _start_aba(self, b_time, b_telem, comp):
        """Apparent win: revert to A and re-measure (A'), then compare B vs the NEWER
        A'. The user re-enters the PREVIOUS values for the confirmation lap."""
        a_records = list(self._applied_records)        # .previous = A values, .fields = B
        self._aba = {"b_time": b_time, "b_telem": b_telem, "comp": comp.delta,
                     "records": a_records}
        prev = {}
        for r in a_records:
            prev.update(r.previous)
        rec = rules.Recommendation(
            "confirm_revert", dict(prev),
            "That looked faster - but session-long driver improvement can fake a gain. "
            "Revert to the PREVIOUS values and drive a confirmation measurement.",
            "Confirming the gain is from the TUNE, not you driving better.")
        rec.detail = "A/B/A confirmation: enter the previous values and drive a clean test."
        self.batch = [rec]
        self._applied_records = []
        self.phase = SHOW_CHANGE
        _laplog.info("A/B/A: apparent win (composite %+.3f) - reverting to A to re-measure (A').",
                     comp.delta)
        self.log("[A/B/A] Looked faster - revert to the previous tune and drive a confirmation "
                 "test (separating a real tune gain from driver improvement).")

    def _resolve_aba(self, aprime_time, aprime_telem):
        aba, self._aba = self._aba, None
        self._applied_records = []
        comp = fitness.composite(aba["b_telem"], aprime_telem, self.discipline)
        _laplog.info("A/B/A RESOLVE: B(time %.2f) vs A'(re-measured %.2f); composite(B vs A')=%+.3f.",
                     aba["b_time"], aprime_time, comp.delta)
        if comp.live and comp.delta > fitness.COMPOSITE_IMPROVE_EPS:
            # B still beats the NEWER baseline -> real gain; re-apply B
            b_fields = {}
            for r in aba["records"]:
                b_fields.update(r.fields)
            self._aba_keep = {"b_telem": aba["b_telem"], "b_time": aba["b_time"]}
            # COMMIT B to the best-confirmed tune NOW (it beat the re-measured baseline),
            # so even if the session ends before the user re-applies it, the confirmed
            # gain isn't lost (fixes "exited on baseline despite a faster tune").
            bt = self.state.current.copy()
            for fld, val in b_fields.items():
                bt.set(fld, val)
            self._best_tune = bt
            self._best_tune_lap = aba["b_time"]
            if self.best_segment is None or aba["b_time"] < self.best_segment:
                self.best_segment = aba["b_time"]
            if self.persist:
                self.save_progress("in_progress")     # persist the confirmed gain immediately
            rec = rules.Recommendation(
                "confirm_reapply", dict(b_fields),
                "A/B/A CONFIRMED: the change still beats the re-measured baseline - a real "
                "tune gain. Re-enter these values to keep it.", "Keeping the confirmed gain.")
            rec.detail = "Re-enter the confirmed-faster values and continue."
            self.batch = [rec]
            self.phase = SHOW_CHANGE
            _laplog.info("A/B/A: CONFIRMED real gain (composite %+.3f vs A') - re-applying B.", comp.delta)
            self.log("[A/B/A] CONFIRMED a real tune gain - re-apply the change and continue.")
        else:
            # the apparent gain was driver drift -> keep A, re-anchor to the newer A'
            if aprime_telem.live:
                self._ref_telem = aprime_telem
            if self.best_segment is None or aprime_time < self.best_segment:
                self.best_segment = aprime_time
            self._iters_since_reanchor = 0
            _laplog.info("A/B/A: DISCARDED (composite %+.3f vs A' <= eps) - gain was DRIVER DRIFT; "
                         "kept A and re-anchored to A' %.2f.", comp.delta, aprime_time)
            self.log("[A/B/A] The gain was driver improvement, not the tune - DISCARDED; kept the "
                     "previous tune and re-anchored to your current pace.")
            self._record_outcome("discount")
            self._next_step()

    def _begin_reanchor(self):
        """Re-drive the CURRENT accepted tune to re-baseline against current pace.
        The user clicks Applied (no values change) -> change_applied arms the test."""
        self._reanchor_pending = True
        rec = rules.Recommendation(
            "reanchor", {}, "Re-anchor: drive the CURRENT tune again so later changes are "
            "judged against your current pace (no change to enter).", "")
        rec.detail = "Just drive a clean measurement on the current tune."
        self.batch = [rec]
        self.phase = SHOW_CHANGE
        _laplog.info("RE-ANCHOR: scheduling a re-measure of the accepted tune (every %d iters).",
                     rules.BASELINE_REANCHOR_EVERY)
        self.log("[re-anchor] drive the CURRENT tune again to re-baseline against your pace.")

    def _begin_final_check(self, reason):
        """Honest final check (E): re-measure the ORIGINAL baseline tune once more so
        we can tell a real tune gain from the driver simply getting faster. The user
        re-enters the baseline values, clicks Applied -> change_applied arms the test."""
        self._final_check = True
        self._final_check_done = True
        self.stop_reason = (f"stopped: time budget ({int(self.time_budget_min)} min)"
                            if reason == "budget" else "converged")
        # Instruction-only (no fields): the user re-enters their ORIGINAL baseline by
        # hand. We must NOT revert state.current here - it holds the optimised tune
        # that finish() saves; the final check only compares the MEASURED telemetry.
        rec = rules.Recommendation(
            "final_baseline", {},
            "Final honest check: re-enter your ORIGINAL baseline tune and drive one more "
            "measurement. If the baseline is now as fast, the session gain was driver "
            "improvement - we will say so rather than claim a tune win.", "")
        rec.detail = "Re-enter the original baseline values and drive a clean measurement."
        self.batch = [rec]
        self.phase = SHOW_CHANGE
        _laplog.info("FINAL CHECK (%s): re-measuring the ORIGINAL baseline tune for an honest verdict.",
                     reason)
        self.log("[final check] re-enter the ORIGINAL baseline tune and drive once more - "
                 "separating real tune gains from driver improvement.")

    def _resolve_final_check(self, base_time, base_telem):
        self._final_check = False
        self._applied_records = []
        opt = self._ref_telem
        comp = (fitness.composite(opt, base_telem, self.discipline)
                if (opt is not None and getattr(opt, "live", False) and base_telem.live) else None)
        best = self.best_segment if self.best_segment is not None else base_time
        lap_gain = base_time - best                    # how much the optimised tune still wins on time
        if comp is not None and comp.live:
            confirmed = comp.delta > fitness.COMPOSITE_IMPROVE_EPS
        else:
            confirmed = lap_gain > rules.LAP_IMPROVE_EPS
        if confirmed:
            self.final_verdict = (
                f"Confirmed tune improvement: composite "
                f"{(comp.delta if comp else 0.0):+.3f}, lap {lap_gain:+.2f}s vs a re-measured "
                f"baseline ({base_time:.2f}s).")
            _laplog.info("FINAL CHECK: CONFIRMED tune gain - composite %+.3f, lap delta %+.2fs "
                         "(baseline re-measured %.2fs).", (comp.delta if comp else 0.0), lap_gain, base_time)
        else:
            self.final_verdict = (
                "Net improvement within driver variation - changes NOT confirmed. The "
                f"re-measured baseline ({base_time:.2f}s) is as fast as the 'optimised' tune; "
                "the session's lap-time gain was mostly you driving better, not the tune.")
            _laplog.info("FINAL CHECK: NOT CONFIRMED - re-measured baseline %.2fs matches/beats the "
                         "optimised tune (composite %+.3f); gain was driver improvement.",
                         base_time, (comp.delta if comp else 0.0))
        self.log(f"[final check] {self.final_verdict}")
        self.phase = DONE

    def _apply_fitness_multi(self, test_time: float, noise: float):
        """Lap-time keep/revert gate for a MULTI-LAP test (the DEGRADED path when
        telemetry channels aren't live). Three outcomes:
          IMPROVED (beat best by > LAP_IMPROVE_EPS, above noise) -> KEEP; reset the
            levers' no-improvement counters; remember their improving values.
          REGRESSED (> the noise-aware regress gate) -> REVERT + lock the group AND
            the lever-keys.
          NEUTRAL (in between) -> REVERT (do NOT bank drift) + bump the no-improvement
            counter; at LEVER_NOIMPROVE_CAP, lock the lever and roll back to its last
            improving value (so a slip-chasing diff settles, not drifts to the floor).
        A locked lever no longer fires, so the loop can actually converge."""
        best = self.best_segment
        if best is None:
            self.best_segment = test_time
            self._baseline_lap_s = test_time
            self.state.iteration += 1
            return
        delta = test_time - best
        gate = max(rules.SEGMENT_REGRESS_S, noise)   # regress threshold is noise-aware
        improve_eps = rules.LAP_IMPROVE_EPS          # fixed bar (best-of-N already de-noises)
        has_evidence = any(rules._kind_of(r.lever_group) == "evidence"
                           for r in self._applied_records)
        revert_gate = gate + (ADAPTIVE_EVIDENCE_MARGIN if has_evidence else 0.0)
        n = len(self._applied_records)
        keys = self._batch_lever_keys()
        for r in self._applied_records:           # audit trail: record the after-time
            r.seg_before_s = best
            r.seg_after_s = test_time

        if delta < -improve_eps:
            # IMPROVED - bank it; these levers are making real progress.
            self._keep_batch(test_time)
            self.log(f"[fitness] batch ({n}) improved {delta:+.2f}s (> {improve_eps:.2f}) - kept.")
        elif delta > revert_gate:
            # REGRESSED - revert and lock (group + each lever-key).
            self._revert_batch()
            for r in self._applied_records:
                self.state.mark_converged(r.lever_group)
            for key, _fld, _prev in keys:
                self._lever_locked.add(key)
            self.stale += 1
            _laplog.info("ANTI-FIXATION: batch regressed %+.2fs (> gate %.2f) - REVERTED and "
                         "LOCKED levers %s.", delta, revert_gate, sorted({k for k, _, _ in keys}))
            self.log(f"[fitness] BATCH ({n}) regressed {delta:+.2f}s > gate {revert_gate:.2f}"
                     f"{' (+evidence margin)' if has_evidence else ''} - reverted & locked.")
        else:
            # NEUTRAL - do NOT bank drift: revert, and count it against each lever.
            self._revert_batch()
            self.stale += 1
            self.log(f"[fitness] batch ({n}) {delta:+.2f}s NEUTRAL (no gain > {improve_eps:.2f}) "
                     "- reverted (not banked); counting against the lever(s).")
            self._count_and_lock(keys, delta)
        self._lock_bottoming_if_no_improve(delta < -improve_eps, delta)
        self._last_improvement = best - test_time
        self._applied_records = []
        self.state.iteration += 1

    def _reset_test(self):
        self._test_laps = []
        self._test_packets = []
        self._best_lap_time = None
        self._best_lap_stats = None
        self._best_lap_reading = None

    def _batch_lever_keys(self):
        """(lever_key, field, previous_value) for every field in the applied batch."""
        out = []
        for r in self._applied_records:
            for fld, new in r.fields.items():
                out.append((rules.lever_key(fld, r.previous.get(fld, new), new),
                            fld, r.previous.get(fld, new)))
        return out

    def _apply_fitness_multi(self, test_time: float, noise: float):
        """Keep/revert the batch from a MULTI-LAP test. Three outcomes (the lap-time
        gate is the final authority):
          IMPROVED (beat best by > LAP_IMPROVE_EPS, above noise) -> KEEP; reset the
            levers' no-improvement counters; remember their improving values.
          REGRESSED (> the noise-aware regress gate) -> REVERT + lock the group AND
            the lever-keys.
          NEUTRAL (in between) -> REVERT (do NOT bank drift) + bump the no-improvement
            counter; at LEVER_NOIMPROVE_CAP, lock the lever and roll back to its last
            improving value (so a slip-chasing diff settles, not drifts to the floor).
        A locked lever no longer fires, so the loop can actually converge."""
        best = self.best_segment
        if best is None:
            self.best_segment = test_time
            self._baseline_lap_s = test_time
            self.state.iteration += 1
            return
        delta = test_time - best
        gate = max(rules.SEGMENT_REGRESS_S, noise)   # regress threshold is noise-aware
        improve_eps = rules.LAP_IMPROVE_EPS          # fixed bar (best-of-N already de-noises)
        has_evidence = any(rules._kind_of(r.lever_group) == "evidence"
                           for r in self._applied_records)
        revert_gate = gate + (ADAPTIVE_EVIDENCE_MARGIN if has_evidence else 0.0)
        n = len(self._applied_records)
        keys = self._batch_lever_keys()
        for r in self._applied_records:           # audit trail: record the after-time
            r.seg_before_s = best
            r.seg_after_s = test_time

        if delta < -improve_eps:
            # IMPROVED - bank it; these levers are making real progress.
            for r in self._applied_records:
                r.verdict = "kept"
            self.best_segment = test_time
            self.stale = 0
            for key, fld, _prev in keys:
                self._noimprove[key] = 0
                self._last_improving[fld] = self.state.current.get(fld)
            self.log(f"[fitness] batch ({n}) improved {delta:+.2f}s (> {improve_eps:.2f}) - kept.")
        elif delta > revert_gate:
            # REGRESSED - revert and lock (group + each lever-key).
            for r in reversed(self._applied_records):
                for k, v in r.previous.items():
                    self.state.current.set(k, v)
                r.verdict = "reverted"
                self.state.mark_converged(r.lever_group)
            for key, _fld, _prev in keys:
                self._lever_locked.add(key)
            self.stale += 1
            _laplog.info("ANTI-FIXATION: batch regressed %+.2fs (> gate %.2f) - REVERTED and "
                         "LOCKED levers %s.", delta, revert_gate, sorted({k for k, _, _ in keys}))
            self.log(f"[fitness] BATCH ({n}) regressed {delta:+.2f}s > gate {revert_gate:.2f}"
                     f"{' (+evidence margin)' if has_evidence else ''} - reverted & locked.")
        else:
            # NEUTRAL - do NOT bank drift: revert, and count it against each lever.
            for r in reversed(self._applied_records):
                for k, v in r.previous.items():
                    self.state.current.set(k, v)
                r.verdict = "reverted"
            self.stale += 1
            self.log(f"[fitness] batch ({n}) {delta:+.2f}s NEUTRAL (no gain > {improve_eps:.2f}) "
                     "- reverted (not banked); counting against the lever(s).")
            for key, fld, _prev in keys:
                self._noimprove[key] = self._noimprove.get(key, 0) + 1
                _laplog.info("ANTI-FIXATION: lever '%s' no-improvement %d/%d (delta %+.2fs).",
                             key, self._noimprove[key], rules.LEVER_NOIMPROVE_CAP, delta)
                if self._noimprove[key] >= rules.LEVER_NOIMPROVE_CAP \
                        and key not in self._lever_locked:
                    self._lever_locked.add(key)
                    roll = self._last_improving.get(fld, self.state.current.get(fld))
                    self.state.current.set(fld, roll)
                    _laplog.info("ANTI-FIXATION: lever '%s' hit the no-improve cap (%d) - "
                                 "LOCKED and ROLLED BACK %s to its last improving value %.2f.",
                                 key, rules.LEVER_NOIMPROVE_CAP, fld, roll)
                    self.log(f"[anti-fixation] {fld} ({key.split(':')[1]}) made no gain in "
                             f"{rules.LEVER_NOIMPROVE_CAP} tries - locked at {roll:.2f}; moving on.")
        # bottoming symptom cap (special case): a bottoming change that didn't clearly
        # improve also locks that axle's WHOLE remedy (ride->bump->spring escalation).
        if not (delta < -improve_eps):
            for axle in self._cur_bottoming_axles:
                if axle not in self._bottoming_locked:
                    self._bottoming_locked.add(axle)
                    _laplog.info("ANTI-FIXATION: %s bottoming change did not improve "
                                 "(%+.2fs) - GATE locking the axle's whole remedy.", axle, delta)
                    self.log(f"[anti-fixation] {axle} bottoming change didn't help "
                             f"({delta:+.2f}s) - locked; moving to other levers.")
        self._cur_bottoming_axles = set()
        self._last_improvement = best - test_time
        self._applied_records = []
        self.state.iteration += 1

    def _read_lap_heat(self):
        # Console mode: no in-game Heat screen to screenshot/OCR. Skip it entirely and
        # use the single per-corner UDP TireTemp (already aggregated into stats for the
        # pressure L/R balance). tyre_reading=None makes camber/toe degrade to lap-time
        # tuning - we never fabricate a 3-zone reading we don't have.
        if self.console_mode:
            self.tyre_reading = None
            self.last_reader = "console (single UDP temp)"
            return
        path, g, udp = (self.lap_heat_fn() if self.lap_heat_fn else (None, 0.0, None))
        self._read_heat(path, g, udp_temps=udp)
        self._warn_temp_blind_once()       # tarmac + no reading -> warn once, plainly

    def _track_fixation(self):
        """Anti-fixation: count CONSECUTIVE iterations spent on each axle's
        bottoming remedy. At the cap, LOCK that axle's bottoming so the loop stops
        piling onto one lever and the next rule runs instead. Records this batch's
        bottoming axles so the fitness gate can also lock on a regression."""
        symptoms = {r.symptom for r in self.batch if getattr(r, "symptom", "")}
        primary = next((s for s in symptoms if s.startswith("bottoming_")), None)
        if primary and primary == self._last_symptom:
            self._symptom_streak += 1
        else:
            self._symptom_streak = 1 if primary else 0
            self._last_symptom = primary
        self._cur_bottoming_axles = set()
        for axle in ("front", "rear"):
            if f"bottoming_{axle}" in symptoms:
                self._cur_bottoming_axles.add(axle)
                self._bottoming_attempts[axle] = self._bottoming_attempts.get(axle, 0) + 1
                if (self._bottoming_attempts[axle] >= rules.BOTTOMING_CAP
                        and axle not in self._bottoming_locked):
                    self._bottoming_locked.add(axle)
                    _laplog.info("ANTI-FIXATION: %s bottoming hit the cap (%d consecutive "
                                 "attempts) - LOCKING the axle's bottoming remedy; the loop "
                                 "moves to the next rule instead of stiffening bump again.",
                                 axle, self._bottoming_attempts[axle])
                    self.log(f"[anti-fixation] {axle} bottoming capped at "
                             f"{rules.BOTTOMING_CAP} attempts - accepting the residual and "
                             "moving to the next lever.")
            else:
                self._bottoming_attempts[axle] = 0     # streak broken -> reset

    # ---- shared helpers (both modes) ------------------------------------
    def _consume_ride_progress(self):
        """If the last ride-height raise didn't reduce bottoming, lock that axle."""
        if self._last_ride_change is None:
            return
        axle = self._last_ride_change["axle"]
        before = self._last_ride_change["susp_before"]
        now = (self.stats.susp_min_front if axle == "front" else self.stats.susp_min_rear)
        if now <= before + RIDE_IMPROVE_MARGIN and axle not in self.ride_locked:
            self.ride_locked.add(axle)
            self.log(f"[ride] {axle} ride raise stopped helping; locking it.")
        self._last_ride_change = None

    def _read_heat(self, heat_path, peak_g, udp_temps=None, manual_reading=None):
        """Read the captured PEAK-LOAD frame, cross-checked against the frame's UDP
        TireTemp. PRIMARY: bundled LOCAL OCR (RapidOCR PP-OCR ONNX) - offline, no
        API key, detects text anywhere (resolution / aspect / HUD-scale free).
        FALLBACK chain: [opt-in Anthropic vision API] -> RapidOCR -> Tesseract.
        If no clean read, camber is tuned by LAP-TIME SEARCH (tyre_reading=None);
        the manual dialog is opt-in (temp_mode='manual'), never the default."""
        from ..vision import read_tyres
        reading = None
        if manual_reading is not None:
            self.tyre_reading = manual_reading
            self.last_reader = "manual"
            return
        if not heat_path:
            self.log("[heat] no peak-load frame this lap - camber by lap-time search "
                     "(drive a real corner with the Heat page up to use temps).")
            self.tyre_reading = None
            self.last_reader = "none"
            return
        # OPT-IN manual mode: dialog every lap (only when the user chose it).
        if self.temp_mode == "manual" and self.manual_temp_fn is not None:
            self.tyre_reading = self.manual_temp_fn(heat_path)
            self.last_reader = "manual"
            return
        # 0) OPT-IN - Anthropic vision API, only if enabled AND a key is present.
        if self.use_vision_api and read_tyres.vision_available():
            reading = read_tyres.vision_read_image(heat_path, udp_temps=udp_temps)
            if reading is not None:
                self.last_reader = "vision_api"
                self.log("[heat] vision API read the frame (UDP-checked).")
        # 1) PRIMARY - bundled local RapidOCR (offline, any resolution / aspect)
        if reading is None and read_tyres.rapidocr_available():
            reading = read_tyres.rapidocr_read_image(heat_path, udp_temps=udp_temps)
            if reading is not None:
                self.last_reader = "rapidocr"
                self.log("[heat] local OCR (RapidOCR) read the frame (UDP-checked).")
        # 2) FALLBACK - Tesseract (resolution-relative boxes, 16:9 best-effort)
        if reading is None:
            reading = read_tyres.ocr_heat_page(heat_path, udp_temps=udp_temps)
            if reading is not None:
                self.last_reader = "tesseract"
                self.log("[heat] Tesseract OCR read the frame (UDP-checked).")
        if reading is None:
            # Never block on manual: fall through with no reading and let the
            # tuner search camber by lap time this iteration.
            self.last_reader = "camber_search"
            self.log("[heat] no clean OCR read - tuning camber by LAP-TIME SEARCH "
                     "this lap (press the manual hotkey to enter temps if you prefer).")
        self.tyre_reading = reading

    def _apply_fitness(self, after: Optional[float]):
        """Keep/revert the applied BATCH by lap/segment time. Does NOT set phase.
        On a regression the WHOLE batch is reverted and each lever locked."""
        best = self.best_segment
        n = len(self._applied_records)
        if after is None:
            self.log("[fitness] no valid time - keeping the batch on mechanical evidence.")
        elif best is None:
            self.best_segment = after
            self._baseline_lap_s = after
            self.log(f"[fitness] {after:.2f}s reference set.")
        elif after - best > rules.SEGMENT_REGRESS_S:
            reverted = []
            # revert EACH batched record by its own stored previous values (LIFO).
            # (revert_last() only ever targets history[-1], so it can't unwind a
            # multi-change batch - restore each record explicitly.)
            for r in reversed(self._applied_records):
                for k, v in r.previous.items():
                    self.state.current.set(k, v)
                r.verdict = "reverted"
                self.state.mark_converged(r.lever_group)
                reverted.append(r.lever_group)
            self.stale += 1
            self.log(f"[fitness] BATCH regressed {after-best:+.2f}s ({best:.2f}->{after:.2f}) - "
                     f"reverted all {n}: {reverted}.")
        else:
            for r in self._applied_records:
                r.verdict = "kept"
            self.stale = self.stale + 1 if after >= best - 1e-3 else 0
            self.best_segment = min(best, after)
            self.log(f"[fitness] batch ({n}) {after-best:+.2f}s ({best:.2f}->{after:.2f}) - kept.")
        self._applied_records = []
        self.state.iteration += 1

    # ---- segment timing (MANUAL free-roam: hotkey-driven, no alt-tab) ----
    def mark_segment_start(self):
        # [F9] while still auto-detecting commits to MANUAL free-roam mode.
        if self.mode is None and self.phase == DRIVE_AUTO:
            self.mode = MODE_MANUAL
            self.phase = BASELINE_TIME
            self.log("Manual free-roam mode selected ([F9]/[F10] segment markers).")
        self._seg_start = segment.grab_mark(self.listener)
        if self._seg_start is None:
            self.log("[timer] no telemetry at start mark")
        else:
            self.log("[timer] segment START marked")

    def mark_segment_end(self) -> Optional[segment.SegmentRun]:
        if self._seg_start is None:
            self.log("[timer] mark START first")
            return None
        end = segment.grab_mark(self.listener)
        if end is None:
            self.log("[timer] no telemetry at end mark")
            return None
        run = segment.measure(self.listener, self._seg_start, end, self.ref_distance)
        self._seg_start = None
        if not run.valid:
            self.log(f"[timer] {run.note}")
            return run
        self.log(f"[timer] {run.elapsed_s:.2f}s over {run.distance_m:.0f} m")
        if self.phase == BASELINE_TIME:
            self.best_segment = run.elapsed_s
            self._baseline_lap_s = run.elapsed_s
            self.ref_distance = run.distance_m
            self.phase = TEST
        elif self.phase == CHANGE_TIME:
            self._apply_fitness(run.elapsed_s if run.valid else None)
            self.phase = TEST
        return run

    # ---- characterisation test + Heat OCR -------------------------------
    def begin_test(self):
        self._test_mark = self.listener.mark if self.listener else 0
        self.phase = TEST
        self.log("Drive the test with the Heat page visible.")

    def end_test(self, heat_path: Optional[str] = None, peak_g: float = 0.0,
                 udp_temps: Optional[dict] = None, manual_reading: Optional[dict] = None):
        """MANUAL free-roam: end the characterisation drive and compute a batch."""
        window = self.listener.drain_since(self._test_mark) if self.listener else []
        self.stats = aggregate(window)
        self._consume_ride_progress()
        self._read_heat(heat_path, peak_g, udp_temps=udp_temps, manual_reading=manual_reading)
        self._compute_batch()

    def _compute_batch(self):
        """Build the change(s) for this test lap: all evidence-driven changes
        together + up to `changes_per_test` search-driven steps."""
        # The diff rule is drivetrain-aware (FWD front-only, RWD rear-only, AWD
        # centre+rear). Force the effective drivetrain (manual override wins over the
        # telemetry-detected one) onto the stats the rules read, so a misdetected
        # DrivetrainType can't hand a FWD car rear/centre-diff inputs.
        if self.stats is not None:
            eff = self.effective_drivetrain()
            if eff != getattr(self.stats, "drivetrain", None):
                _laplog.info("DRIVETRAIN: tuning as %s (override=%s, detected=%s).", eff,
                             self.drivetrain_override or "none",
                             self.identity.drivetrain if self.identity else "?")
            self.stats.drivetrain = eff
        self.batch = rules.analyze_batch(
            self.stats, self.state.current, self.discipline, self.tyre_reading,
            converged=self.state.converged_levers, limits=self.limits,
            ride_locked=self.ride_locked, max_search=self.changes_per_test,
            bottoming_locked=self._bottoming_locked,
            step_mult=rules.step_mult_for(self.aggressiveness),
            bottoming_attempts=self._bottoming_attempts,
            lever_locked=self._lever_locked,
            rejected_fields=self._rejected_fields)
        if not self.batch:
            if self.stale >= 2 or self.best_segment is None:
                self.phase = DONE
                self.finish()
                return
            self.stale += 1
            self.log("No rule fired; one more lap/run to confirm.")
            self.phase = self._drive_phase()
            return
        self._track_fixation()        # anti-fixation: count + cap same-axle bottoming
        # remember a ride-height raise so the next lap can check it helped
        for rec in self.batch:
            if rec.group == "ride_height":
                axle = "front" if "ride_height_f" in rec.fields else "rear"
                self._last_ride_change = {
                    "axle": axle,
                    "susp_before": (self.stats.susp_min_front if axle == "front"
                                    else self.stats.susp_min_rear)}
        self.phase = SHOW_CHANGE
        ev = sum(1 for r in self.batch if r.kind == "evidence")
        se = len(self.batch) - ev
        _laplog.info("batch emitted: %d change(s) [%d evidence, %d search]: %s",
                     len(self.batch), ev, se,
                     [(r.group, r.fields) for r in self.batch])
        self._first_change_logged = True

    # ---- apply the change(s) --------------------------------------------
    def change_applied(self):
        """User entered the shown BATCH. AUTO-LAP: ignore the out-lap, the next
        full lap measures the whole batch. MANUAL: time the same segment again."""
        if not self.batch:
            return
        # A/B/A confirmed B beats the re-measured baseline: the user re-enters B's
        # values; accept WITHOUT another measurement (we already confirmed the gain).
        if self.batch[0].group == "confirm_reapply":
            for rec in self.batch:
                self.state.apply_change(rec.group, rec.fields, rec.reason, rec.feel_for)
                self._on_car.update(rec.fields)        # user physically re-entered B
            keep = self._aba_keep or {}
            self._aba_keep = None
            if keep.get("b_telem") is not None and getattr(keep["b_telem"], "live", False):
                self._ref_telem = keep["b_telem"]
            bt = keep.get("b_time")
            improved = bt is not None and (self.best_segment is None or bt <= self.best_segment + 1e-9)
            if bt is not None and (self.best_segment is None or bt < self.best_segment):
                self.best_segment = bt
            self._best_tune = self.state.current.copy()    # B is now physically applied
            self._best_tune_lap = bt if bt is not None else self._best_tune_lap
            self.stale = 0
            self.batch = []
            self._applied_records = []
            self.state.iteration += 1
            self._record_outcome("gain" if improved else "flat")
            self.log("[A/B/A] confirmed change re-applied and kept; continuing.")
            self._next_step()
            return
        self._reset_test()                 # a new change starts a fresh multi-lap test
        self._applied_records = []
        for rec in self.batch:
            applied = self.state.apply_change(rec.group, rec.fields, rec.reason, rec.feel_for)
            applied.seg_before_s = self.best_segment
            self._applied_records.append(applied)
            self._on_car.update(rec.fields)            # user physically entered these
        if self.batch[0].group == "final_baseline":
            # instruction-only rec; the user re-enters the baseline tune by hand
            self._on_car = self.baseline.as_dict() if self.baseline else self._on_car
        if self.mode == MODE_AUTO:
            self._awaiting_test = True
            self._await_state = "out_lap"
            self._pre_change_last_lap = self.last_lap_s   # carried-over value to reject
            self.arm_next_lap()        # ignore the partial out-lap on the new tune
            self.phase = DRIVE_AUTO
            _laplog.info("WAITING_FOR_MEASURED_LAP entered [out_lap]: %d change(s) applied; "
                         "skip_laps=%d, carried-over LastLap=%.2f. A reload/restart re-arms "
                         "the out-lap; only a fresh full green lap is measured.",
                         len(self.batch), self._skip_laps, self.last_lap_s or 0.0)
            self.log(f"Applied {len(self.batch)} change(s). RESTART the Rivals event, then drive "
                     "a clean lap - the out-lap is ignored; the next FULL lap is measured.")
        else:
            self.phase = CHANGE_TIME

    # ---- finish ----------------------------------------------------------
    def _meta(self) -> dict:
        ident = self.identity
        return {"car": ident.name if ident else "Car",
                "car_class": (self.target_class
                              or (ident.target_class if ident else "S1 800")),
                "drivetrain": ident.drivetrain if ident else "AWD"}

    def save_on_exit(self):
        """Called from the shutdown path (window close / tray Quit / atexit). If a
        session is in progress and not already finalised, persist the best confirmed
        tune so an abnormal exit never loses it, then flush+close the session log."""
        try:
            if self.state and self.baseline and not self._saved and self.phase != DONE:
                self.log("Session ended early - saving the best confirmed tune so far.")
                self.save_progress("interrupted")
        except Exception:
            _laplog.exception("save_on_exit failed")
        finally:
            self._close_session_log()

    def best_confirmed_tune(self):
        """The tune that is actually SAVED: the best CONFIRMED one (never a mid-flight
        reverted state.current). Falls back to the baseline if nothing was confirmed."""
        if self._best_tune is not None:
            return self._best_tune
        return self.baseline.copy() if self.baseline else (self.state.current if self.state else None)

    def save_progress(self, status: str) -> bool:
        """Write the session JSON + shareable tune for the BEST CONFIRMED tune. Safe to
        call ANY time (after baseline, on convergence/budget, or on an abnormal exit) so
        a session is never lost. Returns True on success; surfaces write errors."""
        if not self.state or not self.baseline:
            return False
        m = self._meta()
        out = self.best_confirmed_tune()
        improved = (self._best_tune_lap is not None and self._baseline_lap_s is not None
                    and self._best_tune_lap < self._baseline_lap_s - 1e-6)
        best_lap = self._best_tune_lap if improved else (self._baseline_lap_s or self.best_segment)
        try:
            store.save_session(
                self.state, car=m["car"], car_class=m["car_class"], discipline=self.discipline,
                front_weight_pct=self.front_weight_pct, drivetrain=m["drivetrain"],
                baseline=self.baseline, stats_log=[], started_iso=self.started_iso,
                status=status, limits=self.limits, best_lap_s=best_lap,
                finished_iso=_dt.datetime.now().isoformat(timespec="seconds"),
                final_tune=out)
            self.export = store.export_tune(
                self.state, car=m["car"], car_class=m["car_class"], discipline=self.discipline,
                front_weight_pct=self.front_weight_pct, drivetrain=m["drivetrain"],
                best_lap_s=best_lap, final_tune=out)
            try:
                store.append_cumulative_log(
                    self.state, self.baseline, car=m["car"], car_class=m["car_class"],
                    discipline=self.discipline, drivetrain=m["drivetrain"],
                    started_iso=self.started_iso, best_lap_s=best_lap,
                    baseline_lap_s=self._baseline_lap_s)
            except Exception:
                _laplog.exception("cumulative log append failed")
            return True
        except Exception as e:
            _laplog.exception("save_progress(%s) FAILED", status)
            self.fail(f"could not save the session ({e}); see app.log - the sessions "
                      "folder may be unwritable.")
            return False

    def finish(self):
        if not self.state:
            return
        if self.final_verdict:
            self.log(self.final_verdict)
        # state which tune was chosen and WHY (best confirmed vs baseline)
        improved = (self._best_tune_lap is not None and self._baseline_lap_s is not None
                    and self._best_tune_lap < self._baseline_lap_s - 1e-6)
        if improved:
            self.log(f"Final tune = BEST CONFIRMED ({self._best_tune_lap:.2f}s, "
                     f"{self._confirmed_gains} confirmed change(s) vs baseline "
                     f"{self._baseline_lap_s:.2f}s).")
        else:
            self.log("Final tune = the original baseline (no change beat it once driver "
                     "improvement was accounted for).")
        ok = self.save_progress(self.stop_reason or "converged")
        if ok:
            self._saved = True             # finalised - save_on_exit won't re-save
            self.log(f"Tune saved. Shareable files in {self.export['folder']}.")
        self._close_session_log()
        self.phase = DONE

    def env_info(self) -> dict:
        """Environment summary for the support bundle: resolution, which reader was
        used, whether an API key is present."""
        from ..vision import capture, read_tyres
        res = None
        try:
            res = capture.screen_size()
        except Exception:
            res = None
        return {
            "resolution": res,
            "temp_reader_used": self.last_reader,
            "temp_mode": self.temp_mode,
            "vision_api_opted_in": self.use_vision_api,
            "anthropic_api_key_present": read_tyres.vision_available(),
            "rapidocr_available": read_tyres.rapidocr_available(),
            "discipline": self.discipline,
            "car": self._meta()["car"],
            "iterations": self.state.iteration if self.state else 0,
            "best_lap_s": self.best_segment,
            "restart_count": self._restart_count,
        }

    def write_support_bundle(self, app_log: Optional[str] = None,
                             heat_frames: Optional[list] = None) -> Optional[str]:
        """One shareable zip a user can send for support. Returns the path."""
        m = self._meta()
        try:
            path = store.write_support_bundle(
                car=m["car"], discipline=self.discipline, env=self.env_info(),
                app_log=app_log, heat_frames=heat_frames)
            self.log(f"Support bundle written: {path}")
            return path
        except Exception as e:
            _laplog.exception("support bundle failed")
            self.fail(f"support bundle: {e}")
            return None

    def fail(self, msg: str):
        """Record an error to surface in the overlay (never vanish silently)."""
        self.error = msg
        self.log(f"ERROR: {msg}")

    # ---- guided workflow: 6 explicit steps, ONE action line each ---------
    def guided_step(self) -> dict:
        """Map the internal phase/mode to ONE of the 6 user-facing steps and the
        single 'what to do now' line for it. This drives the Simple view; it never
        changes the tuning logic, only how it's narrated."""
        ph, mode = self.phase, self.mode
        port = self.port
        auto = mode == MODE_AUTO
        # 1. SELECT CAR
        if ph == WAIT_TELEMETRY:
            return self._g(1, "Select car",
                           f"Start driving in FH6 (Data Out on, port {port}) - "
                           "waiting for telemetry.")
        if ph == CONFIRM_CAR:
            name = self.identity.name if self.identity else "the car"
            return self._g(1, "Select car",
                           f"{name} detected. Press [F8] to confirm and open setup.")
        if ph == SETUP:
            return self._g(1, "Select car",
                           "Pick discipline and enter the slider ranges, then Apply.")
        # 2. APPLY INITIAL TUNE
        if ph == APPLY_BASELINE:
            return self._g(2, "Apply initial tune",
                           "Enter the tune below in the in-game tune menu, then load a "
                           "Rivals event and press [F8].")
        # 6. CONVERGED
        if ph == DONE:
            return self._g(6, "Converged",
                           "Tuning complete - your final tune and shareable files are saved.")
        # measuring vs changing depends on whether a change is on the table
        if ph == SHOW_CHANGE:
            return self._g(4, "Apply changes",
                           "Apply these changes in the tune menu, RESTART the Rivals event, "
                           "then press [F8] and drive 2 laps.")
        if ph in (TEST, BASELINE_TIME, CHANGE_TIME):
            # manual free-roam timing path
            if ph == TEST:
                return self._g(3 if self.best_segment is None else 5,
                               "Baseline laps" if self.best_segment is None else "Test laps",
                               "Open the Heat page, press [F8] to start the test, drive, "
                               "then [F11] when done.")
            mark = "baseline" if ph == BASELINE_TIME else "test"
            return self._g(3 if ph == BASELINE_TIME else 5,
                           "Baseline laps" if ph == BASELINE_TIME else "Test laps",
                           f"Mark your {mark} segment: [F9] at START, [F10] at END.")
        if ph == DRIVE_AUTO:
            if not auto:
                return self._g(3, "Baseline laps",
                               "Drive a lap with the Heat page up - auto-lap starts when "
                               "the lap timer moves. (Free-roam? Press [F9] to mark a segment.)")
            if self._awaiting_test:
                if self._await_state == "out_lap":
                    return self._g(5, "Test laps",
                                   "Out-lap - drive a clean lap. The lap right after a change "
                                   "(and any reload) is ignored; the next FULL lap is measured.")
                return self._g(5, "Test laps",
                               "Measuring... drive a clean lap; it's timed and compared to "
                               "your best, then the next change appears.")
            if self.best_segment is None:
                return self._g(3, "Baseline laps",
                               "Drive 2 laps: lap 1 is a warm-up (ignored), lap 2 is your "
                               "baseline. Keep the Heat page visible.")
            return self._g(5, "Test laps",
                           "Keep driving clean laps with the Heat page up.")
        return self._g(1, "Select car", "")

    def _g(self, n: int, title: str, action: str) -> dict:
        return {"number": n, "title": title, "action": action, "total": 6}

    # ---- view controls (presentation only; never touch tuning) ----------
    def toggle_view_mode(self) -> str:
        self.view_mode = "advanced" if self.view_mode == "simple" else "simple"
        self.log(f"View: {self.view_mode.upper()}.")
        return self.view_mode

    def set_view_mode(self, mode: str) -> None:
        if mode in ("simple", "advanced"):
            self.view_mode = mode

    # ---- data providers for the MAIN WINDOW (between-session management) --
    def previous_tunes(self) -> list:
        return store.list_sessions()

    def stats_summary(self) -> dict:
        return store.stats_summary()

    def rename_car(self, ordinal: int, name: str) -> bool:
        """Edit a saved car name (Settings). Updates the live identity if it's the
        current car. Blank name forgets it."""
        ok = ordinals.save_name(ordinal, name)
        if self.identity and self.identity.ordinal == ordinal:
            self.identity.name = ordinals.name_for(ordinal)
            self.identity.known = ordinals.is_known(ordinal)
        return ok

    def forget_car(self, ordinal: int) -> bool:
        return self.rename_car(ordinal, "")

    def settings_view(self) -> dict:
        """Data for the Settings tab: saved car names + output folders + modes."""
        return {
            "car_names": [{"ordinal": o, "name": n}
                          for o, n in sorted(ordinals.user_names().items())],
            "names_path": ordinals.NAMES_PATH,
            "tunes_folder": store.SESSIONS_DIR,
            "temp_mode": self.temp_mode,
            "view_mode": self.view_mode,
            "overlay_capturable": self.overlay_capturable,
            "vision_api_opted_in": self.use_vision_api,
            "changes_per_test": self.changes_per_test,
            "laps_per_test": self.laps_per_test,
            "lap_agg": self.lap_agg,
        }

    HELP_TEXT = (
        f"{PRODUCT_NAME.upper()} - QUICK GUIDE\n"
        "The tool reads telemetry + your Heat-page screenshot and tells you EXACT\n"
        "values to enter. You drive; it never touches the game.\n\n"
        "STEPS\n"
        "  1 Select car   - confirm the detected car, pick discipline + slider ranges.\n"
        "  2 Apply tune    - enter the shown values in-game, load a Rivals event.\n"
        "  3 Baseline laps - drive 2 laps (lap 1 warm-up ignored, lap 2 = baseline).\n"
        "  4 Changes       - apply the shown changes, RESTART the event, drive 2 laps.\n"
        "  5 Test laps     - lap 1 warm-up (ignored), lap 2 timed vs your best.\n"
        "  6 Converged     - final tune + shareable files saved.\n\n"
        "HOTKEYS\n"
        "  [F8] advance/confirm/apply   [F11] end manual test\n"
        "  [F9]/[F10] manual segment start/end (free-roam, no lap timer)\n"
        "  [F6] Simple/Advanced view    [F7] switch tab    [Ctrl+F12] quit\n\n"
        "TYRE TEMPS are read locally (offline). If a lap can't be read, camber is\n"
        "tuned by lap time instead - it never blocks. Output files (value sheet,\n"
        "JSON, optn.club block) are VALUES to type in - NOT an in-game share code.")

    # ---- view model ------------------------------------------------------
    def packet_age_s(self) -> Optional[float]:
        if self.listener and self.listener.last_packet_time:
            return round(time.time() - self.listener.last_packet_time, 1)
        return None

    def status(self) -> dict:
        snap = self.snapshot()
        live = None
        if snap:
            live = {"speed_mph": round(snap.speed_mph, 1),
                    "rpm": round(snap.current_engine_rpm),
                    "gear": snap.gear, "lat_g": round(snap.lateral_g, 2),
                    "drivetrain": snap.drivetrain_name,
                    "drivetrain_raw": snap.drivetrain_type,   # raw int, per frame
                    "num_cylinders": snap.num_cylinders}
        change = None
        if self.rec and self.rec.is_change():
            change = {"group": self.rec.group, "detail": self.rec.detail,
                      "reason": self.rec.reason, "feel": self.rec.feel_for,
                      "fields": self.rec.fields}
        batch = [{"group": r.group, "fields": r.fields, "detail": r.detail,
                  "kind": r.kind} for r in self.batch]
        laps = None
        if snap:
            laps = {"race_on": int(snap.is_race_on), "lap": int(snap.lap_number),
                    "cur": round(snap.current_lap, 1), "last": round(snap.last_lap, 2)}
        mode_label = self.mode or ("detecting" if self.phase == DRIVE_AUTO else None)
        test_target = self._target_laps() if (self.mode == MODE_AUTO
                                              and self.phase == DRIVE_AUTO) else None
        return {
            "phase": self.phase,
            "step": self.guided_step(),
            "view_mode": self.view_mode,
            "restart_count": self._restart_count,
            "test_laps_done": len(self._test_laps),
            "test_target": test_target,
            "test_best": min(self._test_laps) if self._test_laps else None,
            "port": self.port,
            "packet_age_s": self.packet_age_s(),
            "error": self.error,
            "mode": self.mode,
            "mode_label": mode_label,
            "laps": laps,
            "lap_number": self.lap_number,
            "last_lap_s": self.last_lap_s,
            "car": self.identity.summary() if self.identity else None,
            "discipline": self.discipline,
            "iteration": self.state.iteration if self.state else 0,
            "best_segment_s": self.best_segment,
            "ui": self.ui_state(),
            "progress": self.progress_state(),
            "console_mode": self.console_mode,
            "console_notice": self.CONSOLE_NOTICE if self.console_mode else None,
            "lan_ip": self.lan_ip() if self.console_mode else None,
            "temp_blind": self.temp_blind(),
            "temp_notice": self.TEMP_BLIND_NOTICE if self.temp_blind() else None,
            "budget_min": self.time_budget_min or None,
            "budget_remaining_s": self.budget_remaining_s(),
            "budget_expired": self._budget_expired,
            "rigour": self.rigour,
            "stop_reason": self.stop_reason or None,
            "final_verdict": self.final_verdict or None,
            "live": live,
            "change": change,
            "batch": batch,
            "changes_per_test": self.changes_per_test,
            "checklist": self.baseline_checklist() if self.phase == APPLY_BASELINE else None,
            "export": self.export if self.phase == DONE else None,
            # ADVANCED-view extras (cheap; the overlay shows them only in advanced)
            "tyre_reading": self.tyre_reading,
            "last_reader": self.last_reader,
            "history": [{"group": h.lever_group, "fields": h.fields,
                         "verdict": h.verdict, "reason": h.reason}
                        for h in (self.state.history[-6:] if self.state else [])],
            "messages": self.messages[-6:],
        }
