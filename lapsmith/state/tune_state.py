"""The mutable tune state: a dict of exact lever values plus change history.

Every recommendation the analyzer makes is applied through `apply_change`,
which records an undoable history entry. This is what lets the fitness gate
revert a single lever to its previous absolute value.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# Sentinel for car-specific values the tool does NOT set to a blind number:
# the baseline leaves them at STOCK and they are tuned from telemetry / user
# input (final drive) or set range-relative only once the car's range is known.
STOCK = -1.0

# Fixed in-game caps that don't vary much by car (the menu hard-limits these).
# Every lever here is a FIXED-RANGE setting (safe to clamp to a known cap).
RANGE_CAPS: Dict[str, Tuple[float, float]] = {
    "arb_f": (1.0, 65.0), "arb_r": (1.0, 65.0),
    "brake_pressure": (0.0, 100.0), "brake_balance": (0.0, 100.0),
    "diff_center": (0.0, 100.0),
    "diff_rear_accel": (0.0, 100.0), "diff_rear_decel": (0.0, 100.0),
    "diff_front_accel": (0.0, 100.0), "diff_front_decel": (0.0, 100.0),
    "pressure_f": (15.0, 55.0), "pressure_r": (15.0, 55.0),   # sane psi bounds
    "camber_f": (-10.0, 2.0), "camber_r": (-10.0, 2.0),
    "toe_f": (-5.0, 5.0), "toe_r": (-5.0, 5.0),
    "caster": (5.0, 7.0),                                      # Forza caster range
}

# CAR-SPECIFIC levers: range read off the slider ends at setup (per axle where
# the slider range differs front vs rear). final_drive is tuned from telemetry.
# Ride height is now PER AXLE too (front and rear sliders differ); a legacy single
# ride_height_min/max pair is still honoured as a fallback (see CarLimits.bounds).
_DYNAMIC = {
    "ride_height_f": ("ride_height_front_min", "ride_height_front_max"),
    "ride_height_r": ("ride_height_rear_min", "ride_height_rear_max"),
    "spring_f": ("spring_front_min", "spring_front_max"),
    "spring_r": ("spring_rear_min", "spring_rear_max"),
    "aero_front": ("aero_front_min", "aero_front_max"),
    "aero_rear": ("aero_rear_min", "aero_rear_max"),
}


@dataclass
class CarLimits:
    """Per-car achievable ranges. Dynamic ones (ride height F/R, springs F/R, aero
    F/R) are read off the slider ends at setup, PER AXLE because the front and
    rear slider ranges differ; everything else uses the fixed menu caps."""
    # ride height, PER AXLE (front and rear sliders differ)
    ride_height_front_min: Optional[float] = None
    ride_height_front_max: Optional[float] = None
    ride_height_rear_min: Optional[float] = None
    ride_height_rear_max: Optional[float] = None
    # legacy single ride-height pair (pre per-axle); used as a fallback only
    ride_height_min: Optional[float] = None
    ride_height_max: Optional[float] = None
    spring_front_min: Optional[float] = None
    spring_front_max: Optional[float] = None
    spring_rear_min: Optional[float] = None
    spring_rear_max: Optional[float] = None
    aero_front_min: Optional[float] = None
    aero_front_max: Optional[float] = None
    aero_rear_min: Optional[float] = None
    aero_rear_max: Optional[float] = None

    def bounds(self, lever: str) -> Optional[Tuple[float, float]]:
        if lever in _DYNAMIC:
            lo_attr, hi_attr = _DYNAMIC[lever]
            lo, hi = getattr(self, lo_attr), getattr(self, hi_attr)
            if (lo is None or hi is None) and lever in ("ride_height_f", "ride_height_r"):
                # fall back to the legacy single ride-height pair if per-axle unset
                lo = self.ride_height_min if lo is None else lo
                hi = self.ride_height_max if hi is None else hi
            if lo is None or hi is None:
                return None
            return (lo, hi)
        return RANGE_CAPS.get(lever)

    def clamp(self, lever: str, value: float) -> Tuple[float, bool, str]:
        """Return (clamped_value, was_clamped, message)."""
        b = self.bounds(lever)
        if not b:
            return value, False, ""
        lo, hi = b
        if value > hi:
            return hi, True, f"{lever} at car max {hi:g} - cannot raise further"
        if value < lo:
            return lo, True, f"{lever} at car min {lo:g} - cannot lower further"
        return value, False, ""

    def at_max(self, lever: str, value: float) -> bool:
        b = self.bounds(lever)
        return bool(b) and value >= b[1] - 1e-6

    def at_min(self, lever: str, value: float) -> bool:
        b = self.bounds(lever)
        return bool(b) and value <= b[0] + 1e-6

    def lerp(self, lever: str, frac: float, fallback: float) -> float:
        """Position a value at `frac` (0..1) within the lever's range; if the
        range is unknown, return `fallback`."""
        b = self.bounds(lever)
        if not b:
            return fallback
        lo, hi = b
        return lo + (hi - lo) * max(0.0, min(1.0, frac))

    def as_dict(self) -> dict:
        return asdict(self)


# Canonical lever set. Values are exact, type-faithful to the in-game menu.
@dataclass
class Tune:
    # tyres
    tyre_compound: str = "Slick"
    pressure_f: float = 29.0          # psi
    pressure_r: float = 29.0
    # alignment
    caster: float = 7.0               # deg
    camber_f: float = -1.4            # deg (negative)
    camber_r: float = -0.8
    toe_f: float = 0.0                # deg
    toe_r: float = 0.0
    # anti-roll bars (1..65 scale)
    arb_f: float = 6.0
    arb_r: float = 60.0
    # springs (kgf/mm)
    spring_f: float = 90.0
    spring_r: float = 115.0
    # ride height (cm)
    ride_height_f: float = 5.0
    ride_height_r: float = 6.0
    # damping
    bump_f: float = 5.0
    bump_r: float = 7.0
    rebound_f: float = 8.0
    rebound_r: float = 11.0
    # brakes
    brake_pressure: float = 100.0     # %
    brake_balance: float = 50.0       # % front
    # differential (AWD: center is rear-bias %)
    diff_center: float = 75.0         # % to rear (AWD only)
    diff_rear_accel: float = 80.0     # %
    diff_rear_decel: float = 15.0
    diff_front_accel: float = 20.0
    diff_front_decel: float = 0.0
    # aero (downforce units - car-specific range, stored as the menu value)
    aero_front: float = 0.0
    aero_rear: float = 0.0
    # gearing
    final_drive: float = 3.50

    def copy(self) -> "Tune":
        return copy.deepcopy(self)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def get(self, key: str) -> Any:
        return getattr(self, key)

    def set(self, key: str, value: Any) -> None:
        if not hasattr(self, key):
            raise KeyError(f"unknown tune lever: {key}")
        setattr(self, key, value)


@dataclass
class ChangeRecord:
    lever_group: str
    fields: Dict[str, Any]            # field -> new value
    previous: Dict[str, Any]          # field -> old value
    reason: str
    feel_for: str
    iteration: int
    # filled in after the timed segment by the fitness gate (seconds)
    seg_before_s: Optional[float] = None
    seg_after_s: Optional[float] = None
    seg_distance_m: Optional[float] = None
    verdict: str = "pending"          # pending | kept | reverted

    def as_dict(self) -> dict:
        return asdict(self)


class TuneState:
    def __init__(self, tune: Tune):
        self.current: Tune = tune
        self.history: List[ChangeRecord] = []
        self.converged_levers: set[str] = set()
        self.iteration: int = 0

    def apply_change(self, group: str, fields: Dict[str, Any], reason: str,
                     feel_for: str) -> ChangeRecord:
        previous = {k: self.current.get(k) for k in fields}
        rec = ChangeRecord(
            lever_group=group, fields=dict(fields), previous=previous,
            reason=reason, feel_for=feel_for, iteration=self.iteration,
        )
        for k, v in fields.items():
            self.current.set(k, v)
        self.history.append(rec)
        return rec

    def revert_last(self) -> Optional[ChangeRecord]:
        if not self.history:
            return None
        rec = self.history[-1]
        for k, v in rec.previous.items():
            self.current.set(k, v)
        rec.verdict = "reverted"
        return rec

    def keep_last(self) -> Optional[ChangeRecord]:
        if not self.history:
            return None
        self.history[-1].verdict = "kept"
        return self.history[-1]

    def mark_converged(self, lever_group: str) -> None:
        self.converged_levers.add(lever_group)

    def diff_from_baseline(self, baseline: Tune) -> Dict[str, tuple]:
        out = {}
        b = baseline.as_dict()
        c = self.current.as_dict()
        for k in c:
            if c[k] != b[k]:
                out[k] = (b[k], c[k])
        return out
