"""Read the in-game tyre-temp (Heat) page: 3-zone inner/mid/outer per tyre.

These per-tread temps are the ONLY way to tune camber correctly - the UDP feed
gives just one temp per tyre. The reading is done by AUTOMATED OCR (pytesseract)
of a screenshot; if OCR is unavailable or low-confidence, it falls back to typed
manual entry (never hard-blocks).

Heat page layout (from a real 2560x1440 capture): four corner blocks in the four
screen quadrants - Front Left top-left, Front Right top-right, Rear Left
bottom-left, Rear Right bottom-right. Each block stacks Inner / Middle / Outer
vertically (top->bottom). Values look like "66.8 C". The reader anchors on
relative position (quadrant + vertical order) so it is not pinned to one
resolution, detects the unit (C/F), and normalizes everything to Celsius.

Returns: {"FL": {"inner": c, "mid": c, "outer": c}, "FR": {...}, "RL": {...}, "RR": {...}}
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from . import capture

KIND = "tyre_temps_3zone"
SCHEMA = ('{"unit":"C"|"F", "FL":{"inner":t,"mid":t,"outer":t},"FR":{...},'
          '"RL":{...},"RR":{...}} - report the unit shown on the page; inner = '
          'side nearest car centre, outer = side nearest the bodywork edge')

_TYRES = ("FL", "FR", "RL", "RR")
_ZONES = ("inner", "mid", "outer")
_PLAUSIBLE_C = (-20.0, 200.0)   # accept after F->C normalization


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


# --------------------------------------------------------------------------
# Value-text parsing - pure, unit-testable without an image or Tesseract.
# --------------------------------------------------------------------------
def _parse_temp_text(txt: str) -> Optional[float]:
    """Parse one digit-whitelisted OCR string into a temperature. Handles a clean
    decimal ('66.8'), a dropped decimal ('668' -> 66.8, '1210' -> 121.0), and a
    2-digit read ('66')."""
    if not txt:
        return None
    m = re.search(r"\d{1,3}\.\d", txt)
    if m:
        return float(m.group())
    digits = re.sub(r"[^\d]", "", txt)
    if len(digits) in (3, 4):                 # decimal point lost in OCR
        return float(f"{digits[:-1]}.{digits[-1]}")
    if len(digits) == 2:
        return float(digits)
    return None


def _unit_from_values(vals: List[float]) -> str:
    """Detect C vs F from magnitude (digit whitelist drops the degree glyph):
    real tyre temps rarely exceed ~130C, but in F they routinely do."""
    if not vals:
        return "C"
    vals = sorted(vals)
    median = vals[len(vals) // 2]
    return "F" if median > 130.0 else "C"


def _is_valid(out: Dict[str, Dict[str, float]]) -> bool:
    lo, hi = _PLAUSIBLE_C
    count = 0
    for tyre in _TYRES:
        z = out.get(tyre) or {}
        for k in _ZONES:
            if k not in z:
                return False
            if not (lo <= z[k] <= hi):
                return False
            count += 1
    return count == 12


def _normalize(data: dict) -> Dict[str, Dict[str, float]]:
    unit = str(data.get("unit", "C")).strip().upper()
    conv = _f_to_c if unit == "F" else (lambda x: x)
    out: Dict[str, Dict[str, float]] = {}
    for tyre in _TYRES:
        z = data.get(tyre) or {}
        try:
            out[tyre] = {k: conv(float(z[k])) for k in _ZONES if k in z}
        except (TypeError, ValueError):
            out[tyre] = {}
    return out


UDP_XCHECK_TOL_C = 12.0           # OCR/vision tyre avg must be within this of UDP TireTemp


def _udp_crosscheck(out: Dict[str, Dict[str, float]],
                    udp_temps: Optional[Dict[str, float]]):
    """Each tyre's reading average (Celsius) must land within a few degrees of the
    trusted UDP TireTemp for that corner. Returns (ok, per-tyre detail dict)."""
    if not udp_temps:
        return True, {}
    detail = {}
    ok = True
    for tyre in _TYRES:
        z = out.get(tyre) or {}
        if len(z) == 3 and tyre in udp_temps:
            avg = sum(z.values()) / 3.0
            d = avg - udp_temps[tyre]
            detail[tyre] = f"read {avg:.0f} vs udp {udp_temps[tyre]:.0f} ({d:+.0f})"
            if abs(d) > UDP_XCHECK_TOL_C:
                ok = False
    return ok, detail


# === PRIMARY reader: RapidOCR (bundled, OFFLINE, resolution-INDEPENDENT) =====
# PP-OCR ONNX models on onnxruntime (CPU, ~tens of MB, Apache-2.0). Detects +
# recognizes text ANYWHERE in the frame - no fixed boxes, no API, no network.
# Numbers are anchored to tyres by the on-screen LABELS (Front/Rear Left/Right),
# with a position-only fallback - both relative to detected positions, so it's
# resolution / aspect / HUD-scale independent.
_CORNER_ALIASES = {
    "FL": ("frontleft",), "FR": ("frontright",),
    "RL": ("rearleft",), "RR": ("rearright",),
}
_RAPID_ENGINE = None


def rapidocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        try:
            import rapidocr  # newer package name  # noqa: F401
            return True
        except Exception:
            return False


def _get_rapid_engine():
    global _RAPID_ENGINE
    if _RAPID_ENGINE is not None:
        return _RAPID_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        from rapidocr import RapidOCR  # type: ignore
    _RAPID_ENGINE = RapidOCR()        # bundled default PP-OCR models
    return _RAPID_ENGINE


def _box_center(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return sum(xs) / len(xs), sum(ys) / len(ys)


# FH6 Heat-page temps are always shown with ONE decimal (e.g. "66.8", "152.3°F").
# Requiring that exact shape rejects HUD junk - speed "015", position "1/12",
# lap "1:23.4", gamertags - so the positional fallback isn't polluted.
_TEMP_RE = re.compile(r"^[+-]?\d{1,3}\.\d\s*[°\xba]?\s*[CFcf]?$")


def _temp_tokens(tokens):
    """From [(text,(x,y))] keep TEMPERATURE numbers as (val,x,y). Matches only the
    single-decimal Heat-page format, dropping speed / lap / position / name junk."""
    out = []
    for text, (x, y) in tokens:
        t = str(text).strip()
        if not _TEMP_RE.match(t):
            continue
        val = _parse_temp_text(re.sub(r"[^\d.]", "", t))
        if val is not None and 20.0 <= val <= 300.0:
            out.append((val, x, y))
    return out


def _corner_centers(tokens):
    corners = {}
    for text, (x, y) in tokens:
        t = re.sub(r"[^a-z]", "", text.lower())
        for key, aliases in _CORNER_ALIASES.items():
            if any(a in t for a in aliases):
                corners[key] = (x, y)
    return corners


def _zones_from_sorted(triple):
    vals = [v for _, v in sorted(triple)]      # sorted by y (top->bottom)
    return {"inner": vals[0], "mid": vals[1], "outer": vals[2]}


def _map_by_labels(numbers, corners):
    """Assign each number to its NEAREST corner label, then top->bottom in each."""
    if len(corners) < 4:
        return None
    buckets = {k: [] for k in corners}
    for val, x, y in numbers:
        k = min(corners, key=lambda c: (corners[c][0] - x) ** 2 + (corners[c][1] - y) ** 2)
        buckets[k].append((y, val))
    out = {}
    for k, lst in buckets.items():
        if len(lst) >= 3:
            out[k] = _zones_from_sorted(lst[:3] if len(lst) == 3 else sorted(lst)[:3])
    return out if len(out) == 4 else None


def _map_positional(numbers):
    """No labels: split left/right by x, each column's numbers top->bottom into a
    front (upper 3) and rear (next 3) tyre. Relative to detected positions."""
    if len(numbers) < 12:
        return None
    xs = sorted(n[1] for n in numbers)
    xmed = xs[len(xs) // 2]
    left = [n for n in numbers if n[1] < xmed]
    right = [n for n in numbers if n[1] >= xmed]
    if len(left) < 6 or len(right) < 6:
        return None

    def column(group):
        g = sorted(group, key=lambda n: n[2])      # by y
        front = [(n[2], n[0]) for n in g[:3]]
        rear = [(n[2], n[0]) for n in g[3:6]]
        return _zones_from_sorted(front), _zones_from_sorted(rear)

    fl, rl = column(left)
    fr, rr = column(right)
    return {"FL": fl, "FR": fr, "RL": rl, "RR": rr}


def tokens_to_reading(tokens, udp_temps=None):
    """Pure: [(text,(x,y))] -> normalized Celsius schema, or None. Anchors numbers
    to tyres by labels (fallback: position), detects unit, UDP cross-checks."""
    log = logging.getLogger("lapsmith.ocr")
    numbers = _temp_tokens(tokens)
    if len(numbers) < 12:
        log.warning("RapidOCR: only %d temp tokens found", len(numbers))
        return None
    mapping = _map_by_labels(numbers, _corner_centers(tokens)) or _map_positional(numbers)
    if not mapping:
        log.warning("RapidOCR: could not map %d numbers to tyres", len(numbers))
        return None
    unit = _choose_unit([v for vals in mapping.values() for v in vals.values()], udp_temps)
    raw = {"unit": unit, **mapping}
    out = _normalize(raw)
    if not _is_valid(out):
        return None
    ok, detail = _udp_crosscheck(out, udp_temps)
    log.info("RapidOCR read unit=%s xcheck=%s -> %s", unit, detail or "none",
             {k: {z: round(v, 1) for z, v in d.items()} for k, d in out.items()})
    return out if ok else None


def rapidocr_read_image(path: str, udp_temps: Optional[Dict[str, float]] = None
                        ) -> Optional[Dict[str, Dict[str, float]]]:
    """PRIMARY reader. Local PP-OCR ONNX, no network, no API key. Reads the FULL
    frame at native resolution (no crop). Returns Celsius schema or None."""
    log = logging.getLogger("lapsmith.ocr")
    if not rapidocr_available():
        return None
    try:
        eng = _get_rapid_engine()
        result, _elapse = eng(path)
    except Exception as e:
        log.warning("RapidOCR failed: %s", e)
        return None
    if not result:
        log.warning("RapidOCR found no text in %s", path)
        return None
    tokens = []
    for item in result:
        try:
            box, text = item[0], item[1]
            tokens.append((str(text), _box_center(box)))
        except Exception:
            continue
    reading = tokens_to_reading(tokens, udp_temps)
    if reading is None:
        # Diagnostic for "0 temp tokens": did RapidOCR see NO text (overlay not in the
        # captured frame / wrong capture) or LOTS of text but none parsed as temps (the
        # temp page wasn't up, or a number-format issue)? The distinction tells a user
        # whether to fix capture vs make sure the tyre-temp page is actually showing.
        nums = _temp_tokens(tokens)
        sample = [t[0] for t in tokens[:8]]
        log.warning("RapidOCR: %d text tokens detected, %d looked like temps "
                    "(need >=12). Sample: %s%s", len(tokens), len(nums), sample,
                    " <- no text at all: the tyre-temp page is likely not in the "
                    "captured frame" if not tokens else "")
    return reading


# === OPT-IN reader: vision model via Anthropic API (off by default) ==========
# A vision model reads the whole image regardless of resolution / aspect / HUD
# scale - no pixel coordinates. This is the universal path; Tesseract (below) is
# a best-effort 16:9 fallback and manual entry is the final fallback.
_VISION_ZONE = {"type": "object", "additionalProperties": False,
                "properties": {z: {"type": "number"} for z in _ZONES},
                "required": list(_ZONES)}
_VISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"unit": {"type": "string", "enum": ["C", "F"]},
                   **{t: _VISION_ZONE for t in _TYRES}},
    "required": ["unit", *_TYRES],
}


def vision_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


def _parse_vision_json(text: str, udp_temps=None):
    """Pure: parse the model's JSON -> normalized Celsius schema, or None.
    Applies the UDP cross-check when udp_temps is given."""
    log = logging.getLogger("lapsmith.vision")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        log.warning("vision returned non-JSON: %r", (text or "")[:200])
        return None
    out = _normalize(data)
    if not _is_valid(out):
        log.warning("vision JSON missing/implausible temps: %s", data)
        return None
    ok, detail = _udp_crosscheck(out, udp_temps)
    log.info("vision read unit=%s xcheck=%s -> %s", data.get("unit"), detail or "none",
             {k: {z: round(v, 1) for z, v in d.items()} for k, d in out.items()})
    if not ok:
        log.warning("vision rejected by UDP cross-check: %s", detail)
        return None
    return out


def vision_read_image(path: str, udp_temps: Optional[Dict[str, float]] = None
                      ) -> Optional[Dict[str, Dict[str, float]]]:
    """Read the Heat page with a vision-capable Claude model (Anthropic API).
    Captures the FULL game surface at native resolution; downscales only enough
    that digits stay legible (long edge <= FH6_VISION_MAX_EDGE). Returns the
    schema in Celsius, or None to fall through to Tesseract/manual."""
    log = logging.getLogger("lapsmith.vision")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        log.warning("vision: could not open %s", path)
        return None
    max_edge = int(os.environ.get("FH6_VISION_MAX_EDGE", "2200"))
    w, h = img.size
    scale = min(1.0, max_edge / max(w, h))     # downscale only if too large; NO crop
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    model = os.environ.get("FH6_VISION_MODEL", "claude-opus-4-8")
    prompt = ("This is a Forza Horizon tyre-temperature 'Heat' telemetry page overlaid on "
              "gameplay. Read the Inner / Middle / Outer tread temperatures for all four "
              "tyres - Front Left, Front Right, Rear Left, Rear Right - and the unit shown "
              "(C or F). Inner = side nearest the car centre, outer = nearest the bodywork "
              f"edge. Return only JSON matching: {SCHEMA}")
    try:
        client = anthropic.Anthropic()
        resp = client.with_options(timeout=float(os.environ.get("FH6_VISION_TIMEOUT", "30"))
                                   ).messages.create(
            model=model, max_tokens=400,
            messages=[{"role": "user", "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt}]}],
            output_config={"format": {"type": "json_schema", "schema": _VISION_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
    except Exception as e:
        log.warning("vision API call failed (%s): %s", model, e)
        return None
    return _parse_vision_json(text, udp_temps)


# --------------------------------------------------------------------------
# OCR driver
# --------------------------------------------------------------------------
def _bundled_tesseract() -> Optional[str]:
    """Locate a Tesseract binary bundled with the app (so OCR works zero-setup):
      1. $FH6_TESSERACT (explicit override),
      2. PyInstaller temp dir (sys._MEIPASS)/tesseract/tesseract.exe,
      3. ./tesseract/tesseract.exe next to the package.
    Returns a path or None (then the system PATH copy, if any, is used)."""
    import sys
    env = os.environ.get("FH6_TESSERACT")
    cands = []
    if env:
        cands.append(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(os.path.join(meipass, "tesseract", "tesseract.exe"))
        cands.append(os.path.join(meipass, "tesseract.exe"))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands.append(os.path.join(here, "tesseract", "tesseract.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def configure_tesseract() -> Optional[str]:
    """Point pytesseract at the bundled binary if present. Returns the path used."""
    try:
        import pytesseract
    except Exception:
        return None
    path = _bundled_tesseract()
    if path:
        pytesseract.pytesseract.tesseract_cmd = path
    return path


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        configure_tesseract()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


# 12 TIGHT value boxes as fractions of (width, height) - just the digits. Left
# corners' numbers sit far left, right corners' far right; front cluster upper,
# rear cluster mid (a real 1440p Heat page). Inner/Middle/Outer stacked top->bottom.
# CALIBRATE by setting FH6_HEAT_BOXES to JSON:
#   {"x_left":[0.005,0.15],"x_right":[0.85,0.995],
#    "y_front":[[0.195,0.275],[0.30,0.38],[0.415,0.495]],
#    "y_rear":[[0.48,0.56],[0.585,0.665],[0.70,0.78]]}
_BOX_DEFAULTS = {
    "x_left": [0.005, 0.150], "x_right": [0.850, 0.995],
    "y_front": [[0.195, 0.275], [0.300, 0.380], [0.415, 0.495]],
    "y_rear": [[0.480, 0.560], [0.585, 0.665], [0.700, 0.780]],
}
OCR_UPSCALE = 6
OCR_VALID_RAW = (20.0, 290.0)     # plausible single temp before unit normalize (C or F)


def _box_config() -> dict:
    cfg = dict(_BOX_DEFAULTS)
    env = os.environ.get("FH6_HEAT_BOXES")
    if env:
        try:
            import json
            cfg.update(json.loads(env))
        except Exception:
            logging.getLogger("lapsmith.ocr").warning(
                "FH6_HEAT_BOXES is not valid JSON - using defaults.")
    return cfg


def _value_boxes(w: int, h: int) -> Dict[str, Dict[str, Tuple[int, int, int, int]]]:
    cfg = _box_config()
    boxes: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {}
    for tyre in _TYRES:
        xr = cfg["x_left"] if tyre[1] == "L" else cfg["x_right"]
        ys = cfg["y_front"] if tyre[0] == "F" else cfg["y_rear"]
        boxes[tyre] = {z: (int(xr[0] * w), int(ys[i][0] * h),
                           int(xr[1] * w), int(ys[i][1] * h))
                       for i, z in enumerate(_ZONES)}
    return boxes


def _otsu(gray) -> int:
    """Otsu threshold from a PIL 'L' image histogram (no numpy/scipy)."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * hist[i] for i in range(256))
    wB = 0
    sumB = 0.0
    best_var = -1.0
    thr = 128
    for t in range(256):
        wB += hist[t]
        if wB == 0:
            continue
        wF = total - wB
        if wF == 0:
            break
        sumB += t * hist[t]
        mB = sumB / wB
        mF = (sum_all - sumB) / wF
        between = wB * wF * (mB - mF) ** 2
        if between > best_var:
            best_var = between
            thr = t
    return thr


def _read_box(crop):
    """Read one tight digit box. Upscale ~6x + light blur, then try several
    thresholds (Otsu first) and pick the first that yields a plausible number.
    Returns (value_or_None, threshold_used, [attempt strings])."""
    import pytesseract
    from PIL import Image, ImageFilter
    g = crop.convert("L").resize(
        (max(1, crop.width * OCR_UPSCALE), max(1, crop.height * OCR_UPSCALE)),
        Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.0))
    otsu = _otsu(g)
    attempts = []
    lo, hi = OCR_VALID_RAW
    for th in (otsu, otsu - 18, otsu + 18, 200, 175, 150, 125):
        th = max(1, min(254, th))
        # digits are the bright pixels (> th) -> render them BLACK on white
        bw = g.point(lambda p, _t=th: 0 if p > _t else 255)
        try:
            txt = pytesseract.image_to_string(
                bw, config="--psm 7 -c tessedit_char_whitelist=0123456789.").strip()
        except Exception as e:
            txt = f"<err:{e}>"
        val = _parse_temp_text(txt)
        attempts.append(f"th{th}:'{txt}'->{val}")
        if val is not None and lo <= val <= hi:
            return val, th, attempts
    return None, None, attempts


def _choose_unit(values: List[float], udp_c: Optional[Dict[str, float]]) -> str:
    """Unit detection. If UDP temps (Celsius) are available, pick the unit whose
    normalized OCR best matches them; else fall back to magnitude."""
    if not values:
        return "C"
    if udp_c:
        med_ocr = sorted(values)[len(values) // 2]
        med_udp = sorted(udp_c.values())[len(udp_c) // 2]
        err_c = abs(med_ocr - med_udp)
        err_f = abs(_f_to_c(med_ocr) - med_udp)
        return "F" if err_f < err_c else "C"
    return _unit_from_values(values)


def ocr_heat_page(path: str, udp_temps: Optional[Dict[str, float]] = None
                  ) -> Optional[Dict[str, Dict[str, float]]]:
    """OCR a Heat-page screenshot into the schema (Celsius), or None if unreliable.

    Per box: adaptive (Otsu) + multi-threshold with upscale/blur, format+range
    validated. `udp_temps` (the trusted UDP TireTemp per tyre IN CELSIUS, from the
    captured frame) is cross-checked: each tyre's OCR average must land within a
    few degrees, else the reading is rejected. Dumps per-box text + chosen
    threshold + the cross-check to app.log every attempt."""
    log = logging.getLogger("lapsmith.ocr")
    try:
        import pytesseract  # noqa: F401
        from PIL import Image
    except Exception:
        return None
    configure_tesseract()
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        log.warning("OCR: could not open %s", path)
        return None
    w, h = img.size
    boxes = _value_boxes(w, h)

    raw: dict = {}
    dump: Dict[str, str] = {}
    values: List[float] = []
    for tyre in _TYRES:
        zone_vals = {}
        for z in _ZONES:
            try:
                val, th, attempts = _read_box(img.crop(boxes[tyre][z]))
            except Exception as e:
                val, th, attempts = None, None, [f"<err:{e}>"]
            dump[f"{tyre}.{z}"] = f"={val} (th={th}) {attempts}"
            if val is not None:
                zone_vals[z] = val
                values.append(val)
        if len(zone_vals) == 3:
            raw[tyre] = zone_vals

    unit = _choose_unit(values, udp_temps)
    raw["unit"] = unit
    out = _normalize(raw)

    # UDP cross-check (both in Celsius): reject if any tyre's OCR average is too
    # far from the trusted UDP temp - that's a misread, not a real reading.
    ok_xcheck, xcheck = _udp_crosscheck(out, udp_temps)
    log.info("OCR boxes %dx%d unit=%s xcheck=%s | %s", w, h, unit,
             xcheck or "none", dump)

    if _is_valid(out) and ok_xcheck:
        log.info("OCR success: %s", {k: {z: round(v, 1) for z, v in d.items()}
                                     for k, d in out.items()})
        return out
    if not ok_xcheck:
        log.warning("OCR rejected by UDP cross-check (misread): %s", xcheck)
    else:
        log.warning("OCR failed validation (need 12 plausible temps; got %d).", len(values))
    return None


# --------------------------------------------------------------------------
# manual fallback + public entry points
# --------------------------------------------------------------------------
def _manual() -> Dict[str, Dict[str, float]]:
    unit = ""
    while unit not in ("C", "F"):
        unit = (input("Are the on-screen tyre temps in C or F? [C/F]: ").strip().upper() or "C")
    print(f"Enter tyre tread temps in {unit} (inner / mid / outer per tyre).")
    out: dict = {"unit": unit}
    for tyre in _TYRES:
        zones = {}
        for z in _ZONES:
            while True:
                raw = input(f"  {tyre} {z:5s} {unit}: ").strip()
                try:
                    zones[z] = float(raw)
                    break
                except ValueError:
                    print("    enter a number")
        out[tyre] = zones
    return _normalize(out)


def read_image(path: str, *, manual: bool = False, announce=None
               ) -> Dict[str, Dict[str, float]]:
    """Read an already-captured Heat-page screenshot. OCR first, manual fallback."""
    say = announce or print
    if not manual:
        if ocr_available():
            result = ocr_heat_page(path)
            if result is not None:
                say("[ocr] Heat page read automatically (12 temps).")
                return result
            say("[ocr] Could not confidently read 12 temps from the screenshot.")
        else:
            say("[ocr] Tesseract/pytesseract not available "
                "(pip install pytesseract + install Tesseract-OCR).")
    say("[vision] Falling back to manual tyre-temp entry.")
    return _manual()


def read(*, manual: bool = False, tag: int | None = None,
         image_path: Optional[str] = None, timeout_s: float = 180.0,
         announce=None) -> Dict[str, Dict[str, float]]:
    """Capture (if needed) and read the Heat page. If `image_path` is given (e.g.
    a peak-load frame grabbed during the drive) it is OCR'd directly."""
    say = announce or print
    if manual:
        return _manual()
    if image_path is None:
        say(">> Make sure the in-game tyre-temperature (Heat) page is visible.")
        if not capture.backend_available():
            say("[vision] No screenshot backend; switching to manual entry.")
            return _manual()
        image_path = capture.grab("tyre_temps", monotonic_tag=tag)
    return read_image(image_path, manual=False, announce=say)
