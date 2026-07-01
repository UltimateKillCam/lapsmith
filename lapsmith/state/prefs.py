"""Tiny JSON key/value preferences store (app data dir), for settings that must
persist across runs - currently the tuning time budget. One source of truth shared
by the setup form and the main-window Settings control.

Call set_store_path() once at startup (like ordinals); get/set are no-ops-safe if it
was never set (returns defaults, skips writing).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_log = logging.getLogger("lapsmith.prefs")
_path: str | None = None
_cache: dict | None = None

DEFAULT_TIME_BUDGET_MIN = 20.0      # mirrors rules.DEFAULT_TIME_BUDGET_MIN
DEFAULT_TELEMETRY_UNIT_SYSTEM = "english"


def set_store_path(path: str) -> None:
    global _path, _cache
    _path = path
    _cache = None
    _load()


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    if _path and os.path.isfile(_path):
        try:
            with open(_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cache = data
        except Exception:
            _log.exception("prefs load failed (%s); using defaults", _path)
    return _cache


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set(key: str, value: Any) -> None:
    cache = _load()
    cache[key] = value
    if not _path:
        return
    try:
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        with open(_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        _log.exception("prefs save failed (%s)", _path)


def time_budget_min() -> float:
    """Persisted budget (minutes); default 20. 0 = unlimited."""
    try:
        return float(get("time_budget_min", DEFAULT_TIME_BUDGET_MIN))
    except (TypeError, ValueError):
        return DEFAULT_TIME_BUDGET_MIN


def telemetry_unit_system() -> str:
    """Persisted telemetry display units; default preserves existing mph readouts."""
    try:
        from ..units import telemetry_unit_system as _clean
        return _clean(get("telemetry_unit_system", DEFAULT_TELEMETRY_UNIT_SYSTEM))
    except Exception:
        return DEFAULT_TELEMETRY_UNIT_SYSTEM
