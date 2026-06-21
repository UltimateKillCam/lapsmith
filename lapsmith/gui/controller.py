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

from ..telemetry.listener import TelemetryListener
from ..telemetry.session import aggregate, TestStats, HIGH_G_THRESHOLD
from ..telemetry import segment
from ..identity import identify, is_live, CarIdentity
from .. import ordinals, PRODUCT_NAME
from ..knowledge.baseline import build_baseline, format_checklist
from ..knowledge import rules
from ..state.tune_state import Tune, TuneState, CarLimits
from ..state import store
from ..telemetry.laps import LapWatcher, LapResult

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
    last_lap_s: Optional[float] = None
    lap_number: int = 0
    # multi-lap fitness (single laps are too noisy)
    laps_per_test: object = "adaptive"   # "adaptive" | 1 | 2 | 3
    lap_agg: str = "best"                # "best" (default) | "median"
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
    def start(self):
        if self.listener is None:
            self.listener = TelemetryListener(port=self.port)
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()

    def log(self, msg: str):
        self.messages.append(msg)
        self.messages = self.messages[-50:]

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
        self.stale = 0
        self.export = None
        self.batch = []
        self._applied_records = []
        self._reset_test()
        self.tyre_reading = None
        self.last_reader = None
        self.ride_locked = set()
        self._last_ride_change = None
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
                    target_class: Optional[str] = None):
        self.discipline = discipline
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
        drivetrain = self.identity.drivetrain if self.identity else "AWD"
        car = self.identity.name if self.identity else "Car"
        self.baseline = build_baseline(car, target, discipline,
                                       self.front_weight_pct, drivetrain,
                                       limits=self.limits)
        self.state = TuneState(self.baseline.copy())
        self.phase = APPLY_BASELINE
        self.log(f"Baseline built for {car} ({target}, {drivetrain}).")

    def baseline_checklist(self) -> str:
        if not self.baseline:
            return ""
        ident = self.identity
        target = self.target_class or (ident.target_class if ident else "S1 800")
        return format_checklist(self.baseline, ident.name if ident else "Car",
                                target,
                                self.discipline, self.front_weight_pct,
                                ident.drivetrain if ident else "AWD")

    def baseline_applied(self):
        """User entered the baseline. Mode is NOT locked here - it stays
        'detecting' and engages AUTO-LAP the moment the lap timer is seen
        advancing (even if the user enters the Rivals event later). Until then,
        [F9] commits to manual free-roam markers."""
        self.mode = None
        self._watcher.reset()
        self._tick_mark = self.listener.mark if self.listener else 0
        self.phase = DRIVE_AUTO
        self.log("Detecting lap timing... drive a lap (AUTO-LAP engages when the "
                 "timer advances), or press [F9] to mark a manual segment.")

    def _drive_phase(self) -> str:
        return DRIVE_AUTO if self.mode == MODE_AUTO else TEST

    # ---- AUTO-LAP: one iteration = one lap ------------------------------
    def arm_next_lap(self):
        """Ignore the next lap completion (a partial out-lap on a fresh tune)."""
        self._skip_laps = 1

    def _instrument(self, snap):
        """Log the raw lap fields to app.log on change (offset-verification)."""
        if snap is None:
            return
        dbg = (int(snap.is_race_on), int(snap.lap_number),
               round(snap.current_lap, 1), round(snap.last_lap, 2))
        if dbg != self._lap_dbg:
            self._lap_dbg = dbg
            _laplog.info("lap-fields RAW: IsRaceOn@0=%d LapNumber@312=%d "
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
                _laplog.info("tick DETECT: n=%d prevLap=%s curLap=%s prevCur=%s "
                             "curCur=%s advancing=%s completed=%d",
                             len(pkts), prev_lap,
                             (last.lap_number if last else None), prev_cur,
                             (round(last.current_lap, 2) if last else None),
                             adv, len(laps))
            # engage AUTO-LAP the instant the lap timer is seen advancing
            if self.mode is None and adv:
                self.mode = MODE_AUTO
                self.arm_next_lap()       # ignore the partial lap already in progress
                _laplog.info("MODE TRANSITION detecting -> AUTO (lap timer advancing)")
                self.log("AUTO-LAP engaged - lap timer is live. Each completed lap is "
                         "captured automatically.")
            # an event RESTART (race off->on, or lap counter reset) re-arms the
            # warm-up discard so lap 1 of the fresh standing start is ignored.
            if self._watcher.pop_restarted() and self.mode == MODE_AUTO:
                self.arm_next_lap()
                self._reset_test()
                self._restart_count += 1
                self.log("Event restart detected - discarding the warm-up lap; the "
                         "next FULL lap is measured.")
            if self.mode != MODE_AUTO:
                return
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
        if self._skip_laps > 0:
            self._skip_laps -= 1
            self.log(f"[lap {lap.lap_number}] warm-up/out-lap ignored "
                     f"({lap.last_lap_s:.2f}s) - drive the next full lap.")
            return
        if lap.last_lap_s <= 0:
            self.log(f"[lap {lap.lap_number}] no valid LastLap - skipped.")
            return
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

    def _finalize_test(self):
        laps = self._test_laps
        test_time = min(laps) if self.lap_agg != "median" else _median(laps)
        noise = (max(laps) - min(laps)) if len(laps) >= 2 else 0.0
        self.stats = self._best_lap_stats if self._best_lap_stats is not None else self.stats
        self.tyre_reading = self._best_lap_reading
        self._consume_ride_progress()
        _laplog.info("test finalized: laps=%s -> %s %.2fs noise %.2fs",
                     [round(x, 2) for x in laps], self.lap_agg, test_time, noise)
        if self._awaiting_test:
            self._apply_fitness_multi(test_time, noise)
            self._awaiting_test = False
        elif self.best_segment is None:
            self.best_segment = test_time
            self._baseline_lap_s = test_time
            self.log(f"[test] baseline reference {test_time:.2f}s "
                     f"(best of {len(laps)}, noise {noise:.2f}s).")
        self._reset_test()
        self._compute_batch()

    def _reset_test(self):
        self._test_laps = []
        self._best_lap_time = None
        self._best_lap_stats = None
        self._best_lap_reading = None

    def _apply_fitness_multi(self, test_time: float, noise: float):
        """Keep/revert the batch from a MULTI-LAP test. Only act when the delta
        clearly exceeds lap-to-lap noise; within noise = inconclusive (hold, don't
        lock). Evidence-driven batches need an extra margin to be reverted."""
        best = self.best_segment
        if best is None:
            self.best_segment = test_time
            self._baseline_lap_s = test_time
            self.state.iteration += 1
            return
        delta = test_time - best
        gate = max(rules.SEGMENT_REGRESS_S, noise)
        has_evidence = any(rules._kind_of(r.lever_group) == "evidence"
                           for r in self._applied_records)
        revert_gate = gate + (ADAPTIVE_EVIDENCE_MARGIN if has_evidence else 0.0)
        n = len(self._applied_records)
        if delta > revert_gate:
            for r in reversed(self._applied_records):
                for k, v in r.previous.items():
                    self.state.current.set(k, v)
                r.verdict = "reverted"
                self.state.mark_converged(r.lever_group)
            self.stale += 1
            self.log(f"[fitness] BATCH ({n}) regressed {delta:+.2f}s > gate {revert_gate:.2f}"
                     f"{' (+evidence margin)' if has_evidence else ''} - reverted & locked.")
        elif delta < -gate:
            for r in self._applied_records:
                r.verdict = "kept"
            self.best_segment = min(best, test_time)
            self.stale = 0
            self.log(f"[fitness] batch ({n}) improved {delta:+.2f}s (noise {noise:.2f}) - kept.")
        else:
            for r in self._applied_records:
                r.verdict = "kept"
            self.best_segment = min(best, test_time)
            self.stale += 1
            self.log(f"[fitness] batch ({n}) {delta:+.2f}s within noise {noise:.2f} - "
                     "INCONCLUSIVE, holding the change (kept, not locked).")
        self._last_improvement = best - test_time
        self._applied_records = []
        self.state.iteration += 1

    def _read_lap_heat(self):
        path, g, udp = (self.lap_heat_fn() if self.lap_heat_fn else (None, 0.0, None))
        self._read_heat(path, g, udp_temps=udp)

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
        self.batch = rules.analyze_batch(
            self.stats, self.state.current, self.discipline, self.tyre_reading,
            converged=self.state.converged_levers, limits=self.limits,
            ride_locked=self.ride_locked, max_search=self.changes_per_test)
        if not self.batch:
            if self.stale >= 2 or self.best_segment is None:
                self.phase = DONE
                self.finish()
                return
            self.stale += 1
            self.log("No rule fired; one more lap/run to confirm.")
            self.phase = self._drive_phase()
            return
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
        self._reset_test()                 # a new change starts a fresh multi-lap test
        self._applied_records = []
        for rec in self.batch:
            applied = self.state.apply_change(rec.group, rec.fields, rec.reason, rec.feel_for)
            applied.seg_before_s = self.best_segment
            self._applied_records.append(applied)
        if self.mode == MODE_AUTO:
            self._awaiting_test = True
            self.arm_next_lap()        # ignore the partial out-lap on the new tune
            self.phase = DRIVE_AUTO
            self.log(f"Applied {len(self.batch)} change(s) - ignoring the out-lap; the next "
                     "COMPLETE lap measures the batch.")
        else:
            self.phase = CHANGE_TIME

    # ---- finish ----------------------------------------------------------
    def _meta(self) -> dict:
        ident = self.identity
        return {"car": ident.name if ident else "Car",
                "car_class": (self.target_class
                              or (ident.target_class if ident else "S1 800")),
                "drivetrain": ident.drivetrain if ident else "AWD"}

    def finish(self):
        if not self.state:
            return
        m = self._meta()
        finished_iso = _dt.datetime.now().isoformat(timespec="seconds")
        store.save_session(self.state, car=m["car"], car_class=m["car_class"],
                           discipline=self.discipline, front_weight_pct=self.front_weight_pct,
                           drivetrain=m["drivetrain"], baseline=self.baseline, stats_log=[],
                           started_iso=self.started_iso, status="converged",
                           limits=self.limits, best_lap_s=self.best_segment,
                           finished_iso=finished_iso)
        # shareable value sheet (.txt + optn block) + clean JSON, in the known folder
        self.export = store.export_tune(
            self.state, car=m["car"], car_class=m["car_class"], discipline=self.discipline,
            front_weight_pct=self.front_weight_pct, drivetrain=m["drivetrain"],
            best_lap_s=self.best_segment)
        # cumulative log (paste into an LLM to refine the method)
        try:
            store.append_cumulative_log(
                self.state, self.baseline, car=m["car"], car_class=m["car_class"],
                discipline=self.discipline, drivetrain=m["drivetrain"],
                started_iso=self.started_iso, best_lap_s=self.best_segment,
                baseline_lap_s=self._baseline_lap_s)
        except Exception:
            _laplog.exception("cumulative log append failed")
        self.log(f"Tune saved. Shareable files in {self.export['folder']}.")
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
                return self._g(5, "Test laps",
                               "Drive 2 laps after the restart: lap 1 is a warm-up (ignored), "
                               "lap 2 is timed and compared to your best.")
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
