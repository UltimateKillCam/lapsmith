"""The session loop: baseline -> test -> read -> one exact change -> repeat.

The human drives; this orchestrates capture, analysis and the fitness gate, and
prints exact values one lever group at a time. Fitness is a TOOL-SIDE segment
timer (the game's lap timer does not run in open free-roam).
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .telemetry.listener import TelemetryListener, wait_for_feed
from .telemetry.session import aggregate, TestStats, HIGH_G_THRESHOLD
from .telemetry import segment
from .knowledge.baseline import (build_baseline, format_checklist,
                                 canon_discipline, canon_class)
from .knowledge import rules
from .state.tune_state import Tune, TuneState, CarLimits
from .state import store
from .vision import read_tyres, read_tune, capture
from . import PRODUCT_NAME
from .units import format_speed, telemetry_unit_system

# minimum lateral load (g) that counts as a "real corner" for the Heat capture
LOAD_MIN_G = HIGH_G_THRESHOLD


@dataclass
class Config:
    discipline: str
    front_weight_pct: float
    car: Optional[str] = None          # None -> auto-detect from telemetry
    car_class: Optional[str] = None    # None -> auto-detect from PI
    drivetrain: Optional[str] = None   # None -> auto-detect from DrivetrainType
    port: int = 5607
    manual_vision: bool = False
    verify_tune: bool = False
    max_iters: int = 40
    skip_validation: bool = False
    limits: Optional[CarLimits] = None
    started_iso: str = ""
    telemetry_unit_system: str = "english"


# --- minimal console UX (rich if available) --------------------------------
class UI:
    def __init__(self):
        self._rich = None
        try:
            from rich.console import Console  # type: ignore
            self._rich = Console()
        except Exception:
            pass

    def rule(self, title: str):
        if self._rich:
            self._rich.rule(f"[bold]{title}")
        else:
            print("\n" + "=" * 70 + f"\n {title}\n" + "=" * 70)

    def say(self, msg: str = ""):
        if self._rich:
            self._rich.print(msg)
        else:
            print(msg)

    def panel(self, body: str, title: str = ""):
        if self._rich:
            from rich.panel import Panel  # type: ignore
            self._rich.print(Panel(body, title=title))
        else:
            print(("\n--- " + title + " ---\n") if title else "")
            print(body)

    def pause(self, msg: str = "Press ENTER when ready..."):
        try:
            input(f"\n>> {msg} ")
        except EOFError:
            pass

    def ask(self, msg: str) -> str:
        try:
            return input(f"\n>> {msg} ").strip()
        except EOFError:
            return ""


_TEST_HINTS = {
    "road": ("Find a quiet stretch with several hard left and right corners and one "
             "heavy-braking zone. Drive ~2-3 minutes hard, sustaining cornering load "
             "both directions, then stop."),
    "touge": ("Run a winding hill section: lots of tight linked corners, trail-braking "
              "into them. ~2-3 minutes, then stop."),
    "dirt": ("Find a dirt road/track with corners and bumps. Drive hard ~2-3 minutes, "
             "including some rough surface, then stop."),
    "cc": ("Drive open cross-country terrain with jumps and ruts, ~2-3 minutes, then stop."),
    "topspeed": ("Find the longest straight you can. Accelerate to top speed and hold it, "
                 "with a couple of high-speed sweepers, then stop."),
    "drag": ("Do 2-3 full standing-start pulls to top speed on a flat straight, then stop."),
}


# --- validation gate --------------------------------------------------------
def _probe_max(listener: TelemetryListener, ui: UI, instruction: str,
               extract: Callable) -> Optional[float]:
    """Have the user perform an action, capture the window, return max(extract)."""
    mark = listener.mark
    ui.pause(instruction)
    window = listener.drain_since(mark)
    vals = [extract(p) for p in window if p is not None]
    return max(vals) if vals else None


def _live_watch(listener: TelemetryListener, seconds: float = 6.0, fps: float = 4.0,
                unit_system: str = "english") -> None:
    """Refresh the sled block in place (\\r) for `seconds` so the human sees the
    numbers move as they drive, before being asked to confirm."""
    interval = 1.0 / fps
    deadline = time.time() + seconds
    while time.time() < deadline:
        s = listener.snapshot()
        if s:
            speed = format_speed(s.speed, unit_system, 1)
            line = (f"  Speed {speed:>10s} | RPM {s.current_engine_rpm:6.0f} | "
                    f"Gear {s.gear:2d} | Steer {s.steer:+4d} | Thr {s.accel:3d} | "
                    f"Brk {s.brake:3d} | {s.drivetrain_name:3s} | {s.packet_len}B")
            sys.stdout.write("\r" + line + "    ")
            sys.stdout.flush()
        time.sleep(interval)
    sys.stdout.write("\n")
    sys.stdout.flush()


@dataclass
class GateResult:
    passed: bool
    drivetrain: str = "?"          # read from a CONFIRMED-LIVE (moving) packet
    confirmed_packet: object = None


def _best_moving_packet(listener: TelemetryListener):
    """The highest-speed live packet in the ring buffer - a guaranteed non-zero,
    non-stale frame to read identity/drivetrain/temps from."""
    best = None
    for p in listener.drain_since(0):
        if p is None:
            continue
        if p.is_race_on or p.speed > 1.0:
            if best is None or p.speed > best.speed:
                best = p
    return best


def _looks_zero_default_temps(p) -> bool:
    """True if all four tyre temps are equal and near the 0F (-17.8C) default -
    i.e. a zeroed/stationary frame, not a real reading."""
    temps = [p.tire_temp_fl, p.tire_temp_fr, p.tire_temp_rl, p.tire_temp_rr]
    return (max(temps) - min(temps) < 0.5) and all(t < -10.0 for t in temps)


def _resolve_identity(cfg: Config, ui: UI, packet) -> None:
    """Fill car / class / drivetrain from a confirmed-live packet, leaving any
    value the user explicitly passed untouched. Defaults applied if nothing live."""
    from .identity import identify, is_live
    if is_live(packet):
        ident = identify(packet)
        ui.say(f"Detected: {ident.summary()}")
        if not cfg.car:
            cfg.car = ident.name
        if not cfg.car_class:
            cfg.car_class = ident.target_class
        if not cfg.drivetrain or cfg.drivetrain == "?":
            cfg.drivetrain = ident.drivetrain
    else:
        ui.say("No live frame to auto-detect from - using provided/default values.")
    cfg.car = cfg.car or "Car"
    cfg.car_class = cfg.car_class or "S1 800"
    cfg.drivetrain = cfg.drivetrain or "AWD"


def _validation_gate(listener: TelemetryListener, ui: UI, cfg: Config) -> "GateResult":
    """Confirm the parser is decoding correctly - INCLUDING fields downstream of
    the FH6 12-byte insert (tyre temp, throttle, brake, gear, steer, suspension).
    Speed/RPM alone live in the early sled block and prove nothing about those.
    Returns a GateResult carrying the drivetrain read from a confirmed-live frame.
    """
    ui.rule("VALIDATION GATE")
    ui.say("Confirming the packet decodes correctly before trusting any numbers.")
    ui.say("Start driving in free-roam so packets flow.")
    if not wait_for_feed(listener, timeout_s=30.0):
        ui.say(f"No telemetry on port {cfg.port}. Check Data Out ON, IP 127.0.0.1, "
               "Port matches, firewall allows Python. Cannot validate.")
        return GateResult(False)

    snap = listener.snapshot()
    if snap is None:
        return GateResult(False)

    # Report what datagram sizes are arriving (324B = normal live FH6).
    lengths = ", ".join(f"{L}B x{c}" for L, c in sorted(listener.observed_lengths.items()))
    ui.say(f"Datagrams received: {lengths or 'none'}   (324B = normal live FH6; "
           f"base {323}, +1 trailing byte)")
    if listener.short_count:
        ui.say(f"   ! {listener.short_count} datagram(s) shorter than 323B were dropped.")

    # 1. sled-block sanity (Speed/RPM vs HUD) - LIVE polling so values update.
    ui.say("Watch the live values update as you drive:")
    while True:
        _live_watch(listener, seconds=6.0, fps=4.0,
                    unit_system=telemetry_unit_system(cfg.telemetry_unit_system))
        ans = ui.ask("Do Speed / RPM / Gear track the HUD?  [y]es / [n]o / "
                     "[r]efresh to watch again").lower()
        if ans in ("y", "yes"):
            break
        if ans in ("n", "no"):
            ui.say("Sled offsets wrong - aborting.")
            return GateResult(False)
        # anything else (incl. 'r' / empty) -> watch another window

    # 2. POST-INSERT field: throttle byte (offset 315, downstream of the insert).
    #    This window is the car at speed under full throttle -> a confirmed-live
    #    frame we also read DrivetrainType from (bug: never trust a zero frame).
    a = _probe_max(listener, ui, "Drive at speed, hold FULL THROTTLE ~2s, then press ENTER.",
                   lambda p: p.accel)
    ui.say(f"   peak Accel byte = {a} (expect ~255)")
    if a is None or a < 230:
        ui.say("Throttle field did not read ~255 - post-insert offsets look wrong. Aborting.")
        return GateResult(False)
    confirmed = _best_moving_packet(listener)
    drivetrain = confirmed.drivetrain_name if confirmed else "?"
    if confirmed:
        ui.say(f"   confirmed-live drivetrain = {drivetrain} "
               f"(at {format_speed(confirmed.speed, cfg.telemetry_unit_system, 0)})")

    # 3. POST-INSERT field: brake byte (offset 316)
    b = _probe_max(listener, ui, "Hold FULL BRAKE for ~2s, then press ENTER.",
                   lambda p: p.brake)
    ui.say(f"   peak Brake byte = {b} (expect ~255)")
    if b is None or b < 230:
        ui.say("Brake field did not read ~255 - aborting.")
        return GateResult(False)

    # 4. TAIL field: steering range (offset 320, signed)
    smin = _probe_max(listener, ui,
                      "Swing the steering FULL LEFT then FULL RIGHT, then press ENTER.",
                      lambda p: abs(p.steer))
    ui.say(f"   peak |Steer| = {smin} (expect ~127)")
    if smin is None or smin < 100:
        ui.say("Steering field did not reach full range - aborting.")
        return GateResult(False)

    # 5. suspension travel must stay within [0,1]
    window = listener.drain_since(0)
    susp = [v for p in window for v in
            (p.susp_norm_fl, p.susp_norm_fr, p.susp_norm_rl, p.susp_norm_rr)]
    if susp and (min(susp) < -0.05 or max(susp) > 1.05):
        ui.say(f"   NormalizedSuspensionTravel out of [0,1] range "
               f"({min(susp):.2f}..{max(susp):.2f}) - offsets suspect. Aborting.")
        return GateResult(False)
    ui.say(f"   suspension travel within range "
           f"({min(susp):.2f}..{max(susp):.2f})" if susp else "   (no suspension samples)")

    # 6. POST-INSERT tyre temps (offset 268, F->C), sampled from a MOVING frame.
    #    Auto-flag the 0-default (all four equal near -18C / 0F) instead of asking
    #    the user to judge a zeroed/stationary instant.
    moving = _best_moving_packet(listener) or listener.snapshot()
    if moving:
        if _looks_zero_default_temps(moving):
            ui.say(f"   tyre temps read the 0-default ({moving.tire_temp_fl:.0f}C on all "
                   "four = raw 0F) - that's a zeroed/stationary frame, not an offset "
                   "error (throttle/brake/steer already validated the insert). They will "
                   "rise once the tyres load up; not blocking on it.")
        else:
            ui.say(f"   tyre temps C: FL {moving.tire_temp_fl:.0f} FR {moving.tire_temp_fr:.0f} "
                   f"RL {moving.tire_temp_rl:.0f} RR {moving.tire_temp_rr:.0f} (plausible)")

    ui.say("\nValidation PASSED - post-insert and tail fields decode correctly.")
    return GateResult(True, drivetrain=drivetrain, confirmed_packet=confirmed)


# --- segment timing ---------------------------------------------------------
def _time_segment(listener: TelemetryListener, ui: UI,
                  reference_distance: Optional[float]) -> Optional[segment.SegmentRun]:
    ui.say("Timed run (free-roam segment timer):")
    ui.pause("Drive to your segment START point, then press ENTER.")
    start = segment.grab_mark(listener)
    if start is None:
        ui.say("[timer] No telemetry at start - is Data Out flowing?")
        return None
    ui.pause("Now drive the segment. Press ENTER the instant you cross the END point.")
    end = segment.grab_mark(listener)
    if end is None:
        ui.say("[timer] No telemetry at end.")
        return None
    run = segment.measure(listener, start, end, reference_distance)
    if not run.valid:
        ui.say(f"[timer] {run.note}")
    else:
        ui.say(f"[timer] segment: {run.elapsed_s:.2f}s over {run.distance_m:.0f} m")
    return run


def _ask_float(ui: UI, prompt: str) -> Optional[float]:
    raw = ui.ask(prompt)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        ui.say("   (not a number - skipped)")
        return None


def _pair(ui: UI, lo_prompt: str, hi_prompt: str, warn: str):
    """Ask a min/max pair; return (lo, hi) or (None, None) if incomplete."""
    lo = _ask_float(ui, lo_prompt)
    hi = _ask_float(ui, hi_prompt)
    if lo is None or hi is None:
        if lo is not None or hi is not None:
            ui.say(f"   {warn}")
        return None, None
    return lo, hi


def gather_limits(ui: UI) -> CarLimits:
    """Read this car's adjustable ranges off the slider ends (ENTER to skip).
    Spring and aero ranges are asked PER AXLE because the front/rear sliders have
    different ranges on most cars."""
    ui.rule("CAR SLIDER RANGES")
    ui.say("So the tool never asks for a value the car can't reach, enter this car's")
    ui.say("RIDE HEIGHT, SPRING (front & rear) and AERO (front & rear) ranges. In the")
    ui.say("tune menu, slide each to BOTH ends and read the min/max. ENTER to skip any.")
    lim = CarLimits()
    lim.ride_height_min, lim.ride_height_max = _pair(
        ui, "Ride height MIN (cm):", "Ride height MAX (cm):",
        "ride-height range incomplete - using discipline default cm.")
    lim.spring_front_min, lim.spring_front_max = _pair(
        ui, "FRONT spring MIN (kgf/mm):", "FRONT spring MAX (kgf/mm):",
        "front spring range incomplete - using default scaling.")
    lim.spring_rear_min, lim.spring_rear_max = _pair(
        ui, "REAR spring MIN (kgf/mm):", "REAR spring MAX (kgf/mm):",
        "rear spring range incomplete - using default scaling.")
    lim.aero_front_min, lim.aero_front_max = _pair(
        ui, "FRONT aero MIN:", "FRONT aero MAX:",
        "front aero range incomplete - aero left at stock/guidance.")
    lim.aero_rear_min, lim.aero_rear_max = _pair(
        ui, "REAR aero MIN:", "REAR aero MAX:",
        "rear aero range incomplete - aero left at stock/guidance.")
    return lim


def _drive_test_with_heat(listener: TelemetryListener, ui: UI, tag: int,
                          manual_vision: bool):
    """Drive the characterisation test with the Heat page visible; screenshot the
    highest-lateral-load frame for OCR. Returns (window, best_path, peak_g)."""
    ui.say(">> Open the in-game tyre-temperature (Heat) page NOW and KEEP IT "
           "VISIBLE while you drive this test.")
    mark = listener.mark
    do_capture = (not manual_vision) and capture.backend_available()
    best = {"g": 0.0, "path": None}
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            s = listener.snapshot()
            if s is not None:
                g = abs(s.lateral_g)
                if g >= LOAD_MIN_G and g > best["g"] + 0.03:
                    try:
                        best["path"] = capture.grab("tyre_temps", monotonic_tag=tag)
                        best["g"] = g
                    except Exception:
                        pass
            time.sleep(0.05)

    th = threading.Thread(target=worker, daemon=True) if do_capture else None
    if th:
        th.start()
        ui.say("   (capturing the Heat page at peak cornering load automatically...)")
    ui.pause("Drive the test with the Heat page up. Press ENTER the moment you stop.")
    stop.set()
    if th:
        th.join(timeout=1.5)
    window = listener.drain_since(mark)
    return window, best["path"], best["g"]


def run(cfg: Config, ui: Optional[UI] = None) -> str:
    ui = ui or UI()
    disc = canon_discipline(cfg.discipline)

    ui.rule(f"{PRODUCT_NAME}  -  {cfg.car or '(auto-detect)'}")
    ui.say(f"{disc} | front weight {cfg.front_weight_pct:.0f}%  "
           "(car/class/drivetrain detected from telemetry)")
    ui.say(f"Expected in-game Data Out: IP 127.0.0.1, Port {cfg.port} "
           "(Settings -> HUD and Gameplay -> Data Out).")

    listener = TelemetryListener(port=cfg.port)
    listener.start()
    try:
        if not cfg.skip_validation:
            gate = _validation_gate(listener, ui, cfg)
            if not gate.passed:
                ui.panel(
                    "VALIDATION FAILED - the packet offsets do not match this game build.\n\n"
                    "Do NOT trust any value this tool would emit: with wrong offsets the\n"
                    "analyzer reads garbage and would tell you to set nonsense numbers.\n\n"
                    "What to do:\n"
                    "  1. STOP. Apply nothing.\n"
                    "  2. Re-check Data Out is ON, IP 127.0.0.1, Port matches --port,\n"
                    "     and the firewall allows Python.\n"
                    "  3. If Speed/RPM matched but a post-insert field (throttle, brake,\n"
                    "     steer, tyre temp) did not, the FH6 layout has shifted - re-derive\n"
                    "     byte offsets against github.com/TheBanHammer/fh6-tel\n"
                    "     (src-tauri/src/parser.rs) and the official FH6 Data Out doc,\n"
                    "     update lapsmith/telemetry/parser.py, then re-run.\n"
                    "  4. Re-run this command - the gate must PASS before tuning starts.\n\n"
                    "(--skip-validation bypasses this, but ONLY for simulator dry runs.)",
                    "STOP - offsets do not match")
                return "aborted_validation"
            # Identity (car / class / drivetrain) from the CONFIRMED-LIVE frame -
            # never a zero/stale snapshot (that defaulted AWD cars to FWD).
            _resolve_identity(cfg, ui, gate.confirmed_packet)
        else:
            _resolve_identity(cfg, ui, listener.snapshot())

        cls = canon_class(cfg.car_class)

        # --- per-car ranges (so nothing exceeds the car's sliders) ---
        limits = cfg.limits if cfg.limits is not None else gather_limits(ui)
        cfg.limits = limits

        # --- baseline (range-relative + clamped to the car) ---
        baseline = build_baseline(cfg.car, cls, disc, cfg.front_weight_pct,
                                  cfg.drivetrain, limits=limits)
        state = TuneState(baseline.copy())
        ui.panel(format_checklist(baseline, cfg.car, cls, disc, cfg.front_weight_pct,
                                  cfg.drivetrain), "INITIAL TUNE")
        if limits.bounds("ride_height_f"):
            ui.say(f"(ride height set within this car's {limits.ride_height_min:g}-"
                   f"{limits.ride_height_max:g} cm range)")
        ui.pause("Apply ALL values above in-game, then press ENTER.")

        # establish the reference segment time + distance on the baseline tune
        ui.say("\nFirst, set a BASELINE time on your segment so changes can be judged.")
        ref_run = _time_segment(listener, ui, reference_distance=None)
        best_segment: Optional[float] = ref_run.elapsed_s if (ref_run and ref_run.valid) else None
        ref_distance: Optional[float] = ref_run.distance_m if (ref_run and ref_run.valid) else None
        if best_segment is None:
            ui.say("[timer] No valid baseline segment - the fitness gate will keep changes "
                   "on mechanical evidence until a valid segment is set.")

        stats_log: List[dict] = []
        stale = 0
        capture_tag = 0
        ride_locked: set = set()              # axles where raising ride stopped helping
        last_ride_change = None               # {"axle":..,"susp_before":..} from prior iter
        RIDE_IMPROVE_MARGIN = 0.02            # min susp-travel gain to call a raise "useful"

        while state.iteration < cfg.max_iters:
            state.iteration += 1
            ui.rule(f"Iteration {state.iteration}")

            # --- characterisation drive WITH the Heat page up; capture at load ---
            capture_tag += 1
            ui.say(_TEST_HINTS.get(disc, _TEST_HINTS["road"]))
            window, heat_path, peak_g = _drive_test_with_heat(
                listener, ui, capture_tag, cfg.manual_vision)
            stats = aggregate(window)
            ui.say(f"[captured {stats.n_packets} packets over ~{stats.duration_s:.0f}s, "
                   f"{stats.n_corner_frames} cornering frames, peak {stats.max_lateral_g:.2f} g]")
            if stats.n_packets < 30:
                ui.say("Very little telemetry captured - make sure Data Out is flowing and "
                       "drive a bit longer.")

            # --- tyre-temp reading (OCR of the peak-load Heat frame; normalized to C) ---
            if heat_path and peak_g >= LOAD_MIN_G:
                ui.say(f"[heat] read at peak cornering load ({peak_g:.2f} g).")
                tyre_reading = read_tyres.read(manual=cfg.manual_vision,
                                               image_path=heat_path, announce=ui.say)
            else:
                ui.say("[heat] No real cornering load seen this run - temps even out when "
                       "coasting/stopped, so the camber reading may be NON-DIAGNOSTIC. "
                       "Capturing current Heat page anyway.")
                tyre_reading = read_tyres.read(manual=cfg.manual_vision, tag=capture_tag,
                                               announce=ui.say)

            # --- no-progress check: did the last ride-height RAISE help? ---
            if last_ride_change is not None:
                axle = last_ride_change["axle"]
                before = last_ride_change["susp_before"]
                now = stats.susp_min_front if axle == "front" else stats.susp_min_rear
                if now <= before + RIDE_IMPROVE_MARGIN and axle not in ride_locked:
                    ride_locked.add(axle)
                    ui.say(f"[ride] raising {axle} ride height stopped reducing bottoming "
                           f"(min travel {before:.2f} -> {now:.2f}); locking ride height on "
                           "that axle and switching to bump/spring.")
                last_ride_change = None

            # --- analyze -> one change ---
            rec = rules.analyze(stats, state.current, disc, tyre_reading,
                                converged=state.converged_levers, limits=limits,
                                ride_locked=ride_locked)

            # gearing was left at STOCK; if telemetry says it should move, ask the
            # user for the CURRENT ratio (only they can read it) then re-analyze so
            # the rule can emit an exact value instead of a blind constant.
            if (not rec.is_change() and state.current.final_drive == rules.STOCK
                    and "gearing" not in state.converged_levers
                    and rules.gearing_wants_change(stats)):
                raw = ui.ask("Gearing needs tuning. Read the CURRENT Final Drive ratio "
                             "from the menu and enter it (e.g. 3.45):")
                try:
                    state.current.final_drive = float(raw)
                    rec = rules.analyze(stats, state.current, disc, tyre_reading,
                                        converged=state.converged_levers, limits=limits,
                                        ride_locked=ride_locked)
                except ValueError:
                    ui.say("   (not a number - skipping gearing this round)")

            stats_log.append({"iteration": state.iteration, "stats": stats.as_dict(),
                              "recommendation": rec.group})

            if not rec.is_change():
                if stale >= 2 or best_segment is None:
                    ui.panel(rec.reason, "CONVERGED")
                    break
                ui.say("No mechanical rule fired; confirming with one more timed run.")
                stale += 1
                continue

            # --- HARD clamp every emitted value to the car's range (defense in
            #     depth: no iteration change may exceed a slider, ever) ---
            for k in list(rec.fields):
                cv, was_clamped, msg = limits.clamp(k, rec.fields[k])
                if was_clamped:
                    ui.say(f"   [clamp] {msg}")
                rec.fields[k] = cv
            # drop any field that clamped back to the current value (no real move)
            rec.fields = {k: v for k, v in rec.fields.items()
                          if abs(v - state.current.get(k)) > 1e-9}
            if not rec.fields:
                ui.say("   (change clamped to a no-op; lever already at its limit)")
                state.mark_converged(rec.group)
                continue

            ui.panel(
                f"CHANGE [{rec.group}]\n\n{rec.detail}\n\n"
                f"Why : {rec.reason}\n"
                f"Feel: {rec.feel_for}",
                "NEXT CHANGE - exact values")

            # record a ride-height RAISE so next iteration can check it helped
            if rec.group == "ride_height":
                axle = "front" if "ride_height_f" in rec.fields else "rear"
                last_ride_change = {"axle": axle,
                                    "susp_before": (stats.susp_min_front if axle == "front"
                                                    else stats.susp_min_rear)}

            applied = state.apply_change(rec.group, rec.fields, rec.reason, rec.feel_for)
            applied.seg_before_s = best_segment

            # --- optional tune-sheet verification ---
            if cfg.verify_tune:
                seen = read_tune.read(expect=rec.fields, manual=cfg.manual_vision,
                                      tag=capture_tag, announce=ui.say)
                for k, target in rec.fields.items():
                    if k in seen and abs(seen[k] - target) > 0.01:
                        ui.say(f"   ! tune sheet shows {k}={seen[k]} but asked {target} - "
                               "re-enter it before timing.")

            # --- fitness gate: tool-side segment timer ---
            ui.say("Apply this ONE change, then time the SAME segment again.")
            run_after = _time_segment(listener, ui, reference_distance=ref_distance)
            after = run_after.elapsed_s if (run_after and run_after.valid) else None
            applied.seg_after_s = after
            applied.seg_distance_m = run_after.distance_m if run_after else None

            verdict, best_segment = _fitness_gate(state, ui, best_segment, after)
            if verdict == "reverted":
                state.mark_converged(rec.group)
                stale += 1
            elif after is not None:
                stale = stale + 1 if (best_segment is not None and after >= best_segment - 1e-3) else 0
            else:
                stale = 0

        status = "converged" if state.iteration < cfg.max_iters else "max_iters"
        return _finish(cfg, state, baseline, stats_log, ui, status)
    finally:
        listener.stop()


def _fitness_gate(state: TuneState, ui: UI, best: Optional[float],
                  after: Optional[float]) -> tuple[str, Optional[float]]:
    if after is None:
        state.keep_last()
        ui.say("[fitness] No valid segment time - keeping on mechanical evidence. "
               "Set a clean segment to enable keep/revert.")
        return "kept", best
    if best is None:
        state.keep_last()
        ui.say(f"[fitness] First valid segment {after:.2f}s - kept as new reference.")
        return "kept", after
    delta = after - best
    if delta > rules.SEGMENT_REGRESS_S:
        rec = state.revert_last()
        ui.say(f"[fitness] Segment REGRESSED {delta:+.2f}s ({best:.2f} -> {after:.2f}). "
               f"Reverting {list(rec.fields)} and locking this lever.")
        ui.say("   >> Set this lever back to its previous value in-game.")
        return "reverted", best
    state.keep_last()
    new_best = min(best, after)
    ui.say(f"[fitness] Segment {('improved' if delta < 0 else 'held')} "
           f"{delta:+.2f}s ({best:.2f} -> {after:.2f}). Keeping the change.")
    return "kept", new_best


def _finish(cfg: Config, state: TuneState, baseline: Tune, stats_log: list,
            ui: UI, status: str) -> str:
    ui.rule("SESSION COMPLETE")
    diff = state.diff_from_baseline(baseline)
    if diff:
        ui.say("Changes from baseline:")
        for k, (old, new) in diff.items():
            ui.say(f"   {k}: {old} -> {new}")
    else:
        ui.say("Baseline already converged - no changes were kept.")

    sp = store.save_session(state, car=cfg.car, car_class=cfg.car_class,
                            discipline=cfg.discipline, front_weight_pct=cfg.front_weight_pct,
                            drivetrain=cfg.drivetrain, baseline=baseline,
                            stats_log=stats_log, started_iso=cfg.started_iso, status=status,
                            limits=cfg.limits)
    fp = store.save_final_tune_txt(state.current, car=cfg.car, car_class=cfg.car_class,
                                   discipline=cfg.discipline,
                                   front_weight_pct=cfg.front_weight_pct,
                                   drivetrain=cfg.drivetrain)
    ui.say(f"\nSaved session : {sp}")
    ui.say(f"Saved tune    : {fp}")
    ui.panel(format_checklist(state.current, cfg.car, cfg.car_class, cfg.discipline,
                              cfg.front_weight_pct, cfg.drivetrain), "FINAL TUNE")
    return status
