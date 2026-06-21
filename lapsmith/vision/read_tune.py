"""Read the in-game tune sheet screenshot to VERIFY the human entered values.

This is a safety check: after the operator applies a change, we capture the
tune page and confirm the lever we just changed reads the value we asked for.
Returns a partial dict of {lever_name: value}; missing keys are simply unknown.
"""
from __future__ import annotations

from typing import Dict, Optional

from . import capture, bridge

KIND = "tune_sheet"
SCHEMA = ('flat object of any of: pressure_f, pressure_r, camber_f, camber_r, '
          'toe_f, toe_r, caster, arb_f, arb_r, spring_f, spring_r, '
          'ride_height_f, ride_height_r, bump_f, bump_r, rebound_f, rebound_r, '
          'brake_pressure, brake_balance, diff_center, diff_rear_accel, '
          'diff_rear_decel, diff_front_accel, diff_front_decel, aero_front, '
          'aero_rear, final_drive -> numeric value as shown in the menu')


def _manual(expect: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    if not expect:
        return {}
    print("Confirm the tune sheet shows these values (ENTER if correct, or type actual):")
    out: Dict[str, float] = {}
    for k, v in expect.items():
        raw = input(f"  {k} (expected {v}): ").strip()
        if raw:
            try:
                out[k] = float(raw)
            except ValueError:
                pass
        else:
            out[k] = v
    return out


def read(*, expect: Optional[Dict[str, float]] = None, manual: bool = False,
         tag: int | None = None, timeout_s: float = 120.0, announce=None) -> Dict[str, float]:
    say = announce or print
    if not manual and not capture.backend_available():
        manual = True
    if manual:
        return _manual(expect)
    say(">> Open the in-game tune sheet (so the changed values are visible).")
    path = capture.grab("tune_sheet", monotonic_tag=tag)
    data = bridge.request(path, KIND, SCHEMA, lambda: _manual(expect),
                          manual=False, timeout_s=timeout_s, announce=say)
    out: Dict[str, float] = {}
    for k, v in (data or {}).items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out
