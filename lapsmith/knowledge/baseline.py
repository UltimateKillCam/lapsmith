"""Embedded FH6 knowledge base -> a fully concrete starting tune.

Turns (class, discipline, front_weight_%, drivetrain) into exact lever values.
Confidence tags from the brief: VERIFIED = real FH6 tunes/in-game; METHOD =
physics-derived; HEURISTIC = sensible default. These are *starting points* -
the clean-lap fitness gate is the final authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..state.tune_state import Tune, CarLimits, STOCK

PI_CEILING = {"A": 700, "S1": 800, "S2": 900, "R": 998}

# Range-relative baselines (fraction of the car's slider range, 0=min .. 1=max).
# (front, rear); rear slightly higher than front for rake on tarmac.
_RIDE_FRAC = {
    "road": (0.05, 0.12), "touge": (0.20, 0.27), "topspeed": (0.05, 0.10),
    "drag": (0.15, 0.18), "dirt": (0.80, 0.82), "cc": (0.95, 0.92),
}
# spring firmness as a fraction of the car's spring range (heavier axle biased up)
_SPRING_FRAC = {
    "road": 0.60, "touge": 0.50, "topspeed": 0.55, "drag": 0.45,
    "dirt": 0.25, "cc": 0.12,
}
# aero downforce as (front, rear) fraction of the car's aero range:
# downforce circuits = high front + rear as balance; dirt/CC near MIN; top-speed
# /drag = trim both for low drag.
_AERO_FRAC = {
    "road": (1.0, 0.6), "touge": (1.0, 0.6), "topspeed": (0.0, 0.0),
    "drag": (0.0, 0.0), "dirt": (0.05, 0.05), "cc": (0.05, 0.05),
}

# Disciplines we understand. Aliases map to a canonical key.
_DISCIPLINE_ALIASES = {
    "road": "road", "road circuit": "road", "circuit": "road", "tarmac": "road",
    "touge": "touge", "mountain": "touge",
    "dirt": "dirt", "rally": "dirt",
    "cross country": "cc", "cc": "cc", "crosscountry": "cc",
    "drag": "drag", "top speed": "topspeed", "topspeed": "topspeed",
}


def canon_discipline(d: str) -> str:
    key = d.strip().lower()
    if key in _DISCIPLINE_ALIASES:
        return _DISCIPLINE_ALIASES[key]
    # fuzzy contains
    for alias, canon in _DISCIPLINE_ALIASES.items():
        if alias in key:
            return canon
    return "road"


def canon_class(c: str) -> str:
    c = c.strip().upper().replace(" ", "")
    for k in PI_CEILING:
        if c.startswith(k):
            return k
    return "S1"


def _compound(discipline: str) -> str:
    return {
        "road": "Slick", "touge": "Slick", "topspeed": "Slick",
        "dirt": "Offroad", "cc": "Offroad", "drag": "Drag",
    }.get(discipline, "Slick")


def build_baseline(car: str, car_class: str, discipline: str,
                   front_weight_pct: float, drivetrain: str = "AWD",
                   limits: CarLimits | None = None) -> Tune:
    """Return an exact starting Tune, clamped to the car's achievable ranges.

    If `limits` carries the car's ride-height/spring slider ends, those baselines
    are computed RANGE-RELATIVE; otherwise hardcoded cm/kgf-mm starts are used.
    """
    cls = canon_class(car_class)
    disc = canon_discipline(discipline)
    dt = drivetrain.upper()
    fw = front_weight_pct / 100.0
    front_heavy = front_weight_pct >= 55.0

    t = Tune()
    t.tyre_compound = _compound(disc)
    t.caster = 7.0  # VERIFIED

    # --- pressure (VERIFIED bands) ---
    if disc in ("road", "touge", "topspeed", "drag"):
        t.pressure_f = t.pressure_r = 29.0
    elif disc == "dirt":
        t.pressure_f = t.pressure_r = 30.0
    else:  # cc
        t.pressure_f = t.pressure_r = 18.0

    # --- camber (VERIFIED starts) ---
    if disc in ("road", "touge", "topspeed", "drag"):
        t.camber_f, t.camber_r = -1.4, -0.8
    elif disc == "dirt":
        t.camber_f, t.camber_r = -0.3, 0.0
    else:  # cc
        t.camber_f, t.camber_r = -0.3, 0.3

    # --- toe (HEURISTIC) ---
    t.toe_f = 0.1 if disc == "dirt" else 0.0
    t.toe_r = 0.0

    # --- ARBs (VERIFIED starts; pattern soft-front/stiff-rear on road) ---
    if disc in ("road", "touge", "topspeed", "drag"):
        if dt == "RWD":
            t.arb_f, t.arb_r = 8.0, 60.0
        else:
            t.arb_f, t.arb_r = 6.0, 60.0
    elif disc == "dirt":
        t.arb_f, t.arb_r = 4.0, 18.0
    else:  # cc
        t.arb_f, t.arb_r = 49.0, 49.0

    # --- springs (METHOD: stiffer on heavier axle; PER-AXLE range) ---
    # The spring slider range differs front vs rear, so each axle is placed within
    # ITS OWN entered range; an axle without a range falls back to scaled defaults.
    base = _SPRING_FRAC[disc]
    bias = 0.12
    frac_f, frac_r = (base + bias, base - bias) if front_heavy else (base - bias, base + bias)
    base_soft, base_firm = _spring_pair(disc)
    split = max(0.35, min(0.65, fw))
    fb_f = round((base_firm if front_heavy else base_soft) * (0.85 + 0.30 * split), 1)
    fb_r = round((base_soft if front_heavy else base_firm) * (0.85 + 0.30 * (1 - split)), 1)
    if limits is not None and limits.bounds("spring_f"):
        t.spring_f = round(limits.lerp("spring_f", frac_f, fb_f), 1)
    else:
        t.spring_f = fb_f
    if limits is not None and limits.bounds("spring_r"):
        t.spring_r = round(limits.lerp("spring_r", frac_r, fb_r), 1)
    else:
        t.spring_r = fb_r

    # --- ride height (RANGE-RELATIVE if we know the car's range, else cm) ---
    if limits is not None and limits.bounds("ride_height_f"):
        # dirt/cc sit near the car's max; road/top-speed near its min.
        ff, fr = _RIDE_FRAC[disc]
        t.ride_height_f = round(limits.lerp("ride_height_f", ff, t.ride_height_f), 1)
        t.ride_height_r = round(limits.lerp("ride_height_r", fr, t.ride_height_r), 1)
    else:
        rh = {
            "road": (5.0, 6.0), "touge": (8.0, 9.0), "topspeed": (5.0, 6.0),
            "drag": (8.0, 8.0), "dirt": (18.0, 18.0), "cc": (50.0, 48.0),
        }[disc]
        t.ride_height_f, t.ride_height_r = rh

    # --- damping (VERIFIED starts) ---
    if disc in ("road", "touge", "topspeed", "drag"):
        t.bump_f, t.bump_r = 5.0, 7.0
        t.rebound_f, t.rebound_r = 8.0, 11.0   # ~1.5x bump
    elif disc == "dirt":
        t.bump_f, t.bump_r = 2.5, 2.5
        t.rebound_f, t.rebound_r = 10.0, 10.0  # ~4x bump
    else:  # cc (inverted: high bump, low rebound)
        t.bump_f, t.bump_r = 13.0, 14.0
        t.rebound_f, t.rebound_r = 3.0, 3.0

    # --- brakes (VERIFIED ranges) ---
    t.brake_pressure = 100.0
    t.brake_balance = 50.0

    # --- diff (VERIFIED starts, AWD) ---
    if dt == "AWD":
        t.diff_center = {"road": 75.0, "touge": 85.0, "dirt": 65.0,
                         "cc": 90.0, "topspeed": 75.0, "drag": 75.0}[disc]
    else:
        t.diff_center = 0.0  # N/A; menu hides center for non-AWD
    t.diff_rear_accel = 80.0
    t.diff_rear_decel = 15.0
    t.diff_front_accel = 0.0 if dt == "RWD" else 20.0
    t.diff_front_decel = 0.0

    # --- aero (CAR-SPECIFIC: range-relative within the entered F/R aero range) ---
    # No hardcoded numbers - a fixed 100 isn't settable on many cars. If the aero
    # range is unknown we leave it at STOCK with printed guidance instead.
    aff, afr = _AERO_FRAC[disc]
    if limits is not None and limits.bounds("aero_front"):
        t.aero_front = round(limits.lerp("aero_front", aff, STOCK), 0)
    else:
        t.aero_front = STOCK
    if limits is not None and limits.bounds("aero_rear"):
        t.aero_rear = round(limits.lerp("aero_rear", afr, STOCK), 0)
    else:
        t.aero_rear = STOCK

    # --- gearing (CAR-SPECIFIC: leave at STOCK; tuned from telemetry) ---
    # A fixed final-drive ratio means a different top speed on every car, so the
    # baseline does NOT emit a number - the gearing rule tunes it from RPM/Speed.
    t.final_drive = STOCK

    # Final safety: clamp every lever to the car's achievable range.
    if limits is not None:
        clamp_tune(t, limits)
    return t


def clamp_tune(t: Tune, limits: CarLimits) -> list[str]:
    """Clamp every numeric lever in `t` to the car's range in place. Levers left
    at STOCK (final drive, aero with no entered range) are skipped, not clamped.
    Returns human-readable messages for any value that had to be clamped."""
    msgs: list[str] = []
    for lever, val in t.as_dict().items():
        if not isinstance(val, (int, float)) or val == STOCK:
            continue
        new, clamped, msg = limits.clamp(lever, float(val))
        if clamped:
            t.set(lever, new)
            msgs.append(msg)
    return msgs


def _spring_pair(disc: str) -> tuple[float, float]:
    """(soft axle, firm axle) base spring rates by discipline firmness."""
    return {
        "road": (90.0, 115.0),
        "touge": (80.0, 100.0),
        "topspeed": (90.0, 110.0),
        "drag": (70.0, 95.0),
        "dirt": (35.0, 45.0),
        "cc": (22.0, 30.0),
    }.get(disc, (90.0, 115.0))


# --- pretty printing --------------------------------------------------------


def format_checklist(t: Tune, car: str, car_class: str, discipline: str,
                     front_weight_pct: float, drivetrain: str) -> str:
    cls = canon_class(car_class)
    disc = canon_discipline(discipline)
    pi = PI_CEILING[cls]
    L = []
    L.append(f"INITIAL TUNE  -  {car}")
    L.append(f"Class {cls} (build to PI {pi})  |  {disc.upper()}  |  "
             f"{drivetrain}  |  front weight {front_weight_pct:.0f}%")
    L.append("Enter these EXACT values in the in-game tune menu:")
    L.append("")
    L.append("TYRES")
    L.append(f"  Compound .............. {t.tyre_compound}")
    L.append(f"  Pressure  Front ....... {t.pressure_f:.1f} psi")
    L.append(f"  Pressure  Rear ........ {t.pressure_r:.1f} psi")
    L.append("ALIGNMENT")
    L.append(f"  Camber    Front ....... {t.camber_f:+.1f} deg")
    L.append(f"  Camber    Rear ........ {t.camber_r:+.1f} deg")
    L.append(f"  Toe       Front ....... {t.toe_f:+.1f} deg")
    L.append(f"  Toe       Rear ........ {t.toe_r:+.1f} deg")
    L.append(f"  Caster ................ {t.caster:.1f} deg")
    L.append("ANTI-ROLL BARS")
    L.append(f"  Front ................. {t.arb_f:.0f}")
    L.append(f"  Rear .................. {t.arb_r:.0f}")
    L.append("SPRINGS (kgf/mm)")
    L.append(f"  Front ................. {t.spring_f:.1f}")
    L.append(f"  Rear .................. {t.spring_r:.1f}")
    L.append("RIDE HEIGHT (cm)")
    L.append(f"  Front ................. {t.ride_height_f:.1f}")
    L.append(f"  Rear .................. {t.ride_height_r:.1f}")
    L.append("DAMPING")          # in-game order: Rebound above Bump
    L.append(f"  Rebound   Front ....... {t.rebound_f:.1f}")
    L.append(f"  Rebound   Rear ........ {t.rebound_r:.1f}")
    L.append(f"  Bump      Front ....... {t.bump_f:.1f}")
    L.append(f"  Bump      Rear ........ {t.bump_r:.1f}")
    L.append("BRAKES")
    L.append(f"  Pressure .............. {t.brake_pressure:.0f} %")
    L.append(f"  Balance ............... {t.brake_balance:.0f} % front")
    L.append("DIFFERENTIAL")
    if drivetrain.upper() == "AWD":
        L.append(f"  Center (to rear) ...... {t.diff_center:.0f} %")
    L.append(f"  Rear  Accel ........... {t.diff_rear_accel:.0f} %")
    L.append(f"  Rear  Decel ........... {t.diff_rear_decel:.0f} %")
    if drivetrain.upper() != "RWD":
        L.append(f"  Front Accel ........... {t.diff_front_accel:.0f} %")
        L.append(f"  Front Decel ........... {t.diff_front_decel:.0f} %")
    L.append("AERO")
    if t.aero_front == STOCK or t.aero_rear == STOCK:
        hint = _aero_stock_hint(disc)
        L.append(f"  Front / Rear .......... {hint}")
        L.append("                          (enter aero F/R slider ranges for exact values)")
    else:
        L.append(f"  Front ................. {t.aero_front:.0f}")
        L.append(f"  Rear .................. {t.aero_rear:.0f}")
    L.append("GEARING")
    if t.final_drive == STOCK:
        L.append("  Final Drive ........... leave at STOCK - tuned from telemetry")
    else:
        L.append(f"  Final Drive ........... {t.final_drive:.2f}")
    return "\n".join(L)


def _aero_stock_hint(disc: str) -> str:
    if disc in ("road", "touge"):
        return "front to MAX downforce, rear ~mid (balance)"
    if disc in ("topspeed", "drag"):
        return "front and rear to MIN (low drag)"
    return "minimum downforce both ends"
