"""Display-unit helpers.

Telemetry parsing and analysis keep canonical units (m/s for speed, Celsius after
FH6's Fahrenheit tyre temps are normalized).  These helpers are intentionally
small and live at the display edge so UI surfaces can agree without changing the
math that drives tuning decisions.
"""
from __future__ import annotations

TELEMETRY_UNIT_ENGLISH = "english"
TELEMETRY_UNIT_METRIC = "metric"
TELEMETRY_UNIT_SYSTEMS = (TELEMETRY_UNIT_ENGLISH, TELEMETRY_UNIT_METRIC)
DEFAULT_TELEMETRY_UNIT_SYSTEM = TELEMETRY_UNIT_ENGLISH


def telemetry_unit_system(value: object, default: str = DEFAULT_TELEMETRY_UNIT_SYSTEM) -> str:
    """Return a supported telemetry display unit system.

    ``english`` is the default because existing LapSmith telemetry readouts are
    mph-first.  Invalid persisted values fall back safely instead of leaking into
    rendering code.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v in TELEMETRY_UNIT_SYSTEMS:
            return v
    return default if default in TELEMETRY_UNIT_SYSTEMS else DEFAULT_TELEMETRY_UNIT_SYSTEM


def speed_value_unit(speed_mps: float, unit_system: str = DEFAULT_TELEMETRY_UNIT_SYSTEM) -> tuple[float, str]:
    """Convert canonical meters/second into the selected display speed unit."""
    if telemetry_unit_system(unit_system) == TELEMETRY_UNIT_METRIC:
        return speed_mps * 3.6, "km/h"
    return speed_mps * 2.236936, "mph"


def format_speed(speed_mps: float, unit_system: str = DEFAULT_TELEMETRY_UNIT_SYSTEM,
                 decimals: int = 1) -> str:
    """Display speed with a consistent unit label."""
    value, unit = speed_value_unit(speed_mps, unit_system)
    return f"{value:.{decimals}f} {unit}"
