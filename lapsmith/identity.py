"""Auto-detect car identity / class / PI / drivetrain from a live telemetry frame.

The user no longer types car/class/drivetrain - we read it from the packet:
  * CarOrdinal           -> car identity (name via ordinals.py; unknown -> "Car #N")
  * CarClass             -> class letter (D/C/B/A/S1/S2/X) for display
  * CarPerformanceIndex  -> PI, and the suggested build-to-ceiling target class
  * DrivetrainType       -> FWD/RWD/AWD

Identity MUST be read from a confirmed-live (moving, non-zero) frame - a zeroed
frame reports ordinal 0 / FWD and would misconfigure the tune.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

from . import ordinals
from .telemetry.parser import Packet

_log = logging.getLogger("lapsmith.identity")

# Forza CarClass enum -> letter (display only)
_CLASS_LETTER = {0: "D", 1: "C", 2: "B", 3: "A", 4: "S1", 5: "S2", 6: "X", 7: "X"}

# Forza DrivetrainType (s32 @224, PRE-insert): EXACTLY this mapping.
_DRIVETRAIN = {0: "FWD", 1: "RWD", 2: "AWD"}


@dataclass
class CarIdentity:
    ordinal: int
    name: str
    pi: int
    car_class_enum: int
    class_letter: str
    drivetrain: str           # FWD / RWD / AWD
    known: bool               # is the ordinal in the name map?
    target_class: str         # suggested build-to ceiling: "A 700" etc.
    drivetrain_raw: int = -1  # RAW DrivetrainType int (0/1/2) for diagnosis
    num_cylinders: int = 0    # sanity check: inline-6 (e.g. 2JZ) must read 6

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        tag = "" if self.known else "  (unknown ordinal - tunes fully anyway)"
        return (f"{self.name}  |  PI {self.pi} ({self.class_letter})  |  "
                f"{self.drivetrain} (raw {self.drivetrain_raw})  ->  "
                f"target {self.target_class}{tag}")


def class_letter(car_class_enum: int) -> str:
    """Raw CarClass-enum -> letter (diagnostic only). The DISPLAYED class comes from
    the car's PI via the shared table (class_for_pi), so the 'PI nnn (class)' label
    and the Target-class dropdown can't disagree."""
    return _CLASS_LETTER.get(car_class_enum, "?")


def suggest_target_class(pi: int) -> str:
    """The car's OWN class (letter + PI ceiling) from its PI - the default build
    target. No longer bumps the car up a class; the user can override it in setup.
    Uses the shared class table so classes/ceilings stay consistent everywhere."""
    from .knowledge.baseline import class_for_pi, PI_CEILING
    if pi <= 0:
        return f"A {PI_CEILING['A']}"
    c = class_for_pi(pi)
    return f"{c} {PI_CEILING[c]}"


def is_live(packet: Optional[Packet]) -> bool:
    """A usable identity frame: race on or actually moving, with a real ordinal."""
    return bool(packet) and (packet.is_race_on or packet.speed > 1.0) and packet.car_ordinal > 0


def identify(packet: Packet) -> CarIdentity:
    from .knowledge.baseline import class_for_pi
    raw_dt = packet.drivetrain_type
    ncyl = packet.num_cylinders
    # Log the RAW pre-insert car-info ints so a misdetect can be diagnosed from
    # one line. Sanity: an inline-6 (2JZ Supra) must read NumCylinders=6; if not,
    # the car-info offsets (212/216/220/224/228) are off and need re-walking.
    _log.info("car-info RAW: CarOrdinal@212=%d CarClass@216=%d PI@220=%d "
              "DrivetrainType@224=%d(%s) NumCylinders@228=%d",
              packet.car_ordinal, packet.car_class, packet.car_pi,
              raw_dt, _DRIVETRAIN.get(raw_dt, "?"), ncyl)
    return CarIdentity(
        ordinal=packet.car_ordinal,
        name=ordinals.name_for(packet.car_ordinal),
        pi=packet.car_pi,
        car_class_enum=packet.car_class,
        class_letter=class_for_pi(packet.car_pi),   # PI-derived, shared table
        drivetrain=_DRIVETRAIN.get(raw_dt, "?"),
        known=ordinals.is_known(packet.car_ordinal),
        target_class=suggest_target_class(packet.car_pi),
        drivetrain_raw=raw_dt,
        num_cylinders=ncyl,
    )
