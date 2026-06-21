"""Lap detection from telemetry, for Rivals / circuit auto-lap mode.

When IsRaceOn=1 and the lap fields are live, a lap completes when LapNumber
increments (or CurrentLap resets toward 0). The completed lap's time is LastLap
(@300); the diagnostic telemetry is the buffer of packets accumulated over that
lap. This removes the manual F9/F10 segment markers - one iteration = one lap.

Offsets used (standard Forza dash + the verified +12 Horizon shift):
  DistanceTraveled f32 @292, BestLap @296, LastLap @300, CurrentLap @304,
  LapNumber u16 @312  (cross-checked: LapNumber@312 -> RacePosition@314 ->
  the already-validated Accel@315 / Brake@316).
VERIFY live: the emitted LastLap must match the in-game lap time before trusting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .parser import Packet

# CurrentLap dropping by at least this many seconds = a lap rollover (start line).
CURRENT_LAP_RESET_DROP = 5.0


@dataclass
class LapResult:
    lap_number: int          # the lap that just finished
    last_lap_s: float        # its time (LastLap field)
    packets: List[Packet]    # telemetry accumulated over that lap


class LapWatcher:
    def __init__(self):
        self.prev_lap_number: Optional[int] = None
        self.prev_current_lap: Optional[float] = None
        self.buf: List[Packet] = []
        self._seen_live = False
        self._advancing = False        # the lap TIMER is actually running
        self._was_race_on = False      # to detect a race off->on (event restart)
        self._restarted = False        # latched until pop_restarted() reads it

    def reset(self):
        self.prev_lap_number = None
        self.prev_current_lap = None
        self.buf = []

    def pop_restarted(self) -> bool:
        """Return (and clear) whether an event RESTART was seen since the last
        call: IsRaceOn cycled off->on, or the lap counter jumped backwards (a
        fresh standing start). The controller uses this to re-arm the warm-up
        (out-lap) discard so lap 1 of the new run is ignored."""
        r = self._restarted
        self._restarted = False
        return r

    def lap_fields_live(self) -> bool:
        """Have we seen any non-zero lap field (weaker signal)?"""
        return self._seen_live

    def advancing(self) -> bool:
        """Has the lap timer actually ADVANCED (LapNumber incremented, or
        CurrentLap risen tick-over-tick while racing)? This - not a one-shot
        snapshot at the stationary start line - is what engages AUTO-LAP."""
        return self._advancing

    def feed(self, packets: List[Packet]) -> List[LapResult]:
        results: List[LapResult] = []
        for p in packets:
            if not p.is_race_on:
                # left the timed session - drop any partial lap. Remember we were
                # racing so the next race-on frame counts as a restart.
                if self._was_race_on:
                    self._restarted = True
                self._was_race_on = False
                self.reset()
                continue
            # race resumed after being off => event restart (re-arm warm-up discard)
            if not self._was_race_on and self._seen_live:
                self._restarted = True
            self._was_race_on = True
            if p.lap_number > 0 or p.current_lap > 0 or p.last_lap > 0:
                self._seen_live = True

            if self.prev_lap_number is None:
                self.prev_lap_number = p.lap_number
                self.prev_current_lap = p.current_lap

            # lap counter jumped BACKWARDS without a race-off (in-place restart)
            if p.lap_number < self.prev_lap_number:
                self._restarted = True
                self.buf = []
                self.prev_lap_number = p.lap_number
                self.prev_current_lap = p.current_lap
                self.buf.append(p)
                continue

            # timer is running if the lap counter ticked up or CurrentLap rose
            if p.lap_number > self.prev_lap_number:
                self._advancing = True
            elif (self.prev_current_lap is not None
                  and p.current_lap > self.prev_current_lap + 0.05):
                self._advancing = True

            completed = False
            if p.lap_number > self.prev_lap_number:
                completed = True
            elif (self.prev_current_lap is not None
                  and self.prev_current_lap - p.current_lap > CURRENT_LAP_RESET_DROP
                  and p.lap_number >= self.prev_lap_number):
                # CurrentLap reset toward 0 across the start line
                completed = True

            if completed:
                results.append(LapResult(
                    lap_number=self.prev_lap_number,
                    last_lap_s=p.last_lap,
                    packets=self.buf))
                self.buf = []
                self.prev_lap_number = p.lap_number

            self.prev_current_lap = p.current_lap
            self.buf.append(p)
        return results
