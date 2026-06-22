"""Telemetry-primary fitness: a composite score from per-lap telemetry, binned by
TRACK POSITION so the SAME corner/segment is compared across laps.

Why: a single lap time is noisy and DRIVER-CONFOUNDED - over a session the driver
learns the track and laps get faster regardless of the tune. The car's telemetry
(cornering g, corner-exit forward g, traction efficiency, corner speed) reflects
what the TUNE changed, not how hard the driver pressed. We bin by DistanceTraveled
(Rivals = same track every lap) so line variance cancels: bin k is the same place.

Lap time stays as a SECONDARY guardrail (anti-Goodhart): if the composite says
"better" but lap time is clearly and repeatably worse, the change is NOT kept.

If the needed channels aren't live (a car/mode without lateral/longitudinal accel
or slip), the composite reports not-live and the caller degrades to lap time.

All weights/thresholds are module constants so they can be tuned after sessions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..telemetry.parser import Packet
from ..telemetry.session import HIGH_G_THRESHOLD, THROTTLE_ON

# --- tunables ---------------------------------------------------------------
TELEM_BINS = 24                 # track-position bins per lap
MIN_FRAMES_FOR_BINNING = 40     # below this, telemetry is too sparse -> degrade
# composite channel weights (higher composite = a better-handling car)
W_GRIP = 1.0                    # lateral grip in corners
W_EXIT = 1.6                    # corner-exit forward g (how quickly it accelerates)
W_TRACTION = 1.0                # forward g per unit (wasteful) slip
W_MINSPEED = 0.6                # minimum corner speed (carries more speed = faster)
# a composite gain must clear this to be an "apparent win" (triggers A/B/A)
COMPOSITE_IMPROVE_EPS = 0.015
# anti-Goodhart guardrail: composite says better but lap time is worse by > this
# (clearly beyond noise) -> distrust the composite, do NOT keep.
LAPTIME_GUARDRAIL_S = 0.25
# dirt traction: wheelspin up to the discipline slip floor is WANTED, not wasteful
_DIRT_LIKE = {"dirt", "cc"}
_EPS = 1e-6


@dataclass
class LapTelemetry:
    """Per-track-position-bin channel means for ONE measurement (1+ laps merged)."""
    grip: List[float] = field(default_factory=list)       # mean |lateral g| per bin
    exit_g: List[float] = field(default_factory=list)     # mean fwd g (throttle on)
    slip: List[float] = field(default_factory=list)       # mean rear combined slip
    speed: List[float] = field(default_factory=list)      # mean speed (m/s)
    steer: List[float] = field(default_factory=list)      # mean |steer|
    corner: List[bool] = field(default_factory=list)      # was this bin a corner?
    onthrottle: List[bool] = field(default_factory=list)  # was this bin throttle-on?
    n_frames: int = 0
    pos_src: str = "none"        # which field gave track position
    live: bool = False           # do the channels carry real signal?


def _track_positions(pkts: List[Packet]):
    """Pick the best within-lap track-position signal: DistanceTraveled, else the
    X position, else the lap timer (time, weaker but always present)."""
    for fld in ("distance_traveled", "position_x"):
        vals = [getattr(p, fld) for p in pkts]
        if max(vals) - min(vals) > 1.0:
            return vals, fld
    vals = [p.current_lap for p in pkts]
    return vals, "current_lap"


def bin_lap(packets: List[Packet], n_bins: int = TELEM_BINS) -> LapTelemetry:
    """Reduce a lap (or merged laps) of packets to per-track-position-bin channels."""
    lt = LapTelemetry(grip=[0.0] * n_bins, exit_g=[0.0] * n_bins, slip=[0.0] * n_bins,
                      speed=[0.0] * n_bins, steer=[0.0] * n_bins,
                      corner=[False] * n_bins, onthrottle=[False] * n_bins)
    pkts = [p for p in packets if p.is_race_on] or list(packets)
    pkts = [p for p in pkts if p.speed > 1.0] or pkts
    lt.n_frames = len(pkts)
    if len(pkts) < MIN_FRAMES_FOR_BINNING:
        return lt
    pos, lt.pos_src = _track_positions(pkts)
    lo, span = min(pos), (max(pos) - min(pos)) or 1.0
    cnt = [0] * n_bins
    g_acc = [0.0] * n_bins
    sp_acc = [0.0] * n_bins
    st_acc = [0.0] * n_bins
    corner_cnt = [0] * n_bins
    thr_cnt = [0] * n_bins
    exit_acc = [0.0] * n_bins
    slip_acc = [0.0] * n_bins
    any_grip = any_exit = False
    for p, x in zip(pkts, pos):
        b = min(n_bins - 1, max(0, int((x - lo) / span * n_bins)))
        cnt[b] += 1
        latg = abs(p.lateral_g)
        g_acc[b] += latg
        sp_acc[b] += p.speed
        st_acc[b] += abs(p.steer)
        if latg >= HIGH_G_THRESHOLD:
            corner_cnt[b] += 1
            any_grip = True
        if p.accel >= THROTTLE_ON:
            thr_cnt[b] += 1
            fwd = p.accel_z / 9.80665                  # forward (surge) g
            exit_acc[b] += fwd
            slip_acc[b] += (abs(p.tire_slip_ratio_rl) + abs(p.tire_slip_ratio_rr)) / 2.0
            if abs(fwd) > 0.05:
                any_exit = True
    for b in range(n_bins):
        if cnt[b]:
            lt.grip[b] = g_acc[b] / cnt[b]
            lt.speed[b] = sp_acc[b] / cnt[b]
            lt.steer[b] = st_acc[b] / cnt[b]
            lt.corner[b] = corner_cnt[b] >= max(1, cnt[b] // 3)
        if thr_cnt[b]:
            lt.exit_g[b] = exit_acc[b] / thr_cnt[b]
            lt.slip[b] = slip_acc[b] / thr_cnt[b]
            lt.onthrottle[b] = True
    lt.live = any_grip and any_exit
    return lt


# lever group -> the telemetry channel it primarily targets (TARGETED check)
_GROUP_CHANNEL = {
    "diff": "exit", "damping_bump": "exit", "damping": "exit", "gearing": "exit",
    "arb": "grip", "springs": "grip", "camber": "grip", "camber_search": "grip",
    "aero": "grip", "ride_height": "grip", "pressure": "grip",
}


def targeted_channel(group: str) -> str:
    return _GROUP_CHANNEL.get(group, "")


def _traction(exit_g: float, slip: float, slip_floor: float) -> float:
    """Forward g per unit WASTEFUL slip. On dirt, slip up to slip_floor is wanted,
    so it isn't penalised."""
    waste = max(0.0, slip - slip_floor)
    return exit_g / (1.0 + waste)


@dataclass
class CompositeResult:
    delta: float = 0.0           # cand - ref; > 0 means cand handles better
    grip: float = 0.0
    exit: float = 0.0
    traction: float = 0.0
    minspeed: float = 0.0
    targeted: float = 0.0        # delta of the channel the change targeted
    live: bool = False


def composite(cand: LapTelemetry, ref: LapTelemetry, discipline: str,
              group: str = "") -> CompositeResult:
    """Composite handling-quality delta of `cand` vs `ref`, matched per track bin.
    Positive = cand is the better-handling car. `group` (the changed lever) selects
    the TARGETED channel reported separately. Not-live if either side lacks signal."""
    res = CompositeResult()
    if not (cand.live and ref.live) or len(cand.grip) != len(ref.grip):
        return res
    res.live = True
    n = len(cand.grip)
    slip_floor = 0.60 if discipline in _DIRT_LIKE else 0.0   # tie to dirt slip thresh

    grip_d, grip_n = 0.0, 0
    exit_d, exit_n = 0.0, 0
    trac_d = 0.0
    cand_corner_sp, ref_corner_sp = [], []
    for b in range(n):
        if ref.corner[b] or cand.corner[b]:
            grip_d += cand.grip[b] - ref.grip[b]
            grip_n += 1
            cand_corner_sp.append(cand.speed[b])
            ref_corner_sp.append(ref.speed[b])
        if ref.onthrottle[b] or cand.onthrottle[b]:
            exit_d += cand.exit_g[b] - ref.exit_g[b]
            trac_d += (_traction(cand.exit_g[b], cand.slip[b], slip_floor)
                       - _traction(ref.exit_g[b], ref.slip[b], slip_floor))
            exit_n += 1
    res.grip = grip_d / grip_n if grip_n else 0.0
    res.exit = exit_d / exit_n if exit_n else 0.0
    res.traction = trac_d / exit_n if exit_n else 0.0
    res.minspeed = ((min(cand_corner_sp) - min(ref_corner_sp))
                    if cand_corner_sp and ref_corner_sp else 0.0)
    res.delta = (W_GRIP * res.grip + W_EXIT * res.exit
                 + W_TRACTION * res.traction + W_MINSPEED * res.minspeed)
    ch = targeted_channel(group)
    res.targeted = {"grip": res.grip, "exit": res.exit}.get(ch, res.delta)
    return res
