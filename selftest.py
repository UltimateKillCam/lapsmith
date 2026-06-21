"""End-to-end dry run with NO game: parser offsets, rules, and a live UDP loop.

    python selftest.py

Exercises:
  1. parser offset correctness (Fahrenheit->Celsius, speed offset, etc.),
  2. analyzer fires the expected rule per synthetic scenario,
  3. a real 127.0.0.1 UDP round-trip through the listener.
"""
from __future__ import annotations

import json
import socket
import struct
import time

from lapsmith.telemetry.parser import (parse, _FIELDS, FH6_BASE_LEN,
                                           FH6_WITH_WEAR_LEN, ParseError)
from lapsmith.telemetry.listener import TelemetryListener
from lapsmith.telemetry.session import aggregate
from lapsmith.telemetry import segment
from lapsmith.knowledge import rules
from lapsmith.knowledge.baseline import build_baseline
from lapsmith.state.tune_state import CarLimits, Tune
from lapsmith.vision import read_tyres
from lapsmith import simulator

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# Independently-authored expected offset table (NOT derived from parser._FIELDS).
# OFFSET LAYOUT (PositionX@244, Speed@256, TireTemp@268, base 323 = FM7 311 + a
# 12-byte Horizon insert) is corroborated by the official FH6 doc, fh6-tel
# parser.rs, richstokes FH4_packetformat.dat (FH4 carries a 12-byte insert after
# NumCylinders - CarCategory(4)+HorizonUnknown1(4)+HorizonUnknown2(4) - so FH4's
# downstream offsets match FH6's), and a first-principles walk. The SEMANTICS of
# bytes 232-243 (CarGroup/SmashableVelDiff/SmashableMass) come from the FH6 doc +
# fh6-tel only; those FH6 names are absent from the FH4 file (grep confirms).
EXPECTED_OFFSETS = {
    "is_race_on": (0, "i"), "timestamp_ms": (4, "I"), "engine_max_rpm": (8, "f"),
    "current_engine_rpm": (16, "f"), "accel_x": (20, "f"),
    "susp_norm_fl": (68, "f"), "tire_slip_ratio_fl": (84, "f"),
    "tire_slip_angle_fl": (164, "f"), "tire_combined_slip_fl": (180, "f"),
    "susp_travel_m_fl": (196, "f"), "car_ordinal": (212, "i"),
    "car_class": (216, "i"), "drivetrain_type": (224, "i"),
    "num_cylinders": (228, "i"),
    "car_group": (232, "i"), "smashable_vel_diff": (236, "f"),
    "smashable_mass": (240, "f"),                       # FH6 12-byte insert
    "position_x": (244, "f"), "speed": (256, "f"), "power": (260, "f"),
    "tire_temp_fl_f": (268, "f"), "boost": (284, "f"),
    "distance_traveled": (292, "f"), "best_lap": (296, "f"),
    "last_lap": (300, "f"), "current_lap": (304, "f"),     # auto-lap fitness/timing
    "lap_number": (312, "H"), "race_position": (314, "B"), "accel": (315, "B"),
    "brake": (316, "B"), "gear": (319, "B"), "steer": (320, "b"),
}


def test_offsets():
    print("\n== parser exact offsets ==")
    fields_map = {n: (o, f) for n, f, o in _FIELDS}
    # 1. parser._FIELDS must match the independent expected table
    mism = [n for n, (o, f) in EXPECTED_OFFSETS.items() if fields_map.get(n) != (o, f)]
    check(f"_FIELDS offsets match expected table (mismatches: {mism})", not mism)

    # 2. round-trip a distinct value into each offset and read it back
    buf = bytearray(FH6_BASE_LEN)
    vals = {}
    for i, (name, (off, fmt)) in enumerate(EXPECTED_OFFSETS.items()):
        val = (1 if fmt in "iI" else 0) + (i + 1)
        if fmt in "fB":
            val = float(i + 7) if fmt == "f" else (i + 7)
        if fmt == "b":
            val = -(i + 1)
        if fmt in "HB":
            val = (i + 7) % 200
        struct.pack_into("<" + fmt, buf, off, val)
        vals[name] = val
    pkt = parse(bytes(buf))
    bad = []
    for name, v in vals.items():
        got = getattr(pkt, name)
        if name == "tire_temp_fl_f":
            continue  # checked via Celsius below
        if abs(float(got) - float(v)) > 0.001:
            bad.append((name, v, got))
    check(f"every field reads back at its offset (bad: {bad})", not bad)

    check("FH6 base length is 323", FH6_BASE_LEN == 323)
    check("drivetrain decodes (2 -> AWD)", parse(_awd_buf()).drivetrain_name == "AWD")

    # 3. Fahrenheit -> Celsius at byte 268
    b2 = bytearray(FH6_BASE_LEN)
    struct.pack_into("<f", b2, 268, 212.0)        # 212F == 100C
    check("212F at byte 268 -> 100C", abs(parse(bytes(b2)).tire_temp_fl - 100.0) < 0.05)
    struct.pack_into("<f", b2, 268, 32.0)         # 32F == 0C
    check("32F at byte 268 -> 0C", abs(parse(bytes(b2)).tire_temp_fl - 0.0) < 0.05)

    # 4. variable length: trailing bytes are OPTIONAL, interpreted by count
    check("rejects < 323 bytes", _raises(lambda: parse(bytes(322))))
    check("accepts 323 (no trailing, no wear)",
          parse(bytes(FH6_BASE_LEN)).tire_wear_fl is None)

    # 324 = NORMAL live FH6 (base + 1 HorizonTrailingUnknown byte): must parse,
    # core fields intact, no wear. This is the real-stream case that was failing.
    awd324 = bytearray(_awd_buf()) + bytearray(1)        # 323 -> 324
    struct.pack_into("<f", awd324, 256, 61.0)            # Speed
    struct.pack_into("<f", awd324, 268, 212.0)           # TireTempFL 212F=100C
    p324 = parse(bytes(awd324))
    check("accepts 324 (normal live FH6)", p324.packet_len == 324)
    check("324 core fields decode (AWD, speed, 100C, no wear)",
          p324.drivetrain_name == "AWD" and abs(p324.speed - 61.0) < 0.01
          and abs(p324.tire_temp_fl - 100.0) < 0.05 and p324.tire_wear_fl is None)

    # 339 = base + 16-byte tyre-wear block at offset 323
    bw = bytearray(FH6_WITH_WEAR_LEN)
    struct.pack_into("<f", bw, 323, 0.85)
    check("accepts 339 and reads tyre wear",
          abs((parse(bytes(bw)).tire_wear_fl or 0) - 0.85) < 0.001)


def _awd_buf():
    b = bytearray(FH6_BASE_LEN)
    struct.pack_into("<i", b, 224, 2)
    return bytes(b)


def _raises(fn):
    try:
        fn()
        return False
    except ParseError:
        return True


def test_segment_timer():
    print("\n== free-roam segment timer (time + position, NOT DistanceTraveled) ==")
    # 12.5s; displacement from PositionX/Z = 300/400 -> 500m. DistanceTraveled = 0
    # everywhere (it does not advance in free-roam) and must be IGNORED.
    start = segment.Mark(timestamp_ms=10_000, position_x=0.0, position_z=0.0,
                         speed=40.0, distance_m=0.0)
    end = segment.Mark(timestamp_ms=22_500, position_x=300.0, position_z=400.0,
                       speed=40.0, distance_m=0.0)
    run = segment.measure(None, start, end, reference_distance_m=500.0)
    check("elapsed 12.5s (from TimestampMS)", abs(run.elapsed_s - 12.5) < 0.001 and run.valid)
    check("displacement 500m from PositionX/Z with DistanceTraveled=0",
          abs(run.distance_m - 500.0) < 0.001)

    # u32 TimestampMS overflow handled; movement via position
    start = segment.Mark((1 << 32) - 500, 0.0, 0.0, 40.0, 0.0)
    end = segment.Mark(500, 0.0, 300.0, 40.0, 0.0)
    run = segment.measure(None, start, end, reference_distance_m=300.0)
    check(f"overflow elapsed 1.0s (got {run.elapsed_s:.3f})", abs(run.elapsed_s - 1.0) < 0.001)

    # car barely moved (position delta tiny) even though time elapsed -> invalid
    start = segment.Mark(0, 10.0, 10.0, 0.0, 0.0)
    end = segment.Mark(5000, 12.0, 11.0, 0.0, 0.0)
    run = segment.measure(None, start, end, reference_distance_m=500.0)
    check("no real movement -> invalid", not run.valid)

    # wrong segment (displacement far off reference) is rejected
    start = segment.Mark(0, 0.0, 0.0, 40.0, 0.0)
    end = segment.Mark(5000, 0.0, 120.0, 40.0, 0.0)
    run = segment.measure(None, start, end, reference_distance_m=500.0)
    check("mismatched-displacement run flagged invalid", not run.valid)


def test_unit_reading():
    print("\n== unit-aware tyre reading ==")
    # page in Fahrenheit -> normalized to Celsius
    f_data = {"unit": "F",
              "FL": {"inner": 212, "mid": 194, "outer": 176},  # 100/90/80 C
              "FR": {"inner": 212, "mid": 194, "outer": 176},
              "RL": {"inner": 176, "mid": 176, "outer": 176},
              "RR": {"inner": 176, "mid": 176, "outer": 176}}
    out = read_tyres._normalize(f_data)
    check("F page inner FL -> 100C", abs(out["FL"]["inner"] - 100.0) < 0.1)
    check("F page outer FL -> 80C", abs(out["FL"]["outer"] - 80.0) < 0.1)
    c_data = {"unit": "C", "FL": {"inner": 95, "mid": 85, "outer": 80}}
    out = read_tyres._normalize(c_data)
    check("C page passes through", abs(out["FL"]["inner"] - 95.0) < 0.01)


def test_limits_clamping():
    print("\n== per-car ranges & clamping ==")
    lim = CarLimits(ride_height_min=8.0, ride_height_max=15.5,
                    spring_front_min=40.0, spring_front_max=120.0,
                    spring_rear_min=60.0, spring_rear_max=180.0)
    v, clamped, msg = lim.clamp("ride_height_f", 19.0)
    check(f"ride height 19 -> clamped to 15.5 ({msg})", clamped and abs(v - 15.5) < 1e-6)
    v, clamped, _ = lim.clamp("arb_r", 80.0)
    check("ARB 80 -> clamped to 65", clamped and abs(v - 65.0) < 1e-6)
    v, clamped, _ = lim.clamp("ride_height_f", 12.0)
    check("ride height 12 within range -> not clamped", not clamped and v == 12.0)

    # PER-AXLE springs: front and rear use independent ranges
    vf, cf, _ = lim.clamp("spring_f", 150.0)   # front max 120
    vr, cr, _ = lim.clamp("spring_r", 150.0)   # rear max 180 (still in range)
    check("front spring 150 -> clamped to 120 (front range)", cf and abs(vf - 120.0) < 1e-6)
    check("rear spring 150 -> NOT clamped (rear range to 180)", not cr and abs(vr - 150.0) < 1e-6)

    # analyze-level: an emitted value beyond a cap is clamped in the recommendation
    tune = build_baseline("Test", "S1 800", "road", 48.0, "AWD")
    tune.arb_f = 2.0   # understeer fix softens front ARB by 3 -> -1 -> clamp to 1
    s = aggregate(_window("understeer"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None, limits=lim)
    check(f"analyze clamps ARB to min 1 (got {rec.group} {rec.fields})",
          rec.group == "arb" and abs(rec.fields.get("arb_f", -9) - 1.0) < 1e-6)

    # range-relative baseline: dirt sits near the car's max, road near its min
    low = CarLimits(ride_height_min=5.0, ride_height_max=20.0)
    road = build_baseline("Car", "S1 800", "road", 48.0, "AWD", limits=low)
    dirt = build_baseline("Car", "S1 800", "dirt", 48.0, "AWD", limits=low)
    check(f"road ride low ({road.ride_height_f:.1f}) < dirt ride high ({dirt.ride_height_f:.1f})",
          road.ride_height_f < 8.0 and dirt.ride_height_f > 16.0)
    check("baseline never exceeds car max", dirt.ride_height_f <= 20.0 + 1e-6)

    # per-axle spring baseline stays within each axle's OWN range
    b = build_baseline("Car", "S1 800", "road", 48.0, "AWD", limits=lim)
    check(f"front spring baseline in [40,120] (got {b.spring_f})", 40.0 <= b.spring_f <= 120.0)
    check(f"rear spring baseline in [60,180] (got {b.spring_r})", 60.0 <= b.spring_r <= 180.0)


def test_lever_pinned_bottoming():
    print("\n== lever-pinned fallback (capped levers) ==")
    # ride height already at the car's max + bottoming -> bump, NOT ride height
    lim = CarLimits(ride_height_min=8.0, ride_height_max=15.5)
    tune = build_baseline("Test", "S1 800", "road", 48.0, "AWD", limits=lim)
    tune.ride_height_f = 15.5   # at car max
    s = aggregate(_window("front_bottoming"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None, limits=lim)
    check(f"maxed ride + bottoming -> bump (got {rec.group} {list(rec.fields)})",
          rec.group == "damping_bump" and "bump_f" in rec.fields)

    # ride AND bump both maxed -> escalate to SPRING
    lim2 = CarLimits(ride_height_min=8.0, ride_height_max=15.5,
                     spring_front_min=40.0, spring_front_max=200.0,
                     spring_rear_min=40.0, spring_rear_max=200.0)
    tune2 = build_baseline("Test", "S1 800", "road", 48.0, "AWD", limits=lim2)
    tune2.ride_height_f = 15.5
    tune2.bump_f = rules.BUMP_CAP    # bump at its ceiling
    rec2 = rules.analyze(aggregate(_window("front_bottoming")), tune2, "road",
                         tyre_reading=None, limits=lim2)
    check(f"maxed ride + maxed bump -> spring (got {rec2.group} {list(rec2.fields)})",
          rec2.group == "springs" and "spring_f" in rec2.fields)


def test_aero_and_gearing():
    print("\n== aero (per-axle range) & gearing (stock + telemetry) ==")
    from lapsmith.state.tune_state import STOCK
    # aero range-relative + clamped to ENTERED F/R ranges; dirt near MIN
    aero = CarLimits(aero_front_min=50.0, aero_front_max=300.0,
                     aero_rear_min=80.0, aero_rear_max=520.0)
    dirt = build_baseline("Car", "S2 900", "dirt", 50.0, "AWD", limits=aero)
    check(f"dirt aero front near MIN (got {dirt.aero_front}, range 50-300)",
          dirt.aero_front <= 50.0 + 0.10 * (300.0 - 50.0) + 1.0)
    check(f"dirt aero rear near MIN (got {dirt.aero_rear}, range 80-520)",
          dirt.aero_rear <= 80.0 + 0.10 * (520.0 - 80.0) + 1.0)
    road = build_baseline("Car", "S2 900", "road", 50.0, "AWD", limits=aero)
    check(f"road aero front near MAX (got {road.aero_front})", road.aero_front >= 290.0)
    check("aero values inside entered ranges",
          80.0 <= road.aero_rear <= 520.0 and 50.0 <= road.aero_front <= 300.0)
    v, clamped, _ = aero.clamp("aero_rear", 9999.0)
    check("aero clamp to entered rear max 520", clamped and abs(v - 520.0) < 1e-6)

    # no aero range -> STOCK, never a blind number
    nostock = build_baseline("Car", "S2 900", "road", 50.0, "AWD")
    check("no aero range -> aero STOCK", nostock.aero_front == STOCK and nostock.aero_rear == STOCK)

    # baseline emits NO fixed final-drive number
    check("baseline final drive == STOCK (no hardcoded ratio)",
          build_baseline("Car", "S1 800", "road", 50.0, "AWD").final_drive == STOCK)

    # gearing RULE direction from redline-vs-straight (needs a numeric current ratio)
    tune = build_baseline("Car", "S1 800", "topspeed", 50.0, "AWD")
    tune.final_drive = 3.50
    rl = rules.analyze(aggregate(_window("redline")), tune, "topspeed",
                       tyre_reading=None, limits=CarLimits())
    check(f"redline on straight -> LENGTHEN (lower ratio) (got {rl.group} {rl.fields})",
          rl.group == "gearing" and rl.fields["final_drive"] < 3.50)
    tune2 = build_baseline("Car", "S1 800", "road", 50.0, "AWD")
    tune2.final_drive = 3.50
    sh = rules.analyze(aggregate(_window("neutral")), tune2, "road",
                       tyre_reading=None, limits=CarLimits())
    check(f"never near redline -> SHORTEN (higher ratio) (got {sh.group} {sh.fields})",
          sh.group == "gearing" and sh.fields["final_drive"] > 3.50)

    # gearing rule stays silent while final drive is STOCK (unknown ratio)
    tstock = build_baseline("Car", "S1 800", "topspeed", 50.0, "AWD")
    silent = rules.analyze(aggregate(_window("redline")), tstock, "topspeed",
                           tyre_reading=None, limits=CarLimits())
    check("gearing silent while final drive STOCK; wants_change flags it",
          silent.group != "gearing" and rules.gearing_wants_change(aggregate(_window("redline"))))


def test_ocr_value_parsing():
    print("\n== Heat-page OCR value parsing + box layout ==")
    pt = read_tyres._parse_temp_text
    check("clean decimal '66.8' -> 66.8", pt("66.8") == 66.8)
    check("dropped decimal '668' -> 66.8", pt("668") == 66.8)
    check("dropped decimal '1210' -> 121.0", pt("1210") == 121.0)
    check("two digits '66' -> 66.0", pt("66") == 66.0)
    check("noise '~69.4C' -> 69.4", pt("~69.4C") == 69.4)
    check("garbage '' -> None", pt("") is None)
    check("garbage 'xx' -> None", pt("xx") is None)

    # unit auto-detect from magnitude (digit whitelist drops the degree glyph)
    check("C magnitudes -> C", read_tyres._unit_from_values([66.8, 69.2, 71.0]) == "C")
    check("F magnitudes -> F", read_tyres._unit_from_values([176.0, 194.0, 212.0]) == "F")

    # 12 resolution-relative boxes: left corners on the left half, right on the
    # right half; front cluster above rear; inner above outer.
    boxes = read_tyres._value_boxes(2560, 1440)
    check("12 value boxes (4 tyres x 3 zones)",
          sum(len(v) for v in boxes.values()) == 12)
    fl_in = boxes["FL"]["inner"]; fr_in = boxes["FR"]["inner"]
    rl_in = boxes["RL"]["inner"]; fl_out = boxes["FL"]["outer"]
    check("FL box on left half, FR on right half",
          fl_in[2] < 2560 / 2 and fr_in[0] > 2560 / 2)
    check("front cluster above rear (FL inner y < RL inner y)", fl_in[1] < rl_in[1])
    check("inner above outer within a corner (FL)", fl_in[1] < fl_out[1])

    # F page normalizes to C end-to-end through _normalize
    fpage = {"unit": "F", "FL": {"inner": 212, "mid": 194, "outer": 176},
             "FR": {"inner": 212, "mid": 194, "outer": 176},
             "RL": {"inner": 212, "mid": 194, "outer": 176},
             "RR": {"inner": 212, "mid": 194, "outer": 176}}
    norm = read_tyres._normalize(fpage)
    check("F page -> C (212F=100C)", abs(norm["FL"]["inner"] - 100.0) < 0.1)

    # Otsu threshold separates a clear bimodal histogram
    class _G:
        def __init__(self, hist): self._h = hist
        def histogram(self): return self._h
    h = [0] * 256
    for i in range(0, 30):
        h[i] = 100         # dark background cluster
    for i in range(220, 256):
        h[i] = 100         # bright digit cluster
    thr = read_tyres._otsu(_G(h))
    # a valid split: everything > thr is the bright (digit) cluster
    check(f"Otsu separates the clusters (got {thr})", 28 <= thr < 220)

    # UDP-assisted unit detection: same magnitudes resolve C vs F by matching UDP
    udp = {"FL": 66.0, "FR": 67.0, "RL": 69.0, "RR": 70.0}   # Celsius (from UDP)
    check("OCR ~66 with UDP ~66 -> unit C",
          read_tyres._choose_unit([66.8, 69.2, 71.0], udp) == "C")
    check("OCR ~152 (F) with UDP ~66C -> unit F",
          read_tyres._choose_unit([150.0, 156.0, 160.0], udp) == "F")

    # config-adjustable boxes via FH6_HEAT_BOXES
    import os as _os, json as _json
    _os.environ["FH6_HEAT_BOXES"] = _json.dumps(
        {"x_left": [0.0, 0.2], "x_right": [0.8, 1.0],
         "y_front": [[0.1, 0.2], [0.2, 0.3], [0.3, 0.4]],
         "y_rear": [[0.5, 0.6], [0.6, 0.7], [0.7, 0.8]]})
    b = read_tyres._value_boxes(1000, 1000)
    check("FH6_HEAT_BOXES overrides FL inner box", b["FL"]["inner"] == (0, 100, 200, 200))
    _os.environ.pop("FH6_HEAT_BOXES", None)


def test_stage1_fixes():
    print("\n== stage-1 core-bug fixes ==")
    from lapsmith.main_loop import _best_moving_packet, _looks_zero_default_temps

    # BUG 1: iteration clamp respects entered max + ride_locked fires the fallback
    lim = CarLimits(ride_height_min=8.0, ride_height_max=15.5)
    t = build_baseline("Car", "S1 800", "dirt", 50.0, "AWD", limits=lim)
    t.ride_height_f = 15.0          # below max; a raw raise would hit 16.0
    s = aggregate(_window("front_bottoming"))
    rec = rules.analyze(s, t, "dirt", tyre_reading=None, limits=lim)
    check(f"ride raise clamped to car max 15.5 (got {rec.fields})",
          rec.group == "ride_height" and rec.fields["ride_height_f"] <= 15.5 + 1e-6)
    rec2 = rules.analyze(s, t, "dirt", tyre_reading=None, limits=lim,
                         ride_locked={"front"})
    check(f"ride_locked front -> bump, not ride (got {rec2.group} {list(rec2.fields)})",
          rec2.group == "damping_bump" and "bump_f" in rec2.fields)

    # BUG 2: AWD read from a live packet -> baseline keeps the centre diff
    awd = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))
    check("live packet decodes AWD + moving", awd.drivetrain_name == "AWD" and awd.speed > 1.0)
    bAWD = build_baseline("Car", "S1 800", "road", 50.0, "AWD")
    bRWD = build_baseline("Car", "S1 800", "road", 50.0, "RWD")
    check("AWD baseline has a centre diff (>0)", bAWD.diff_center > 0)
    check("RWD baseline has no centre diff (0)", bRWD.diff_center == 0)

    # never build from a zero/stale frame: pick the moving packet, flag 0-defaults
    zero = parse(bytes(FH6_BASE_LEN))      # all zero -> FWD, speed 0, temps ~-17.8C

    class _Stub:
        def __init__(self, pkts): self._p = pkts
        def drain_since(self, n): return self._p
        def snapshot(self): return self._p[-1]

    best = _best_moving_packet(_Stub([zero, awd]))
    check("best_moving_packet skips the zero frame, returns AWD",
          best is not None and best.drivetrain_name == "AWD")

    # BUG 3: 0-default tyre temps (all equal near -18C / 0F) are auto-flagged
    check("zero-default temps flagged", _looks_zero_default_temps(zero))
    check("real temps not flagged", not _looks_zero_default_temps(awd))


def test_identity_autodetect():
    print("\n== Stage 2: telemetry auto-detect (car/class/PI/drivetrain) ==")
    from lapsmith import identity, ordinals
    pkt = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))
    check("frame is a live identity frame", identity.is_live(pkt))
    ident = identity.identify(pkt)
    check(f"car name from ordinal ({ident.name})", "CLK GTR" in ident.name)
    check("drivetrain AWD from DrivetrainType", ident.drivetrain == "AWD")
    check(f"target class from PI 800 -> A 800 (own class, no bump) (got {ident.target_class})",
          ident.target_class == "A 800")
    check("PI 950 -> S2 998 (own class)", identity.suggest_target_class(950) == "S2 998")
    check("PI 650 -> B 700 (own class, not bumped to A)",
          identity.suggest_target_class(650) == "B 700")
    # unknown ordinal (a later-update car) still works, shows Car #<n>
    check("unknown ordinal -> 'Car #<n>'", ordinals.name_for(987654) == "Car #987654")
    check("unknown ordinal not 'known'", not ordinals.is_known(987654))
    # zero/stale frame is NOT a valid identity frame
    check("zero frame rejected for identity", not identity.is_live(parse(bytes(FH6_BASE_LEN))))


def test_gui_controller():
    print("\n== Stage 2: headless GUI controller ==")
    from lapsmith.gui import controller as C
    from lapsmith import identity
    pkt = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))

    ctrl = C.Controller()
    ctrl.identity = identity.identify(pkt)        # AWD CLK GTR, PI 800
    lim = CarLimits(ride_height_min=8.0, ride_height_max=15.5)
    ctrl.apply_setup("road", lim, front_weight_pct=48.0)
    check("setup builds baseline + state", ctrl.baseline is not None and ctrl.state is not None)
    check("phase -> APPLY_BASELINE", ctrl.phase == C.APPLY_BASELINE)
    # target class: defaults to the car's own detected class, user can override and
    # the choice flows through to the baseline + the saved car_class metadata.
    check("target class defaults to detected (no override)",
          ctrl.target_class == ctrl.identity.target_class)
    ctrl.apply_setup("road", lim, front_weight_pct=48.0, target_class="S2 998")
    check("user-selected target class wins + flows to car_class metadata",
          ctrl.target_class == "S2 998" and ctrl._meta()["car_class"] == "S2 998")
    # the dropdown options + default label come from the shared class table
    from lapsmith.knowledge import baseline as _bl
    opts = _bl.target_class_options()
    check("target dropdown options reuse the class table (D..X with ceilings)",
          opts == ["D 500", "C 600", "B 700", "A 800", "S1 900", "S2 998", "X 999"]
          and _bl.class_target_label("B") == "B 700")
    check("AWD detected -> baseline keeps centre diff", ctrl.baseline.diff_center > 0)

    # drive a bottoming test -> controller computes a clamped change
    ctrl.state.current.ride_height_f = 15.0       # below max; raw raise would hit 16
    ctrl.stats = aggregate(_window("front_bottoming"))
    ctrl._compute_batch()
    check(f"controller emits a change at SHOW_CHANGE (phase={ctrl.phase})",
          ctrl.phase == C.SHOW_CHANGE and ctrl.rec is not None)
    rf = ctrl.rec.fields.get("ride_height_f")
    check(f"controller-clamped ride <= car max 15.5 (got {ctrl.rec.fields})",
          rf is None or rf <= 15.5 + 1e-6)

    st = ctrl.status()
    check("status() renders phase + car + change", st["phase"] == C.SHOW_CHANGE
          and st["car"] and st["change"] is not None)
    check("status() exposes heartbeat + error fields",
          "packet_age_s" in st and "error" in st)

    # failures surface (never vanish): fail() sets a shown error string
    ctrl.fail("boom (see app.log)")
    check("fail() surfaces error in status", ctrl.status()["error"] == "boom (see app.log)")

    # cancelling setup returns to CONFIRM_CAR (so F8 can reopen it), not a dead end
    c2 = C.Controller()
    c2.identity = identity.identify(pkt)
    c2.confirm_car()
    check("confirm_car -> SETUP phase", c2.phase == C.SETUP)


class _StubListener:
    """Minimal listener for segment-timer tests: snapshot() returns a set packet."""
    def __init__(self):
        self.pkt = None
        self.last_packet_time = 0.0
        self.packet_count = 0

    def snapshot(self):
        return self.pkt


def _seg_packet(ts, x, z):
    from lapsmith.telemetry.parser import Packet
    return Packet(timestamp_ms=ts, position_x=x, position_z=z, speed=40.0,
                  is_race_on=1)


def _drive_fitness(elapsed_s):
    """Build a controller in CHANGE_TIME with one applied change, then run the
    segment-end -> _resolve_fitness path with a given elapsed time. Returns ctrl."""
    from lapsmith.gui import controller as C
    from lapsmith import identity
    pkt = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))
    ctrl = C.Controller()
    ctrl.identity = identity.identify(pkt)
    ctrl.apply_setup("road", CarLimits())
    ctrl.best_segment = 30.0
    ctrl.ref_distance = 500.0
    ctrl.listener = _StubListener()
    ctrl.stats = aggregate(_window("understeer"))
    ctrl._compute_batch()          # -> a real change (arb), phase SHOW_CHANGE
    ctrl.change_applied()           # applies it, history has 1 record, phase CHANGE_TIME
    # mark START then END; displacement 300/400 -> 500m (== reference)
    ctrl.listener.pkt = _seg_packet(0, 0.0, 0.0)
    ctrl.mark_segment_start()
    ctrl.listener.pkt = _seg_packet(int(elapsed_s * 1000), 300.0, 400.0)
    ctrl.mark_segment_end()         # -> _resolve_fitness (THE path that crashed live)
    return ctrl


def test_fitness_resolution():
    print("\n== Stage 2: segment-end -> fitness (keep & revert, the live crash path) ==")
    from lapsmith.gui import controller as C
    # KEEP branch: faster than best -> kept, no exception, advances to TEST
    keep = _drive_fitness(28.0)
    check("keep branch: no crash, advances to TEST", keep.phase == C.TEST and keep.error is None)
    check("keep branch: best segment updated to 28.0", abs(keep.best_segment - 28.0) < 1e-6)
    check("keep branch: iteration advanced", keep.state.iteration == 1)

    # REVERT branch: slower by > regress -> revert + mark_converged(lever_group)
    rev = _drive_fitness(31.0)      # 31 > 30 + 0.2
    check("revert branch: no crash (mark_converged uses lever_group)",
          rev.phase == C.TEST and rev.error is None)
    check("revert branch: a lever group was locked", len(rev.state.converged_levers) >= 1)
    check("revert branch: best segment unchanged (30.0)", abs(rev.best_segment - 30.0) < 1e-6)


def _car_packet(dt, ncyl, ordinal=568, pi=700, car_class=4):
    v = simulator.frame(0.5, "understeer")
    v["drivetrain_type"] = dt
    v["num_cylinders"] = ncyl
    v["car_ordinal"] = ordinal
    v["car_pi"] = pi
    v["car_class"] = car_class
    return parse(simulator._build_packet(v))


def test_drivetrain_detection():
    print("\n== drivetrain detection (raw DrivetrainType @224, mapping 0/1/2) ==")
    from lapsmith import identity
    from lapsmith.gui import controller as C
    # mapping is EXACTLY 0=FWD, 1=RWD, 2=AWD (no shift)
    check("raw 0 -> FWD", _car_packet(0, 4).drivetrain_name == "FWD")
    check("raw 1 -> RWD (NOT AWD)", _car_packet(1, 6).drivetrain_name == "RWD")
    check("raw 2 -> AWD", _car_packet(2, 8).drivetrain_name == "AWD")

    # the reported Supra RZ: Car #568, stock RWD, 2JZ inline-6
    supra = identity.identify(_car_packet(1, 6, ordinal=568))
    check(f"Supra #568 RWD detected (got {supra.drivetrain})", supra.drivetrain == "RWD")
    check("raw DrivetrainType captured = 1", supra.drivetrain_raw == 1)
    check("NumCylinders@228 reads 6 (2JZ inline-6 sanity)", supra.num_cylinders == 6)

    # re-read every frame: a drivetrain swap (AWD conversion) IS reflected
    class _Stub:
        def __init__(self, p): self.pkt = p; self.last_packet_time = 1.0
        def snapshot(self): return self.pkt
    ctrl = C.Controller()
    ctrl.identity = identity.identify(_car_packet(1, 6))       # starts RWD
    ctrl.listener = _Stub(_car_packet(2, 6))                   # now reads AWD
    ctrl.refresh_identity()
    check("drivetrain swap reflected on re-read (RWD -> AWD)", ctrl.identity.drivetrain == "AWD")
    check("status surfaces raw drivetrain + cylinders",
          ctrl.status()["live"]["drivetrain_raw"] == 2
          and ctrl.status()["live"]["num_cylinders"] == 6)


def _lap_packets(lap_number, current_lap, last_lap, n=30, scenario="understeer"):
    out = []
    for i in range(n):
        v = simulator.frame(i * 0.05, scenario)
        v["is_race_on"] = 1
        v["lap_number"] = lap_number
        v["current_lap"] = current_lap
        v["last_lap"] = last_lap
        out.append(parse(simulator._build_packet(v)))
    return out


def test_auto_lap():
    print("\n== Stage 2: auto-lap mode (Rivals/circuit) ==")
    from lapsmith.telemetry.laps import LapWatcher, LapResult
    from lapsmith.gui import controller as C
    from lapsmith import identity

    # --- LapWatcher: a completion when LapNumber increments; LastLap = its time ---
    w = LapWatcher()
    stream = _lap_packets(1, 5.0, 0.0) + _lap_packets(2, 1.0, 54.0)
    results = w.feed(stream)
    check(f"lap completion detected ({len(results)})", len(results) == 1)
    check("LastLap used as the lap time (54.0)",
          results and abs(results[0].last_lap_s - 54.0) < 1e-6 and results[0].lap_number == 1)
    check("lap fields flagged live", w.lap_fields_live())
    w2 = LapWatcher()
    w2.feed(_lap_packets(0, 0.0, 0.0))   # free-roam: lap fields 0
    check("free-roam: lap fields NOT live", not w2.lap_fields_live())

    # --- advancing(): timer actually running, not a stationary snapshot ---
    wadv = LapWatcher()
    wadv.feed(_lap_packets(1, 1.0, 0.0))      # at the line
    wadv.feed(_lap_packets(1, 3.0, 0.0))      # CurrentLap rose
    check("advancing() true when CurrentLap rises tick-over-tick", wadv.advancing())
    wflat = LapWatcher()
    wflat.feed(_lap_packets(0, 0.0, 0.0))
    wflat.feed(_lap_packets(0, 0.0, 0.0))     # stationary at the line / free-roam
    check("advancing() false when fields flat (stationary/free-roam)", not wflat.advancing())

    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    class _Feed:
        def __init__(self): self.q = []; self.n = 0; self.last_packet_time = 1.0
        def push(self, p): self.q += p; self.n += len(p)
        @property
        def mark(self): return self.n
        def drain_since(self, m): o = self.q; self.q = []; return o
        def snapshot(self): return self.q[-1] if self.q else None

    # --- mode is NOT locked at baseline; engages AUTO only when timer advances ---
    cda = C.Controller(); cda.identity = ident
    cda.apply_setup("road", CarLimits()); cda.listener = _Feed(); cda.baseline_applied()
    check("baseline does NOT lock the mode (stays detecting)",
          cda.mode is None and cda.phase == C.DRIVE_AUTO)
    cda.listener.push(_lap_packets(1, 0.0, 0.0)); cda.tick()       # at line, stationary
    check("stationary at line stays detecting (not wrongly manual)", cda.mode is None)
    cda.listener.push(_lap_packets(1, 0.6, 0.0)); cda.tick()       # CurrentLap rises (same lap)
    check("AUTO-LAP engages once the lap timer advances; out-lap armed",
          cda.mode == C.MODE_AUTO and cda._skip_laps == 1)

    # entering Rivals LATER (after a spell of dead fields) still engages auto
    clate = C.Controller(); clate.identity = ident
    clate.apply_setup("road", CarLimits()); clate.listener = _Feed(); clate.baseline_applied()
    for _ in range(3):
        clate.listener.push(_lap_packets(0, 0.0, 0.0)); clate.tick()
    check("still detecting after a stationary spell", clate.mode is None)
    clate.listener.push(_lap_packets(2, 1.0, 50.0)); clate.tick()
    clate.listener.push(_lap_packets(2, 3.0, 50.0)); clate.tick()
    check("AUTO engages when entering the event later", clate.mode == C.MODE_AUTO)

    # free-roam: [F9] commits to MANUAL
    cdm = C.Controller(); cdm.identity = ident
    cdm.apply_setup("road", CarLimits()); cdm.listener = _Feed(); cdm.baseline_applied()
    cdm.mark_segment_start()
    check("[F9] while detecting commits to MANUAL",
          cdm.mode == C.MODE_MANUAL and cdm.phase == C.BASELINE_TIME)

    # --- FULL path THROUGH tick(): engage -> out-lap -> baseline -> first change ---
    # (this is the exact path that was dead: tick() never ran while detecting)
    cend = C.Controller(); cend.identity = ident
    cend.apply_setup("road", CarLimits()); cend.listener = _Feed(); cend.baseline_applied()
    cend.listener.push(_lap_packets(1, 1.0, 0.0)); cend.tick()
    cend.listener.push(_lap_packets(1, 3.0, 0.0)); cend.tick()      # CurrentLap rose -> engage
    check("tick path: AUTO engaged from detecting", cend.mode == C.MODE_AUTO)
    cend.listener.push(_lap_packets(2, 1.0, 53.0)); cend.tick()     # lap 1 completes -> out-lap skipped
    check("tick path: out-lap skipped, no reference yet", cend.best_segment is None)
    cend.listener.push(_lap_packets(3, 1.0, 52.0)); cend.tick()     # lap 2 completes -> baseline + change
    check("tick path: baseline reference from a full lap (52.0)",
          cend.best_segment is not None and abs(cend.best_segment - 52.0) < 1e-6)
    check("tick path: FIRST CHANGE emitted (overlay would show NEXT)",
          cend.phase == C.SHOW_CHANGE and cend.rec is not None and cend.rec.is_change())
    check("tick path: F8 now has something to act on (change_applied works)",
          (cend.change_applied() or True) and cend.phase == C.DRIVE_AUTO and cend._awaiting_test)

    # --- one-iteration-per-lap flow: out-lap ignored, baseline ref, keep then revert ---
    def fresh():
        c = C.Controller()
        c.identity = ident
        c.apply_setup("road", CarLimits())
        c.mode = C.MODE_AUTO
        return c

    c = fresh()
    c.arm_next_lap()                                   # baseline out-lap
    c._on_lap(LapResult(1, 54.0, _lap_packets(1, 5, 0)))   # ignored
    check("baseline out-lap ignored (no reference yet)", c.best_segment is None)
    c._on_lap(LapResult(2, 54.0, _lap_packets(2, 5, 54.0)))  # baseline reference + change
    check("baseline lap sets reference 54.0", abs(c.best_segment - 54.0) < 1e-6)
    check("a change is shown after the baseline lap", c.phase == C.SHOW_CHANGE and c.rec is not None)

    # apply change -> next lap is a faster TEST lap -> KEEP
    c.change_applied()
    check("change applied -> DRIVE_AUTO, awaiting test, out-lap armed",
          c.phase == C.DRIVE_AUTO and c._awaiting_test and c._skip_laps == 1)
    c._on_lap(LapResult(3, 99.0, _lap_packets(3, 5, 99.0)))   # out-lap ignored
    check("out-lap after change ignored", c.best_segment == 54.0 and c._awaiting_test)
    c._on_lap(LapResult(4, 52.5, _lap_packets(4, 5, 52.5)))   # faster -> keep
    check("faster test lap kept; best -> 52.5", abs(c.best_segment - 52.5) < 1e-6)
    check("after fitness, next change shown (no crash)", c.phase == C.SHOW_CHANGE)

    # REVERT branch on a separate controller
    c2 = fresh()
    c2.best_segment = 50.0
    c2.stats = aggregate(_window("understeer"))
    c2._compute_batch(); c2.change_applied()
    c2._on_lap(LapResult(5, 99.0, _lap_packets(5, 5, 99.0)))  # out-lap ignored
    c2._on_lap(LapResult(6, 51.0, _lap_packets(6, 5, 51.0)))  # slower by >0.2 -> revert
    check("slower test lap reverts; best stays 50.0 (no crash)",
          abs(c2.best_segment - 50.0) < 1e-6 and len(c2.state.converged_levers) >= 1)


def test_batch_changes():
    print("\n== batch changes (evidence together, search rate-limited, batch revert) ==")
    from lapsmith.gui import controller as C
    from lapsmith import identity
    tune = build_baseline("Car", "S1 800", "road", 48.0, "AWD")
    reading = {"FL": {"inner": 95, "mid": 85, "outer": 80},     # hot inner -> camber
               "FR": {"inner": 95, "mid": 85, "outer": 80},
               "RL": {"inner": 82, "mid": 81, "outer": 80},
               "RR": {"inner": 82, "mid": 81, "outer": 80}}
    s = aggregate(_window("understeer"))                        # understeer -> arb
    batch = rules.analyze_batch(s, tune, "road", tyre_reading=reading, max_search=1)
    groups = [r.group for r in batch]
    kinds = {r.group: r.kind for r in batch}
    check(f"batch has camber (evidence) + arb (search) (got {groups})",
          "camber" in groups and "arb" in groups)
    check("camber=evidence, arb=search",
          kinds.get("camber") == "evidence" and kinds.get("arb") == "search")
    check("max_search=1 -> at most 1 search change",
          sum(1 for r in batch if r.kind == "search") == 1)
    ev1 = sum(1 for r in batch if r.kind == "evidence")
    batch3 = rules.analyze_batch(s, tune, "road", tyre_reading=reading, max_search=3)
    check("evidence count independent of max_search",
          sum(1 for r in batch3 if r.kind == "evidence") == ev1)

    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    def fresh():
        c = C.Controller(); c.identity = ident
        c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
        c.best_segment = 50.0; c.tyre_reading = reading
        c.stats = aggregate(_window("understeer"))
        c._compute_batch()
        return c

    # KEEP the whole batch on a faster lap
    ck = fresh(); n = len(ck.batch)
    check(f"batch has >1 change ({n})", n >= 2)
    ck.change_applied()
    check("change_applied records the whole batch", len(ck._applied_records) == n)
    h = len(ck.state.history)
    ck._apply_fitness(49.0)
    check("faster lap keeps batch (best 49.0, history intact)",
          abs(ck.best_segment - 49.0) < 1e-6 and len(ck.state.history) == h)

    # REVERT the whole batch on a slower lap; every touched lever restored
    cr = fresh(); n2 = len(cr.batch)
    pre = {k: cr.state.current.get(k) for r in cr.batch for k in r.fields}
    cr.change_applied()
    cr._apply_fitness(51.0)            # slower by >0.2 -> revert ALL
    restored = all(abs(cr.state.current.get(k) - v) < 1e-9 for k, v in pre.items())
    check("slower lap reverts the WHOLE batch (every lever restored)", restored)
    check("each batched lever locked (converged grew)",
          len(cr.state.converged_levers) >= n2)
    check("best unchanged after revert (50.0)", abs(cr.best_segment - 50.0) < 1e-6)


def test_multi_lap_fitness():
    print("\n== multi-lap fitness (noise-robust keep/revert) ==")
    from lapsmith.gui import controller as C
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    reading = {"FL": {"inner": 95, "mid": 85, "outer": 80},
               "FR": {"inner": 95, "mid": 85, "outer": 80},
               "RL": {"inner": 82, "mid": 81, "outer": 80},
               "RR": {"inner": 82, "mid": 81, "outer": 80}}

    def fresh(laps="adaptive", agg="best"):
        c = C.Controller(); c.identity = ident
        c.apply_setup("road", CarLimits(), laps_per_test=laps, lap_agg=agg)
        c.mode = C.MODE_AUTO
        c.tyre_reading = reading
        return c

    def lap(t):
        from lapsmith.telemetry.laps import LapResult
        return LapResult(2, t, _lap_packets(2, 5, t))

    def apply_then(c, times):
        c.change_applied()
        c._on_lap(lap(999.0))                  # out-lap (change_applied armed skip=1)
        for t in times:
            c._on_lap(lap(t))

    # adaptive target: 1 early, 3 once stale
    c = fresh()
    check("adaptive: 1 lap at iteration 0", c._target_laps() == 1)
    c.state.iteration = 3; c.stale = 1
    check("adaptive: 3 laps once stale", c._target_laps() == 3)

    # fixed 3-lap test: collects 3, finalizes on the 3rd, represents by BEST
    c = fresh(laps=3); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    c.change_applied(); c._on_lap(lap(999.0))           # out-lap
    c._on_lap(lap(52.0))                                 # lap 1/3 - no decision yet
    check("after lap 1/3 still collecting (no fitness yet)",
          len(c._test_laps) == 1 and c.phase == C.DRIVE_AUTO and c._awaiting_test)
    c._on_lap(lap(49.6))                                 # lap 2/3
    c._on_lap(lap(49.8))                                 # lap 3/3 -> finalize, best=49.6
    check("3 laps finalize; best-of-N improves (49.6 < 50.0)",
          abs(c.best_segment - 49.6) < 1e-6 and not c._awaiting_test)

    # NOISE gate: a regression WITHIN lap spread is inconclusive -> held, not locked
    c = fresh(laps=3); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    apply_then(c, (50.4, 50.1, 50.6))           # best 50.1, spread 0.5s is the noise
    check("within-noise regression NOT reverted (inconclusive hold)",
          len(c.state.converged_levers) == 0)

    # EVIDENCE protection: a small regression past the plain gate is NOT reverted
    # unless it clears the extra evidence margin.
    c = fresh(laps=2); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    check("batch contains an evidence change (camber)",
          any(r.kind == "evidence" for r in c.batch))
    apply_then(c, (50.35, 50.35))               # +0.35: > 0.2 gate but < 0.2+0.3 evidence
    check("evidence change survives a small regression (not reverted)",
          len(c.state.converged_levers) == 0)
    c = fresh(laps=2); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    apply_then(c, (50.9, 50.9))                 # +0.9 clearly beyond gate+evidence
    check("clearly-worse evidence batch IS reverted", len(c.state.converged_levers) >= 1)

    # median aggregation option (best_segment high so it clearly improves)
    c = fresh(laps=3, agg="median"); c.best_segment = 60.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    apply_then(c, (52.0, 53.0, 58.0))           # median 53.0
    check("median aggregate uses the middle lap (53.0)", abs(c.best_segment - 53.0) < 1e-6)


def test_lateral_capture_axis():
    print("\n== Heat capture triggers on LATERAL g, not longitudinal ==")
    from lapsmith.gui import app
    corner = simulator.frame(0.5, "understeer"); corner["accel_x"] = 12.0; corner["accel_z"] = 1.0
    launch = simulator.frame(0.5, "understeer"); launch["accel_x"] = 1.0; launch["accel_z"] = 12.0
    pc = parse(simulator._build_packet(corner)); pl = parse(simulator._build_packet(launch))
    check(f"cornering frame -> high lateral g ({app.lateral_g(pc):.2f})", app.lateral_g(pc) > 1.0)
    check(f"launch/straight frame -> low lateral g ({app.lateral_g(pl):.2f})", app.lateral_g(pl) < 0.2)
    check("default lateral axis is AccelerationX", app.LATERAL_AXIS == "x")

    # cornering-peak gate (A): reject crash spikes / longitudinal / sudden stops
    cp = app.is_cornering_peak
    check("sustained 1.2g lateral corner accepted", cp(1.2, 0.3, 0.0, 3))
    check("15g lateral spike (crash) rejected", not cp(15.0, 2.0, 0.0, 5))
    check("longitudinal-dominant frame rejected", not cp(1.5, 8.0, 0.0, 5))
    check("sudden speed drop (impact) rejected", not cp(1.5, 0.5, 12.0, 5))
    check("single-frame blip (not sustained) rejected", not cp(1.5, 0.3, 1, 1))
    check("below load threshold rejected", not cp(0.3, 0.1, 0.0, 5))


def test_distinct_rear_temps():
    print("\n== UDP TireTemp offsets distinct (RL != RR) ==")
    b = bytearray(FH6_BASE_LEN)
    struct.pack_into("<f", b, 268, 150.0)   # FL
    struct.pack_into("<f", b, 272, 160.0)   # FR
    struct.pack_into("<f", b, 276, 170.0)   # RL
    struct.pack_into("<f", b, 280, 180.0)   # RR
    p = parse(bytes(b))
    check("RL@276 and RR@280 decode distinctly",
          abs(p.tire_temp_rl - p.tire_temp_rr) > 1.0
          and round(p.tire_temp_rl, 1) != round(p.tire_temp_rr, 1))
    check("all four corners distinct",
          len({round(p.tire_temp_fl, 1), round(p.tire_temp_fr, 1),
               round(p.tire_temp_rl, 1), round(p.tire_temp_rr, 1)}) == 4)


def test_vision_reader():
    print("\n== vision reader: JSON parse + UDP cross-check ==")
    from lapsmith.vision import read_tyres
    udp = {"FL": 66.0, "FR": 67.0, "RL": 69.0, "RR": 70.0}     # Celsius (from UDP)
    good = json.dumps({"unit": "C",
                       "FL": {"inner": 65, "mid": 66, "outer": 67},
                       "FR": {"inner": 66, "mid": 67, "outer": 68},
                       "RL": {"inner": 68, "mid": 69, "outer": 70},
                       "RR": {"inner": 69, "mid": 70, "outer": 71}})
    out = read_tyres._parse_vision_json(good, udp)
    check("valid JSON within UDP tolerance accepted", out is not None
          and abs(out["FL"]["inner"] - 65.0) < 0.1)
    # Fahrenheit page normalizes to C, then cross-checks
    fpage = json.dumps({"unit": "F",
                        "FL": {"inner": 149, "mid": 151, "outer": 153},  # ~65-67C
                        "FR": {"inner": 151, "mid": 153, "outer": 155},
                        "RL": {"inner": 155, "mid": 156, "outer": 158},
                        "RR": {"inner": 156, "mid": 158, "outer": 160}})
    outf = read_tyres._parse_vision_json(fpage, udp)
    check("F-unit JSON normalizes to C and passes cross-check", outf is not None
          and 60 < outf["FL"]["inner"] < 72)
    # a misread far from UDP is rejected
    bad = json.dumps({"unit": "C",
                      "FL": {"inner": 120, "mid": 121, "outer": 122},   # +55C off UDP
                      "FR": {"inner": 66, "mid": 67, "outer": 68},
                      "RL": {"inner": 68, "mid": 69, "outer": 70},
                      "RR": {"inner": 69, "mid": 70, "outer": 71}})
    check("reading far from UDP rejected (misread)",
          read_tyres._parse_vision_json(bad, udp) is None)
    check("non-JSON rejected", read_tyres._parse_vision_json("not json", udp) is None)
    check("vision_available() False without ANTHROPIC_API_KEY",
          read_tyres.vision_available() is False
          or bool(__import__("os").environ.get("ANTHROPIC_API_KEY")))


def _heat_tokens(w, h, unit_suffix="", *, jitter=0.0, hud_junk=False):
    """Synthesize RapidOCR-style [(text,(x,y))] for the Heat page at ANY w,h.
    Layout (fractional, so it scales to any resolution / aspect):
      FL top-left, FR top-right, RL mid-left, RR mid-right; each tyre's 3 zones
      stacked inner/mid/outer top->bottom. Optionally add labels + HUD junk."""
    temps = {  # inner, mid, outer (C)
        "FL": (63.7, 66.6, 67.5), "FR": (66.0, 66.9, 66.6),
        "RL": (62.5, 66.5, 68.1), "RR": (66.6, 66.5, 66.2)}
    cols = {"L": 0.40, "R": 0.60}
    rows = {"F": 0.30, "R": 0.55}        # front cluster higher than rear
    zrow = (0.0, 0.06, 0.12)             # inner/mid/outer vertical offset
    toks = []
    for corner, vals in temps.items():
        cx = cols[corner[1]] * w + jitter
        cy0 = rows[corner[0]] * h
        for i, v in enumerate(vals):
            txt = f"{v:.1f}{unit_suffix}"
            toks.append((txt, (cx, (cy0 + zrow[i] * h))))
    if hud_junk:
        toks += [("015", (0.5 * w, 0.92 * h)),     # speed
                 ("P 1/12", (0.05 * w, 0.05 * h)),  # position
                 ("1:23.4", (0.5 * w, 0.04 * h))]   # lap time
    return toks, temps


def _labelled_tokens(w, h):
    toks, temps = _heat_tokens(w, h, hud_junk=True)
    cols = {"L": 0.40, "R": 0.60}
    rows = {"F": 0.30, "R": 0.55}
    names = {"FL": "Front Left", "FR": "Front Right",
             "RL": "Rear Left", "RR": "Rear Right"}
    for corner, name in names.items():
        toks.append((name, (cols[corner[1]] * w, rows[corner[0]] * h - 0.05 * h)))
    return toks, temps


def test_rapidocr_mapping():
    print("\n== RapidOCR: resolution-independent token -> tyre mapping ==")
    from lapsmith.vision import read_tyres
    udp = {"FL": 65.9, "FR": 66.5, "RL": 65.7, "RR": 66.4}     # ~ averages
    # Same logical frame at THREE resolutions/aspects -> identical reading.
    for label, (w, h) in [("1920x1080", (1920, 1080)),
                          ("2560x1440", (2560, 1440)),
                          ("ultrawide 3440x1440", (3440, 1440))]:
        toks, temps = _labelled_tokens(w, h)
        out = read_tyres.tokens_to_reading(toks, udp_temps=udp)
        ok = out is not None and all(
            abs(out[c]["inner"] - temps[c][0]) < 0.2 and
            abs(out[c]["outer"] - temps[c][2]) < 0.2 for c in temps)
        check(f"label-anchored read correct @ {label}", ok)
    # No labels at all -> positional fallback still maps correctly (1440p).
    toks, temps = _heat_tokens(2560, 1440, hud_junk=True)
    out = read_tyres.tokens_to_reading(toks, udp_temps=udp)
    check("positional fallback (no labels) maps 12 numbers",
          out is not None and abs(out["RL"]["outer"] - temps["RL"][2]) < 0.2)
    # Fahrenheit page normalizes to C via UDP-assisted unit choice.
    ftoks = []
    cols = {"L": 0.40, "R": 0.60}; rows = {"F": 0.30, "R": 0.55}; zrow = (0.0, 0.06, 0.12)
    for c, vals in temps.items():
        for i, v in enumerate(vals):
            ftoks.append((f"{v*9/5+32:.1f}", (cols[c[1]]*2560, rows[c[0]]*1440 + zrow[i]*1440)))
    outf = read_tyres.tokens_to_reading(ftoks, udp_temps=udp)
    check("Fahrenheit tokens normalize to C", outf is not None
          and 60 < outf["FL"]["mid"] < 72)
    # Too few numbers -> no reading (camber falls to lap-time search).
    check("insufficient tokens -> None", read_tyres.tokens_to_reading(
        [("66.0", (10, 10)), ("67.0", (20, 20))], udp_temps=udp) is None)
    # A misread far from UDP is rejected by the cross-check.
    bad, _ = _labelled_tokens(1920, 1080)
    bad = [("120.0" if t == "63.7" else t, xy) for t, xy in bad]   # FL inner way off
    out_bad = read_tyres.tokens_to_reading(bad, udp_temps={"FL": 65.9})
    check("UDP cross-check rejects a misread token set", out_bad is None)


def test_camber_search():
    print("\n== camber-by-lap-time search when no temps ==")
    from lapsmith.knowledge import rules
    tune = build_baseline("Test Car", "S1 800", "road", 48.0, "AWD")
    stats = aggregate(_window("understeer"))
    # With a tyre reading, the SEARCH rule stays silent (evidence rule owns camber).
    # Front inner-outer delta > CAMBER_C so evidence camber fires.
    reading = {"FL": {"inner": 74, "mid": 70, "outer": 66},
               "FR": {"inner": 74, "mid": 70, "outer": 66},
               "RL": {"inner": 67, "mid": 67, "outer": 67},
               "RR": {"inner": 67, "mid": 67, "outer": 67}}
    rec = rules._rule_camber_search(stats, tune, "road", reading, True, rules.CarLimits())
    check("camber search silent when temps exist", rec is None)
    # No reading on a road disc -> proposes a more-negative FRONT camber step.
    rec = rules._rule_camber_search(stats, tune, "road", None, True, rules.CarLimits())
    check("camber search fires with no temps (road)", rec is not None
          and rec.group == "camber_search" and rec.kind == "search"
          and rec.fields["camber_f"] < tune.camber_f)
    # Not a grip discipline -> silent.
    check("camber search silent off-road-band",
          rules._rule_camber_search(stats, tune, "drag", None, False, rules.CarLimits()) is None)
    # analyze_batch tags it search (rate-limited), present only when no reading.
    batch = rules.analyze_batch(stats, tune, "road", tyre_reading=None,
                                limits=rules.CarLimits(), max_search=5)
    groups = [r.group for r in batch]
    check("analyze_batch includes camber_search when no temps",
          "camber_search" in groups)
    kinds = {r.group: r.kind for r in batch}
    check("camber_search classified as search kind",
          kinds.get("camber_search") == "search")
    # With temps present, evidence camber appears and camber_search does not.
    batch2 = rules.analyze_batch(stats, tune, "road", tyre_reading=reading,
                                 limits=rules.CarLimits(), max_search=5)
    g2 = [r.group for r in batch2]
    check("with temps: evidence camber present, search absent",
          "camber" in g2 and "camber_search" not in g2)


def test_temp_reader_chain():
    print("\n== controller temp-reader fallback chain (offline, no key) ==")
    from lapsmith.gui import controller as Cmod
    from lapsmith.vision import read_tyres
    ctrl = Cmod.Controller()
    # Stub the readers so we can assert ORDER without RapidOCR/Tesseract installed.
    calls = []
    good = {"FL": {"inner": 65, "mid": 66, "outer": 67},
            "FR": {"inner": 65, "mid": 66, "outer": 67},
            "RL": {"inner": 65, "mid": 66, "outer": 67},
            "RR": {"inner": 65, "mid": 66, "outer": 67}}
    orig = (read_tyres.rapidocr_available, read_tyres.rapidocr_read_image,
            read_tyres.ocr_heat_page, read_tyres.vision_available)
    try:
        read_tyres.rapidocr_available = lambda: True
        read_tyres.rapidocr_read_image = lambda p, udp_temps=None: (calls.append("rapid"), good)[1]
        read_tyres.ocr_heat_page = lambda p, udp_temps=None: (calls.append("tess"), None)[1]
        read_tyres.vision_available = lambda: False
        ctrl.manual_temp_fn = lambda p: (calls.append("manual"), good)[1]
        # AUTO mode: RapidOCR wins, manual never called.
        ctrl.temp_mode = "auto"; ctrl.use_vision_api = False
        ctrl._read_heat("frame.png", 1.0, udp_temps=None)
        check("auto: RapidOCR primary used", calls == ["rapid"] and ctrl.tyre_reading == good)
        # RapidOCR fails -> Tesseract -> no read -> reading None, manual NOT called.
        calls.clear()
        read_tyres.rapidocr_read_image = lambda p, udp_temps=None: (calls.append("rapid"), None)[1]
        ctrl._read_heat("frame.png", 1.0, udp_temps=None)
        check("auto: falls to Tesseract then None (no manual block)",
              calls == ["rapid", "tess"] and ctrl.tyre_reading is None)
        # MANUAL opt-in mode: dialog used directly.
        calls.clear(); ctrl.temp_mode = "manual"
        ctrl._read_heat("frame.png", 1.0, udp_temps=None)
        check("manual mode: dialog used, OCR skipped",
              calls == ["manual"] and ctrl.tyre_reading == good)
    finally:
        (read_tyres.rapidocr_available, read_tyres.rapidocr_read_image,
         read_tyres.ocr_heat_page, read_tyres.vision_available) = orig


def test_product_tweaks():
    print("\n== product: per-axle ride bounds + damping order ==")
    from lapsmith.state.tune_state import CarLimits
    from lapsmith.state.store import _optn_club_block
    from lapsmith.knowledge.baseline import format_checklist
    lim = CarLimits(ride_height_front_min=4.0, ride_height_front_max=12.0,
                    ride_height_rear_min=6.0, ride_height_rear_max=15.0)
    check("front ride bounds separate from rear",
          lim.bounds("ride_height_f") == (4.0, 12.0)
          and lim.bounds("ride_height_r") == (6.0, 15.0))
    leg = CarLimits(ride_height_min=8.0, ride_height_max=15.5)
    check("legacy single ride pair still honoured (CLI back-compat)",
          leg.bounds("ride_height_f") == (8.0, 15.5)
          and leg.bounds("ride_height_r") == (8.0, 15.5))
    t = build_baseline("Car", "S1 800", "road", 48.0, "AWD")
    sheet = format_checklist(t, "Car", "S1 800", "road", 48.0, "AWD")
    d = sheet.index("DAMPING")
    check("tune sheet: Rebound listed before Bump (FH6 menu order)",
          sheet.index("Rebound", d) < sheet.index("Bump", d))
    blk = _optn_club_block(t, "AWD")
    check("optn block: rebound before bump",
          blk.index("rebound_front") < blk.index("bump_front"))


def test_car_naming():
    print("\n== product: car naming (prompt/save/persist/edit) ==")
    import tempfile, os, json
    from lapsmith import ordinals
    from lapsmith.gui import controller as C
    from lapsmith.identity import CarIdentity
    d = tempfile.mkdtemp(); p = os.path.join(d, "car_names.json")
    ordinals._USER_MAP.clear(); ordinals.set_store_path(p)
    ctrl = C.Controller()
    ctrl.identity = CarIdentity(ordinal=88888, name="Car #88888", pi=800,
        car_class_enum=4, class_letter="S1", drivetrain="RWD", known=False,
        target_class="S1 800", drivetrain_raw=1, num_cylinders=6)
    check("unknown ordinal needs a name", ctrl.needs_car_name() is True)
    ctrl.car_name_prompt_fn = lambda ident: "2020 Supra RZ"
    ctrl.confirm_car()
    check("confirm prompts + saves the name, shows it on identity",
          ctrl.identity.name == "2020 Supra RZ" and not ctrl.needs_car_name())
    check("name persisted to JSON", json.load(open(p)) == {"88888": "2020 Supra RZ"})
    ordinals._USER_MAP.clear(); ordinals.set_store_path(p)
    check("name reloads from disk", ordinals.name_for(88888) == "2020 Supra RZ")
    ctrl.rename_car(88888, "Supra (edited)")
    check("rename updates store + live identity",
          ordinals.name_for(88888) == "Supra (edited)" and ctrl.identity.name == "Supra (edited)")
    ctrl.forget_car(88888)
    check("forget reverts to Car #N", ordinals.name_for(88888) == "Car #88888")


def test_restart_and_warmup():
    print("\n== product: restart detection + warm-up discard ==")
    from lapsmith.telemetry.laps import LapWatcher
    from lapsmith.gui import controller as C
    from lapsmith import identity
    # race off->on after live laps = restart
    w = LapWatcher()
    w.feed(_lap_packets(1, 5.0, 0.0))
    w.feed(_lap_packets(0, 0.0, 0.0, n=2))     # race-off frames carry is_race_on=1?
    # explicitly send race-off then race-on
    class _P:
        def __init__(s, r, l, c, t=0.0):
            s.is_race_on=r; s.lap_number=l; s.current_lap=c; s.last_lap=t
    w = LapWatcher()
    w.feed([_P(1,1,5.0), _P(1,1,30.0)])
    w.pop_restarted()
    w.feed([_P(0,0,0.0)]); w.feed([_P(1,0,1.0)])
    check("race off->on flags a restart", w.pop_restarted() is True)
    # lap counter backwards = restart
    w2 = LapWatcher()
    w2.feed([_P(1,3,5.0)]); w2.pop_restarted()
    w2.feed([_P(1,0,1.0)])
    check("lap counter reset flags a restart", w2.pop_restarted() is True)
    # controller re-arms the warm-up discard on a restart (through tick)
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    class _Feed:
        def __init__(self): self.q=[]; self.n=0; self.last_packet_time=1.0
        def push(self,p): self.q+=p; self.n+=len(p)
        @property
        def mark(self): return self.n
        def drain_since(self,m): o=self.q; self.q=[]; return o
        def snapshot(self): return self.q[-1] if self.q else None
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits()); c.listener=_Feed(); c.baseline_applied()
    c.listener.push(_lap_packets(1,1.0,0.0)); c.tick()
    c.listener.push(_lap_packets(1,3.0,0.0)); c.tick()      # engage AUTO (arms skip=1)
    c._skip_laps = 0                                        # pretend warm-up consumed
    # event restarted in place: lap counter jumps back to 0 (no completion this tick)
    c.listener.push(_lap_packets(0,0.0,0.0)); c.tick()
    check("controller re-arms warm-up discard on restart",
          c._skip_laps == 1 and c._restart_count >= 1)


def test_step_machine():
    print("\n== product: 6-step guided workflow ==")
    from lapsmith.gui import controller as C
    c = C.Controller()
    seen = {}
    c.phase = C.WAIT_TELEMETRY; seen[1] = c.guided_step()
    check("step 1 select car waits for telemetry",
          seen[1]["number"] == 1 and "telemetry" in seen[1]["action"].lower())
    c.phase = C.APPLY_BASELINE
    check("step 2 apply tune", c.guided_step()["number"] == 2)
    c.phase = C.DRIVE_AUTO; c.mode = C.MODE_AUTO; c.best_segment = None
    g = c.guided_step()
    check("step 3 baseline laps mentions warm-up",
          g["number"] == 3 and "warm-up" in g["action"].lower())
    c.phase = C.SHOW_CHANGE
    g = c.guided_step()
    check("step 4 changes says RESTART the event",
          g["number"] == 4 and "restart" in g["action"].lower())
    c.phase = C.DRIVE_AUTO; c.best_segment = 50.0; c._awaiting_test = True
    check("step 5 test laps", c.guided_step()["number"] == 5)
    c.phase = C.DONE
    check("step 6 converged", c.guided_step()["number"] == 6)


def test_file_outputs():
    print("\n== product: share export + cumulative log + support zip ==")
    import tempfile, os, json, zipfile
    from lapsmith.state import store
    from lapsmith.state.tune_state import TuneState
    d = tempfile.mkdtemp(); store.set_sessions_dir(d)
    base = build_baseline("2020 Supra RZ", "S1 800", "road", 48.0, "RWD")
    st = TuneState(base.copy())
    r1 = st.apply_change("camber", {"camber_f": -1.8},
                         "front inner 12C hotter than outer", "even contact"); r1.verdict = "kept"
    r2 = st.apply_change("arb", {"arb_f": 8.0}, "understeer - soften front ARB", "rotation")
    for k, v in r2.previous.items():
        st.current.set(k, v)
    r2.verdict = "reverted"; st.iteration = 4
    store.save_session(st, car="2020 Supra RZ", car_class="S1 800", discipline="road",
                       front_weight_pct=48.0, drivetrain="RWD", baseline=base, stats_log=[],
                       started_iso="2026-06-21T10:00:00", status="converged", best_lap_s=92.34)
    exp = store.export_tune(st, car="2020 Supra RZ", car_class="S1 800", discipline="road",
                            front_weight_pct=48.0, drivetrain="RWD", best_lap_s=92.34)
    txt = open(exp["txt"], encoding="utf-8").read()
    check("share sheet has FINAL TUNE + optn block + 'not a share code' note",
          "FINAL TUNE" in txt and "optn.club" in txt and "NOT an" in txt)
    tj = json.load(open(exp["json"]))
    check("share JSON carries the exact values + manual-not-sharecode note",
          tj["values"]["camber_f"] == -1.8 and "not an in-game share code" in tj["note"])
    clog = store.append_cumulative_log(st, base, car="2020 Supra RZ", car_class="S1 800",
            discipline="road", drivetrain="RWD", started_iso="2026-06-21T10:00:00",
            best_lap_s=92.34, baseline_lap_s=94.10)
    body = open(clog, encoding="utf-8").read()
    check("cumulative log records kept+reverted with evidence + lap delta",
          "KEPT" in body and "REVERTED" in body and "12C hotter" in body and "-1.76s" in body)
    store.append_cumulative_log(st, base, car="Car2", car_class="S1 800", discipline="dirt",
            drivetrain="AWD", started_iso="2026-06-21T11:00:00", best_lap_s=80.0)
    check("cumulative log grows (append, not overwrite)",
          open(clog, encoding="utf-8").read().count("## ") == 2)
    ls = store.list_sessions()
    check("list_sessions returns the saved tune summary",
          len(ls) == 1 and ls[0]["car"] == "2020 Supra RZ" and ls[0]["best_lap_s"] == 92.34)
    log = os.path.join(d, "app.log"); open(log, "w").write("x\n" * 50)
    hf = os.path.join(d, "tyre_temps_1.png"); open(hf, "wb").write(b"PNG")
    env = {"resolution": [2560, 1440], "temp_reader_used": "rapidocr",
           "anthropic_api_key_present": False}
    zp = store.write_support_bundle(car="2020 Supra RZ", discipline="road", env=env,
                                    app_log=log, heat_frames=[hf])
    names = zipfile.ZipFile(zp).namelist()
    check("support zip bundles env + session + final tune + app.log + heat frame",
          {"environment.json", "session.json", "app.log"}.issubset(set(names))
          and any(n.startswith("heat_frames/") for n in names))


def test_two_surface_split():
    print("\n== product: two surfaces (live overlay vs management window) ==")
    import tempfile, os
    from lapsmith.state import store
    from lapsmith import ordinals
    from lapsmith.gui import controller as C, overlay
    d = tempfile.mkdtemp(); store.set_sessions_dir(d)
    ordinals._USER_MAP.clear(); ordinals.set_store_path(os.path.join(d, "cn.json"))
    ordinals.save_name(12345, "Test Supra")
    c = C.Controller()
    # OVERLAY is live-tuning only now: no tab state, only simple/advanced.
    check("overlay has no tab state (tabs moved to the window)",
          not hasattr(c, "tab") and not hasattr(c, "cycle_tab"))
    check("default overlay view is SIMPLE", c.view_mode == "simple")
    check("toggle -> advanced", c.toggle_view_mode() == "advanced")
    c.set_view_mode("simple")
    check("set_view_mode back to simple", c.view_mode == "simple")
    for vm in ("simple", "advanced"):
        c.view_mode = vm
        html = overlay._render(c.status())
        check(f"overlay renders the live HUD ({vm})",
              bool(html) and "Step" in html and "<div" in html)
    check("overlay renderers for tabs are gone",
          not any(hasattr(overlay, fn) for fn in
                  ("_render_previous", "_render_settings", "_render_help", "_tab_bar")))
    # MANAGEMENT data the window reads straight off the controller:
    sv = c.settings_view()
    check("window Settings data lists saved car names",
          any(n["name"] == "Test Supra" for n in sv["car_names"]))
    check("window Help text is the step guide", "STEPS" in c.HELP_TEXT)
    check("window Previous Tunes + Dashboard providers exist",
          isinstance(c.previous_tunes(), list) and isinstance(c.stats_summary(), dict))


def test_car_import():
    print("\n== product: car-name DB import (CSV/TSV/JSON, merge) ==")
    import tempfile, os, json, importlib
    from lapsmith import ordinals, car_import
    d = tempfile.mkdtemp()
    ordinals._USER_MAP.clear()
    ordinals.set_store_path(os.path.join(d, "cn.json"))
    # a name the user set/edited must always win over an import
    ordinals.save_name(100, "My Edited Name")

    s = car_import.import_text("100,Should Not Win\n200,Toyota Supra\n300,Nissan GTR\n", "a.csv")
    check("CSV ordinal,name imports + fills gaps, user name kept",
          s["imported"] == 2 and s["already"] == 1
          and ordinals.name_for(100) == "My Edited Name"
          and ordinals.name_for(200) == "Toyota Supra")
    s = car_import.import_text("name,ordinal\nMazda RX7,400\nHonda NSX,500\n", "b.csv")
    check("CSV name,ordinal (reverse order auto-detected) + header skipped",
          s["imported"] == 2 and s["malformed"] == 0 and ordinals.name_for(400) == "Mazda RX7")
    s = car_import.import_text("600\tFord GT\n700\tBMW M3\n", "c.tsv")
    check("TSV imports", s["imported"] == 2 and ordinals.name_for(700) == "BMW M3")
    s = car_import.import_text(json.dumps({"800": "Lambo", "900": "Ferrari"}), "d.json")
    check("JSON {ordinal:name} object", s["imported"] == 2 and ordinals.name_for(800) == "Lambo")
    s = car_import.import_text(json.dumps(
        [{"id": 1000, "name": "Koenigsegg"}, {"ordinal": 1100, "model": "Pagani"}]), "e.json")
    check("JSON list of objects (id/name, ordinal/model)",
          s["imported"] == 2 and ordinals.name_for(1100) == "Pagani")
    s = car_import.import_text(json.dumps([[1200, "Aston"], ["McLaren", 1300]]), "f.json")
    check("JSON list of [ordinal,name] / [name,ordinal] pairs",
          s["imported"] == 2 and ordinals.name_for(1300) == "McLaren")
    s = car_import.import_text("badrow\n1400,Bugatti\n,,\njunk;junk\n", "g.csv")
    check("malformed rows counted, valid row still imported",
          s["imported"] == 1 and s["malformed"] >= 1)
    s = car_import.import_text("200,Different Name\n", "h.csv")
    check("re-import never overwrites an existing name",
          s["imported"] == 0 and s["already"] == 1 and ordinals.name_for(200) == "Toyota Supra")

    # the REAL Nexus "Forza Horizon 6 Car ID List" export: UTF-8 BOM, semicolon-
    # delimited, CRLF, an 11-column header mapped BY NAME (display_name beats model).
    hdr = ("car_id;display_name;year;make;model;asset;manufacturer_code;"
           "raw_model;confidence;zip_file;internal_path")
    body = [f"{900000 + i};Make {i} Display {i};2020;Make{i};Model{i};asset_{i};"
            f"MC{i};raw{i};0.99;cars_{i}.zip;/data/cars/{i}" for i in range(638)]
    body.append("not_an_id;Bad Ordinal;2020;X;Y;a;b;c;d;e;f")   # non-int ordinal
    body.append(";Missing Ordinal;2020;X;Y;a;b;c;d;e;f")         # empty ordinal
    nexus = "﻿" + "\r\n".join([hdr] + body) + "\r\n"
    mp, malformed = car_import.parse_text(nexus, "car_names.csv")
    check("Nexus CSV (BOM, semicolon, CRLF, 11-col header) -> ~638 by-name",
          len(mp) == 638 and malformed == 2
          and mp[900000] == "Make 0 Display 0")     # display_name, not model

    # Nexus JSON: {ordinal: {record}} - store the INNER display_name, NOT the object.
    recs = {str(910000 + i): {"display_name": f"Rec {i} Car {i}", "year": 1989,
                              "make": f"Make{i}", "model": f"Model{i}"}
            for i in range(638)}
    mp2, mal2 = car_import.parse_text(json.dumps(recs), "cars.json")
    check("Nexus JSON dict-of-records -> ~638 names (inner display_name, not the object)",
          len(mp2) == 638 and mal2 == 0 and mp2[910000] == "Rec 0 Car 0"
          and "{" not in mp2[910000] and "display_name" not in mp2[910000])

    # REPAIR: a previously stored serialized-record blob is overwritten on import,
    # but a genuine user-typed name is still preserved.
    ordinals.save_name(920001, "{'display_name': '1989 Volkswagen Golf Rallye', 'year': 1989}")
    ordinals.save_name(920002, "My Hand-Typed Name")
    s = car_import.import_text("920001,Volkswagen Golf Rallye\n920002,Should Not Win\n", "r.csv")
    check("import overwrites serialized-record junk but keeps genuine user names",
          ordinals.name_for(920001) == "Volkswagen Golf Rallye"
          and ordinals.name_for(920002) == "My Hand-Typed Name"
          and s["imported"] == 1 and s["already"] == 1)
    # the credited Nexus link the dialog/CLI point at (no bundled data)
    check("import points users at the Nexus FH6 Car ID List page",
          car_import.NEXUS_CAR_LIST_URL == "https://www.nexusmods.com/forzahorizon6/mods/309"
          and "Forza Horizon 6" in car_import.NEXUS_CAR_LIST_TITLE)
    check("no third-party car list is bundled in the package",
          not os.path.exists(os.path.join(os.path.dirname(car_import.__file__), "car_names.csv"))
          and not os.path.exists(os.path.join(os.path.dirname(car_import.__file__), "cars.json")))

    # CLI: python -m lapsmith.import-cars <file>  (hyphenated module via string import)
    cli = importlib.import_module("lapsmith.import-cars")
    csv_path = os.path.join(d, "more.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("1500,Porsche 911\n1600,Audi R8\n")
    names_path = os.path.join(d, "cli_names.json")
    rc = cli.main([csv_path, "--names-file", names_path])
    saved = json.load(open(names_path, encoding="utf-8"))
    check("CLI import-cars writes names + returns 0",
          rc == 0 and saved.get("1500") == "Porsche 911" and saved.get("1600") == "Audi R8")
    check("CLI on a missing file returns 2 (read error)",
          cli.main([os.path.join(d, "nope.csv"), "--names-file", names_path]) == 2)
    empty = os.path.join(d, "empty.csv"); open(empty, "w").write("")
    check("CLI on a file with no recognisable names returns 1",
          cli.main([empty, "--names-file", names_path]) == 1)


def test_reset_session():
    print("\n== product: reset_session for a fresh run (long-lived app) ==")
    from lapsmith.gui import controller as C
    c = C.Controller()
    # dirty up some session state
    c.best_segment = 50.0; c._baseline_lap_s = 52.0; c.mode = C.MODE_AUTO
    c._awaiting_test = True; c.stale = 2; c._restart_count = 3
    c.export = {"folder": "x"}; c.tyre_reading = {"FL": {}}; c.last_reader = "rapidocr"
    old_started = c.started_iso
    c.reset_session()
    check("reset clears best/baseline/mode/awaiting/export/reader",
          c.best_segment is None and c._baseline_lap_s is None and c.mode is None
          and c._awaiting_test is False and c.export is None
          and c.tyre_reading is None and c.last_reader is None
          and c.stale == 0 and c._restart_count == 0)
    check("reset stamps a fresh started_iso", c.started_iso != old_started or old_started == "")


def test_end_to_end_run():
    print("\n== product: FULL simulated end-to-end run (loop intact) ==")
    import tempfile, os
    from lapsmith.state import store
    from lapsmith import ordinals, identity
    from lapsmith.gui import controller as C
    from lapsmith.telemetry.laps import LapResult
    d = tempfile.mkdtemp(); store.set_sessions_dir(d)
    ordinals._USER_MAP.clear(); ordinals.set_store_path(os.path.join(d, "cn.json"))
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    c = C.Controller(started_iso="2026-06-21T12:00:00"); c.identity = ident
    # name the (unknown) car, then setup
    c.car_name_prompt_fn = lambda i: "E2E Test Car"
    if c.needs_car_name():
        c.confirm_car()
    else:
        c.phase = C.SETUP
    c.identity.name = "E2E Test Car"          # deterministic name for the assertions
    c.apply_setup("road", CarLimits(), changes_per_test=1, laps_per_test=1)
    check("after setup -> APPLY_BASELINE with a baseline tune",
          c.phase == C.APPLY_BASELINE and c.baseline is not None)
    c.baseline_applied(); c.mode = C.MODE_AUTO
    reading = {"FL": {"inner": 95, "mid": 85, "outer": 80},
               "FR": {"inner": 95, "mid": 85, "outer": 80},
               "RL": {"inner": 82, "mid": 81, "outer": 80},
               "RR": {"inner": 82, "mid": 81, "outer": 80}}
    c.lap_heat_fn = lambda: (None, 0.0, None)        # no frame; we inject reading directly
    # baseline: warm-up discarded, lap 2 sets the reference + first change
    c.arm_next_lap()
    c._on_lap(LapResult(1, 60.0, _lap_packets(1, 5, 0.0)))
    check("baseline warm-up lap discarded", c.best_segment is None)
    c.tyre_reading = reading                          # pretend OCR read the Heat page
    c._on_lap(LapResult(2, 60.0, _lap_packets(2, 5, 60.0)))
    check("baseline reference set; baseline_lap_s captured + first change shown",
          c.best_segment == 60.0 and c._baseline_lap_s == 60.0 and c.phase == C.SHOW_CHANGE)
    # drive several real keep/revert iterations: apply -> warm-up -> faster test lap.
    # (laps_per_test=1 so each test finalizes in one timed lap.)
    iters = 0; ln = 10
    while c.phase == C.SHOW_CHANGE and iters < 8:
        iters += 1
        c.tyre_reading = reading
        c.change_applied()
        check_phase = c.phase == C.DRIVE_AUTO and c._awaiting_test
        ln += 2
        c._on_lap(LapResult(ln, 99.0, _lap_packets(ln, 5, 99.0)))      # warm-up ignored
        c.tyre_reading = reading
        c._on_lap(LapResult(ln + 1, 58.0 - iters, _lap_packets(ln + 1, 5, 58.0 - iters)))
    check("loop ran multiple apply/test iterations without crashing", iters >= 3)
    check("improving laps were kept (best beat the 60.0 baseline)",
          c.best_segment is not None and c.best_segment < 60.0)
    # finalize the run (normally _compute_batch calls finish() at convergence)
    if c.phase != C.DONE:
        c.finish()
    check("finish() reaches DONE and writes the share files",
          c.phase == C.DONE and bool(c.export)
          and os.path.exists(c.export["txt"]) and os.path.exists(c.export["json"]))
    check("session JSON + cumulative log exist after the run",
          os.path.exists(os.path.join(d, "cumulative_tune_log.md"))
          and len(store.list_sessions()) == 1)
    # the support bundle the app writes on completion
    zp = c.write_support_bundle(app_log=None, heat_frames=[])
    check("support bundle written for the run", bool(zp) and os.path.exists(zp))
    # the new tune appears in the management surfaces (Previous Tunes + Dashboard)
    prev = c.previous_tunes()
    check("completed tune appears in Previous Tunes",
          len(prev) == 1 and prev[0]["car"] == "E2E Test Car")
    ss0 = c.stats_summary()
    check("Dashboard stats reflect the run", ss0["total_tunes"] == 1)

    # SECOND run on the SAME long-lived controller (app stays alive between tunes):
    # reset_session must give a clean slate and the dashboard must then show 2.
    c.reset_session()
    check("after reset_session the controller is clean for a new car",
          c.best_segment is None and c.export is None and c.phase == C.DONE)
    c.identity.name = "E2E Test Car"   # same car/disc -> overwrites; use a 2nd disc
    c.apply_setup("dirt", CarLimits(), changes_per_test=1, laps_per_test=1)
    c.baseline_applied(); c.mode = C.MODE_AUTO
    c.arm_next_lap(); c._on_lap(LapResult(1, 70.0, _lap_packets(1, 5, 0.0)))
    c.tyre_reading = reading
    c._on_lap(LapResult(2, 70.0, _lap_packets(2, 5, 70.0)))
    if c.phase == C.SHOW_CHANGE:
        c.change_applied()
        c._on_lap(LapResult(20, 99.0, _lap_packets(20, 5, 99.0)))
        c._on_lap(LapResult(21, 68.0, _lap_packets(21, 5, 68.0)))
    if c.phase != C.DONE:
        c.finish()
    ss1 = c.stats_summary()
    check("Dashboard counts BOTH sessions after a second run",
          ss1["total_tunes"] == 2 and ss1["by_car"].get("E2E Test Car") == 2)


def test_tesseract_path():
    print("\n== Stage 2: bundled-Tesseract path wiring ==")
    from lapsmith.vision import read_tyres
    import os, tempfile
    # no env / no bundle -> returns None, no crash
    os.environ.pop("FH6_TESSERACT", None)
    check("configure_tesseract() safe with no binary", read_tyres.configure_tesseract() is None)
    # explicit override via env is picked up
    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
        fake = f.name
    os.environ["FH6_TESSERACT"] = fake
    check("FH6_TESSERACT override detected", read_tyres._bundled_tesseract() == fake)
    os.environ.pop("FH6_TESSERACT", None)
    os.unlink(fake)

    # overlay capture display-affinity decision (Show overlay in recordings)
    from lapsmith.vision import capture
    os.environ.pop("LAPSMITH_OVERLAY_CAPTURABLE", None)
    check("overlay hidden from capture by default (setting OFF, no env)",
          capture.overlay_capturable(False) is False)
    check("overlay capturable when the setting is ON",
          capture.overlay_capturable(True) is True)
    os.environ["LAPSMITH_OVERLAY_CAPTURABLE"] = "1"
    check("LAPSMITH_OVERLAY_CAPTURABLE env force-enables capture",
          capture.overlay_capturable(False) is True)
    os.environ.pop("LAPSMITH_OVERLAY_CAPTURABLE", None)


def _window(scenario, n=240):
    return [parse(simulator._build_packet(simulator.frame(i * 0.05, scenario)))
            for i in range(n)]


def test_rules():
    print("\n== analyzer scenarios ==")
    tune = build_baseline("Test Car", "S1 800", "road", 48.0, "AWD")

    s = aggregate(_window("understeer"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None)
    # road baseline has rear ARB at the soft-floor (60), so the fix is to
    # soften the FRONT ARB - either lever is a valid anti-understeer move.
    check(f"understeer -> arb (got {rec.group} {list(rec.fields)})",
          rec.group == "arb" and ("arb_f" in rec.fields or "arb_r" in rec.fields))

    s = aggregate(_window("oversteer"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None)
    check(f"oversteer -> arb/diff (got {rec.group})", rec.group in ("arb", "diff"))

    s = aggregate(_window("front_bottoming"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None)
    check(f"front bottoming -> ride_height (got {rec.group})",
          rec.group in ("ride_height", "damping_bump"))

    s = aggregate(_window("hot_front"))
    rec = rules.analyze(s, tune, "road", tyre_reading=None)
    check(f"hot front L/R -> pressure (got {rec.group})", rec.group == "pressure")

    # camber needs the screenshot reading
    reading = {"FL": {"inner": 95, "mid": 85, "outer": 80},
               "FR": {"inner": 95, "mid": 85, "outer": 80},
               "RL": {"inner": 82, "mid": 81, "outer": 80},
               "RR": {"inner": 82, "mid": 81, "outer": 80}}
    s = aggregate(_window("neutral"))
    rec = rules.analyze(s, tune, "road", tyre_reading=reading)
    check(f"hot inner front -> camber reduce (got {rec.group})",
          rec.group == "camber" and rec.fields.get("camber_f", -9) > tune.camber_f)


def test_udp_roundtrip():
    print("\n== live UDP loopback ==")
    port = 5699
    listener = TelemetryListener(port=port)
    listener.start()
    time.sleep(0.2)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(200):
        # emit the REAL live size: 324 bytes (base + 1 trailing byte)
        pkt = simulator._build_packet(simulator.frame(i * 0.05, "understeer")) + b"\x00"
        sock.sendto(pkt, ("127.0.0.1", port))
        time.sleep(0.001)
    time.sleep(0.3)
    check(f"received packets ({listener.packet_count})", listener.packet_count >= 150)
    check("no parse errors on 324B stream", listener.error_count == 0)
    check("listener logged 324B datagrams", listener.observed_lengths.get(324, 0) >= 150)
    snap = listener.snapshot()
    check("snapshot decodes (speed>0, len 324)",
          snap is not None and snap.speed > 0 and snap.packet_len == 324)
    listener.stop()


if __name__ == "__main__":
    test_offsets()
    test_segment_timer()
    test_unit_reading()
    test_limits_clamping()
    test_lever_pinned_bottoming()
    test_aero_and_gearing()
    test_stage1_fixes()
    test_identity_autodetect()
    test_drivetrain_detection()
    test_gui_controller()
    test_fitness_resolution()
    test_auto_lap()
    test_batch_changes()
    test_multi_lap_fitness()
    test_lateral_capture_axis()
    test_distinct_rear_temps()
    test_vision_reader()
    test_rapidocr_mapping()
    test_camber_search()
    test_temp_reader_chain()
    test_product_tweaks()
    test_car_naming()
    test_restart_and_warmup()
    test_step_machine()
    test_file_outputs()
    test_two_surface_split()
    test_car_import()
    test_reset_session()
    test_end_to_end_run()
    test_tesseract_path()
    test_ocr_value_parsing()
    test_rules()
    test_udp_roundtrip()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
