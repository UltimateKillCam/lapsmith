"""Tool-side segment timer for OPEN FREE-ROAM, where the game's lap timer
(BestLap/LastLap) does not run.

The user picks a fixed stretch of road and marks a start point and an end point.
We grab the packet nearest each mark and compute elapsed time from the packet
`TimestampMS` field (handling its u32 overflow). Movement is confirmed via the
straight-line **PositionX/Z displacement** between the marks - NOT
`DistanceTraveled`, which does not advance in free-roam (it stays ~0). The same
displacement, driven between the same two points, also confirms it was the same
segment so a shorter time can't be faked by driving a shorter path.

This elapsed time is the fitness metric for the keep/revert gate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .parser import Packet
from .listener import TelemetryListener

U32 = 1 << 32

# A run only counts as "the same segment" if its displacement is within this
# fraction of the reference run's displacement.
DISTANCE_TOLERANCE = 0.10   # +-10%
MIN_SEGMENT_DISTANCE_M = 50.0


@dataclass
class Mark:
    timestamp_ms: int
    position_x: float
    position_z: float
    speed: float
    distance_m: float = 0.0   # kept for logging only; NOT used for timing/validity

    @classmethod
    def from_packet(cls, p: Packet) -> "Mark":
        return cls(p.timestamp_ms, p.position_x, p.position_z, p.speed,
                   p.distance_traveled)


@dataclass
class SegmentRun:
    elapsed_s: float
    distance_m: float          # straight-line PositionX/Z displacement
    start: Mark
    end: Mark
    valid: bool
    note: str = ""


def _elapsed_ms(start_ts: int, end_ts: int) -> int:
    """Elapsed milliseconds, accounting for u32 TimestampMS overflow."""
    return (end_ts - start_ts) % U32


def _displacement(start: Mark, end: Mark) -> float:
    dx = end.position_x - start.position_x
    dz = end.position_z - start.position_z
    return math.hypot(dx, dz)


def measure(listener: TelemetryListener, start: Mark, end: Mark,
            reference_distance_m: Optional[float] = None) -> SegmentRun:
    elapsed = _elapsed_ms(start.timestamp_ms, end.timestamp_ms) / 1000.0
    distance = _displacement(start, end)   # PositionX/Z, not DistanceTraveled
    valid = True
    note = ""

    if distance < MIN_SEGMENT_DISTANCE_M:
        valid = False
        note = (f"only {distance:.0f} m of position change - the car barely moved "
                "between marks; pick a longer stretch.")
    elif elapsed <= 0.0 or elapsed > 600.0:
        valid = False
        note = f"implausible elapsed time {elapsed:.1f}s - re-mark the segment."
    elif reference_distance_m is not None and reference_distance_m > 0:
        ratio = distance / reference_distance_m
        if abs(ratio - 1.0) > DISTANCE_TOLERANCE:
            valid = False
            note = (f"displacement {distance:.0f} m differs {((ratio-1)*100):+.0f}% from "
                    f"the reference {reference_distance_m:.0f} m - not the same segment, "
                    "result ignored.")

    return SegmentRun(elapsed_s=elapsed, distance_m=distance, start=start,
                      end=end, valid=valid, note=note)


def grab_mark(listener: TelemetryListener) -> Optional[Mark]:
    snap = listener.snapshot()
    return Mark.from_packet(snap) if snap else None
