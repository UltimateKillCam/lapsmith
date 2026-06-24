"""Aggregate a capture window of FH6 packets into the stats the analyzer needs.

A "test" is one drive between two ENTER presses. We turn the raw packet stream
into a single TestStats object: temp balance, min suspension travel per axle,
mean slip angles inside the high-lateral-G window, throttle-correlated slip,
RPM/speed extremes and the best clean lap.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from statistics import mean
from typing import List, Optional

from .parser import Packet

# Only consider frames where the car is actually loaded up in a corner when
# judging understeer/oversteer balance.
HIGH_G_THRESHOLD = 0.6          # |lateral g| above this = "in a corner"
THROTTLE_ON = 200               # accel byte (0..255) above this = on power
BRAKE_ON = 120                  # brake byte above this = braking


def _avg(xs: List[float]) -> float:
    return mean(xs) if xs else 0.0


@dataclass
class TestStats:
    n_packets: int = 0
    duration_s: float = 0.0

    # tyre temps (Celsius), averaged over moving frames
    temp_fl: float = 0.0
    temp_fr: float = 0.0
    temp_rl: float = 0.0
    temp_rr: float = 0.0

    # suspension: min normalized travel per axle (min over the test = worst case)
    susp_min_front: float = 1.0
    susp_min_rear: float = 1.0
    susp_max_front: float = 0.0
    susp_max_rear: float = 0.0
    # bottoming COVERAGE: per track-position bin, the MIN normalized travel in that
    # bin. Lets us tell a localized kerb/sidewalk strike (1 bin) from widespread
    # bottoming (many bins) instead of chasing the single worst spike on the lap.
    susp_bin_min_front: list = field(default_factory=list)
    susp_bin_min_rear: list = field(default_factory=list)

    # balance: mean tyre slip ANGLE (deg) per axle, only in the high-G window
    slip_angle_front: float = 0.0
    slip_angle_rear: float = 0.0
    n_corner_frames: int = 0
    max_lateral_g: float = 0.0

    # diff: mean rear slip RATIO while on throttle vs front (AWD push)
    on_throttle_front_slip: float = 0.0
    on_throttle_rear_slip: float = 0.0
    braking_rear_slip: float = 0.0

    # PER-WHEEL detail (corner that locks/spins worst) - sharper than the axle average.
    power_spin_wheel: str = ""        # driven wheel spinning most on throttle (FL/FR/RL/RR)
    power_spin_slip: float = 0.0      # its slip ratio
    brake_lock_wheel: str = ""        # wheel locking most under braking
    brake_lock_slip: float = 0.0      # its (most negative) slip ratio magnitude
    brake_lock_confirmed: bool = False  # WheelRotationSpeed ~0 while moving (true lockup)

    # suspension BALANCE (from per-corner normalized travel in the corner window)
    susp_min_fl: float = 1.0
    susp_min_fr: float = 1.0
    susp_min_rl: float = 1.0
    susp_min_rr: float = 1.0
    pitch_bias: float = 0.0           # front avg compression - rear (neg = nose dives more)
    roll_asym_front: float = 0.0      # |FL-FR| compression in corners (body roll, front)
    roll_asym_rear: float = 0.0       # |RL-RR| compression in corners (body roll, rear)

    vert_g_rms: float = 0.0           # vertical-accel RMS (ride harshness)

    # which richer channels carried signal this measurement (graceful-degrade flags)
    chan_wheel_rotation: bool = False
    chan_vertical_accel: bool = False
    chan_per_wheel_slip: bool = False
    chan_suspension: bool = False

    # combined slip (greasy detection) per axle
    combined_slip_front: float = 0.0
    combined_slip_rear: float = 0.0

    # powertrain / straight
    max_rpm_seen: float = 0.0
    engine_max_rpm: float = 0.0
    top_speed_ms: float = 0.0
    hit_redline: bool = False
    min_corner_exit_rpm_frac: float = 1.0

    # laps
    best_lap: float = 0.0
    last_lap: float = 0.0

    drivetrain: str = "?"

    def axle_temp_balance(self) -> dict:
        return {
            "front_lr_delta": self.temp_fl - self.temp_fr,
            "rear_lr_delta": self.temp_rl - self.temp_rr,
            "front_avg": (self.temp_fl + self.temp_fr) / 2,
            "rear_avg": (self.temp_rl + self.temp_rr) / 2,
        }

    def bottoming_coverage(self, thresh: float):
        """How WIDESPREAD is the bottoming, from the per-track-position bins.
        Returns (frac_front, zones_front, frac_rear, zones_rear): the fraction of
        lap bins where the axle's min travel <= thresh, and the count of distinct
        contiguous zones. A localized kerb strike is ~1 bin / 1 zone; a too-low car
        bottoms across many. Empty (no coverage data) -> (0, 0)."""
        def cov(bins):
            if not bins:
                return 0.0, 0
            hit = [b <= thresh for b in bins]
            frac = sum(hit) / len(bins)
            zones = sum(1 for i, h in enumerate(hit) if h and (i == 0 or not hit[i - 1]))
            return frac, zones
        ff, zf = cov(self.susp_bin_min_front)
        fr, zr = cov(self.susp_bin_min_rear)
        return ff, zf, fr, zr

    def channels_available(self) -> dict:
        """Which richer telemetry channels carried signal this measurement, so a rule
        can degrade gracefully and the session log can record what was live."""
        return {"per_wheel_slip": self.chan_per_wheel_slip,
                "wheel_rotation": self.chan_wheel_rotation,
                "suspension": self.chan_suspension,
                "vertical_accel": self.chan_vertical_accel}

    def as_dict(self) -> dict:
        return asdict(self)


def aggregate(packets: List[Packet]) -> TestStats:
    s = TestStats()
    pkts = [p for p in packets if p.is_race_on]
    if not pkts:
        # fall back to all packets if IsRaceOn never went true (free-roam can
        # report 0 in some states); keep moving frames only
        pkts = [p for p in packets if p.speed > 1.0]
    if not pkts:
        return s

    s.n_packets = len(pkts)
    s.duration_s = max(0.0, (pkts[-1].timestamp_ms - pkts[0].timestamp_ms) / 1000.0)
    s.drivetrain = pkts[0].drivetrain_name
    s.engine_max_rpm = max(p.engine_max_rpm for p in pkts) or 0.0

    moving = [p for p in pkts if p.speed > 2.0] or pkts

    s.temp_fl = _avg([p.tire_temp_fl for p in moving])
    s.temp_fr = _avg([p.tire_temp_fr for p in moving])
    s.temp_rl = _avg([p.tire_temp_rl for p in moving])
    s.temp_rr = _avg([p.tire_temp_rr for p in moving])

    front_norm = [min(p.susp_norm_fl, p.susp_norm_fr) for p in moving]
    rear_norm = [min(p.susp_norm_rl, p.susp_norm_rr) for p in moving]
    s.susp_min_front = min(front_norm) if front_norm else 1.0
    s.susp_min_rear = min(rear_norm) if rear_norm else 1.0
    s.susp_max_front = max([max(p.susp_norm_fl, p.susp_norm_fr) for p in moving], default=0.0)
    s.susp_max_rear = max([max(p.susp_norm_rl, p.susp_norm_rr) for p in moving], default=0.0)

    # bottoming COVERAGE (#1): bin moving frames by track position (DistanceTraveled,
    # else X, else lap timer) so the ride rule can require WIDESPREAD bottoming, not a
    # single kerb spike. Each bin holds the worst (min) travel seen at that point.
    N_BOTTOM_BINS = 24
    if len(moving) >= N_BOTTOM_BINS:
        for fld in ("distance_traveled", "position_x"):
            pv = [getattr(p, fld) for p in moving]
            if max(pv) - min(pv) > 1.0:
                break
        else:
            pv = [p.current_lap for p in moving]
        lo = min(pv)
        span = (max(pv) - min(pv)) or 1.0
        fb = [1.0] * N_BOTTOM_BINS
        rb = [1.0] * N_BOTTOM_BINS
        for p, x in zip(moving, pv):
            b = int((x - lo) / span * N_BOTTOM_BINS)
            b = 0 if b < 0 else (N_BOTTOM_BINS - 1 if b >= N_BOTTOM_BINS else b)
            fb[b] = min(fb[b], p.susp_norm_fl, p.susp_norm_fr)
            rb[b] = min(rb[b], p.susp_norm_rl, p.susp_norm_rr)
        s.susp_bin_min_front = fb
        s.susp_bin_min_rear = rb

    # balance window: high lateral g
    corner = [p for p in moving if abs(p.lateral_g) >= HIGH_G_THRESHOLD]
    s.n_corner_frames = len(corner)
    s.max_lateral_g = max((abs(p.lateral_g) for p in moving), default=0.0)
    if corner:
        s.slip_angle_front = _avg([(abs(p.tire_slip_angle_fl) + abs(p.tire_slip_angle_fr)) / 2 for p in corner])
        s.slip_angle_rear = _avg([(abs(p.tire_slip_angle_rl) + abs(p.tire_slip_angle_rr)) / 2 for p in corner])
        s.combined_slip_front = _avg([(p.tire_combined_slip_fl + p.tire_combined_slip_fr) / 2 for p in corner])
        s.combined_slip_rear = _avg([(p.tire_combined_slip_rl + p.tire_combined_slip_rr) / 2 for p in corner])

    on_throttle = [p for p in moving if p.accel >= THROTTLE_ON]
    if on_throttle:
        s.on_throttle_front_slip = _avg([(abs(p.tire_slip_ratio_fl) + abs(p.tire_slip_ratio_fr)) / 2 for p in on_throttle])
        s.on_throttle_rear_slip = _avg([(abs(p.tire_slip_ratio_rl) + abs(p.tire_slip_ratio_rr)) / 2 for p in on_throttle])
    braking = [p for p in moving if p.brake >= BRAKE_ON]
    if braking:
        s.braking_rear_slip = _avg([(abs(p.tire_slip_ratio_rl) + abs(p.tire_slip_ratio_rr)) / 2 for p in braking])

    # ---- PER-WHEEL detail: which CORNER spins/locks worst (sharper than the axle
    # average), so a decision and its "why" can name the wheel. -------------------
    _WHEELS = (("FL", "tire_slip_ratio_fl", "wheel_rot_fl"),
               ("FR", "tire_slip_ratio_fr", "wheel_rot_fr"),
               ("RL", "tire_slip_ratio_rl", "wheel_rot_rl"),
               ("RR", "tire_slip_ratio_rr", "wheel_rot_rr"))
    s.chan_per_wheel_slip = any(abs(getattr(p, sr)) > 1e-4 for p in moving
                                for _, sr, _ in _WHEELS)
    s.chan_wheel_rotation = any(abs(getattr(p, wr)) > 1e-4 for p in moving
                                for _, _, wr in _WHEELS)
    drivetrain_wheels = {"FWD": ("FL", "FR"), "RWD": ("RL", "RR")}.get(
        s.drivetrain, ("FL", "FR", "RL", "RR"))
    if on_throttle and s.chan_per_wheel_slip:      # worst-spinning DRIVEN wheel on power
        best = ("", 0.0)
        for name, sr, _ in _WHEELS:
            if name not in drivetrain_wheels:
                continue
            v = _avg([max(0.0, getattr(p, sr)) for p in on_throttle])
            if v > best[1]:
                best = (name, v)
        s.power_spin_wheel, s.power_spin_slip = best
    if braking and s.chan_per_wheel_slip:          # wheel locking most under braking
        worst = ("", 0.0)
        for name, sr, wr in _WHEELS:
            lock = _avg([max(0.0, -getattr(p, sr)) for p in braking])  # negative slip = locking
            if lock > worst[1]:
                worst = (name, lock)
        s.brake_lock_wheel, s.brake_lock_slip = worst
        if s.chan_wheel_rotation and s.brake_lock_wheel:        # rotation ~0 while moving
            wr = dict((n, w) for n, _, w in _WHEELS)[s.brake_lock_wheel]
            s.brake_lock_confirmed = any(
                abs(getattr(p, wr)) < 1.0 and p.speed > 5.0 for p in braking)

    # ---- suspension BALANCE (per-corner compression in the corner window) --------
    if corner and any(p.susp_norm_fl or p.susp_norm_fr or p.susp_norm_rl or p.susp_norm_rr
                      for p in corner):
        s.chan_suspension = True
        s.susp_min_fl = min(p.susp_norm_fl for p in corner)
        s.susp_min_fr = min(p.susp_norm_fr for p in corner)
        s.susp_min_rl = min(p.susp_norm_rl for p in corner)
        s.susp_min_rr = min(p.susp_norm_rr for p in corner)
        front_c = _avg([(p.susp_norm_fl + p.susp_norm_fr) / 2 for p in corner])
        rear_c = _avg([(p.susp_norm_rl + p.susp_norm_rr) / 2 for p in corner])
        s.pitch_bias = front_c - rear_c            # < 0 = front compresses more (soft front)
        s.roll_asym_front = _avg([abs(p.susp_norm_fl - p.susp_norm_fr) for p in corner])
        s.roll_asym_rear = _avg([abs(p.susp_norm_rl - p.susp_norm_rr) for p in corner])

    # ---- vertical-accel RMS (ride harshness) -------------------------------------
    vert = [p.accel_y / 9.80665 for p in moving]
    if any(abs(v) > 1e-3 for v in vert):
        s.chan_vertical_accel = True
        s.vert_g_rms = (sum(v * v for v in vert) / len(vert)) ** 0.5

    s.max_rpm_seen = max((p.current_engine_rpm for p in moving), default=0.0)
    s.top_speed_ms = max((p.speed for p in moving), default=0.0)
    if s.engine_max_rpm:
        s.hit_redline = s.max_rpm_seen >= 0.99 * s.engine_max_rpm

    # corner-exit RPM: look at frames where throttle is high but speed is low-ish
    # (just off a corner). Use min rpm fraction while accelerating hard.
    exit_frames = [p for p in on_throttle if p.engine_max_rpm]
    if exit_frames:
        s.min_corner_exit_rpm_frac = min(p.current_engine_rpm / p.engine_max_rpm for p in exit_frames)

    laps = [p.best_lap for p in pkts if p.best_lap and p.best_lap > 0]
    s.best_lap = min(laps) if laps else 0.0
    last = [p.last_lap for p in pkts if p.last_lap and p.last_lap > 0]
    s.last_lap = last[-1] if last else 0.0

    return s
