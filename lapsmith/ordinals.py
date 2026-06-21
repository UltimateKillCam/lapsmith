"""CarOrdinal -> display name map. DISPLAY ONLY - tuning never depends on it.

Any car is identified by its CarOrdinal in the Data Out packet. A known ordinal
shows a friendly name; an unknown one (e.g. a car added in a later update) shows
"Car #<ordinal>" and still works fully (ranges come from the sliders, tuning from
telemetry). The map is updatable at runtime from a JSON file so new ordinals can
be added without a code change.

Seed names cross-referenced from the FH5/FH6 cars list style ordinals used by
community tools (e.g. the car_data.h table in FH6 telemetry dashboards).
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Dict, Optional

_DISPLAY_RE = re.compile(r"""['"]display_name['"]\s*:\s*['"](.*?)['"]""")


def _clean_name(value) -> str:
    """A stored name may be a serialized car-record blob from an old import bug
    (e.g. "{'display_name': '1989 VW Golf Rallye', 'year': 1989, ...}"). Pull just
    the display name so the UI shows the clean name, not the whole dict."""
    s = str(value).strip()
    if s.startswith("{") or "display_name" in s:
        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, dict):
                for key in ("display_name", "name", "model"):
                    v = obj.get(key)
                    if v:
                        return str(v).strip()
        except (ValueError, SyntaxError):
            pass
        m = _DISPLAY_RE.search(s)
        if m:
            return m.group(1).strip()
    return s

# Small seed set (display only). Extend via a user JSON map - see load_user_map.
ORDINALS: Dict[int, str] = {
    247: "1969 Toyota 2000GT",
    1450: "2018 Aston Martin Vantage",
    2779: "2019 Mercedes-AMG GT R",
    2474: "1997 Mercedes-Benz CLK GTR",
    3290: "2021 Porsche 911 GT3",
    1457: "2015 Aston Martin Vulcan",
    3360: "2020 Koenigsegg Jesko",
}

_USER_MAP: Dict[int, str] = {}

# Where user-assigned names persist (ordinal -> name). The app points this at its
# data dir; defaults to ./car_names.json (override with FH6_CAR_NAMES).
NAMES_PATH = os.environ.get("FH6_CAR_NAMES", "car_names.json")


def name_for(ordinal: Optional[int]) -> str:
    """Friendly name, or 'Car #<ordinal>' for an unknown/updated car."""
    if ordinal is None or ordinal <= 0:
        return "Unknown car"
    if ordinal in _USER_MAP:
        return _clean_name(_USER_MAP[ordinal])
    return ORDINALS.get(ordinal, f"Car #{ordinal}")


def is_known(ordinal: Optional[int]) -> bool:
    """True if we have a friendly name for this ordinal (seed OR user-saved)."""
    return bool(ordinal) and (ordinal in _USER_MAP or ordinal in ORDINALS)


def is_user_named(ordinal: Optional[int]) -> bool:
    """True only if the USER saved a name for this ordinal (not just a seed)."""
    return bool(ordinal) and ordinal in _USER_MAP


def user_names() -> Dict[int, str]:
    """Copy of the user-saved {ordinal: name} map (for the Settings editor)."""
    return dict(_USER_MAP)


def set_store_path(path: str) -> int:
    """Point the persistent store at `path` and (re)load it. Returns count loaded."""
    global NAMES_PATH
    NAMES_PATH = path
    return load_user_map(path)


def _looks_like_record(name: str) -> bool:
    """True if a stored 'name' is actually a serialized record blob from the old
    import bug (e.g. "{'display_name': '...', 'year': 1989, ...}") rather than a
    real display name. Such junk is safe to OVERWRITE on import."""
    s = (name or "").strip()
    return s.startswith("{") or "display_name" in s


def bulk_fill(mapping: Dict[int, str], path: Optional[str] = None) -> Dict[str, int]:
    """MERGE an imported {ordinal: name} map, filling GAPS only - any name the user
    already set or edited is kept untouched. EXCEPTION: a previously stored value
    that looks like a serialized record (old-bug junk) is overwritten with the real
    name. Writes the store once. Returns {'imported': added, 'already': skipped}."""
    path = path or NAMES_PATH
    added = skipped = 0
    for ordinal, name in mapping.items():
        try:
            o = int(ordinal)
        except (TypeError, ValueError):
            continue
        nm = str(name or "").strip()
        if o <= 0 or not nm:
            continue
        existing = _USER_MAP.get(o)
        if existing is not None and not _looks_like_record(existing):
            skipped += 1                  # genuine user/imported name wins
            continue
        _USER_MAP[o] = nm                 # new gap, or repairing a record blob
        added += 1
    if added:
        _flush(path)
    return {"imported": added, "already": skipped}


def save_name(ordinal: int, name: str, path: Optional[str] = None) -> bool:
    """Persist ordinal -> name to the JSON store and update the in-memory map.
    A blank name DELETES any saved entry. Returns True on a successful write."""
    if not ordinal or ordinal <= 0:
        return False
    path = path or NAMES_PATH
    name = (name or "").strip()
    if name:
        _USER_MAP[int(ordinal)] = name
    else:
        _USER_MAP.pop(int(ordinal), None)
    return _flush(path)


def delete_name(ordinal: int, path: Optional[str] = None) -> bool:
    return save_name(ordinal, "", path)


def _flush(path: str) -> bool:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in _USER_MAP.items()}, f, indent=2)
        return True
    except OSError:
        return False


def load_user_map(path: str) -> int:
    """Merge an updatable {ordinal: name} JSON map. Returns count loaded."""
    if not path or not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        n = 0
        repaired = False
        for k, v in raw.items():
            try:
                clean = _clean_name(v)        # repair old serialized-record blobs
                if clean != str(v):
                    repaired = True
                _USER_MAP[int(k)] = clean
                n += 1
            except (TypeError, ValueError):
                continue
        if repaired:
            _flush(path)                      # rewrite the store with clean names
        return n
    except (json.JSONDecodeError, OSError):
        return 0
