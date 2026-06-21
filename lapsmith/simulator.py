"""FH6 Data Out simulator - emits packets in the exact FH6 layout to a UDP port.

Used for dry-runs: drives the whole pipeline (listener -> session -> rules)
with no game. Round-trips through the same offset table as the parser, so if
the simulator and parser agree, the parser is internally consistent. (It does
NOT prove offsets match the real game - only a live HUD check does that.)

Scenarios bias the synthetic data so specific rules fire:
  neutral | understeer | oversteer | front_bottoming | hot_front | redline
"""
from __future__ import annotations

import argparse
import math
import socket
import struct
import time

from .telemetry.parser import _FIELDS, FH6_BASE_LEN


def _build_packet(values: dict) -> bytes:
    buf = bytearray(FH6_BASE_LEN)
    for name, fmt, off in _FIELDS:
        if name in values:
            struct.pack_into("<" + fmt, buf, off, values[name])
    return bytes(buf)


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


_SCENARIO_ALIASES = {"even": "neutral", "bottoming": "front_bottoming"}


def frame(t: float, scenario: str) -> dict:
    """Build one frame of synthetic telemetry at time t (seconds)."""
    scenario = _SCENARIO_ALIASES.get(scenario, scenario)
    # oscillate steering left/right to create corners
    phase = math.sin(t * 0.8)
    lateral_g = 1.0 * phase           # +-1.0 g
    speed = 45.0 + 10.0 * math.cos(t * 0.4)   # ~45 m/s cruising
    rpm_frac = 0.6 + 0.35 * abs(math.cos(t * 0.5))
    # integral of speed ~ distance; closed-form so frame() stays stateless
    distance = 45.0 * t + (10.0 / 0.4) * math.sin(t * 0.4)

    v = {
        "is_race_on": 1,
        "timestamp_ms": int(t * 1000) & 0xFFFFFFFF,
        "engine_max_rpm": 8000.0,
        "engine_idle_rpm": 900.0,
        "current_engine_rpm": 8000.0 * rpm_frac,
        "accel_x": lateral_g * 9.80665,   # lateral axis
        "accel_y": 0.0,
        "accel_z": 0.0,
        "vel_x": speed,
        "drivetrain_type": 2,             # AWD
        "car_ordinal": 2474,              # a known ordinal (display map)
        "car_class": 6,
        "car_pi": 800,
        "num_cylinders": 8,
        "speed": speed,
        "power": 300000.0,
        "gear": 4,
        "accel": 220,                     # mostly on throttle
        "brake": 0,
        "steer": int(phase * 100),
        "distance_traveled": 0.0,         # FH free-roam: this does NOT advance
        "position_x": distance,           # car travels along +X; timer uses this
        "position_z": 0.0,
        # nominal even temps / mid suspension / small slip
        "susp_norm_fl": 0.5, "susp_norm_fr": 0.5,
        "susp_norm_rl": 0.5, "susp_norm_rr": 0.5,
        "tire_temp_fl_f": _c_to_f(80.0), "tire_temp_fr_f": _c_to_f(80.0),
        "tire_temp_rl_f": _c_to_f(80.0), "tire_temp_rr_f": _c_to_f(80.0),
        "tire_slip_angle_fl": 2.0, "tire_slip_angle_fr": 2.0,
        "tire_slip_angle_rl": 2.0, "tire_slip_angle_rr": 2.0,
        "tire_slip_ratio_fl": 0.1, "tire_slip_ratio_fr": 0.1,
        "tire_slip_ratio_rl": 0.1, "tire_slip_ratio_rr": 0.1,
        "tire_combined_slip_fl": 0.4, "tire_combined_slip_fr": 0.4,
        "tire_combined_slip_rl": 0.4, "tire_combined_slip_rr": 0.4,
        # free-roam: lap fields do NOT advance (set live only by 'rivals')
        "best_lap": 0.0, "last_lap": 0.0, "current_lap": 0.0, "lap_number": 0,
    }

    if scenario == "understeer":
        v["tire_slip_angle_fl"] = v["tire_slip_angle_fr"] = 5.5
        v["tire_slip_angle_rl"] = v["tire_slip_angle_rr"] = 2.5
    elif scenario == "oversteer":
        v["tire_slip_angle_rl"] = v["tire_slip_angle_rr"] = 6.0
        v["tire_slip_angle_fl"] = v["tire_slip_angle_fr"] = 2.5
        v["tire_slip_ratio_rl"] = v["tire_slip_ratio_rr"] = 0.45
    elif scenario == "front_bottoming":
        if math.sin(t * 3.0) > 0.6:
            v["susp_norm_fl"] = v["susp_norm_fr"] = 0.02
    elif scenario == "hot_front":
        v["tire_temp_fl_f"] = _c_to_f(95.0)   # FL much hotter than FR
        v["tire_temp_fr_f"] = _c_to_f(82.0)
    elif scenario == "redline":
        v["current_engine_rpm"] = 7990.0
        v["speed"] = 60.0
        v["vel_x"] = 60.0
    elif scenario == "rivals":
        # live lap timing: a 20s lap; understeer-ish so a change is generated
        lap_dur = 20.0
        lap = int(t // lap_dur)
        v["lap_number"] = lap
        v["current_lap"] = t - lap * lap_dur
        v["last_lap"] = lap_dur if lap > 0 else 0.0
        v["best_lap"] = lap_dur if lap > 0 else 0.0
        v["tire_slip_angle_fl"] = v["tire_slip_angle_fr"] = 5.5
        v["tire_slip_angle_rl"] = v["tire_slip_angle_rr"] = 2.5

    return v


def run(port: int, host: str, scenario: str, hz: float, duration: float) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dt = 1.0 / hz
    t0 = time.perf_counter()
    print(f"FH6 simulator -> {host}:{port}  scenario={scenario}  {hz:.0f} Hz")
    n = 0
    while True:
        t = time.perf_counter() - t0
        if duration and t >= duration:
            break
        pkt = _build_packet(frame(t, scenario))
        sock.sendto(pkt, (host, port))
        n += 1
        time.sleep(dt)
    print(f"sent {n} packets")


def main() -> None:
    ap = argparse.ArgumentParser(description="FH6 Data Out simulator")
    ap.add_argument("--port", type=int, default=5607)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--scenario", default="understeer",
                    choices=["neutral", "even", "understeer", "oversteer",
                             "front_bottoming", "bottoming", "hot_front", "redline",
                             "rivals"])
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = forever")
    args = ap.parse_args()
    try:
        run(args.port, args.host, args.scenario, args.hz, args.duration)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
