"""The analyzer: TestStats + tyre-temp reading + current Tune -> ONE exact change.

Rules run in the brief's tuning order (pressure -> ride/springs -> camber ->
ARBs -> damping -> diff -> gearing -> aero). Only the FIRST group that needs a
change is emitted, so cause and effect stay clean across iterations.

Every threshold and step size is a module-level constant so they can be refined
after a few real sessions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, List

from ..telemetry.session import TestStats
from ..state.tune_state import Tune, CarLimits, STOCK

# === tunable constants ======================================================
# A. pressure
TEMP_BAL_C = 6.0          # axle-mate temp delta that triggers a pressure drop
PRESSURE_STEP = 0.5       # psi
PRESSURE_MIN, PRESSURE_MAX = 25.0, 34.0     # road band
GREASY_COMBINED_SLIP = 1.0   # combined slip above this on a hot axle = greasy
COLD_COMBINED_SLIP = 0.3
# B. ride height / springs / bump
BOTTOM_THRESH = 0.05      # normalized susp travel <= this = bottoming (road/tarmac)
# Dirt/CC cars sit and travel low; some bottoming is expected and acceptable, so a
# LOWER threshold there means we only chase genuinely severe bottoming, not forever.
BOTTOM_THRESH_DIRT = 0.02
STIFF_THRESH = 0.55       # never drops below this = under-using travel
RIDE_STEP = 1.5           # cm  (was 1.0 - too timid; convergence was glacial)
BUMP_STEP = 2.0           # was 1.0
BUMP_SOFTEN_STEP = 1.0    # was 0.5
BUMP_CAP = 20.0           # stiffest bump we'll dial in before escalating to springs
# Anti-fixation: max consecutive iterations the loop may spend on ONE axle's
# bottoming remedy before locking it, accepting the residual, and moving on.
BOTTOMING_CAP = 2
# change aggressiveness -> step multiplier for the iterative search levers
STEP_MULT = {"fine": 0.5, "normal": 1.0, "coarse": 2.0}
# C. camber (from tyre-temp screenshot inner/mid/outer)
CAMBER_C = 5.0            # inner-outer delta (deg C) tolerance band
CAMBER_STEP = 0.2         # deg
# D. balance / ARBs
US_DEG = 1.5             # front slip-angle exceeds rear by this = understeer
OS_DEG = 1.5
ARB_STIFFEN_STEP = 5.0
ARB_SOFTEN_STEP = 3.0
ARB_MAX = 65.0
ARB_MIN = 1.0
ARB_REAR_SOFT_FLOOR = 60.0   # if rear already >= this, adjust front instead
# E. diff
DIFF_STEP = 5.0
DIFF_CENTER_MAX = 90.0
# On-throttle slip-ratio triggers are TARMAC values. On dirt/CC that wheelspin is
# WANTED, so the thresholds are discipline-keyed (much higher off-road) the same
# way BOTTOM_THRESH is - normal dirt slip (0.4-0.5) must NOT read as a fault.
ON_POWER_OS_SLIP = 0.30        # road: on-throttle rear slip ratio -> power oversteer
ON_POWER_OS_SLIP_DIRT = 0.60   # dirt/CC: only flag well past normal wheelspin
EXIT_US_FRONT_SLIP = 0.25      # road: on-throttle front slip ratio (AWD push)
EXIT_US_FRONT_SLIP_DIRT = 0.50
ENTRY_INSTAB_SLIP = 0.30       # road: braking rear slip ratio (entry instability)
ENTRY_INSTAB_SLIP_DIRT = 0.55
# Driveability floor: on dirt the lap-time gate can't see that an open rear diff
# kills drive/powerslide, so never let the accel diff fall below this % off-road.
DIRT_ACCEL_DIFF_FLOOR = 50.0
# F. gearing
FINAL_DRIVE_STEP = 0.10
LOW_EXIT_RPM_FRAC = 0.70
# G. aero
AERO_STEP_FRAC = 0.10     # 10% of range
AERO_RANGE = 1000.0       # assumed menu range for the 10% step
# fitness gate (lap/segment time, seconds)
SEGMENT_REGRESS_S = 0.2   # regress past this (noise-aware) -> revert + lock the lever
LAP_REGRESS_S = SEGMENT_REGRESS_S         # alias: the regression threshold
LAP_IMPROVE_EPS = 0.10    # must beat the best lap by THIS (above noise) to count as
                          # a real improvement; smaller deltas are NEUTRAL (reverted)
# Anti-fixation: max consecutive NON-IMPROVING attempts on ONE lever (field +
# direction) before it is locked and rolled back to its last improving value.
LEVER_NOIMPROVE_CAP = 2

# --- drift-robust methodology (a single, driver-confounded lap is too weak) -----
LAPS_PER_TEST = 2              # clean green laps aggregated into ONE measurement
WARMUP_LAPS = 2               # cold/learning laps before the first baseline anchor
BASELINE_REANCHOR_EVERY = 3   # re-measure the accepted tune every N iterations
# wall-clock session budget (real minutes from the first Rivals lap; 0 = unlimited)
DEFAULT_TIME_BUDGET_MIN = 20

# road-discipline disciplines for which the road pressure band applies
_ROAD_LIKE = {"road", "touge", "topspeed", "drag"}
_DIRT_LIKE = {"dirt", "cc"}


def _diff_slip_thresh(disc: str, road_val: float, dirt_val: float) -> float:
    """Discipline-keyed slip-ratio threshold (dirt/CC tolerate far more wheelspin)."""
    return dirt_val if disc in _DIRT_LIKE else road_val


@dataclass
class Recommendation:
    group: str
    fields: Dict[str, float]      # lever -> new absolute value
    reason: str
    feel_for: str
    detail: str = ""
    kind: str = "search"          # "evidence" (own measurement) | "search" (lap-time tuned)
    symptom: str = ""             # e.g. "bottoming_front" - lets the loop cap fixation

    def is_change(self) -> bool:
        return bool(self.fields)


CONVERGED = Recommendation(group="(converged)", fields={},
                           reason="No rule fired - tyres even, no bottoming, "
                                  "balance within bands.", feel_for="")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def lever_key(field: str, old: float, new: float) -> str:
    """Identity of a tuning move for the anti-fixation cap: the lever FIELD plus the
    DIRECTION of change, e.g. 'diff_rear_accel:down'. Locking this key stops the
    loop re-firing that exact lever in that direction."""
    return f"{field}:{'down' if new < old else 'up'}"


def _filter_locked_levers(rec: "Recommendation", tune: Tune,
                          lever_locked) -> Optional["Recommendation"]:
    """Drop fields whose (field, direction) move is LOCKED (capped by the gate); if
    every field is locked the rule produced nothing usable -> None (so it does NOT
    count as 'a rule fired' and the loop can converge)."""
    if not lever_locked:
        return rec
    kept = {f: v for f, v in rec.fields.items()
            if lever_key(f, tune.get(f), v) not in lever_locked}
    if not kept:
        return None
    rec.fields = kept
    return rec


def _round(v: float, q: float = 0.1) -> float:
    return round(round(v / q) * q, 3)


def analyze(stats: TestStats, tune: Tune, discipline: str,
            tyre_reading: Optional[Dict[str, Dict[str, float]]] = None,
            converged: Optional[set] = None,
            limits: Optional[CarLimits] = None,
            ride_locked: Optional[set] = None,
            lever_locked: Optional[set] = None) -> Recommendation:
    """Return the single next change, or CONVERGED if nothing fires.

    `tyre_reading` is the 3-zone tyre-temp page read (per tyre inner/mid/outer
    in Celsius); required for camber (rule C). `converged` is the set of lever
    groups already locked by the fitness gate - those are skipped. `limits` clamps
    every emitted value to the car's achievable range; a change that clamps to a
    no-op is skipped so the loop can move to another lever. `ride_locked` is the
    set of axles ("front"/"rear") where raising ride height has stopped helping -
    the bottoming fix skips the ride-height step and goes straight to bump/spring.
    """
    converged = converged or set()
    limits = limits or CarLimits()
    ride_locked = ride_locked or set()
    disc = discipline
    road_band = disc in _ROAD_LIKE

    for rule in (_rule_pressure, _rule_ride, _rule_camber, _rule_diff,
                 _rule_gearing, _rule_aero, _rule_arb, _rule_damping,
                 _rule_camber_search):
        if rule is _rule_ride:
            rec = _rule_ride(stats, tune, disc, tyre_reading, road_band, limits,
                             ride_locked)
        else:
            rec = rule(stats, tune, disc, tyre_reading, road_band, limits)
        if not rec or not rec.is_change() or rec.group in converged:
            continue
        rec = _apply_limits(rec, tune, limits)
        if rec is None:
            continue
        rec = _filter_locked_levers(rec, tune, lever_locked)
        if rec is not None:
            return rec
    return CONVERGED


# --- aggressiveness ----------------------------------------------------------
def step_mult_for(aggressiveness: str) -> float:
    """fine/normal/coarse -> step multiplier for the iterative search levers."""
    return STEP_MULT.get(aggressiveness, 1.0)


# EVIDENCE-driven groups each have their OWN measurement justifying them, so they
# can be applied together and confirmed with a single lap. SEARCH-driven groups
# (the handling cluster: ARBs, damping, fine springs) are tuned only by lap time
# and interact strongly, so they're rate-limited per lap.
_EVIDENCE_GROUPS = {"pressure", "ride_height", "camber", "diff", "gearing", "aero",
                    "brakes", "alignment", "springs"}
_SEARCH_GROUPS = {"arb", "damping_bump", "damping", "camber_search"}


def _kind_of(group: str) -> str:
    return "evidence" if group in _EVIDENCE_GROUPS else "search"


def analyze_batch(stats: TestStats, tune: Tune, discipline: str,
                  tyre_reading: Optional[Dict[str, Dict[str, float]]] = None,
                  converged: Optional[set] = None,
                  limits: Optional[CarLimits] = None,
                  ride_locked: Optional[set] = None,
                  max_search: int = 1,
                  bottoming_locked: Optional[set] = None,
                  step_mult: float = 1.0,
                  bottoming_attempts: Optional[dict] = None,
                  lever_locked: Optional[set] = None) -> List[Recommendation]:
    """Build a BATCH for one test lap: ALL firing evidence-driven changes (each
    justified by its own measurement) plus up to `max_search` search-driven
    (handling-cluster) changes. Returns [] if nothing fires.

    `bottoming_locked` axles are skipped by the ride rule (anti-fixation cap / gate
    revert); `step_mult` scales the iterative search-lever steps (aggressiveness);
    `bottoming_attempts` per-axle escalates the bottoming step on repeats.

    Evaluation runs against a working copy so two rules can't both grab the same
    lever (e.g. ride-height bottoming bump vs damping-softening bump)."""
    converged = converged or set()
    limits = limits or CarLimits()
    ride_locked = ride_locked or set()
    road_band = discipline in _ROAD_LIKE
    work = tune.copy()
    evidence: List[Recommendation] = []
    search: List[Recommendation] = []

    for rule in (_rule_pressure, _rule_ride, _rule_camber, _rule_diff,
                 _rule_gearing, _rule_aero, _rule_arb, _rule_damping,
                 _rule_camber_search):
        if rule is _rule_ride:
            rec = _rule_ride(stats, work, discipline, tyre_reading, road_band, limits,
                             ride_locked, bottoming_locked, step_mult, bottoming_attempts)
        elif rule is _rule_camber_search:
            rec = _rule_camber_search(stats, work, discipline, tyre_reading, road_band,
                                      limits, step_mult)
        else:
            rec = rule(stats, work, discipline, tyre_reading, road_band, limits)
        if not rec or not rec.is_change() or rec.group in converged:
            continue
        rec = _apply_limits(rec, work, limits)
        if rec is None:
            continue
        rec = _filter_locked_levers(rec, work, lever_locked)
        if rec is None:
            continue                        # all fields locked -> not "a rule fired"
        rec.kind = _kind_of(rec.group)
        for k, v in rec.fields.items():     # accumulate so later rules see it
            work.set(k, v)
        (evidence if rec.kind == "evidence" else search).append(rec)

    return evidence + search[:max(0, max_search)]


def _apply_limits(rec: Recommendation, tune: Tune, limits: CarLimits) -> Optional[Recommendation]:
    """Clamp each emitted field to the car's range. Drop fields that clamp to the
    current value (already pinned); return None if nothing actually moves."""
    new_fields: Dict[str, float] = {}
    notes = []
    for lever, value in rec.fields.items():
        clamped, was_clamped, msg = limits.clamp(lever, value)
        if abs(clamped - tune.get(lever)) < 1e-9:
            # clamping pinned it to where it already is - no real change
            notes.append(msg or f"{lever} already at its limit")
            continue
        new_fields[lever] = clamped
        if was_clamped:
            notes.append(msg)
    if not new_fields:
        return None
    if notes:
        rec.detail = (rec.detail + "  [" + "; ".join(notes) + "]").strip()
    rec.fields = new_fields
    return rec


# --- A. pressure ------------------------------------------------------------
def _rule_pressure(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    lo, hi = (PRESSURE_MIN, PRESSURE_MAX) if road_band else (12.0, 36.0)
    bal = stats.axle_temp_balance()

    # left/right imbalance on an axle -> drop the hotter tyre (single shared
    # pressure per axle in FH, so we drop that axle's pressure)
    if abs(bal["front_lr_delta"]) > TEMP_BAL_C:
        new = _clamp(tune.pressure_f - PRESSURE_STEP, lo, hi)
        if new != tune.pressure_f:
            hotter = "FL" if bal["front_lr_delta"] > 0 else "FR"
            return Recommendation(
                "pressure", {"pressure_f": _round(new)},
                f"Front {hotter} runs {abs(bal['front_lr_delta']):.1f} C hotter "
                f"than its mate (> {TEMP_BAL_C} C).",
                "More even front grip L-vs-R; less mid-corner front push to one side.",
                f"Front pressure {tune.pressure_f:.1f} -> {new:.1f} psi")
    if abs(bal["rear_lr_delta"]) > TEMP_BAL_C:
        new = _clamp(tune.pressure_r - PRESSURE_STEP, lo, hi)
        if new != tune.pressure_r:
            hotter = "RL" if bal["rear_lr_delta"] > 0 else "RR"
            return Recommendation(
                "pressure", {"pressure_r": _round(new)},
                f"Rear {hotter} runs {abs(bal['rear_lr_delta']):.1f} C hotter than its mate.",
                "More even rear grip L-vs-R; steadier exits.",
                f"Rear pressure {tune.pressure_r:.1f} -> {new:.1f} psi")

    # whole-axle hot+greasy -> drop; cold+low-grip -> raise
    fr = _axle_pressure_grip(stats.combined_slip_front, bal["front_avg"], tune.pressure_f, lo, hi)
    if fr is not None:
        new, why, feel = fr
        return Recommendation("pressure", {"pressure_f": _round(new)}, why, feel,
                              f"Front pressure {tune.pressure_f:.1f} -> {new:.1f} psi")
    rr = _axle_pressure_grip(stats.combined_slip_rear, bal["rear_avg"], tune.pressure_r, lo, hi)
    if rr is not None:
        new, why, feel = rr
        return Recommendation("pressure", {"pressure_r": _round(new)}, why, feel,
                              f"Rear pressure {tune.pressure_r:.1f} -> {new:.1f} psi")
    return None


def _axle_pressure_grip(combined_slip, avg_temp, cur, lo, hi):
    # "hot" judged relative to a target window; combined slip flags greasy/cold.
    if combined_slip > GREASY_COMBINED_SLIP and avg_temp > 90.0:
        new = _clamp(cur - PRESSURE_STEP, lo, hi)
        if new != cur:
            return new, (f"Axle hot ({avg_temp:.0f} C) and greasy "
                         f"(combined slip {combined_slip:.2f} > {GREASY_COMBINED_SLIP})."), \
                   "Less overheating, more consistent grip over the run."
    if combined_slip < COLD_COMBINED_SLIP and avg_temp < 70.0:
        new = _clamp(cur + PRESSURE_STEP, lo, hi)
        if new != cur:
            return new, (f"Axle cold ({avg_temp:.0f} C) and low slip "
                         f"({combined_slip:.2f}) - tyre not working."), \
                   "Tyre comes up to temp; sharper response."
    return None


# --- B. ride height / springs / bump ---------------------------------------
def _bottom_thresh(disc: str) -> float:
    """Bottoming threshold by discipline. Dirt/CC cars run low and bottom a little
    by design, so a lower threshold means we don't chase acceptable bottoming."""
    return BOTTOM_THRESH_DIRT if disc in ("dirt", "cc") else BOTTOM_THRESH


def _rule_ride(stats, tune, disc, tyre_reading, road_band, limits,
               ride_locked=None, bottoming_locked=None, step_mult=1.0,
               bottoming_attempts=None) -> Optional[Recommendation]:
    ride_locked = ride_locked or set()
    bottoming_locked = bottoming_locked or set()
    bottoming_attempts = bottoming_attempts or {}
    thresh = _bottom_thresh(disc)
    # Is the car understeering on this run? Stiffening FRONT bump would worsen the
    # push, so the bottoming remedy avoids it and prefers ride height / spring.
    understeer = (stats.n_corner_frames >= 10
                  and (stats.slip_angle_front - stats.slip_angle_rear) > US_DEG)
    for axle in ("front", "rear"):
        if axle in bottoming_locked:
            continue                       # cap hit / gate reverted - leave it be
        susp_min = stats.susp_min_front if axle == "front" else stats.susp_min_rear
        if susp_min <= thresh:
            return _bottoming_fix(axle, susp_min, tune, disc, limits,
                                  ride_ineffective=(axle in ride_locked),
                                  understeer=understeer,
                                  attempts=bottoming_attempts.get(axle, 0),
                                  step_mult=step_mult, thresh=thresh)
    return None


def _bottoming_fix(axle, susp_min, tune, disc, limits, ride_ineffective=False,
                   understeer=False, attempts=0, step_mult=1.0, thresh=BOTTOM_THRESH
                   ) -> Optional[Recommendation]:
    """Escalate one capped lever at a time: ride height -> bump -> spring. Skip the
    ride-height step when it's at the CAR's max OR stopped reducing bottoming
    (`ride_ineffective`). BALANCE-AWARE: with understeer present, avoid stiffening
    FRONT bump (it adds push) and prefer the spring. Steps scale with the
    aggressiveness multiplier and escalate with repeated attempts on the axle."""
    label = axle.capitalize()
    rh = f"ride_height_{axle[0]}"     # ride_height_f / ride_height_r
    bp = f"bump_{axle[0]}"
    sp = f"spring_{axle[0]}"
    rh_max = _ride_cap(disc, limits, rh)
    symptom = f"bottoming_{axle}"
    mult = step_mult * (1 + max(0, attempts))     # escalate per repeat on this axle

    can_raise = (tune.get(rh) < rh_max - 1e-6 and not limits.at_max(rh, tune.get(rh))
                 and not ride_ineffective)
    if can_raise:
        new = _round(tune.get(rh) + RIDE_STEP * mult)
        return Recommendation("ride_height", {rh: new},
            f"{label} bottoming: min normalized travel {susp_min:.2f} <= {thresh}.",
            f"No more harsh {label.lower()} crashes over bumps; steadier end.",
            f"{label} ride height {tune.get(rh):.1f} -> {new:.1f} cm", symptom=symptom)

    # ride pinned. Normally stiffen bump next - BUT with front understeer, front bump
    # would add push, so skip it and go to the spring instead.
    avoid_front_bump = (axle == "front" and understeer)
    if (not avoid_front_bump
            and tune.get(bp) < BUMP_CAP - 1e-6 and not limits.at_max(bp, tune.get(bp))):
        why = ("raising ride height stopped reducing bottoming" if ride_ineffective
               else f"ride height already at car max ({tune.get(rh):.1f} cm)")
        new = _round(min(BUMP_CAP, tune.get(bp) + BUMP_STEP * mult))
        rec = Recommendation("damping_bump", {bp: new},
            f"{label} bottoming and {why} - stiffening bump instead of raising ride.",
            f"{label} rides over compressions without packing onto the bump stops.",
            f"{label} bump {tune.get(bp):.1f} -> {new:.1f}", symptom=symptom)
        if understeer:    # rear-axle bump touched while the front pushes: note it
            rec.detail = (rec.detail + "  [balance: stiffer bump can add understeer - "
                          "the lap-time gate reverts it if it hurts]").strip()
        return rec

    # bump maxed (or front bump avoided for balance) -> stiffen the spring
    new = _round(tune.get(sp) + _spring_step(tune.get(sp)) * mult)
    reason = (f"{label} bottoming; avoiding front bump (would add understeer) - "
              "stiffening the spring instead."
              if avoid_front_bump else
              f"{label} bottoming with ride height AND bump at their caps - stiffening spring.")
    return Recommendation("springs", {sp: new}, reason,
        f"{label} resists compression so it stops bottoming.",
        f"{label} spring {tune.get(sp):.1f} -> {new:.1f} kgf/mm", symptom=symptom)


def _spring_step(cur: float) -> float:
    # ~8% stiffer per step, minimum 1.0 kgf/mm
    return max(1.0, round(cur * 0.08, 1))


def _ride_cap(disc: str, limits, lever: str) -> float:
    """Effective ride-height max: the car's slider max if known, else a
    discipline-sensible default in cm."""
    b = limits.bounds(lever)
    if b:
        return b[1]
    return {"road": 8.0, "touge": 11.0, "topspeed": 8.0, "drag": 10.0,
            "dirt": 25.0, "cc": 55.0}.get(disc, 8.0)


# --- C. camber (screenshot inner/mid/outer) --------------------------------
def _rule_camber(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    if not tyre_reading:
        return None  # need the 3-zone screenshot; handled by main loop prompt
    fa = _axle_inner_outer(tyre_reading, ("FL", "FR"))
    ra = _axle_inner_outer(tyre_reading, ("RL", "RR"))
    if fa is not None:
        rec = _camber_for_axle(fa, tune.camber_f, "camber_f", "Front")
        if rec:
            return rec
    if ra is not None:
        rec = _camber_for_axle(ra, tune.camber_r, "camber_r", "Rear")
        if rec:
            return rec
    return None


def _axle_inner_outer(reading, keys):
    vals = []
    for k in keys:
        z = reading.get(k)
        if not z or "inner" not in z or "outer" not in z:
            continue
        vals.append(z["inner"] - z["outer"])
    if not vals:
        return None
    return sum(vals) / len(vals)


def _camber_for_axle(inner_minus_outer, cur, key, label):
    if inner_minus_outer > CAMBER_C:
        # too much negative camber -> reduce magnitude (toward zero = +0.2)
        new = _round(cur + CAMBER_STEP)
        return Recommendation("camber", {key: new},
            f"{label} inner is {inner_minus_outer:.1f} C hotter than outer "
            f"(> {CAMBER_C}) - too much camber.",
            f"More even {label.lower()} tyre contact; better straight-line + braking grip.",
            f"{label} camber {cur:+.1f} -> {new:+.1f} deg")
    if -inner_minus_outer > CAMBER_C:
        new = _round(cur - CAMBER_STEP)
        return Recommendation("camber", {key: new},
            f"{label} outer is {-inner_minus_outer:.1f} C hotter than inner "
            f"(> {CAMBER_C}) - not enough camber.",
            f"More {label.lower()} grip at full lean mid-corner.",
            f"{label} camber {cur:+.1f} -> {new:+.1f} deg")
    return None


def _rule_camber_search(stats, tune, disc, tyre_reading, road_band, limits,
                        step_mult=1.0) -> Optional[Recommendation]:
    """No tyre-temp read this lap: tune FRONT camber by LAP-TIME search instead of
    by temps. Hill-climbs toward more negative front camber (the usual corner-grip
    direction); fitness keeps it while it helps and locks it on the first regress.
    Distinct 'camber_search' group so it never blocks the temp-driven camber rule
    and isn't given evidence-revert protection. Only on grip (road-like) discs."""
    if tyre_reading:
        return None            # have temps -> evidence camber rule handles it
    if not road_band:
        return None            # camber search is a grip lever; skip drift/drag
    cur = tune.camber_f
    new = _round(cur - CAMBER_STEP * step_mult)   # more negative front = more corner grip
    return Recommendation("camber_search", {"camber_f": new},
        "No tyre-temp read - searching front camber by lap time.",
        "More front grip at full lean (kept only if the lap is faster).",
        f"Front camber {cur:+.1f} -> {new:+.1f} deg (lap-time search)",
        kind="search")


# --- D. balance / ARBs ------------------------------------------------------
def _rule_arb(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    if stats.n_corner_frames < 10:
        return None  # not enough cornering data to judge balance
    fa, ra = stats.slip_angle_front, stats.slip_angle_rear
    if fa - ra > US_DEG:   # understeer
        if tune.arb_r < ARB_REAR_SOFT_FLOOR:
            new = _clamp(tune.arb_r + ARB_STIFFEN_STEP, ARB_MIN, ARB_MAX)
            return Recommendation("arb", {"arb_r": _round(new)},
                f"Understeer: front slip angle {fa:.1f} deg exceeds rear {ra:.1f} "
                f"by > {US_DEG}.",
                "Front tucks into the corner; less push, car rotates more.",
                f"Rear ARB {tune.arb_r:.0f} -> {new:.0f}")
        new = _clamp(tune.arb_f - ARB_SOFTEN_STEP, ARB_MIN, ARB_MAX)
        if new != tune.arb_f:
            return Recommendation("arb", {"arb_f": _round(new)},
                f"Understeer, rear ARB already stiff ({tune.arb_r:.0f}).",
                "Softer front adds front grip; less mid-corner push.",
                f"Front ARB {tune.arb_f:.0f} -> {new:.0f}")
    if ra - fa > OS_DEG:   # oversteer
        if tune.arb_r > ARB_MIN:
            new = _clamp(tune.arb_r - ARB_SOFTEN_STEP, ARB_MIN, ARB_MAX)
            return Recommendation("arb", {"arb_r": _round(new)},
                f"Oversteer: rear slip angle {ra:.1f} deg exceeds front {fa:.1f} "
                f"by > {OS_DEG}.",
                "Rear plants; less snap, more confident throttle mid-corner.",
                f"Rear ARB {tune.arb_r:.0f} -> {new:.0f}")
        new = _clamp(tune.arb_f + ARB_STIFFEN_STEP, ARB_MIN, ARB_MAX)
        return Recommendation("arb", {"arb_f": _round(new)},
            f"Oversteer, rear ARB already soft ({tune.arb_r:.0f}).",
            "Stiffer front trims rotation back toward neutral.",
            f"Front ARB {tune.arb_f:.0f} -> {new:.0f}")
    return None


# --- B'. damping softening (harsh/under-used travel) ------------------------
def _rule_damping(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    # If neither axle ever uses much travel, the car is likely skittish/harsh.
    if stats.susp_min_front >= STIFF_THRESH and stats.susp_max_front <= 0.9:
        new = _round(tune.bump_f - BUMP_SOFTEN_STEP)
        if new >= 0:
            return Recommendation("damping_bump", {"bump_f": new},
                f"Front never compresses past {STIFF_THRESH:.2f} of travel - "
                "stiff/skittish front.",
                "Front tyre follows the road better; more mechanical grip.",
                f"Front bump {tune.bump_f:.1f} -> {new:.1f}")
    if stats.susp_min_rear >= STIFF_THRESH and stats.susp_max_rear <= 0.9:
        new = _round(tune.bump_r - BUMP_SOFTEN_STEP)
        if new >= 0:
            return Recommendation("damping_bump", {"bump_r": new},
                f"Rear never compresses past {STIFF_THRESH:.2f} of travel - "
                "stiff/skittish rear.",
                "Rear tracks bumps; steadier under power.",
                f"Rear bump {tune.bump_r:.1f} -> {new:.1f}")
    return None


# --- E. diff ----------------------------------------------------------------
def _rule_diff(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    os_thresh = _diff_slip_thresh(disc, ON_POWER_OS_SLIP, ON_POWER_OS_SLIP_DIRT)
    us_thresh = _diff_slip_thresh(disc, EXIT_US_FRONT_SLIP, EXIT_US_FRONT_SLIP_DIRT)
    entry_thresh = _diff_slip_thresh(disc, ENTRY_INSTAB_SLIP, ENTRY_INSTAB_SLIP_DIRT)
    # dirt/CC keep a driveability floor on the accel diff (open rear = no drive)
    accel_floor = DIRT_ACCEL_DIFF_FLOOR if disc in _DIRT_LIKE else 0.0
    # on-power oversteer: rear slip rising on throttle (threshold is dirt-aware)
    if stats.on_throttle_rear_slip > os_thresh and \
            stats.on_throttle_rear_slip > stats.on_throttle_front_slip:
        new = _clamp(tune.diff_rear_accel - DIFF_STEP, accel_floor, 100.0)
        if new != tune.diff_rear_accel:
            floor_note = (f" (dirt floor {accel_floor:.0f}% - moderate wheelspin is the "
                          "target, not a fault)" if accel_floor else "")
            return Recommendation("diff", {"diff_rear_accel": _round(new, 1)},
                f"On-power oversteer: rear slip ratio {stats.on_throttle_rear_slip:.2f} "
                f"under throttle (> {os_thresh}).",
                "Cleaner corner exits; rear lays power down without stepping out.",
                f"Rear accel diff {tune.diff_rear_accel:.0f} -> {new:.0f} %{floor_note}")
    # AWD exit understeer: front slip rising on throttle -> send more torque rear
    if stats.drivetrain == "AWD" and stats.on_throttle_front_slip > us_thresh \
            and stats.on_throttle_front_slip > stats.on_throttle_rear_slip:
        new = _clamp(tune.diff_center + DIFF_STEP, 0.0, DIFF_CENTER_MAX)
        if new != tune.diff_center:
            return Recommendation("diff", {"diff_center": _round(new, 1)},
                f"AWD exit understeer: front slip ratio {stats.on_throttle_front_slip:.2f} "
                "on throttle (front scrabbling).",
                "Nose stops pushing on exit; more drive comes from the rear.",
                f"Center diff {tune.diff_center:.0f} -> {new:.0f} % to rear")
    # entry instability under braking
    if stats.braking_rear_slip > entry_thresh:
        new = _clamp(tune.diff_rear_decel - DIFF_STEP, 0.0, 100.0)
        if new != tune.diff_rear_decel:
            return Recommendation("diff", {"diff_rear_decel": _round(new, 1)},
                f"Entry instability: rear slip {stats.braking_rear_slip:.2f} under braking.",
                "Rear stays planted on corner entry; less trail-brake snap.",
                f"Rear decel diff {tune.diff_rear_decel:.0f} -> {new:.0f} %")
    return None


# --- F. gearing -------------------------------------------------------------
# +1 = lengthen (LOWER ratio number, more top speed); -1 = shorten (HIGHER ratio)
def _gearing_direction(stats) -> int:
    if stats.hit_redline and stats.top_speed_ms > 40.0:
        return +1   # bouncing off the limiter with road left -> lengthen
    if stats.engine_max_rpm and stats.max_rpm_seen < 0.90 * stats.engine_max_rpm \
            and stats.top_speed_ms > 30.0:
        return -1   # never near redline in top gear -> shorten
    if stats.min_corner_exit_rpm_frac < LOW_EXIT_RPM_FRAC:
        return -1   # bogging on corner exit -> shorten
    return 0


def gearing_wants_change(stats) -> bool:
    """Whether the telemetry indicates final drive should move. The main loop
    uses this to ask for the car's CURRENT (stock) ratio before the rule emits an
    exact value, since the baseline left final drive at stock."""
    return _gearing_direction(stats) != 0


def _rule_gearing(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    d = _gearing_direction(stats)
    if d == 0 or tune.final_drive == STOCK:
        return None   # nothing to do, or current ratio not known yet
    if d > 0:
        new = _round(tune.final_drive - FINAL_DRIVE_STEP, 0.01)
        return Recommendation("gearing", {"final_drive": new},
            f"Hitting redline on the straight (top speed {stats.top_speed_ms*2.237:.0f} mph) "
            "with road left - gearing too short.",
            "Higher top speed; engine no longer bounces off the limiter.",
            f"Final drive {tune.final_drive:.2f} -> {new:.2f} (lengthen)")
    reason = (f"Never reaches near redline in top gear (peak {stats.max_rpm_seen:.0f}/"
              f"{stats.engine_max_rpm:.0f} rpm) - gearing too long."
              if stats.max_rpm_seen < 0.90 * (stats.engine_max_rpm or 1)
              else f"Bogging on corner exit (drops to "
                   f"{stats.min_corner_exit_rpm_frac*100:.0f}% of redline).")
    new = _round(tune.final_drive + FINAL_DRIVE_STEP, 0.01)
    return Recommendation("gearing", {"final_drive": new}, reason,
        "Stronger acceleration; revs reach the power band.",
        f"Final drive {tune.final_drive:.2f} -> {new:.2f} (shorten)")


# --- G. aero ----------------------------------------------------------------
def _rule_aero(stats, tune, disc, tyre_reading, road_band, limits) -> Optional[Recommendation]:
    if disc not in ("road", "touge", "topspeed"):
        return None
    if tune.aero_rear == STOCK:
        return None   # car-specific: no aero range entered, nothing safe to emit
    # step = 10% of the car's actual rear-aero range (fall back to a fixed span)
    b = limits.bounds("aero_rear") if limits else None
    step = AERO_STEP_FRAC * ((b[1] - b[0]) if b else AERO_RANGE)
    if disc == "topspeed" and stats.max_lateral_g > 1.1 and not stats.hit_redline:
        new = _round(tune.aero_rear - step, 1)
        return Recommendation("aero", {"aero_rear": new},
            "Grip surplus (peak >1.1 g) but not reaching top speed - drag-limited.",
            "Higher trap speed for a small loss of cornering downforce.",
            f"Rear aero {tune.aero_rear:.0f} -> {new:.0f}")
    if disc in ("road", "touge") and stats.max_lateral_g < 1.0:
        new = _round(tune.aero_rear + step, 1)
        return Recommendation("aero", {"aero_rear": new},
            f"Cornering grip-limited (peak only {stats.max_lateral_g:.2f} g) on a "
            "downforce track.",
            "More high-speed grip and confidence through fast corners.",
            f"Rear aero {tune.aero_rear:.0f} -> {new:.0f}")
    return None
