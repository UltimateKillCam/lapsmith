"""Import a community FH6 car-name database (ordinal -> name) into car_names.json.

LapSmith ships NO third-party car list. The user downloads one from the Nexus Mods
"Forza Horizon 6 Car ID List" page and imports it here. Accepts the common
community formats - CSV / TSV (name,ordinal OR ordinal,name, auto-detected) and
JSON (a {ordinal: name} object, a list of {id,name}-style objects, or a list of
[ordinal, name] pairs). MERGE-only: a name the user set or edited always wins;
imported names only fill gaps. Returns a summary so the user can spot-check.
"""
from __future__ import annotations

import csv
import io
import json
import os
from typing import Dict, Optional, Tuple

from . import ordinals, PRODUCT_NAME

# Where to GET the data (shown in the import dialog). NOT bundled - user downloads.
NEXUS_CAR_LIST_TITLE = "Forza Horizon 6 Car ID List"
NEXUS_CAR_LIST_URL = "https://www.nexusmods.com/forzahorizon6/mods/309"
# Credit the mod author by name (confirmed from the Nexus page). The dialog always
# links to the page; we ship NONE of their data - the user downloads it themselves.
NEXUS_CAR_LIST_AUTHOR = "xEDWARDSZz"

# Keys we recognise inside JSON objects (case-insensitive).
_ID_KEYS = ("ordinal", "id", "carid", "car_id", "carordinal", "car_ordinal",
            "value", "ordinalid", "ordinal_id")
_NAME_KEYS = ("name", "model", "car", "carname", "car_name", "title",
              "displayname", "display_name", "fullname", "full_name")
_HEADER_TOKENS = set(_ID_KEYS) | set(_NAME_KEYS)


def _to_int(s) -> Optional[int]:
    """Parse a positive ordinal from a cell, tolerating commas / a trailing .0."""
    try:
        t = str(s).strip().replace(",", "")
        if t == "":
            return None
        n = int(float(t)) if "." in t else int(t)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def default_names_path() -> str:
    """The car_names.json the GUI uses: %APPDATA%/LapSmith/car_names.json."""
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, PRODUCT_NAME, "car_names.json")


def parse_text(text: str, filename: str = "") -> Tuple[Dict[int, str], int]:
    """Parse car-id data into ({ordinal: name}, malformed_count). Format is chosen
    by extension then by content (JSON if it starts with [ or {)."""
    fn = (filename or "").lower()
    stripped = text.lstrip("﻿ \t\r\n")
    if fn.endswith(".json") or stripped[:1] in "[{":
        return _parse_json(stripped)
    # delimited: prefer TSV if tabs dominate or the extension says so
    delim = "\t" if (fn.endswith(".tsv") or
                     (text.count("\t") and text.count("\t") >= text.count(","))) else ","
    return _parse_delimited(text, delim)


def _parse_delimited(text: str, delim: str) -> Tuple[Dict[int, str], int]:
    mapping: Dict[int, str] = {}
    malformed = 0
    for row in csv.reader(io.StringIO(text), delimiter=delim):
        cells = [c.strip() for c in row if c is not None and c.strip() != ""]
        if len(cells) < 2:
            if cells:
                malformed += 1
            continue
        a, b = cells[0], cells[1]
        ai, bi = _to_int(a), _to_int(b)
        if ai and not bi:
            ordn, name = ai, b
        elif bi and not ai:
            ordn, name = bi, a
        elif ai and bi:                     # both numeric: assume ordinal first
            ordn, name = ai, b
        else:                               # neither numeric -> header or junk
            if a.lower() in _HEADER_TOKENS or b.lower() in _HEADER_TOKENS:
                continue                    # silently skip a header row
            malformed += 1
            continue
        mapping[ordn] = name
    return mapping, malformed


def _find(d: dict, keys) -> Optional[object]:
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] not in (None, ""):
            return low[k]
    return None


def _parse_json(text: str) -> Tuple[Dict[int, str], int]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}, 0
    if isinstance(data, dict):
        for key in ("cars", "data", "list", "items"):
            if isinstance(data.get(key), list):
                return _parse_json_list(data[key])
        # otherwise a flat {ordinal: name} (or {name: ordinal}) object
        mapping: Dict[int, str] = {}
        malformed = 0
        for k, v in data.items():
            ki, vi = _to_int(k), _to_int(v)
            if ki and isinstance(v, str) and not vi:
                mapping[ki] = v.strip()
            elif vi and isinstance(k, str) and not ki:
                mapping[vi] = k.strip()
            elif ki:                        # numeric key wins as the ordinal
                mapping[ki] = str(v).strip()
            else:
                malformed += 1
        return mapping, malformed
    if isinstance(data, list):
        return _parse_json_list(data)
    return {}, 0


def _parse_json_list(items) -> Tuple[Dict[int, str], int]:
    mapping: Dict[int, str] = {}
    malformed = 0
    for it in items:
        if isinstance(it, dict):
            ordn = _to_int(_find(it, _ID_KEYS))
            name = _find(it, _NAME_KEYS)
            if ordn and name:
                mapping[ordn] = str(name).strip()
            else:
                malformed += 1
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            ai, bi = _to_int(it[0]), _to_int(it[1])
            if ai and not bi:
                mapping[ai] = str(it[1]).strip()
            elif bi and not ai:
                mapping[bi] = str(it[0]).strip()
            elif ai:
                mapping[ai] = str(it[1]).strip()
            else:
                malformed += 1
        else:
            malformed += 1
    return mapping, malformed


def import_text(text: str, filename: str = "") -> Dict[str, int]:
    """Parse + merge text. Returns a summary: parsed, imported, already, malformed."""
    mapping, malformed = parse_text(text, filename)
    merged = ordinals.bulk_fill(mapping)
    return {
        "parsed": len(mapping),
        "imported": merged["imported"],
        "already": merged["already"],
        "malformed": malformed,
    }


def import_file(path: str) -> Dict[str, int]:
    """Read a downloaded car-id file (CSV/TSV/JSON) and merge it into car_names.json.
    Raises OSError if the file can't be read."""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    return import_text(text, path)
