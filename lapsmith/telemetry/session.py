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

    # balance: mean tyre slip ANGLE (deg) per axle, only in the high-G window
    slip_angle_front: float = 0.0
    slip_angle_rear: float = 0.0
    n_corner_frames: int = 0
    max_lateral_g: float = 0.0

    # diff: mean rear slip RATIO while on throttle vs front (AWD push)
    on_throttle_front_slip: float = 0.0
    on_throttle_rear_slip: float = 0.0
    braking_rear_slip: float = 0.0

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
