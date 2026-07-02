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
    check(f"detected class label from PI 800 -> S1 (shared table) (got {ident.class_letter})",
          ident.class_letter == "S1")
    check(f"target class from PI 800 -> S1 800 (own class, no bump) (got {ident.target_class})",
          ident.target_class == "S1 800")
    check("PI 600 -> B 600 (FH6: 600 is B's ceiling)",
          identity.suggest_target_class(600) == "B 600")
    check("PI 601 -> A 700 (601 crosses into A)",
          identity.suggest_target_class(601) == "A 700")
    check("PI 950 -> R 998 (new class between S2 and X)",
          identity.suggest_target_class(950) == "R 998")
    check("PI 650 -> A 700 (own class)", identity.suggest_target_class(650) == "A 700")
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
    ctrl.apply_setup("road", lim, front_weight_pct=48.0, target_class="R 998")
    check("user-selected target class wins + flows to car_class metadata",
          ctrl.target_class == "R 998" and ctrl._meta()["car_class"] == "R 998")
    # the dropdown options + default label come from the shared class table, and
    # MUST match what the detected-class label derives (single source of truth)
    from lapsmith.knowledge import baseline as _bl
    opts = _bl.target_class_options()
    check("target dropdown options reuse the FH6 class table (incl. R)",
          opts == ["D 400", "C 500", "B 600", "A 700", "S1 800", "S2 900", "R 998", "X 999"]
          and _bl.class_target_label("B") == "B 600")
    check("detected-class label and dropdown agree (PI 600 -> B 600)",
          _bl.class_for_pi(600) == "B" and _bl.class_target_label("B") == "B 600"
          and _bl.class_for_pi(601) == "A")
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

    # re-read every LIVE frame: a drivetrain swap (AWD conversion) IS reflected
    class _Stub:
        def __init__(self, p): self.pkt = p; self.last_packet_time = time.time()
        def snapshot(self): return self.pkt
    ctrl = C.Controller()
    ctrl.identity = identity.identify(_car_packet(1, 6))       # starts RWD
    ctrl.listener = _Stub(_car_packet(2, 6))                   # now reads AWD (same ordinal)
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
    check("AUTO-LAP engages once the lap timer advances; warm-up laps armed",
          cda.mode == C.MODE_AUTO and cda._skip_laps == rules.WARMUP_LAPS)

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
    cend.listener.push(_lap_packets(2, 1.0, 53.0)); cend.tick()     # warm-up lap -> skipped (WARMUP_LAPS=1)
    check("tick path: warm-up lap skipped, no reference yet", cend.best_segment is None)
    cend.listener.push(_lap_packets(3, 1.0, 52.0)); cend.tick()     # full lap -> baseline + change
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


def test_f8_rivals_autolap():
    """Regression for v0.1.3: F8 stopped reaching lap tracking because the frozen
    build silently shipped WITHOUT the `keyboard` dep (no global hotkeys). Guards
    BOTH the hotkey wiring (advance=F8, keyboard importable) AND the full live
    chain detect -> setup -> F8(baseline_applied) -> DRIVE_AUTO -> tick -> AUTO,
    through the real UDP listener + parser + watcher (not a fake feed)."""
    print("\n== regression: F8 -> AUTO lap-tracking in Rivals (full chain) ==")
    import socket, time as _time
    from lapsmith.gui import controller as C
    from lapsmith.gui.hotkeys import HotkeyManager, DEFAULT_BINDINGS
    from lapsmith.telemetry.listener import TelemetryListener
    from lapsmith.state.tune_state import CarLimits
    from lapsmith import simulator

    # advance is F8, and `keyboard` MUST be importable so the FROZEN build can
    # register global hotkeys (it once shipped without it -> F8 dead).
    check("advance hotkey bound to F8", DEFAULT_BINDINGS["advance"] == "f8")
    check("keyboard dep importable (so the frozen build can register hotkeys)",
          HotkeyManager({}).available())

    port = 5661
    ctrl = C.Controller(port=port)
    ctrl.listener = TelemetryListener(port=port); ctrl.listener.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    def send(t):
        sock.sendto(simulator._build_packet(simulator.frame(t, "rivals")), ("127.0.0.1", port))
    try:
        for t in (0.1, 0.2, 0.3):
            send(t)
        _time.sleep(0.2)
        ctrl.poll_identity()
        check("car detected from rivals telemetry", ctrl.identity is not None)
        # START TUNING (button path) -> APPLY_BASELINE
        ctrl.reset_session(); ctrl.confirm_car()
        ctrl.apply_setup("road", CarLimits(), front_weight_pct=50.0,
                         target_class=ctrl.identity.target_class)
        check("setup -> APPLY_BASELINE", ctrl.phase == C.APPLY_BASELINE)
        # F8 at APPLY_BASELINE -> baseline_applied -> DRIVE_AUTO (detecting)
        ctrl.baseline_applied()
        check("F8 -> DRIVE_AUTO, detecting", ctrl.phase == C.DRIVE_AUTO and ctrl.mode is None)
        # drive across a lap boundary (LapNumber 0 -> 1) so the timer is seen advancing
        for t in (19.6, 19.8, 20.0, 20.2, 20.4, 20.6):
            send(t)
        _time.sleep(0.25)
        for _ in range(8):
            ctrl.tick()
            if ctrl.mode == C.MODE_AUTO:
                break
            _time.sleep(0.05)
        check("driving a lap engages AUTO-LAP (F8 reaches lap tracking)",
              ctrl.mode == C.MODE_AUTO)
    finally:
        ctrl.listener.stop()


def test_reload_and_fixation():
    print("\n== regression: reload not measured (B) + anti-fixation cap (A) ==")
    from lapsmith.telemetry.laps import LapWatcher, LAP_TIME_FLOOR
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import rules
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith import identity
    from lapsmith.telemetry.laps import LapResult
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- B1 watcher: a RELOAD counter-reset is NOT a finished lap ---------------
    w = LapWatcher()
    w.feed(_lap_packets(0, 5.0, 0.0, n=2))
    w.feed(_lap_packets(0, 30.0, 0.0, n=2))      # mid lap-0, timer at 30s
    w.pop_restarted()                            # clear the "seen live" restart
    # reload: CurrentLap drops to ~0, LapNumber stays 0, LastLap carried-over (0)
    res = w.feed(_lap_packets(0, 0.5, 0.0, n=2))
    check("reload counter-reset emits NO measured lap", len(res) == 0)
    check("reload counter-reset flags a restart (re-arm out-lap)", w.pop_restarted() is True)
    # a GENUINE completion (LapNumber++ with a fresh, plausible LastLap) does emit
    res2 = w.feed(_lap_packets(1, 0.5, 92.3, n=2))
    check("genuine lap (LapNumber++ + fresh LastLap) emits one measured lap",
          len(res2) == 1 and abs(res2[0].last_lap_s - 92.3) < 0.01)

    # --- B2 controller: WAITING_FOR_MEASURED_LAP - reload doesn't run the gate ---
    cb = C.Controller(); cb.identity = ident
    cb.apply_setup("road", CarLimits()); cb.mode = C.MODE_AUTO
    cb.best_segment = 60.0; cb._baseline_lap_s = 60.0
    cb.stats = aggregate(_window("understeer"))
    cb._compute_batch()
    cb.change_applied()
    check("after a change -> WAITING_FOR_MEASURED_LAP [out_lap]",
          cb._await_state == "out_lap" and cb._awaiting_test and cb._skip_laps == 1)
    cb._on_lap(LapResult(1, 99.0, _lap_packets(1, 5, 99.0)))   # out-lap, skipped
    check("out-lap skipped -> [measuring]", cb._await_state == "measuring")
    prev_best = cb.best_segment
    cb._on_lap(LapResult(1, 2.0, _lap_packets(1, 2, 2.0)))     # reload-short lap
    check("reload-short lap (<= floor) is NOT measured; gate did not run",
          cb.best_segment == prev_best and cb._awaiting_test and cb._await_state == "measuring")
    check("reload re-armed the out-lap", cb._skip_laps >= 1)
    check("LAP_TIME_FLOOR guards short laps", LAP_TIME_FLOOR >= 5.0 and 2.0 <= LAP_TIME_FLOOR)
    cb._skip_laps = 0
    cb._on_lap(LapResult(2, 58.0, _lap_packets(2, 5, 58.0)))   # real lap -> gate
    check("a real measured lap runs the gate (waiting state cleared)",
          cb._await_state is None and not cb._awaiting_test)

    # --- A1 anti-fixation cap: bottoming front locks after the cap, moves on -----
    ca = C.Controller(); ca.identity = ident
    ca.apply_setup("road", CarLimits(), aggressiveness="normal"); ca.mode = C.MODE_AUTO
    ca.best_segment = 60.0
    ca.stats = aggregate(_window("front_bottoming"))
    ca._compute_batch()
    check("bottoming_front fires (attempt 1)",
          any(getattr(r, "symptom", "") == "bottoming_front" for r in ca.batch)
          and ca._bottoming_attempts.get("front") == 1)
    ca._compute_batch()
    check(f"bottoming_front hits cap ({rules.BOTTOMING_CAP}) and LOCKS the axle",
          ca._bottoming_attempts.get("front") == rules.BOTTOMING_CAP
          and "front" in ca._bottoming_locked)
    ca._compute_batch()
    check("after the cap, the loop stops re-firing front bottoming (moves on)",
          not any(getattr(r, "symptom", "") == "bottoming_front" for r in ca.batch))

    # --- A3 balance-aware bottoming + dirt tolerance + A2 aggressiveness ---------
    check("dirt bottoming threshold looser than road",
          rules._bottom_thresh("dirt") < rules._bottom_thresh("road"))
    t = Tune(); t.bump_f = 5.0; t.spring_f = 80.0
    rec_us = rules._bottoming_fix("front", 0.01, t, "road", CarLimits(),
                                  ride_ineffective=True, understeer=True)
    rec_ok = rules._bottoming_fix("front", 0.01, t, "road", CarLimits(),
                                  ride_ineffective=True, understeer=False)
    check("understeer + front bottoming -> SPRING (avoids front bump)",
          rec_us.group == "springs")
    check("no understeer + front bottoming (ride pinned) -> BUMP",
          rec_ok.group == "damping_bump")
    check("aggressiveness: coarse > normal > fine step multiplier",
          rules.step_mult_for("coarse") > rules.step_mult_for("normal") > rules.step_mult_for("fine"))


def _slip_window(rear_slip, n=240):
    out = []
    for i in range(n):
        v = simulator.frame(i * 0.05, "oversteer")
        v["tire_slip_ratio_rl"] = v["tire_slip_ratio_rr"] = rear_slip
        out.append(parse(simulator._build_packet(v)))
    return aggregate(out)


def test_dirt_diff_and_lever_cap():
    print("\n== regression: dirt slip thresholds + general per-lever no-improve cap ==")
    from lapsmith.knowledge import rules
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.gui import controller as C
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- C: discipline-aware on-power slip threshold (the Audi mismatch) ---------
    s = _slip_window(0.45)           # ~0.45 on-power rear slip (normal dirt wheelspin)
    t = Tune(); t.diff_rear_accel = 80.0
    rec_road = rules._rule_diff(s, t, "road", None, True, CarLimits())
    rec_dirt = rules._rule_diff(s, t, "dirt", None, False, CarLimits())
    check("road: 0.45 on-power rear slip -> diff rule FIRES (tarmac fault)",
          rec_road is not None and "diff_rear_accel" in rec_road.fields)
    check("dirt: 0.45 wheelspin is WANTED -> diff rule does NOT fire",
          rec_dirt is None)
    check("dirt on-power threshold (0.60) > road (0.30)",
          rules.ON_POWER_OS_SLIP_DIRT > rules.ON_POWER_OS_SLIP)

    # --- D: dirt accel-diff driveability floor -----------------------------------
    s_hi = _slip_window(0.75)        # severe slip: even dirt would flag it
    t2 = Tune(); t2.diff_rear_accel = 53.0
    rec_floor = rules._rule_diff(s_hi, t2, "dirt", None, False, CarLimits())
    check("dirt: a firing accel-diff change is FLOORED at >= 50% (keeps drive)",
          rec_floor is not None
          and rec_floor.fields.get("diff_rear_accel") >= rules.DIRT_ACCEL_DIFF_FLOOR)
    t3 = Tune(); t3.diff_rear_accel = rules.DIRT_ACCEL_DIFF_FLOOR
    check("dirt: at the floor the accel diff can't be cut further",
          rules._rule_diff(s_hi, t3, "dirt", None, False, CarLimits()) is None)

    # --- A+B: general per-lever cap -> lock + roll back to last-improving ---------
    cc = C.Controller(); cc.identity = ident
    cc.apply_setup("dirt", CarLimits()); cc.mode = C.MODE_AUTO
    cc.best_segment = 52.0
    cc.state.current.set("diff_rear_accel", 35.0)
    cc._last_improving = {"diff_rear_accel": 35.0}     # 35 was the last improving value
    for attempt in (1, 2):
        rec = cc.state.apply_change("diff", {"diff_rear_accel": 30.0}, "neutral test", "")
        cc._applied_records = [rec]
        cc._apply_fitness_multi(52.0, 0.0)             # neutral (no gain): revert + count
        check(f"neutral attempt {attempt}: NOT banked - rolled back to 35.0",
              abs(cc.state.current.get("diff_rear_accel") - 35.0) < 1e-6
              and rec.verdict == "reverted" and rec.seg_after_s == 52.0)
    check("diff_rear_accel:down LOCKED after the cap",
          "diff_rear_accel:down" in cc._lever_locked
          and cc._noimprove.get("diff_rear_accel:down") == rules.LEVER_NOIMPROVE_CAP)
    filt = rules._filter_locked_levers(
        Recommendation("diff", {"diff_rear_accel": 30.0}, "x", ""), cc.state.current,
        cc._lever_locked)
    check("locked diff:down move is filtered out (no longer 'a rule fired')", filt is None)

    # --- B: a genuine improvement KEEPS (Veneno path unaffected, not capped) ------
    cv = C.Controller(); cv.identity = ident
    cv.apply_setup("road", CarLimits()); cv.mode = C.MODE_AUTO
    cv.best_segment = 60.0
    rkeep = cv.state.apply_change("arb", {"arb_r": 55.0}, "improve test", "")
    cv._applied_records = [rkeep]
    cv._apply_fitness_multi(59.0, 0.0)                 # -1.0s > LAP_IMPROVE_EPS -> KEEP
    check("genuine improvement kept (best 59.0), lever not locked, counter reset",
          abs(cv.best_segment - 59.0) < 1e-6 and rkeep.verdict == "kept"
          and "arb_r:up" not in cv._lever_locked and cv._noimprove.get("arb_r:up", 0) == 0)
    check("seg_after_s populated for the keep/revert audit trail",
          rkeep.seg_after_s == 59.0 and rkeep.seg_before_s == 60.0)

    # --- E: a locked lever drops out of analyze_batch so the loop can converge ----
    s_os = _slip_window(0.45)
    to = Tune(); to.diff_rear_accel = 80.0
    open_b = rules.analyze_batch(s_os, to, "road", max_search=0)
    lock_b = rules.analyze_batch(s_os, to, "road", max_search=0,
                                 lever_locked={"diff_rear_accel:down"})
    check("locked lever removed from analyze_batch (enables convergence)",
          any("diff_rear_accel" in r.fields for r in open_b)
          and not any("diff_rear_accel" in r.fields for r in lock_b))


def _telem_lap(lap_time, exit_g=1.0, grip=1.2, slip=0.30, n=150, brk=0, thr_corner=90,
               wheelspin=0.0, vy=0.0, roll=0.0):
    """One lap of synthetic telemetry binnable by track position: corner bins (lateral
    g), corner-exit bins (throttle-on forward g + rear slip), and straights.
    `exit_g` is the corner-exit forward g (the 'how quickly it accelerates' channel);
    `grip` is cornering lateral g. distance_traveled advances so binning works."""
    out = []
    for i in range(n):
        ph = i % 30
        v = {"is_race_on": 1, "speed": 50.0, "distance_traveled": i / n * 1200.0,
             "current_lap": i / n * lap_time, "last_lap": 0.0, "lap_number": 0,
             "engine_max_rpm": 7000.0, "current_engine_rpm": 5200.0,
             "drivetrain_type": 2, "accel": 255, "brake": 0}
        if ph < 12:                       # corner: high lateral g, partial throttle
            v["accel_x"] = grip * 9.80665
            v["accel_z"] = 0.0
            v["accel"] = thr_corner       # driver-input knob (throttle in the corner)
            v["brake"] = brk              # driver-input knob (brake in the corner)
            v["speed"] = 26.0
            v["steer"] = 70
        elif ph < 22:                     # corner exit: on throttle, forward g + slip
            v["accel_x"] = 0.5 * 9.80665
            v["accel_z"] = exit_g * 9.80665
            v["tire_slip_ratio_rl"] = slip
            v["tire_slip_ratio_rr"] = slip
            if wheelspin:                 # richer-channel knob: per-wheel wheelspin
                for w in ("fl", "fr", "rl", "rr"):
                    v[f"tire_slip_ratio_{w}"] = wheelspin
            v["speed"] = 34.0
        else:                             # straight
            v["accel_z"] = 0.3 * 9.80665
            v["speed"] = 60.0
        if vy:                            # richer-channel knob: vertical-g (ride)
            v["accel_y"] = vy * 9.80665
        if roll and ph < 12:              # richer-channel knob: L-R suspension asymmetry
            v["susp_norm_fl"] = 0.5 - roll / 2.0
            v["susp_norm_fr"] = 0.5 + roll / 2.0
            v["susp_norm_rl"] = 0.5 - roll / 2.0
            v["susp_norm_rr"] = 0.5 + roll / 2.0
        out.append(parse(simulator._build_packet(v)))
    return out


def test_telemetry_primary_fitness():
    print("\n== drift-robust: telemetry-primary fitness, A/B/A, budget, honest final check ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import fitness
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.state.tune_state import CarLimits
    from lapsmith.telemetry.laps import LapResult
    from lapsmith import identity
    import time as _t
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- H: the composite, binned by track position, sees an injected exit-g effect --
    ref = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
    better = fitness.bin_lap(_telem_lap(60.0, exit_g=1.35))      # real tune effect
    same = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))         # identical car behaviour
    check("telemetry bins are live (grip + exit channels present)", ref.live and better.live)
    comp_better = fitness.composite(better, ref, "road", group="diff")
    comp_same = fitness.composite(same, ref, "road", group="diff")
    check("composite identifies injected corner-exit forward-g gain (delta > eps)",
          comp_better.delta > fitness.COMPOSITE_IMPROVE_EPS and comp_better.exit > 0)
    check("composite ~0 for an identical car (no false positive from binning)",
          abs(comp_same.delta) <= fitness.COMPOSITE_IMPROVE_EPS)

    def new_ctrl(rigour="quick", budget=0.0):
        c = C.Controller(); c.identity = ident
        c.apply_setup("road", CarLimits(), rigour=rigour, time_budget_min=budget)
        c.mode = C.MODE_AUTO
        c.laps_per_test = 1
        # preset a LIVE baseline anchor so telemetry-primary mode is engaged
        c.best_segment = 60.0
        c._baseline_lap_s = 60.0
        c._ref_telem = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
        c._baseline_telem = c._ref_telem
        return c

    def measure(c, group, fields, lap_time, **telem):
        """Drive ONE change through the REAL gate: apply, out-lap (skipped), measured lap."""
        c._reanchor_pending = False           # isolate from periodic re-anchor
        c._iters_since_reanchor = 0
        c.batch = [Recommendation(group, fields, "test", "")]
        c.change_applied()
        c._on_lap(LapResult(c.lap_number + 1, 91.3, _telem_lap(91.3)))            # out-lap
        c._on_lap(LapResult(c.lap_number + 1, lap_time, _telem_lap(lap_time, **telem)))

    # --- (1) KEEP a real tune effect; DISCARD a neutral change that is only driver drift
    c = new_ctrl()
    measure(c, "diff", {"diff_rear_accel": 70.0}, 59.4, exit_g=1.35)   # real exit-g gain + faster
    check("(1) real tune effect KEPT (telemetry composite, exit-g up)",
          c.state.current.get("diff_rear_accel") == 70.0 and abs(c.best_segment - 59.4) < 1e-6)
    c = new_ctrl()
    measure(c, "arb", {"arb_r": 55.0}, 59.2, exit_g=1.0)              # FASTER lap, but SAME telemetry
    check("(1) driver-drift-only change DISCARDED despite a faster lap (composite flat)",
          c.state.current.get("arb_r") != 55.0 and c.best_segment == 60.0
          and c._noimprove.get("arb_r:down", 0) >= 1)

    # --- (2) anti-Goodhart guardrail: composite up but lap time clearly worse -> not kept
    c = new_ctrl()
    measure(c, "diff", {"diff_rear_accel": 70.0}, 60.6, exit_g=1.4)   # composite up, lap +0.6s worse
    check("(2) guardrail blocks a composite 'win' when lap time is clearly worse",
          c.state.current.get("diff_rear_accel") != 70.0 and c.best_segment == 60.0)

    # --- (3) A/B/A operates on the composite ----------------------------------------
    #   real gain: reverting to A drops exit-g back down -> B beats A' -> confirm & keep
    c = new_ctrl(rigour="confirmed")
    measure(c, "diff", {"diff_rear_accel": 70.0}, 59.5, exit_g=1.35)
    check("(3) apparent win triggers A/B/A (revert-to-A confirmation shown)",
          c._aba is not None and c.phase == C.SHOW_CHANGE and c.batch[0].group == "confirm_revert")
    c.change_applied()                                               # user reverts to A, drives A'
    c._on_lap(LapResult(c.lap_number + 1, 91.7, _telem_lap(91.7)))   # out-lap
    c._on_lap(LapResult(c.lap_number + 1, 59.9, _telem_lap(59.9, exit_g=1.0)))   # A' = low exit-g
    check("(3) A/B/A CONFIRMS a real gain (B beats re-measured A') -> re-apply shown",
          c._aba is None and c.phase == C.SHOW_CHANGE and c.batch[0].group == "confirm_reapply")
    c.change_applied()
    check("(3) confirmed change re-applied and kept",
          c.state.current.get("diff_rear_accel") == 70.0)
    #   driver drift: reverting to A STILL shows high exit-g (the driver, not the tune)
    c = new_ctrl(rigour="confirmed")
    measure(c, "diff", {"diff_rear_accel": 70.0}, 59.5, exit_g=1.35)
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.7, _telem_lap(91.7)))
    c._on_lap(LapResult(c.lap_number + 1, 59.6, _telem_lap(59.6, exit_g=1.35)))  # A' just as quick (same car)
    check("(3) A/B/A DISCARDS a driver-drift win (A' matches B) -> reverted, re-anchored",
          c._aba is None and c.state.current.get("diff_rear_accel") != 70.0
          and not (c.batch and c.batch[0].group == "confirm_reapply"))

    # --- (4) honest final check: 'within driver variation' when only the driver improved
    c = new_ctrl()
    c._begin_final_check(reason="converged")
    check("(4) final check asks to re-measure the ORIGINAL baseline",
          c._final_check and c.batch[0].group == "final_baseline")
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.1, _telem_lap(91.1)))
    c._on_lap(LapResult(c.lap_number + 1, 58.0, _telem_lap(58.0, exit_g=1.0)))   # baseline now FASTER, same car
    check("(4) reports 'within driver variation' (baseline as fast, same telemetry)",
          c.phase == C.DONE and "within driver variation" in c.final_verdict
          and c.stop_reason == "converged")
    #   confirmed-gain final check: optimised car genuinely better than re-measured baseline
    c = new_ctrl()
    c._ref_telem = fitness.bin_lap(_telem_lap(58.0, exit_g=1.4))     # optimised tune really better
    c.best_segment = 58.0
    c._begin_final_check(reason="converged")
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.1, _telem_lap(91.1)))
    c._on_lap(LapResult(c.lap_number + 1, 60.0, _telem_lap(60.0, exit_g=1.0)))   # baseline slower + low exit-g
    check("(4) reports a CONFIRMED tune improvement when the car is genuinely better",
          c.phase == C.DONE and "Confirmed tune improvement" in c.final_verdict)

    # --- (5) wall-clock budget: starts at first lap, stops AFTER the in-flight test ---
    c = new_ctrl(budget=20.0)
    check("(5) budget not started before the first lap", c.budget_remaining_s() is None)
    c._budget_start = _t.perf_counter()         # simulate the first-lap clock start
    check("(5) budget counts down once started", 0 < c.budget_remaining_s() <= 20 * 60)
    c._budget_start = _t.perf_counter() - (20 * 60 + 30)   # simulate elapsed incl. loads/menus
    # an in-flight measurement must finish cleanly, THEN the loop stops via the final check
    # (the deadline passed mid-test; _check_budget latches it at the decision point)
    measure(c, "diff", {"diff_rear_accel": 70.0}, 59.4, exit_g=1.35)
    check("(5) in-flight test finished before stopping (change still gated, not cut)",
          c.state.current.get("diff_rear_accel") == 70.0)
    # the budget-expiry was latched during the measurement and routes to the final check
    check("(5) budget expiry latched, honest final check queued",
          c._budget_expired and c._final_check and c.batch[0].group == "final_baseline")
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.0, _telem_lap(91.0)))
    c._on_lap(LapResult(c.lap_number + 1, 60.0, _telem_lap(60.0, exit_g=1.0)))
    check("(5) stop reason recorded as the time budget",
          c.phase == C.DONE and c.stop_reason == "stopped: time budget (20 min)")

    # --- regression: the Audi drift case (lap-time gain was the driver, not the tune) -
    #   composite stays flat across a 'faster' driver-drift lap -> not banked.
    c = new_ctrl()
    base_best = c.best_segment
    for lt in (59.5, 59.0, 58.5):       # driver keeps getting faster, telemetry unchanged
        measure(c, "diff", {"diff_rear_accel": 70.0}, lt, exit_g=1.0)
    check("(regression/Audi) pure driver drift never banks a tune change",
          c.state.current.get("diff_rear_accel") != 70.0 and c.best_segment == base_best)


def test_checklists_and_overlay_states():
    print("\n== exact change/revert checklists + ACTION/DRIVE overlay + early-finish ==")
    import os, tempfile, types
    import time as _t
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import fitness
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.state.tune_state import CarLimits
    from lapsmith.telemetry.laps import LapResult
    from lapsmith.state import prefs
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    def ctrl(rig="quick", bud=0.0):
        c = C.Controller(); c.identity = ident
        c.apply_setup("road", CarLimits(), rigour=rig, time_budget_min=bud)
        c.mode = C.MODE_AUTO; c.laps_per_test = 1
        c.best_segment = 60.0; c._baseline_lap_s = 60.0
        c._ref_telem = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
        c._baseline_telem = c._ref_telem
        c._on_car = c.state.current.as_dict()      # car physically on the current tune
        return c

    def find(cl, fld):
        return next((x for x in cl if x["field"] == fld), None)

    # --- Item 1: APPLY shows exact field old->new -------------------------------
    c = ctrl()
    c.batch = [Recommendation("diff", {"diff_rear_accel": 70.0}, "t", "")]; c.phase = C.SHOW_CHANGE
    it = find(c.menu_checklist(), "diff_rear_accel")
    check("(1) applying a change lists the exact field old->new (80 -> 70)",
          it and "80" in it["from"] and "70" in it["to"])
    ui = c.ui_state()
    check("(1) apply is ACTION REQUIRED with a checklist + F8",
          ui["klass"] == "action" and ui["checklist"] and "CHANGE THESE NOW" in ui["header"])

    # --- Item 1: REVERT states the exact set-BACK values ------------------------
    c = ctrl()
    c._on_car["diff_rear_decel"] = 10.0            # user physically set 10...
    c.state.current.set("diff_rear_decel", 15.0)   # ...but the gate reverted to 15
    c.batch = []
    it = find(c.menu_checklist(), "diff_rear_decel")
    check("(1) revert states the exact set-back (Rear Decel diff 10 -> 15)",
          it and "10" in it["from"] and "15" in it["to"])

    # --- Item 1: final check with NO diff -> 'already on baseline, just drive' ---
    c = ctrl()
    c._begin_final_check(reason="converged")
    ui = c.ui_state()
    check("(1) final check, car already on baseline -> JUST DRIVE, no checklist",
          ui["klass"] == "drive" and "already on the baseline" in ui["header"].lower()
          and not ui["checklist"])
    # final check WITH a diff -> list the set-back to baseline
    c = ctrl()
    c._on_car["diff_rear_accel"] = 70.0
    c._begin_final_check(reason="converged")
    it = find(c.ui_state()["checklist"], "diff_rear_accel")
    check("(1) final check with a diff lists the set-back to baseline (70 -> 80)",
          c.ui_state()["klass"] == "action" and it and "70" in it["from"] and "80" in it["to"])

    # --- Item 2: ACTION vs JUST-DRIVE are visually distinct classes -------------
    c = ctrl()
    c.batch = [Recommendation("diff", {"diff_rear_accel": 70.0}, "t", "")]; c.change_applied()
    out = c.ui_state()
    check("(2) out-lap is JUST DRIVE (no checklist), not an action prompt",
          out["klass"] == "drive" and "OUT-LAP" in out["header"] and not out["checklist"])
    c._on_lap(LapResult(c.lap_number + 1, 91.3, _telem_lap(91.3)))   # consume the out-lap
    meas = c.ui_state()
    check("(2) measuring lap is JUST DRIVE and labelled MEASURING lap x/y",
          meas["klass"] == "drive" and "MEASURING" in meas["header"])
    c2 = ctrl(); c2.batch = [Recommendation("arb", {"arb_r": 55.0}, "t", "")]; c2.phase = C.SHOW_CHANGE
    check("(2) a shown change is a different class (action) from a measuring lap (drive)",
          c2.ui_state()["klass"] == "action" and meas["klass"] == "drive")
    # re-anchor stays 'no change to enter'
    c3 = ctrl(); c3._begin_reanchor()
    check("(2) re-anchor is JUST DRIVE, no change to enter",
          c3.ui_state()["klass"] == "drive" and "RE-ANCHOR" in c3.ui_state()["header"]
          and not c3.ui_state()["checklist"])

    # --- Item 3: main-window Max-tuning-time control changes + persists ---------
    prefs.set_store_path(os.path.join(tempfile.mkdtemp(), "prefs.json"))
    c = ctrl()
    c.set_time_budget(12.0); prefs.set("time_budget_min", 12.0)
    check("(3) Max-tuning-time updates the live budget", c.time_budget_min == 12.0)
    check("(3) budget persists via prefs (one source of truth)", prefs.time_budget_min() == 12.0)
    c._budget_start = _t.perf_counter() - 13 * 60        # 13 min already elapsed
    c.set_time_budget(12.0)                              # tighten mid-run
    check("(3) tightening the budget mid-run applies to the running clock",
          c._budget_expired)

    # --- Item 4: convergence finishes EARLY (budget unspent), reason 'converged' -
    c = ctrl(bud=20.0); c._budget_start = _t.perf_counter()
    def _converge(self): self.phase = C.DONE; self.batch = []
    c._compute_batch = types.MethodType(_converge, c)
    c._next_step()
    check("(4) convergence wins over re-anchor and stops early (budget unspent)",
          c._final_check and not c._budget_expired and (c.budget_remaining_s() or 0) > 0)
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.0, _telem_lap(91.0)))
    c._on_lap(LapResult(c.lap_number + 1, 60.0, _telem_lap(60.0, exit_g=1.0)))
    check("(4) a converged run saves stop_reason 'converged'",
          c.phase == C.DONE and c.stop_reason == "converged")
    # contrast: a run cut off by the clock saves 'stopped: time budget'
    c = ctrl(bud=20.0); c._budget_start = _t.perf_counter() - (20 * 60 + 30)
    c.batch = [Recommendation("diff", {"diff_rear_accel": 70.0}, "t", "")]; c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.0, _telem_lap(91.0)))
    c._on_lap(LapResult(c.lap_number + 1, 59.4, _telem_lap(59.4, exit_g=1.35)))
    c.change_applied()
    c._on_lap(LapResult(c.lap_number + 1, 91.0, _telem_lap(91.0)))
    c._on_lap(LapResult(c.lap_number + 1, 58.0, _telem_lap(58.0, exit_g=1.0)))
    check("(4/minor) a clock cutoff saves stop_reason 'stopped: time budget' (not 'converged')",
          c.phase == C.DONE and c.stop_reason == "stopped: time budget (20 min)")


def test_console_mode():
    print("\n== console mode: LAN bind, OCR skipped, single-temp fallback, camber degrade ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import fitness, rules
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith.telemetry.session import TestStats
    from lapsmith.telemetry.listener import TelemetryListener
    from lapsmith.telemetry.laps import LapResult
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- 2) bind ALL interfaces (0.0.0.0) in PC + console: captures loopback AND LAN
    #        delivery (the installed-build "no telemetry" fix). Same robust bind both.
    c = C.Controller(); c.identity = ident
    check("PC mode binds all interfaces (0.0.0.0) - captures loopback + adapter delivery",
          c._bind_host() == "0.0.0.0")
    c.set_console_mode(True)
    check("console mode also binds all interfaces (0.0.0.0)",
          c.console_mode and c._bind_host() == "0.0.0.0")
    c.listener = TelemetryListener(port=5651, host=c._bind_host())   # constructed, not started
    check("the listener carries the all-interfaces host to the socket bind target",
          c.listener.host == "0.0.0.0")
    check("LAN IP is surfaced for the console's Data Out target",
          isinstance(c.lan_ip(), str) and c.lan_ip().count(".") == 3)

    # --- 3) console mode skips OCR/screenshot entirely (no error) ----------------
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits(), console_mode=True)
    called = {"ocr": False}
    def _boom():
        called["ocr"] = True
        raise AssertionError("console mode must NOT screenshot/OCR")
    c.lap_heat_fn = _boom
    c.tyre_reading = {"FL": {"inner": 95, "outer": 80}}      # stale 3-zone reading
    c._read_lap_heat()
    check("console: screenshot/OCR step skipped (lap_heat_fn never called)", not called["ocr"])
    check("console: tyre_reading cleared to None (no fabricated 3-zone data)",
          c.tyre_reading is None and "console" in c.last_reader)

    # --- 3) camber/toe degrade; pressure still uses the single per-corner temps --
    s = aggregate(_window("understeer"))
    check("console: zone-based camber rule does NOT fire without a 3-zone reading",
          rules._rule_camber(s, Tune(), "road", None, True, CarLimits()) is None)
    reading = {k: {"inner": 95, "middle": 88, "outer": 80} for k in ("FL", "FR")}
    reading.update({k: {"inner": 84, "middle": 83, "outer": 82} for k in ("RL", "RR")})
    rc = rules._rule_camber(s, Tune(), "road", reading, True, CarLimits())
    check("PC mode: a 3-zone reading still drives a confident camber change",
          rc is not None and "camber" in rc.group)
    st = TestStats(temp_fl=96.0, temp_fr=80.0, temp_rl=85.0, temp_rr=85.0, n_corner_frames=50)
    rp = rules._rule_pressure(st, Tune(), "road", None, True, CarLimits())
    check("console: single per-corner temps still drive the pressure rule (L/R balance)",
          rp is not None and rp.group == "pressure")
    # with NO reading, analyze falls back to lap-time camber_search (graceful degrade)
    rec = rules.analyze(aggregate(_window("understeer")), Tune(), "road", tyre_reading=None)
    check("console: camber degrades to lap-time search (never confident off missing data)",
          rec is None or rec.group != "camber")

    # --- 5) end to end in console mode: loop runs, OCR skipped, notice shown ------
    cc = C.Controller(); cc.identity = ident
    cc.apply_setup("road", CarLimits(), console_mode=True, rigour="quick")
    cc.mode = C.MODE_AUTO; cc.laps_per_test = 1
    cc.best_segment = 60.0; cc._baseline_lap_s = 60.0
    cc._ref_telem = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
    cc._baseline_telem = cc._ref_telem
    cc._on_car = cc.state.current.as_dict()
    cc.lap_heat_fn = _boom                          # would raise if console didn't skip OCR
    cc.batch = [Recommendation("diff", {"diff_rear_accel": 70.0}, "t", "")]; cc.change_applied()
    cc._on_lap(LapResult(cc.lap_number + 1, 91.3, _telem_lap(91.3)))           # out-lap (reads heat)
    cc._on_lap(LapResult(cc.lap_number + 1, 59.4, _telem_lap(59.4, exit_g=1.35)))  # measured
    check("console: end-to-end measurement runs, OCR skipped, change gated normally",
          not called["ocr"] and cc.state.current.get("diff_rear_accel") == 70.0)
    stt = cc.status()
    check("console: honest notice + LAN IP surfaced in status",
          stt["console_mode"] and "less accurate" in (stt["console_notice"] or "").lower()
          and stt["lan_ip"])

    # --- PC mode unchanged: OCR path still runs (tyre_reading honoured) -----------
    cp = C.Controller(); cp.identity = ident
    cp.apply_setup("road", CarLimits(), console_mode=False)
    check("PC mode: console flag off, OCR path intact",
          not cp.console_mode and cp.status()["console_mode"] is False
          and cp.status()["console_notice"] is None)


def test_troubleshooting_v0110():
    print("\n== troubleshooting: oscillation=fixation, springs reachable, drivetrain-aware diff ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import rules
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith.telemetry.session import TestStats
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- #1: a revert-then-re-apply OSCILLATION on one lever counts as fixation ----
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
    c.best_segment = 50.0
    c.state.current.set("ride_height_r", 6.0)
    c._last_improving = {"ride_height_r": 6.0}
    for _ in range(rules.LEVER_NOIMPROVE_CAP):
        rec = c.state.apply_change("ride_height", {"ride_height_r": 7.0}, "t", "")   # raise (up)
        c._applied_records = [rec]
        c._apply_fitness_multi(50.0, 0.0)                                            # neutral -> revert
    check("#1 apply->revert->apply->revert on one lever increments the fixation counter & LOCKS",
          "ride_height_r:up" in c._lever_locked
          and c._noimprove.get("ride_height_r:up") == rules.LEVER_NOIMPROVE_CAP
          and abs(c.state.current.get("ride_height_r") - 6.0) < 1e-6)   # rolled back, not drifted

    # --- #3: springs are REACHABLE once the ARBs are saturated --------------------
    us = TestStats(slip_angle_front=4.0, slip_angle_rear=2.0, n_corner_frames=30)   # understeer
    sat = Tune(); sat.arb_r = rules.ARB_REAR_SOFT_FLOOR; sat.arb_f = rules.ARB_MIN  # ARB saturated
    sb = rules._rule_spring_balance(us, sat, "road", None, True, CarLimits())
    check("#3 ARB saturated + understeer -> spring-balance STIFFENS the rear spring",
          sb is not None and sb.group == "spring_balance" and "spring_r" in sb.fields
          and sb.fields["spring_r"] > sat.spring_r)
    notsat = Tune(); notsat.arb_r = rules.ARB_REAR_SOFT_FLOOR; notsat.arb_f = 6.0    # ARB can still move
    check("#3 spring-balance stays silent while the ARB can still move (never fights ARB)",
          rules._rule_spring_balance(us, notsat, "road", None, True, CarLimits()) is None)
    batch = rules.analyze_batch(us, sat, "road", tyre_reading=None, max_search=3)
    check("#3 springs reachable in the NORMAL flow (analyze_batch emits spring_balance)",
          any(r.group == "spring_balance" for r in batch))

    # --- #2/#4: the diff rule only offers a diff the car actually HAS --------------
    t = Tune()
    fwd = TestStats(drivetrain="FWD", on_throttle_front_slip=0.5, on_throttle_rear_slip=0.1,
                    n_corner_frames=20)
    rf = rules._rule_diff(fwd, t, "road", None, True, CarLimits())
    check("#2 FWD -> FRONT diff only (no rear accel, no centre diff)",
          rf is not None and "diff_front_accel" in rf.fields
          and "diff_rear_accel" not in rf.fields and "diff_center" not in rf.fields)
    fwd_rear = TestStats(drivetrain="FWD", on_throttle_rear_slip=0.9, on_throttle_front_slip=0.1,
                         n_corner_frames=20)
    rfr = rules._rule_diff(fwd_rear, t, "road", None, True, CarLimits())
    check("#2 FWD never gets a REAR-diff change even with rear slip (it has no rear diff)",
          rfr is None or "diff_rear_accel" not in rfr.fields)
    rwd = TestStats(drivetrain="RWD", on_throttle_rear_slip=0.5, on_throttle_front_slip=0.1,
                    n_corner_frames=20)
    rr = rules._rule_diff(rwd, t, "road", None, True, CarLimits())
    check("#4 RWD -> rear accel diff, NO centre diff",
          rr is not None and "diff_rear_accel" in rr.fields and "diff_center" not in rr.fields)
    awd = TestStats(drivetrain="AWD", on_throttle_front_slip=0.5, on_throttle_rear_slip=0.1,
                    n_corner_frames=20)
    ra = rules._rule_diff(awd, t, "road", None, True, CarLimits())
    check("#4 AWD exit understeer -> centre diff (AWD-only branch)",
          ra is not None and "diff_center" in ra.fields)

    # --- #2: manual drivetrain override beats a (mis)detected DrivetrainType -------
    co = C.Controller(); co.identity = ident
    co.apply_setup("road", CarLimits(), drivetrain="FWD")
    check("#2 manual override sets the effective drivetrain (FWD)",
          co.effective_drivetrain() == "FWD")
    co.stats = TestStats(drivetrain="AWD", on_throttle_front_slip=0.5, on_throttle_rear_slip=0.1,
                         n_corner_frames=20)
    co.tyre_reading = None
    co._compute_batch()
    diffs = [r for r in co.batch if r.group == "diff"]
    check("#2 override forces an AWD-detected car to be tuned as FWD (front diff only)",
          co.stats.drivetrain == "FWD" and diffs
          and all("diff_rear_accel" not in r.fields and "diff_center" not in r.fields
                  for r in diffs))
    co.apply_setup("road", CarLimits(), drivetrain="auto")
    check("#2 'auto' clears the override (back to detected drivetrain)",
          co.drivetrain_override is None)


def test_ux_v0112():
    print("\n== UX: input-based A/B/A discount, reject-locks-session, progress reporting ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import fitness, rules
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith.telemetry.laps import LapResult
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- driver-input profile is live and detects different driving ---------------
    ref = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
    same = fitness.bin_lap(_telem_lap(60.0, exit_g=1.35))            # same inputs, better car
    diff = fitness.bin_lap(_telem_lap(60.0, exit_g=1.35, brk=200))   # braking very differently
    check("driver-input channels are live and binned", ref.inputs_live and diff.inputs_live)
    check("input_difference ~0 when the driver drove the same",
          0 <= fitness.input_difference(same, ref) <= fitness.INPUT_DRIVER_THRESH)
    check("input_difference is large when the driver drove differently",
          fitness.input_difference(diff, ref) > fitness.INPUT_DRIVER_THRESH)

    def ctrl(rigour="confirmed"):
        c = C.Controller(); c.identity = ident
        c.apply_setup("road", CarLimits(), rigour=rigour)
        c.mode = C.MODE_AUTO; c.laps_per_test = 1
        c.best_segment = 60.0; c._baseline_lap_s = 60.0; c._session_start_best = 60.0
        c._ref_telem = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))
        c._baseline_telem = c._ref_telem
        c._on_car = c.state.current.as_dict()
        return c

    def measure(c, lap_time, **telem):
        c._reanchor_pending = False; c._iters_since_reanchor = 0
        c.batch = [Recommendation("diff", {"diff_rear_accel": 70.0}, "t", "")]
        c.change_applied()
        c._on_lap(LapResult(c.lap_number + 1, 91.3, _telem_lap(91.3)))
        c._on_lap(LapResult(c.lap_number + 1, lap_time, _telem_lap(lap_time, **telem)))

    # --- #1: apparent gain with DIFFERENT inputs -> DISCOUNT, no A/B/A -------------
    c = ctrl()
    measure(c, 59.5, exit_g=1.35, brk=200)       # looks faster AND drove very differently
    check("#1 apparent gain explained by driver inputs -> discounted, A/B/A SKIPPED",
          c._aba is None and c._aba_saved == 1
          and c.state.current.get("diff_rear_accel") != 70.0)     # not kept
    # --- #1: apparent gain with SAME inputs -> still A/B/A (inconclusive) ----------
    c = ctrl()
    measure(c, 59.5, exit_g=1.35, brk=0)         # faster, same inputs -> can't tell -> A/B/A
    check("#1 apparent gain with similar inputs still triggers A/B/A (the tiebreaker)",
          c._aba is not None and c.phase == C.SHOW_CHANGE
          and c.batch[0].group == "confirm_revert" and c._aba_saved == 0)

    # --- #5: rejecting a change LOCKS it for the session; loop continues -----------
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
    c.best_segment = 55.0
    c.stats = aggregate(_window("understeer")); c.tyre_reading = None
    c._compute_batch()
    rejected = set().union(*[set(r.fields) for r in c.batch]) if c.batch else set()
    check("a change is proposed to reject", c.phase == C.SHOW_CHANGE and rejected)
    c.reject_change()
    check("#5 reject locks the proposed lever(s) for the session (not applied)",
          rejected and rejected <= c._rejected_fields)
    reappeared = False
    for _ in range(6):
        c.stats = aggregate(_window("understeer")); c.tyre_reading = None
        c._compute_batch()
        if c.batch and (set().union(*[set(r.fields) for r in c.batch]) & c._rejected_fields):
            reappeared = True
        if not c.batch or c.phase == C.DONE:
            break
    check("#5 a rejected lever is never proposed again this session", not reappeared)
    # rejected fields don't block convergence: analyze with everything rejected -> []
    s = aggregate(_window("understeer"))
    allf = set().union(*[set(r.fields) for r in
                         rules.analyze_batch(s, Tune(), "road", tyre_reading=None, max_search=5)])
    empty = rules.analyze_batch(s, Tune(), "road", tyre_reading=None, max_search=5,
                                rejected_fields=allf)
    check("#5 rejecting all firing levers converges (analyze_batch -> []), never blocks",
          empty == [])

    # --- #2: progress state reports best-vs-start, confirmed gains, trend ----------
    c = ctrl()
    p0 = c.progress_state()
    check("#2 progress reports best-vs-start + confirmed-gains + a plain trend",
          p0["delta_vs_start_s"] == 0.0 and p0["confirmed_gains"] == 0 and p0["trend"])
    c._record_outcome("gain"); c.best_segment = 59.1
    p1 = c.progress_state()
    check("#2 a confirmed gain is counted and best-vs-start updates",
          p1["confirmed_gains"] == 1 and p1["delta_vs_start_s"] < -0.05
          and p1["trend"] in ("Improving", "Fine-tuning"))
    for _ in range(4):
        c._record_outcome("revert")
    check("#2 a dry spell reports 'Not finding much - may finish soon'",
          "finish soon" in c.progress_state()["trend"])

    # --- #4: each proposed change carries a telemetry-tied 'why' -------------------
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
    c.best_segment = 55.0; c.stats = aggregate(_window("understeer")); c.tyre_reading = None
    c._compute_batch()
    ui = c.ui_state()
    check("#4 the action overlay carries a one-line WHY tied to the telemetry + a reject option",
          ui["klass"] == "action" and ui.get("why") and ui.get("can_reject")
          and any(ch.isdigit() for ch in " ".join(ui["why"])))   # references a measured number


def test_critical_fixes_v0113():
    print("\n== critical: crash-safe logs, save-on-exit, final=best-confirmed, on-car F8-only ==")
    import os, json, tempfile
    from lapsmith.gui import controller as C
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith.state.tune_state import CarLimits
    from lapsmith.state import store
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    store.set_sessions_dir(tempfile.mkdtemp(prefix="lapsmith_sessions_"))

    def ctrl():
        c = C.Controller(); c.identity = ident; c.persist = True
        c.apply_setup("road", CarLimits())     # opens the session log + writes a row
        return c

    # --- #1: the session log is written INCREMENTALLY (flushed each line) ----------
    c = ctrl()
    logp = store.session_log_path(c._meta()["car"], c.discipline)
    c.log("incremental-marker-XYZ")
    check("#1 per-session log exists and is flushed mid-session (crash-safe)",
          os.path.exists(logp) and "incremental-marker-XYZ" in open(logp, encoding="utf-8").read())
    sp = store.session_path(c._meta()["car"], c.discipline)
    check("#1 a session row is written as soon as the session starts (not only at the end)",
          os.path.exists(sp) and json.load(open(sp, encoding="utf-8"))["status"] == "in_progress")

    # --- #2 + #3: abnormal exit saves the BEST CONFIRMED tune, never baseline-drift -
    c = ctrl()
    c._baseline_lap_s = 60.0
    base_arb = c.state.current.get("arb_r")
    # a confirmed gain: arb_r changed, best 59.0, snapshot taken
    c.state.current.set("arb_r", base_arb - 5)
    c._best_tune = c.state.current.copy(); c._best_tune_lap = 59.0
    c.best_segment = 59.0; c._confirmed_gains = 1
    # ...then state.current DRIFTS back to the baseline (e.g. mid-A/B/A revert)
    c.state.current.set("arb_r", base_arb)
    check("the live tune drifted back to baseline (the bug-3 trap)",
          c.state.current.get("arb_r") == base_arb)
    c.save_on_exit()                            # simulates the window-X / force-close path
    data = json.load(open(store.session_path(c._meta()["car"], c.discipline), encoding="utf-8"))
    check("#3 saved final tune = BEST CONFIRMED (arb_r kept), NOT the reverted baseline",
          data["final_tune"]["arb_r"] == base_arb - 5)
    check("#3 saved best lap = the confirmed gain (59.0), not the baseline 60.0",
          data["best_lap_s"] == 59.0)
    check("#2 an abnormally-ended session is still written to history",
          data["status"] == "interrupted")
    check("#2 the session log is flushed + closed on exit", c._session_log is None)

    # nothing confirmed -> honestly saves the baseline
    c2 = ctrl(); c2._baseline_lap_s = 61.0
    c2.save_on_exit()
    d2 = json.load(open(store.session_path(c2._meta()["car"], c2.discipline), encoding="utf-8"))
    check("#3 with NO confirmed gain, the saved tune is the baseline (honest)",
          d2["final_tune"] == c2.baseline.as_dict())

    # --- #4: on-car state only changes on a real F8 apply (no phantom reverts) -----
    c = C.Controller(); c.identity = ident
    c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
    c.best_segment = 55.0; c.stats = aggregate(_window("understeer")); c.tyre_reading = None
    on_car_before = dict(c._on_car)
    c._compute_batch()                          # PROPOSE a change (not applied yet)
    check("a change is proposed", c.phase == C.SHOW_CHANGE and c.batch)
    check("#4 proposing a change does NOT touch the on-car state (no pre-confirm record)",
          c._on_car == on_car_before)
    proposed = set().union(*[set(r.fields) for r in c.batch])
    c.reject_change()                           # REJECT it
    check("#4 rejecting a change does NOT touch the on-car state either",
          c._on_car == on_car_before)
    # a rejected/never-applied lever never appears as a 'revert' in the checklist
    revert_fields = {it["field"] for it in c.menu_checklist()}
    check("#4 no phantom revert: a never-applied lever is never in the revert checklist",
          not (proposed & revert_fields) and not (proposed & set(c._on_car)))
    # and after a REAL F8 apply, the on-car state DOES update (atomic with the change)
    c.stats = aggregate(_window("understeer")); c.tyre_reading = None; c._compute_batch()
    if c.phase == C.SHOW_CHANGE and c.batch:
        applied = set().union(*[set(r.fields) for r in c.batch])
        c.change_applied()
        check("#4 the on-car state updates ONLY on the real F8 apply",
              applied <= set(c._on_car))


def test_logging_v0114():
    print("\n== logging: decision log (full trail, raw excluded), bundle, no-temp warning ==")
    import os, json, tempfile, zipfile, logging
    from lapsmith.gui import controller as C
    from lapsmith.gui.controller import _rawlog
    from lapsmith.state.tune_state import CarLimits
    from lapsmith.state import store
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    store.set_sessions_dir(tempfile.mkdtemp(prefix="lapsmith_log_"))
    _rawlog.propagate = False        # mirror production (setup_logging sets this)

    # --- #1/#2: the per-session DECISION log captures the full trail, raw excluded ---
    c = C.Controller(); c.identity = ident; c.persist = True
    c.apply_setup("road", CarLimits(), drivetrain="FWD")    # SETUP + drivetrain decision
    c.best_segment = 55.0
    c.stats = aggregate(_window("understeer")); c.tyre_reading = None
    c._compute_batch()                                      # RULES trace + batch emitted
    # emit some HIGH-FREQUENCY raw telemetry - it must NOT land in the decision log
    _rawlog.info("lap-fields RAW: IsRaceOn@0=1 LapNumber@312=3")
    _rawlog.info("tick DETECT: n=5 advancing=True")
    logp = store.session_log_path(c._meta()["car"], c.discipline)
    text = open(logp, encoding="utf-8").read()
    check("#1 decision log records SESSION START + drivetrain + setup",
          "SESSION START" in text and "DRIVETRAIN" in text)
    check("#1 decision log records the per-iteration eligible-vs-fired RULES trace",
          "RULES" in text and "batch emitted" in text)
    check("#1 decision log records the proposed change",
          "diff" in text or "arb" in text or "camber" in text)
    check("#2 raw per-packet telemetry is EXCLUDED from the decision log (signal not noise)",
          "lap-fields RAW" not in text and "tick DETECT" not in text)

    # --- #3: the support bundle includes the decision log, not packet spam ---------
    c.save_progress("in_progress")     # writes session.json etc. into the bundle dir
    zp = store.write_support_bundle(car=c._meta()["car"], discipline=c.discipline,
                                    env=c.env_info(), app_log=None)
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        decision = z.read("session_decision_log.txt").decode("utf-8") if \
            "session_decision_log.txt" in names else ""
    check("#3 support bundle contains the per-session decision log + environment.json",
          "session_decision_log.txt" in names and "environment.json" in names)
    check("#3 the bundled decision log is the useful trail, not raw packet spam",
          "RULES" in decision and "lap-fields RAW" not in decision and "tick DETECT" not in decision)

    # --- #4: no-temp-reader warning fires on a TARMAC run with temps absent --------
    c.discipline = "road circuit"; c.console_mode = False
    c.tyre_reading = None; c.last_reader = "none"; c.best_segment = 55.0
    check("#4 temp_blind TRUE on tarmac with no temp reading", c.temp_blind() is True)
    c._temp_warned = False
    c._warn_temp_blind_once()
    check("#4 the warning fires once and is recorded in the decision log",
          c._temp_warned and "HEAT SCREEN NOT READ" in open(logp, encoding="utf-8").read())
    st = c.status()
    check("#4 the no-temp notice is surfaced in status (-> persistent overlay banner)",
          st["temp_blind"] and "heat" in (st["temp_notice"] or "").lower())
    # a real reading clears it; dirt + console never warn
    c.tyre_reading = {"FL": {"inner": 90, "outer": 80}}; c.last_reader = "rapidocr"
    check("#4 a real 3-zone reading clears the blind state", c.temp_blind() is False)
    c.tyre_reading = None; c.last_reader = "none"; c.discipline = "dirt"
    check("#4 dirt never warns (camber is lap-time-tuned there by design)", c.temp_blind() is False)
    c.discipline = "road circuit"; c.console_mode = True
    check("#4 console mode never warns (it has its own single-temp notice)", c.temp_blind() is False)


def test_richer_channels_v0115():
    print("\n== richer telemetry: wheel rotation, per-wheel slip, suspension, vertical-g ==")
    from lapsmith.knowledge import fitness, rules
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith.telemetry.session import TestStats

    # --- the previously-unparsed WheelRotationSpeed + vertical accel now parse -------
    p = parse(simulator._build_packet(
        {"is_race_on": 1, "speed": 30.0, "wheel_rot_fl": 12.5, "wheel_rot_rr": 0.3,
         "accel_y": 9.80665}))
    check("WheelRotationSpeed parses per corner (@100-112)",
          abs(p.wheel_rot_fl - 12.5) < 1e-3 and abs(p.wheel_rot_rr - 0.3) < 1e-3)
    check("vertical acceleration (accel_y) parses", abs(p.accel_y - 9.80665) < 1e-3)

    def pk(**kw):
        base = {"is_race_on": 1, "speed": 30.0}
        base.update(kw)
        return parse(simulator._build_packet(base))

    # --- session names the worst-spinning DRIVEN wheel + the locking wheel ----------
    onthr = [pk(accel=255, drivetrain_type=1, tire_slip_ratio_rl=0.52,
                tire_slip_ratio_rr=0.10, tire_slip_ratio_fl=0.04, tire_slip_ratio_fr=0.04)
             for _ in range(30)]
    s = aggregate(onthr)
    check("session identifies the worst-spinning DRIVEN wheel (RWD -> RL)",
          s.power_spin_wheel == "RL" and s.chan_per_wheel_slip)
    brk = [pk(brake=200, tire_slip_ratio_fl=-0.45, tire_slip_ratio_fr=-0.05,
              wheel_rot_fl=0.2, wheel_rot_fr=8.0) for _ in range(30)]
    sb = aggregate(brk)
    check("session names the locking wheel (FL) and CONFIRMS via WheelRotationSpeed ~0",
          sb.brake_lock_wheel == "FL" and sb.brake_lock_confirmed and sb.chan_wheel_rotation)
    check("channels_available reports what was live",
          aggregate(onthr).channels_available()["per_wheel_slip"] is True)

    # --- the diff "why" cites the specific wheel; degrades gracefully without it -----
    st = TestStats(drivetrain="RWD", on_throttle_rear_slip=0.5, on_throttle_front_slip=0.1,
                   n_corner_frames=20, chan_per_wheel_slip=True,
                   power_spin_wheel="RL", power_spin_slip=0.52)
    rec = rules._rule_diff(st, Tune(), "road", None, True, CarLimits())
    check("diff 'why' names the spinning wheel (rear-left spinning ...)",
          rec is not None and "rear-left spinning" in rec.reason)
    st0 = TestStats(drivetrain="RWD", on_throttle_rear_slip=0.5, on_throttle_front_slip=0.1,
                    n_corner_frames=20)        # no per-wheel data
    rec0 = rules._rule_diff(st0, Tune(), "road", None, True, CarLimits())
    check("diff degrades gracefully without per-wheel data (still fires, generic why)",
          rec0 is not None and "diff_rear_accel" in rec0.fields and "spinning" not in rec0.reason)
    st2 = TestStats(drivetrain="RWD", braking_rear_slip=0.6, n_corner_frames=20,
                    chan_per_wheel_slip=True, brake_lock_wheel="RL", brake_lock_confirmed=True)
    rec2 = rules._rule_diff(st2, Tune(), "road", None, True, CarLimits())
    check("braking 'why' names the locking wheel + confirms lockup",
          rec2 is not None and "rear-left locking" in rec2.reason
          and "confirmed locked" in rec2.reason)

    # --- the composite uses the richer channels (and is inert when they're absent) ---
    noisy = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0, wheelspin=0.45, vy=0.6, roll=0.30))
    clean = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0, wheelspin=0.10, vy=0.1, roll=0.05))
    comp = fitness.composite(clean, noisy, "road")
    check("composite rewards less wheelspin + less body roll + smoother ride",
          comp.cleanspin > 0 and comp.bodyroll > 0 and comp.ride > 0 and comp.delta > 0)
    ref = fitness.bin_lap(_telem_lap(60.0, exit_g=1.0))          # richer channels flat (0)
    cand = fitness.bin_lap(_telem_lap(60.0, exit_g=1.35))
    cf = fitness.composite(cand, ref, "road")
    check("composite is unaffected by the richer channels when they're absent (graceful)",
          cf.cleanspin == 0.0 and cf.bodyroll == 0.0 and cf.ride == 0.0 and cf.exit > 0)

    # --- spring-balance 'why' cites front-vs-rear suspension when live ---------------
    sat = Tune(); sat.arb_r = rules.ARB_REAR_SOFT_FLOOR; sat.arb_f = rules.ARB_MIN
    us = TestStats(slip_angle_front=4.0, slip_angle_rear=2.0, n_corner_frames=30,
                   chan_suspension=True, pitch_bias=-0.2)        # front compresses more
    sbr = rules._rule_spring_balance(us, sat, "road", None, True, CarLimits())
    check("spring-balance 'why' cites front-vs-rear suspension travel when live",
          sbr is not None and "front compresses more than rear" in sbr.reason)


def test_recalibration_v0116():
    print("\n== recalibrate triggers (slip-angle units, richer channels), capture, gate-revert ==")
    from lapsmith.knowledge import rules
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith.telemetry.session import TestStats
    from lapsmith.vision import capture
    from lapsmith.gui import controller as C
    from lapsmith.knowledge.rules import Recommendation
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # --- #2: a screenshot backend is ALWAYS available (Pillow ImageGrab is bundled) -
    check("#2 a screenshot backend is available (Pillow ImageGrab, frozen-proof)",
          capture.backend_available())
    check("#2 backend_name reports the active backend (not 'none')",
          capture.backend_name() != "none")

    # --- #1 ARB: fires on NORMALIZED-scale slip-angle imbalance (the root cause) -----
    us = TestStats(slip_angle_front=0.72, slip_angle_rear=0.45, n_corner_frames=20)
    check("#1 ARB fires on a normalized slip-angle imbalance (0.27) - was unreachable at 1.5",
          (rules._rule_arb(us, Tune(), "road", None, True, CarLimits()) or 0) and
          rules._rule_arb(us, Tune(), "road", None, True, CarLimits()).group == "arb")
    neu = TestStats(slip_angle_front=0.50, slip_angle_rear=0.45, n_corner_frames=20)
    check("#1 ARB stays silent when near-balanced (delta 0.05 < 0.18; no over-firing)",
          rules._rule_arb(neu, Tune(), "road", None, True, CarLimits()) is None)
    roll = TestStats(slip_angle_front=0.50, slip_angle_rear=0.46, n_corner_frames=20,
                     chan_suspension=True, roll_asym_front=0.22, roll_asym_rear=0.05)
    rr = rules._rule_arb(roll, Tune(), "road", None, True, CarLimits())
    check("#1 ARB fires from BODY ROLL when slip-angle is inconclusive (richer channel)",
          rr is not None and "rolls more" in rr.reason)

    # --- #1 pressure: fires on a smaller L/R temp delta now (TEMP_BAL_C 6 -> 4) ------
    pt = TestStats(temp_fl=95.0, temp_fr=90.0, temp_rl=85.0, temp_rr=85.0, n_corner_frames=20)
    pr = rules._rule_pressure(pt, Tune(), "road", None, True, CarLimits())
    check("#1 pressure fires on a 5C L/R delta (was gated at 6C)",
          pr is not None and pr.group == "pressure")
    pt2 = TestStats(temp_fl=93.0, temp_fr=90.0, temp_rl=90.0, temp_rr=90.0, n_corner_frames=20)
    check("#1 pressure stays silent below the delta (3C < 4C)",
          rules._rule_pressure(pt2, Tune(), "road", None, True, CarLimits()) is None)

    # --- #1 damping: fires on a harsh/busy ride (vertical-g); degrades when absent ---
    dh = TestStats(vert_g_rms=0.6, chan_vertical_accel=True, susp_min_front=0.3,
                   susp_min_rear=0.5, susp_max_front=0.8, susp_max_rear=0.8, n_corner_frames=20)
    dr = rules._rule_damping(dh, Tune(), "road", None, True, CarLimits())
    check("#1 damping fires on a harsh/busy ride (vertical-g RMS, richer channel)",
          dr is not None and dr.group == "damping_bump" and "vertical-g" in dr.reason.lower())
    dn = TestStats(vert_g_rms=0.6, chan_vertical_accel=False, susp_min_front=0.3,
                   susp_min_rear=0.5, n_corner_frames=20)
    check("#1 damping degrades gracefully without the vertical channel (no fire on garbage)",
          rules._rule_damping(dn, Tune(), "road", None, True, CarLimits()) is None)

    # --- #1 aero: relaxed grip-limited threshold (needs the aero range entered) ------
    lim = CarLimits(aero_rear_min=50.0, aero_rear_max=300.0)
    t = Tune(); t.aero_rear = 100.0
    ga = TestStats(max_lateral_g=1.05, n_corner_frames=20)
    ar = rules._rule_aero(ga, t, "road", None, True, lim)
    check("#1 aero engages on a grip-limited road car (peak 1.05 g < 1.10)",
          ar is not None and ar.group == "aero")

    # --- #3: a gate-driven REVERT syncs on-car so no phantom no-op next checklist ----
    c = C.Controller(); c.identity = ident; c.apply_setup("road", CarLimits()); c.mode = C.MODE_AUTO
    c.best_segment = 50.0
    c.state.current.set("ride_height_f", 10.7); c._on_car["ride_height_f"] = 10.7
    rec = c.state.apply_change("ride_height", {"ride_height_f": 12.2}, "t", "")  # user F8 applied 12.2
    c._applied_records = [rec]; c._on_car["ride_height_f"] = 12.2
    c._revert_batch()                                                            # the gate reverts it
    check("#3 a gate-revert syncs on-car to the reverted value (10.7, not the stale 12.2)",
          abs(c._on_car["ride_height_f"] - 10.7) < 1e-6
          and abs(c.state.current.get("ride_height_f") - 10.7) < 1e-6)
    c._applied_records = []; c.batch = [Recommendation("arb", {"arb_r": 55.0}, "t", "")]
    cl = c.menu_checklist()
    check("#3 the next checklist has NO already-satisfied (from==to) ride entry (no phantom)",
          not any(it["field"] == "ride_height_f" for it in cl)
          and all(it["from"] != it["to"] for it in cl))

    # --- #4: warm-up is now 1 lap ---------------------------------------------------
    check("#4 WARMUP_LAPS is 1 (one cold lap from the start, then measuring)",
          rules.WARMUP_LAPS == 1)


def test_v0123_ocr_box_coord_mapping():
    print("\n== v0.1.23: OCR maps by BOX COORDINATES (order-independent), partial reads, repair ==")
    from lapsmith.vision import read_tyres as RT
    from lapsmith.knowledge import rules
    W, H = 1920, 1080
    CEL = "℃"
    # FH6 layout: FL top-left, FR top-right, RL bottom-left, RR bottom-right; each tyre's
    # zones inner/mid/outer stacked top->bottom. Rear outer much cooler => camber signal.
    grid = {"FL": (0.40 * W, 0.30 * H, [99.5, 97.8, 96.1]),
            "FR": (0.60 * W, 0.30 * H, [99.0, 97.5, 95.8]),
            "RL": (0.40 * W, 0.55 * H, [95.0, 93.0, 82.0]),
            "RR": (0.60 * W, 0.55 * H, [94.5, 92.5, 81.5])}
    udp = {"FL": 97.8, "FR": 97.4, "RL": 90.0, "RR": 89.5}

    def temps():
        out = []
        for _name, (x, y0, zs) in grid.items():
            for i, v in enumerate(zs):
                out.append((f"{v}{CEL}", (x, y0 + i * 0.05 * H)))
        return out
    labels = [("Front Left", (0.40 * W, 0.25 * H)), ("Front Right", (0.60 * W, 0.25 * H)),
              ("Rear Left", (0.40 * W, 0.50 * H)), ("Rear Right", (0.60 * W, 0.50 * H))]

    def maps_ok(toks, label):
        out = RT.tokens_to_reading(toks, udp_temps=udp)
        ok = (out is not None and "FL" in out and abs(out["FL"]["inner"] - 99.5) < 0.2
              and abs(out["FL"]["outer"] - 96.1) < 0.2 and "RR" in out
              and abs(out["RR"]["outer"] - 81.5) < 0.2)
        check(f"{label}: maps every corner by coordinate", ok)
        return out

    # 1) clean order
    maps_ok(temps() + labels, "clean order")
    # 2) labels OUT OF ORDER (Front Right before Front Left) + reversed temp order:
    #    coordinate mapping ignores emission order, so it still maps correctly
    reordered = list(reversed(temps()))
    lbl_oo = [labels[1], labels[0], labels[3], labels[2]]
    maps_ok(reordered + lbl_oo, "labels out of order + reversed temps")
    # 3) scenery / HUD tokens interleaved between temps - must be IGNORED, not pollute
    scenery = [("#FasterTg", (0.5 * W, 0.10 * H)), ("355", (0.7 * W, 0.80 * H)),
               ("Checkpoint", (0.2 * W, 0.90 * H)), ("337 M", (0.8 * W, 0.85 * H)),
               ("Chect", (0.3 * W, 0.05 * H)), ("Heat", (0.5 * W, 0.20 * H))]
    woven = []
    for i, t in enumerate(temps()):
        woven.append(t)
        if i < len(scenery):
            woven.append(scenery[i])
    maps_ok(woven + labels, "scenery interleaved")
    # 4) 11 of 12 (drop FR middle zone) still maps and is USED
    fr_mid_y = 0.30 * H + 0.05 * H
    toks11 = [t for t in temps()
              if not (abs(t[1][0] - 0.60 * W) < 1 and abs(t[1][1] - fr_mid_y) < 1)]
    out11 = RT.tokens_to_reading(toks11 + labels, udp_temps=udp)
    check("11/12 (a zone dropped) still maps and is usable",
          out11 is not None and "FR" in out11 and "inner" in out11["FR"] and "outer" in out11["FR"])

    # 5) the coordinate-mapped 3-zone read actually drives camber (rear outer 13C cooler)
    out = RT.tokens_to_reading(temps() + labels, udp_temps=udp)
    rec = rules._rule_camber(aggregate(_window("understeer")),
                             build_baseline("C", "S1 800", "road", 50.0, "RWD"),
                             "road", out, True, rules.CarLimits())
    check("camber fires from the box-coordinate 3-zone read", rec is not None and rec.group == "camber")

    # 6) controller marks the reader 'ocr_3zone' on a real OCR read (drives camber/toe)
    from lapsmith.gui import controller as C
    import lapsmith.vision.read_tyres as RTmod
    c = C.Controller(); c.discipline = "road"
    orig = (RTmod.rapidocr_available, RTmod.vision_available, RTmod.ocr_heat_page)
    RTmod.rapidocr_available = lambda: True
    RTmod.vision_available = lambda: False
    RTmod.rapidocr_read_image = lambda p, udp_temps=None: {"FL": {"inner": 99.5, "outer": 96.1},
                                                           "FR": {"inner": 99.0, "outer": 95.8}}
    try:
        c._read_heat("frame.png", peak_g=1.0, udp_temps=udp)
        check("OCR success sets temp_reader_used='ocr_3zone' (not blind)",
              c.last_reader == "ocr_3zone" and c.tyre_reading is not None)
    finally:
        RTmod.rapidocr_available, RTmod.vision_available, RTmod.ocr_heat_page = orig


def test_v0123_evidence_keep():
    print("\n== v0.1.23 #2: evidence-backed fault fix KEPT on telemetry; weak change still vetoed ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import fitness as F
    from lapsmith.state.tune_state import CarLimits
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    class _Cand:
        live = True

    def setup(group, fields, prevs, bottoming=False):
        c = C.Controller(); c.identity = ident; c.apply_setup("road", CarLimits())
        c.mode = C.MODE_AUTO; c.rigour = "confirmed"; c.best_segment = 47.06
        c._ref_telem = _Cand()
        c.stats = aggregate(_window("understeer"))   # so _next_step()'s analyze has stats
        if bottoming:
            c._cur_bottoming_axles = {"front"}
        for fld, p in prevs.items():
            c.state.current.set(fld, p)
        c._applied_records = [c.state.apply_change(group, fields, "evidence", "feel")]
        return c

    oc, oi = F.composite, F.input_difference
    try:
        # (a) evidence-backed BOTTOMING fix: composite up, RIDE channel up (bottoming
        #     reduced), but the driver also drove very differently -> normally discounted.
        #     With #2 it is KEPT on the telemetry (a warmer driver can't un-bottom the car).
        F.composite = lambda cand, ref, disc, group="": F.CompositeResult(
            delta=0.05, ride=0.05, traction=0.0, targeted=0.0, live=True)
        F.input_difference = lambda cand, ref: 0.5            # would trigger driver-discount
        ca = setup("ride_height", {"ride_height_f": 12.2}, {"ride_height_f": 10.7}, bottoming=True)
        ca._gate_change(46.55, 0.0, _Cand())
        check("evidence bottoming fix KEPT on telemetry despite a big input change (no veto)",
              ca.state.current.get("ride_height_f") == 12.2
              and ca._aba is None and ca._aba_saved == 0)

        # (b) WEAK change (ARB, no diagnosed physical fault): composite up but the driver
        #     drove differently -> still gets the driver-discount veto (reserved for these).
        F.composite = lambda cand, ref, disc, group="": F.CompositeResult(
            delta=0.05, ride=0.0, traction=0.0, targeted=0.05, live=True)
        F.input_difference = lambda cand, ref: 0.5
        cb = setup("arb", {"arb_r": 55.0}, {"arb_r": 60.0})
        cb._gate_change(46.55, 0.0, _Cand())
        check("weak/no-fault change with a driver-input change still gets the discount veto",
              cb.state.current.get("arb_r") == 60.0 and cb._aba_saved == 1)

        # (c) lap time stays a GUARDRAIL: an evidence fix whose lap is clearly WORSE is
        #     still reverted (we don't keep something that clearly hurt the lap).
        F.composite = lambda cand, ref, disc, group="": F.CompositeResult(
            delta=0.05, ride=0.05, traction=0.0, targeted=0.0, live=True)
        F.input_difference = lambda cand, ref: 0.0
        cc = setup("ride_height", {"ride_height_f": 12.2}, {"ride_height_f": 10.7}, bottoming=True)
        cc._gate_change(47.06 + 0.5, 0.0, _Cand())           # +0.5s clearly worse
        check("lap time stays a guardrail: a clearly-worse lap reverts even an evidence fix",
              cc.state.current.get("ride_height_f") == 10.7)
    finally:
        F.composite, F.input_difference = oc, oi


def test_v0124_drive_only_no_f8():
    print("\n== v0.1.24: drive-only steps auto-advance (no F8); change steps still need F8 ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import rules
    from lapsmith.state.tune_state import CarLimits
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    class _Live:
        live = True

    def fresh():
        c = C.Controller(); c.identity = ident; c.apply_setup("road", CarLimits())
        c.mode = C.MODE_AUTO; c.rigour = "confirmed"; c.best_segment = 47.06
        c.stats = aggregate(_window("understeer"))
        c._on_car = c.state.current.as_dict()
        return c

    # RE-ANCHOR: nothing to change -> drive-only (no F8), overlay is DRIVE class
    c = fresh(); c._begin_reanchor()
    check("re-anchor is a drive-only step (auto-advances, no F8)",
          c.phase == C.SHOW_CHANGE and c.is_drive_only_step())
    ui = c.ui_state()
    check("re-anchor overlay is DRIVE class with no 'F8' in the instruction",
          ui["klass"] == "drive" and "F8" not in ui["sub"])
    c.change_applied()                      # what the pump's auto-advance does
    check("re-anchor auto-advance arms the measured lap (DRIVE_AUTO, no F8 needed)",
          c.phase == C.DRIVE_AUTO)

    # a REAL change step: fields differ -> NOT drive-only; amber, F8 required
    c2 = fresh()
    c2.batch = [rules.Recommendation("arb", {"arb_r": 55.0}, "balance", "")]
    c2.phase = C.SHOW_CHANGE
    check("a real change step is NOT drive-only (still needs F8)", not c2.is_drive_only_step())
    ui2 = c2.ui_state()
    check("a real change overlay is the amber ACTION (CHANGE THESE NOW) with F8",
          ui2["klass"] == "action" and ui2["checklist"])

    # A/B/A confirm_revert that SETS values BACK (A != B) -> NOT drive-only (F8 required)
    c3 = fresh()
    c3.state.current.set("ride_height_f", 10.0); c3._on_car = c3.state.current.as_dict()
    c3.batch = [rules.Recommendation("ride_height", {"ride_height_f": 13.0}, "bottoming", "")]
    c3.change_applied()                     # apply B (on-car -> 13.0)
    c3._start_aba(46.55, _Live(), type("X", (), {"delta": 0.05})())
    check("A/B/A revert with values to set back is NOT drive-only (needs F8)",
          c3.batch[0].group == "confirm_revert" and not c3.is_drive_only_step()
          and c3.ui_state()["klass"] == "action")

    # A/B/A re-drive where NO value changes (A == B) -> drive-only (auto-advance, no F8)
    c4 = fresh()
    c4.state.current.set("ride_height_f", 11.0)
    applied = c4.state.apply_change("ride_height", {"ride_height_f": 11.0}, "noop", "")
    c4._applied_records = [applied]; c4._on_car = c4.state.current.as_dict()
    c4._start_aba(46.55, _Live(), type("X", (), {"delta": 0.05})())
    check("A/B/A re-drive with no value to set is drive-only (auto-advances, no F8)",
          c4.batch[0].group == "confirm_revert" and c4.is_drive_only_step()
          and c4.ui_state()["klass"] == "drive")


def test_v0123_oncar_bundled_revert():
    print("\n== v0.1.23 #3: _on_car stays truthful through a BUNDLED confirm_revert + re-anchor ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import rules, fitness as F
    from lapsmith.state.tune_state import CarLimits
    from lapsmith import identity
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    class _C:
        live = True

    c = C.Controller(); c.identity = ident; c.apply_setup("road", CarLimits())
    c.mode = C.MODE_AUTO; c.rigour = "confirmed"; c.best_segment = 47.06
    c.stats = aggregate(_window("understeer"))
    c._on_car = c.state.current.as_dict()
    base = c.state.current.as_dict()

    # apply a BUNDLED 3-field batch (ride height + camber + diff), like iteration 0
    c.batch = [rules.Recommendation("ride_height", {"ride_height_f": 12.2}, "bottoming 42%", ""),
               rules.Recommendation("camber", {"camber_r": -1.0}, "rear outer hot", ""),
               rules.Recommendation("diff", {"diff_rear_decel": 10.0}, "rear lockup", "")]
    c.change_applied()
    check("on-car == current tune after the bundled apply (all 3 fields)",
          c._on_car.get("ride_height_f") == 12.2 and c._on_car.get("camber_r") == -1.0
          and c._on_car.get("diff_rear_decel") == 10.0)

    # apparent win -> A/B/A: revert the WHOLE bundle to the previous (baseline) values
    c._start_aba(46.55, _C(), type("X", (), {"delta": 0.05})())
    check("A/B/A shows a BUNDLED confirm_revert of all 3 fields",
          c.batch[0].group == "confirm_revert" and len(c.batch[0].fields) == 3)
    c.change_applied()                                  # user reverts to baseline, drives A'
    check("after the bundled revert, on-car == state.current (back to baseline)",
          c._on_car.get("ride_height_f") == base["ride_height_f"]
          and c._on_car.get("camber_r") == base["camber_r"]
          and c._on_car.get("diff_rear_decel") == base["diff_rear_decel"]
          and c._on_car == c.state.current.as_dict())

    # A' is driver-drift -> DISCARD (keep baseline); on-car must still match reality
    oc = F.composite
    F.composite = lambda b, a, disc, group="": F.CompositeResult(delta=0.0, live=True)
    try:
        c._resolve_aba(46.15, _C())
    finally:
        F.composite = oc
    check("after the A/B/A discard, on-car == state.current (no drift)",
          c._on_car == c.state.current.as_dict())

    # RE-ANCHOR while genuinely on the current tune -> 'just drive, no change'
    c._begin_reanchor()
    st = c.ui_state()
    check("re-anchor on the matching tune says 'drive, no change' (empty checklist)",
          st["klass"] == "drive" and not st["checklist"])
    # but if on-car DIVERGES, the re-anchor states EXACTLY what to set (never a false 'just drive')
    c._on_car = dict(c._on_car); c._on_car["arb_f"] = 99.0
    st2 = c.ui_state()
    check("re-anchor with a diverged on-car shows the exact checklist, not 'just drive'",
          st2["klass"] == "action" and any(x["field"] == "arb_f" for x in st2["checklist"]))


def test_v0122_ocr_celsius_parser():
    print("\n== v0.1.22: OCR Celsius parser - FH6's degree-Celsius glyph U+2103, 3-zone read ==")
    from lapsmith.vision import read_tyres as RT
    import re as _re

    # the EXACT token strings RapidOCR returns on the user's real Heat captures
    # (built from code points so this file stays ASCII): U+2103 = ℃, U+00B0 = °
    DEG, CEL = "°", "℃"
    cases = [(f"99.0 {DEG}{CEL}", 99.0), (f"97.1{CEL}", 97.1), (f"970{CEL}", 97.0),
             (f"101.9{DEG}{CEL}", 101.9), (f"85.5{CEL}", 85.5), ("99.0°C", 99.0)]
    for s, want in cases:
        toks = RT._temp_tokens([(s, (0, 0))])
        check(f"parse {ascii(s)} -> {want}", len(toks) == 1 and abs(toks[0][0] - want) < 0.05)

    # REGRESSION GUARD: the old ASCII "°C" matcher caught ZERO of these (the whole bug)
    old = _re.compile(r"^[+-]?\d{1,3}\.\d\s*[°\xba]?\s*[CFcf]?$")
    celsius = [c for c in cases if CEL in c[0]]      # the real U+2103 tokens (not ASCII °C)
    check("OLD '[degree]C'-only regex matched 0 of the real Celsius-glyph tokens (the bug)",
          sum(1 for s, _ in celsius if old.match(str(s).strip())) == 0)
    check("NEW parser matches every real Celsius-glyph token",
          all(RT._looks_like_temp(s) for s, _ in cases))

    # junk rejected (speed / lap / position / gamertag / garble)
    for junk in ["015", "1/12", "1:23.4", "0.6'66", "P1", "568"]:
        check(f"junk {junk!r} is NOT read as a temp", not RT._looks_like_temp(junk))

    # end-to-end: a full 12-token ℃ Heat grid -> a 3-zone read with inner-vs-outer spread
    def grid(drop=()):
        layout = [("FL", 100, [(10, 99.0), (20, 97.1), (30, 85.5)]),
                  ("RL", 100, [(110, 95.0), (120, 93.0), (130, 82.0)]),
                  ("FR", 300, [(10, 98.0), (20, 96.0), (30, 84.0)]),
                  ("RR", 300, [(110, 94.0), (120, 92.0), (130, 81.0)])]
        toks = []
        for name, x, zones in layout:
            for zi, (y, v) in enumerate(zones):
                if (name, zi) not in drop:
                    toks.append((f"{v}{CEL}", (x, y)))   # render with the Celsius glyph
        return toks
    out = RT.tokens_to_reading(grid(), udp_temps=None)
    check("full 12-token Celsius grid -> a real 3-zone reading (was None before the fix)",
          out is not None)
    if out:
        check("FL inner/outer recovered (99.0 / 85.5)",
              abs(out["FL"]["inner"] - 99.0) < 0.1 and abs(out["FL"]["outer"] - 85.5) < 0.1)
        check("camber gets a real inner-vs-outer spread on the front axle",
              (out["FL"]["inner"] - out["FL"]["outer"]) > 5.0)
    # 10 of 12 (both front MIDDLE zones dropped) still reads via gap-split + mid-pad
    out2 = RT.tokens_to_reading(grid(drop={("FL", 1), ("FR", 1)}), udp_temps=None)
    check("10/12 (front middles dropped) still yields a usable inner/outer read",
          out2 is not None and abs(out2["FL"]["inner"] - 99.0) < 0.1
          and abs(out2["FL"]["outer"] - 85.5) < 0.1)


def test_v0120_detection_pause_resilience():
    print("\n== v0.1.20: car detection survives a focus-loss PAUSE; pause vs car-change by ordinal ==")
    from lapsmith.gui import controller as C

    class _Lis:
        def __init__(self): self.pkt = None; self.last_packet_time = 0.0; self.packet_count = 0
        def snapshot(self): return self.pkt
        def drain_since(self, m): return []
        def feed(self, pkt, fresh=True):
            self.pkt = pkt; self.packet_count += 1
            self.last_packet_time = time.time() if fresh else time.time() - 60

    c = C.Controller(); c.phase = C.WAIT_TELEMETRY
    lis = _Lis(); c.listener = lis
    check("before any packet -> no_telemetry", c.detection_state()["state"] == "no_telemetry")

    # detect car A on a LIVE frame
    lis.feed(_car_packet(1, 6, ordinal=568), fresh=True)
    c.track_identity()
    check("car detected from a live frame (ordinal 568)",
          c.identity is not None and c.identity.ordinal == 568)
    check("detection advances WAIT_TELEMETRY -> CONFIRM_CAR", c.phase == C.CONFIRM_CAR)
    ds = c.detection_state()
    check("while live -> car_detected (live)", ds["state"] == "car_detected" and ds["live"])

    # PAUSE (game lost focus): stream goes stale, identity MUST persist
    lis.last_packet_time = time.time() - 60
    c.track_identity()                              # ticking during the pause
    check("identity persists across the pause (not cleared)",
          c.identity is not None and c.identity.ordinal == 568)
    ds = c.detection_state()
    check("paused -> still car_detected but flagged not-live, reassuring message",
          ds["state"] == "car_detected" and ds["live"] is False
          and "paused" in ds["message"].lower())

    # RESUME with the SAME ordinal -> same car, carry on
    lis.feed(_car_packet(1, 6, ordinal=568), fresh=True)
    c.track_identity()
    check("resume with SAME ordinal keeps the same car", c.identity.ordinal == 568)

    # a STALE frame with a DIFFERENT ordinal must NOT trigger a car change
    lis.pkt = _car_packet(2, 8, ordinal=999); lis.last_packet_time = time.time() - 60
    c.track_identity()
    check("a STALE different-ordinal frame does NOT switch cars (acts only on live)",
          c.identity.ordinal == 568)

    # RESUME with a DIFFERENT ordinal on a LIVE frame -> real car change, re-detect
    lis.feed(_car_packet(2, 8, ordinal=999), fresh=True)
    c.track_identity()
    check("resume with a DIFFERENT ordinal (live) re-detects the new car",
          c.identity.ordinal == 999 and c.identity.drivetrain == "AWD")

    # IDENTITY vs MEASUREMENT staleness: a frozen frame is not 'live' for a measurement
    lis.last_packet_time = time.time() - 60
    check("a frozen frame is NOT telemetry_live (no measurement off stale data)",
          c.telemetry_live() is False and c.identity is not None)
    lis.last_packet_time = time.time()
    check("a fresh frame IS telemetry_live", c.telemetry_live() is True)

    # FIRST detection works even if the only frame ever seen is now stale (seen then paused)
    c2 = C.Controller(); c2.phase = C.WAIT_TELEMETRY
    lis2 = _Lis(); lis2.pkt = _car_packet(1, 6, ordinal=568); lis2.packet_count = 5
    lis2.last_packet_time = time.time() - 60       # the car WAS seen, then the game paused
    c2.listener = lis2
    c2.track_identity()
    check("first-detect from a last-seen (now stale) frame still works - no need to keep "
          "the game focused", c2.identity is not None and c2.identity.ordinal == 568)

    # MID-SESSION car change -> prompt to set up again (don't tune the new car stale)
    from lapsmith.state.tune_state import CarLimits
    from lapsmith import identity as _idmod
    c4 = C.Controller()
    c4.identity = _idmod.identify(_car_packet(1, 6, ordinal=568))
    c4.apply_setup("road", CarLimits())            # baseline built -> session active
    c4.phase = C.DRIVE_AUTO
    lis4 = _Lis(); c4.listener = lis4
    lis4.feed(_car_packet(2, 8, ordinal=999), fresh=True)   # a DIFFERENT car, live
    c4.track_identity()
    pend = c4.pending_car_change()
    check("mid-session car change flags a pending re-setup prompt",
          pend is not None and pend["ordinal"] == 999 and pend["old"])
    check("identity updates to the new car (not left stale)", c4.identity.ordinal == 999)
    check("status surfaces the pending car change for the GUI prompt",
          (c4.status().get("car_change_pending") or {}).get("ordinal") == 999)
    c4.clear_car_change()
    check("clear_car_change consumes it (prompt fires once)", c4.pending_car_change() is None)

    # a car change BEFORE setup (pre-session) is a plain re-detect, NOT a prompt
    c5 = C.Controller(); c5.phase = C.WAIT_TELEMETRY
    lis5 = _Lis(); c5.listener = lis5
    lis5.feed(_car_packet(1, 6, ordinal=568), fresh=True); c5.track_identity()
    lis5.feed(_car_packet(2, 8, ordinal=999), fresh=True); c5.track_identity()
    check("pre-setup car change re-detects without a prompt",
          c5.identity.ordinal == 999 and c5.pending_car_change() is None)


def test_v0119_shutdown_units_compound_detection():
    print("\n== v0.1.19: clean-exit guard, psi/bar, compound passthrough, robust detection ==")
    import inspect
    from lapsmith.gui import controller as C, main_window
    from lapsmith.knowledge import baseline as B
    from lapsmith.knowledge.baseline import build_baseline, format_checklist, fmt_pressure, fmt_field
    from lapsmith.state.tune_state import Tune, CarLimits
    from lapsmith import identity

    # ---- SHUTDOWN regression guard: exactly ONE closeEvent, and it QUITS -----------
    src = inspect.getsource(main_window.build_main_window)
    check("MainWindow defines closeEvent exactly once (no hide-to-tray re-definition)",
          src.count("def closeEvent") == 1)
    check("the surviving closeEvent runs the quit hook (X exits, not hide-to-tray)",
          'hooks.get("quit")' in src or "hooks.get('quit')" in src)
    appsrc = inspect.getsource(__import__("lapsmith.gui.app", fromlist=["main"]).main)
    check("startup force-terminates on exit so native OCR/cv2/keyboard threads can't "
          "hold the port (os._exit)", "os._exit(" in appsrc)

    # ---- A) psi <-> bar conversion (1 bar = 14.5038 psi) ---------------------------
    check("psi display is unchanged", fmt_pressure(29.0, "psi") == "29.0 psi")
    check("29.0 psi shows ~2.00 bar (2 decimals, finer than the game's 1)",
          fmt_pressure(29.0, "bar") == "2.00 bar")
    check("bar->psi round-trips within rounding",
          abs(B.pressure_to_psi(2.0, "bar") - 29.0076) < 0.01)
    check("psi unit is a no-op for pressure_to_psi", B.pressure_to_psi(29.0, "psi") == 29.0)
    check("fmt_field converts the pressure field to the chosen unit",
          fmt_field("pressure_f", 29.0, "bar") == "2.00 bar" and
          fmt_field("pressure_f", 29.0, "psi") == "29.0 psi")
    check("non-pressure fields are unaffected by the unit arg",
          fmt_field("camber_f", -1.5, "bar") == "-1.5 deg")

    # ---- C) compound is user-set; NEVER silently 'Slick' when the user picked Rally -
    check("Tune default compound is 'Unspecified', not 'Slick'", Tune().tyre_compound == "Unspecified")
    t_rally = build_baseline("Car", "S1 800", "road", 50.0, "RWD", compound="Rally")
    check("user-chosen Rally compound passes through verbatim on a ROAD build",
          t_rally.tyre_compound == "Rally")
    sheet = format_checklist(t_rally, "Car", "S1 800", "road", 50.0, "RWD")
    check("final sheet shows the user's Rally compound, NOT a hardcoded Slick",
          "Rally" in sheet and "Slick" not in sheet)
    t_unset = build_baseline("Car", "S1 800", "road", 50.0, "RWD", compound="Unspecified")
    check("Unspecified stays Unspecified (never asserted as Slick)",
          t_unset.tyre_compound == "Unspecified")
    # the sheet honours the pressure unit too
    bar_sheet = format_checklist(t_rally, "Car", "S1 800", "road", 50.0, "RWD", pressure_unit="bar")
    check("final sheet pressures render in the selected unit (bar)",
          "bar" in bar_sheet and "psi" not in bar_sheet.split("ALIGNMENT")[0])
    # apply_setup path: compound + unit reach the controller and its checklist
    c = C.Controller(); c.identity = identity.identify(
        parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))
    c.apply_setup("road", CarLimits(), compound="Rally", pressure_unit="bar")
    check("apply_setup stores the user's compound + unit on the controller",
          c.tyre_compound == "Rally" and c.pressure_unit == "bar")
    check("controller baseline checklist shows Rally + bar (no Slick, no psi pressure)",
          "Rally" in c.baseline_checklist() and "Slick" not in c.baseline_checklist())

    # ---- D) detection state: no-telemetry vs telemetry-no-car vs car-detected ------
    c2 = C.Controller()
    check("no listener / 0 packets -> 'no_telemetry' state",
          c2.detection_state()["state"] == "no_telemetry")

    class _Lis:
        packet_count = 7
        def drain_since(self, m): return []
        def snapshot(self): return None
    c2.listener = _Lis()
    check("packets arriving but no car -> 'telemetry_no_car'",
          c2.detection_state()["state"] == "telemetry_no_car"
          and "no car seen yet" in c2.detection_state()["message"].lower())

    # poll_identity detects a car at a STANDSTILL (race off, speed 0, ordinal>0)
    spkt = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))
    spkt.speed = 0.0
    spkt.is_race_on = False
    if spkt.car_ordinal <= 0:
        spkt.car_ordinal = 2468

    class _LisStat:
        packet_count = 9
        def drain_since(self, m): return []      # nothing 'moving'
        def snapshot(self): return spkt
    c3 = C.Controller(); c3.listener = _LisStat(); c3.phase = C.WAIT_TELEMETRY
    ident = c3.poll_identity()
    check("car detected from a stationary frame (no movement / race-off needed)",
          ident is not None and c3.identity is not None)
    check("detection advances WAIT_TELEMETRY -> CONFIRM_CAR", c3.phase == C.CONFIRM_CAR)
    check("once detected, state is 'car_detected'",
          c3.detection_state()["state"] == "car_detected")


def test_telemetry_display_units_v0125():
    print("\n== telemetry display units: persisted English/Metric speed readouts ==")
    import os
    import tempfile
    from lapsmith import simulator
    from lapsmith.gui import controller as C, overlay, web
    from lapsmith.state import prefs
    from lapsmith.units import format_speed, speed_value_unit, telemetry_unit_system

    # Pure helpers: canonical m/s remains the input; only display values change.
    check("telemetry unit sanitizer defaults invalid values to english",
          telemetry_unit_system("bogus") == "english")
    mph, mph_unit = speed_value_unit(10.0, "english")
    kmh, kmh_unit = speed_value_unit(10.0, "metric")
    check("10 m/s displays as 22.4 mph in English units",
          abs(mph - 22.36936) < 0.001 and mph_unit == "mph")
    check("10 m/s displays as 36.0 km/h in Metric units",
          abs(kmh - 36.0) < 0.001 and kmh_unit == "km/h")
    check("format_speed uses consistent labels",
          format_speed(10.0, "english") == "22.4 mph"
          and format_speed(10.0, "metric") == "36.0 km/h")

    # Prefs: persisted choice is one source of truth and invalid data is safe.
    pref_path = os.path.join(tempfile.mkdtemp(), "prefs.json")
    prefs.set_store_path(pref_path)
    check("telemetry units default to english", prefs.telemetry_unit_system() == "english")
    prefs.set("telemetry_unit_system", "metric")
    check("telemetry units persist as metric", prefs.telemetry_unit_system() == "metric")
    prefs.set("telemetry_unit_system", "nonsense")
    check("invalid persisted telemetry unit falls back to english",
          prefs.telemetry_unit_system() == "english")

    pkt = parse(simulator._build_packet(simulator.frame(0.5, "understeer")))
    pkt.speed = 10.0

    class _Lis:
        last_packet_time = time.time()
        def snapshot(self): return pkt

    c = C.Controller(); c.listener = _Lis()
    c.telemetry_unit_system = "english"
    live_en = c.status()["live"]
    check("status() exposes display speed fields in English units",
          live_en["speed_unit"] == "mph" and live_en["speed_text"] == "22.4 mph"
          and abs(live_en["speed_value"] - 22.4) < 0.01)
    c.telemetry_unit_system = "metric"
    live_met = c.status()["live"]
    check("status() exposes display speed fields in Metric units",
          live_met["speed_unit"] == "km/h" and live_met["speed_text"] == "36.0 km/h"
          and abs(live_met["speed_value"] - 36.0) < 0.01)
    check("status() keeps legacy mph field for compatibility",
          abs(live_met["speed_mph"] - 22.4) < 0.1)

    c2 = C.Controller(); c2.listener = _Lis()
    c2.apply_setup("road", CarLimits(), telemetry_unit_system="metric")
    live_apply = c2.status()["live"]
    check("apply_setup accepts telemetry units from the setup screen path",
          live_apply["speed_unit"] == "km/h" and live_apply["speed_text"] == "36.0 km/h")

    html = overlay._render_advanced({"live": live_met, "phase": C.TEST})
    check("overlay advanced render uses the selected telemetry unit",
          "36.0 km/h" in html and "mph" not in html.split("rpm")[0])
    check("LAN web view consumes speed_text instead of hardcoding mph",
          "speed_text" in web._PAGE and "Speed ${s.live.speed_mph} mph" not in web._PAGE)


def test_session_fixes_v0118():
    print("\n== v0.1.18: bottoming-coverage, OCR udp fallback, no re-propose, search/bottom split, fastest lap ==")
    from lapsmith.gui import controller as C
    from lapsmith.knowledge import rules
    from lapsmith.state.tune_state import CarLimits, Tune
    from lapsmith.telemetry.session import TestStats
    from lapsmith import identity
    from lapsmith.vision import capture
    import lapsmith.vision.read_tyres as RT
    ident = identity.identify(parse(simulator._build_packet(simulator.frame(0.5, "understeer"))))

    # ---- #1: bottoming fires only when WIDESPREAD, not for a localized kerb strike ----
    def _bottom_stats(zones_pattern):
        s = TestStats()
        s.n_corner_frames = 30
        s.susp_min_front = 0.01      # a real dip exists somewhere
        s.susp_bin_min_front = list(zones_pattern)
        s.susp_bin_min_rear = [1.0] * len(zones_pattern)
        return s
    thr = rules.BOTTOM_THRESH
    # one kerb: a single bin below threshold (1 zone, ~4% of lap)
    localized = _bottom_stats([1.0] * 23 + [0.01])
    rec = rules._rule_ride(localized, Tune(), "road", None, True, CarLimits())
    check("#1 localized 1-zone bottoming (kerb) is IGNORED, not chased",
          rec is None or not rec.is_change())
    # two sidewalks: 2 isolated bins (2 zones, ~8%) - still below the widespread gate
    two = _bottom_stats([0.01] + [1.0] * 11 + [0.01] + [1.0] * 11)
    rec2 = rules._rule_ride(two, Tune(), "road", None, True, CarLimits())
    check("#1 two isolated sidewalk strikes still IGNORED (below widespread gate)",
          rec2 is None or not rec2.is_change())
    # genuinely too-low car: bottoms across most of the lap
    wide = _bottom_stats([0.01] * 18 + [1.0] * 6)
    rec3 = rules._rule_ride(wide, Tune(), "road", None, True, CarLimits())
    check("#1 WIDESPREAD bottoming (75% of lap) DOES fire ride-height",
          rec3 is not None and rec3.is_change() and rec3.group == "ride_height")
    ff, zf, fr, zr = wide.bottoming_coverage(thr)
    check("#1 coverage reports a high fraction + one contiguous zone for a too-low car",
          ff >= rules.BOTTOM_MIN_FRAC and zf == 1)
    # graceful degrade: NO coverage data -> behave like before (single-min fires)
    nocov = TestStats(); nocov.susp_min_front = 0.01; nocov.n_corner_frames = 30
    rec4 = rules._rule_ride(nocov, Tune(), "road", None, True, CarLimits())
    check("#1 with no coverage data, falls back to single-min behaviour (fires)",
          rec4 is not None and rec4.is_change())

    # ---- #2: screen_size populated (resolution no longer null) + UDP-single fallback ----
    sz = capture.screen_size()
    check("#2 screen_size() returns a real (w,h) via Pillow (resolution not null)",
          sz is not None and sz[0] > 0 and sz[1] > 0)
    c = C.Controller(); c.identity = ident; c.apply_setup("road", CarLimits())
    c.discipline = "road"
    orig = (RT.rapidocr_available, RT.ocr_heat_page, RT.vision_available)
    RT.rapidocr_available = lambda: False
    RT.ocr_heat_page = lambda *a, **k: None
    RT.vision_available = lambda: False
    try:
        c._read_heat("frame.png", peak_g=1.0,
                     udp_temps={"FL": 80, "FR": 81, "RL": 78, "RR": 79})
        check("#2 OCR fails BUT UDP temps present -> 'udp_single' path (not blind search)",
              c.last_reader == "udp_single")
        c._read_heat("frame.png", peak_g=1.0, udp_temps=None)
        check("#2 OCR fails AND no UDP temps -> blind 'camber_search'",
              c.last_reader == "camber_search")
    finally:
        RT.rapidocr_available, RT.ocr_heat_page, RT.vision_available = orig

    # ---- #3: never re-propose the EXACT value already tried+reverted ----
    rec_same = rules.Recommendation("diff", {"diff_rear_decel": 10.0}, "r", "f")
    check("#3 a re-proposed identical reverted value is filtered out",
          rules._filter_tried_values(rec_same, {"diff_rear_decel": {10.0}}) is None)
    rec_diff = rules.Recommendation("diff", {"diff_rear_decel": 12.0}, "r", "f")
    out = rules._filter_tried_values(rec_diff, {"diff_rear_decel": {10.0}})
    check("#3 a DIFFERENT value is still allowed (step elsewhere, not lock-only)",
          out is not None and out.fields == {"diff_rear_decel": 12.0})
    c2 = C.Controller(); c2.apply_setup("road", CarLimits())
    c2.state.current.set("diff_rear_decel", 15.0)
    applied = c2.state.apply_change("diff", {"diff_rear_decel": 10.0}, "r", "f")
    c2._applied_records = [applied]
    c2._revert_batch()
    check("#3 revert records the tried value AND restores the previous",
          10.0 in c2._tried_values.get("diff_rear_decel", set())
          and c2.state.current.get("diff_rear_decel") == 15.0)

    # ---- #4: a search change is NOT bundled into a bottoming batch ----
    s = TestStats()
    s.drivetrain = "RWD"; s.n_corner_frames = 30
    s.slip_angle_front = 0.45; s.slip_angle_rear = 0.15      # understeer -> ARB search
    s.susp_min_front = 0.01
    s.susp_bin_min_front = [0.01] * 20 + [1.0] * 4           # widespread bottoming
    s.susp_bin_min_rear = [1.0] * 24
    batch = rules.analyze_batch(s, Tune(), "road", None, max_search=1, limits=CarLimits())
    groups = {r.group for r in batch}
    check("#4 bottoming batch contains the ride-height evidence change",
          "ride_height" in groups)
    check("#4 an unrelated ARB/damping SEARCH is NOT bundled with the bottoming batch",
          not (groups & rules._SEARCH_GROUPS))
    # control: with NO bottoming, the ARB search IS allowed to appear
    s.susp_min_front = 0.6; s.susp_bin_min_front = [0.6] * 24
    batch2 = rules.analyze_batch(s, Tune(), "road", None, max_search=1, limits=CarLimits())
    check("#4 control: without bottoming, the ARB search change can appear",
          any(r.group in rules._SEARCH_GROUPS for r in batch2))

    # ---- #5: fastest CLEAN lap driven tracked + surfaced separate from confirmed best ----
    c3 = C.Controller(); c3.identity = ident; c3.apply_setup("road", CarLimits())
    c3.mode = C.MODE_AUTO; c3.laps_per_test = 3      # so _collect_lap won't finalize early
    pk = [parse(simulator._build_packet(simulator.frame(x / 10.0, "rivals"))) for x in range(30)]

    class _Lap:
        def __init__(self, t):
            self.last_lap_s = t; self.packets = pk; self.lap_number = 1
    c3._collect_lap(_Lap(46.21))
    check("#5 fastest lap driven recorded from a measured lap", c3._fastest_lap_driven == 46.21)
    c3._collect_lap(_Lap(48.0))
    check("#5 a SLOWER subsequent lap does not replace the fastest driven",
          c3._fastest_lap_driven == 46.21)
    c3.best_segment = 47.29
    st = c3.status()
    check("#5 status surfaces fastest-lap-driven distinct from the confirmed best",
          st["fastest_lap_driven_s"] == 46.21 and st["best_segment_s"] == 47.29)
    check("#5 progress_state also exposes the fastest lap driven",
          c3.progress_state()["fastest_lap_driven_s"] == 46.21)


def test_install_telemetry_v0117():
    print("\n== install bug: 0.0.0.0 bind captures loopback + 'no packets -> firewall' diag ==")
    import socket, time as _t
    from lapsmith.gui import controller as C
    from lapsmith.telemetry.listener import TelemetryListener

    # --- the WHOLE fix: a 0.0.0.0 bind MUST receive packets sent to 127.0.0.1 (loopback)
    port = 5691
    lis = TelemetryListener(port=port, host="0.0.0.0")
    lis.start()
    try:
        pkt = simulator._build_packet(simulator.frame(0.5, "understeer"))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(8):
            s.sendto(pkt, ("127.0.0.1", port)); _t.sleep(0.02)
        s.close()
        _t.sleep(0.4)
        check("0.0.0.0 bind RECEIVES loopback packets sent to 127.0.0.1 (the install-bug fix)",
              lis.packet_count > 0)
    finally:
        lis.stop()

    # --- 'bound but no packets' -> specific firewall/Data-Out diagnostic ------------
    c = C.Controller()
    class _Fake:
        packet_count = 0
        last_packet = None
        last_packet_time = 0.0
        def snapshot(self): return None
        def is_receiving(self, within_s=1.0): return False
    c.listener = _Fake()
    check("diagnostic silent before the listener starts", c.telemetry_diagnostic() is None)
    c._listen_start_t = _t.perf_counter()
    check("diagnostic silent in the grace window (no false alarm)", c.telemetry_diagnostic() is None)
    c._listen_start_t = _t.perf_counter() - (C.TELEMETRY_NODATA_WARN_S + 1)   # elapsed, 0 packets
    msg = c.telemetry_diagnostic()
    check("after N s with 0 packets -> SPECIFIC firewall/Data-Out message (not 'no car')",
          msg is not None and "firewall" in msg.lower() and "data out" in msg.lower())
    c.listener.packet_count = 5
    check("diagnostic clears the moment telemetry arrives", c.telemetry_diagnostic() is None)
    # and it replaces the generic 'waiting for telemetry' guidance at WAIT_TELEMETRY
    c.listener.packet_count = 0
    c._listen_start_t = _t.perf_counter() - (C.TELEMETRY_NODATA_WARN_S + 1)
    c.phase = C.WAIT_TELEMETRY
    check("WAIT_TELEMETRY guidance shows the firewall diagnostic, not a generic 'waiting'",
          "firewall" in (c.guided_step().get("action", "") or "").lower())
    check("status() surfaces the telemetry diagnostic for the overlay",
          "firewall" in (c.status().get("telemetry_diagnostic", "") or "").lower())


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

    # NEUTRAL gate (v0.1.7): a delta within lap spread no longer banks drift - it is
    # REVERTED (best unchanged) and counted against the lever, but not yet locked.
    c = fresh(laps=3); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    apply_then(c, (50.4, 50.1, 50.6))           # best 50.1 vs 50.0 = +0.1 (neutral)
    check("neutral lap NOT banked: best stays 50.0, lever counted not locked",
          abs(c.best_segment - 50.0) < 1e-6 and len(c.state.converged_levers) == 0
          and len(c._lever_locked) == 0 and any(v >= 1 for v in c._noimprove.values()))

    # EVIDENCE protection: a small regression past the plain gate is NEUTRAL (not a
    # hard regress) so the lever is NOT LOCKED unless it clears the evidence margin.
    c = fresh(laps=2); c.best_segment = 50.0
    c.stats = aggregate(_window("understeer")); c._compute_batch()
    check("batch contains an evidence change (camber)",
          any(r.kind == "evidence" for r in c.batch))
    apply_then(c, (50.35, 50.35))               # +0.35: > 0.2 gate but < 0.2+0.3 evidence
    check("evidence change not LOCKED on a small regression (within evidence margin)",
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
    # A single wild misread is REPAIRED (the bad zone dropped), not failing all 4 corners
    # (#1: repair the corner via UDP / outlier-drop rather than discarding the frame).
    bad, _ = _labelled_tokens(1920, 1080)
    bad = [("120.0" if t == "63.7" else t, xy) for t, xy in bad]   # FL inner way off
    out_bad = read_tyres.tokens_to_reading(bad, udp_temps={"FL": 65.9})
    check("a single wild misread is repaired, not failing the whole read",
          out_bad is not None and abs(out_bad.get("FL", {}).get("inner", 0.0) - 120.0) > 40
          and "FR" in out_bad)


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

    # DISPLAY-time repair: a name already stored as a dict-repr blob (old bug, never
    # re-imported) must show the clean display_name, not the whole {...} object.
    blob = ("{'display_name': '1989 Volkswagen Golf Rallye', 'year': 1989, "
            "'make': 'Volkswagen', 'model': 'Golf Rallye', 'car_id': 930001}")
    check("_clean_name extracts display_name from a dict-repr blob",
          ordinals._clean_name(blob) == "1989 Volkswagen Golf Rallye"
          and ordinals._clean_name("Plain Name") == "Plain Name")
    ordinals._USER_MAP[930001] = blob          # simulate the bad on-disk value
    check("name_for shows the clean name, not the raw record dict",
          ordinals.name_for(930001) == "1989 Volkswagen Golf Rallye")
    # load_user_map repairs a file full of blobs AND rewrites it clean
    import json as _json
    bad_path = os.path.join(d, "bad_names.json")
    with open(bad_path, "w", encoding="utf-8") as _f:
        _json.dump({"930002": blob, "930003": "Already Clean"}, _f)
    ordinals._USER_MAP.clear()
    ordinals.load_user_map(bad_path)
    rewritten = _json.load(open(bad_path, encoding="utf-8"))
    check("load_user_map cleans blobs in memory and rewrites the file",
          ordinals.name_for(930002) == "1989 Volkswagen Golf Rallye"
          and rewritten["930002"] == "1989 Volkswagen Golf Rallye"
          and rewritten["930003"] == "Already Clean")
    ordinals._USER_MAP.clear()                 # restore the earlier store state
    ordinals.set_store_path(os.path.join(d, "cn.json"))
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
    test_f8_rivals_autolap()
    test_reload_and_fixation()
    test_dirt_diff_and_lever_cap()
    test_telemetry_primary_fitness()
    test_checklists_and_overlay_states()
    test_console_mode()
    test_troubleshooting_v0110()
    test_ux_v0112()
    test_critical_fixes_v0113()
    test_logging_v0114()
    test_richer_channels_v0115()
    test_recalibration_v0116()
    test_session_fixes_v0118()
    test_v0119_shutdown_units_compound_detection()
    test_telemetry_display_units_v0125()
    test_v0120_detection_pause_resilience()
    test_v0122_ocr_celsius_parser()
    test_v0123_ocr_box_coord_mapping()
    test_v0123_evidence_keep()
    test_v0123_oncar_bundled_revert()
    test_v0124_drive_only_no_f8()
    test_install_telemetry_v0117()
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
