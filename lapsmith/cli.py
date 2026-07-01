"""CLI entry point + session orchestration.

  python -m lapsmith --car "Aston Martin Valkyrie" --class "S2 900" \
      --discipline "road circuit" --port 5607
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys

from .main_loop import Config, run, UI
from .knowledge.baseline import canon_class, canon_discipline, PI_CEILING


def _now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lapsmith",
        description="FH6 telemetry-driven auto-tuning assistant (you drive; it tells "
                    "you exact values).")
    p.add_argument("--car", default=None,
                   help='optional; auto-detected from telemetry (CarOrdinal) if omitted')
    p.add_argument("--class", dest="car_class", default=None,
                   help="optional; auto-detected from PI if omitted (A 700|S1 800|S2 900|R 998)")
    p.add_argument("--discipline", required=True,
                   help="road circuit | touge | dirt | cross country | top speed | drag")
    p.add_argument("--front-weight", type=float, default=None,
                   help="front weight %% from the upgrade screen (e.g. 48). "
                        "If omitted you will be prompted.")
    p.add_argument("--drivetrain", default=None, choices=["FWD", "RWD", "AWD"],
                   help="defaults to AWD, auto-detected from telemetry if available")
    p.add_argument("--port", type=int, default=5607, help="UDP Data Out port (default 5607)")
    p.add_argument("--manual-vision", action="store_true",
                   help="type tyre temps by hand instead of automatic screen reading")
    p.add_argument("--verify-tune", action="store_true",
                   help="screenshot the tune sheet after each change to confirm entry")
    p.add_argument("--skip-validation", action="store_true",
                   help="skip the live validation gate (dry runs / simulator only)")
    p.add_argument("--telemetry-units", choices=["english", "metric"], default="english",
                   help="unit system for live telemetry readouts (default: english)")
    p.add_argument("--max-iters", type=int, default=40)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    disc = canon_discipline(args.discipline)
    # car/class/drivetrain are auto-detected from telemetry inside run() when not
    # given. None here means "detect"; canon_class only applied if provided.
    cls = canon_class(args.car_class) if args.car_class else None
    print(f"Car        : {args.car or '(auto-detect from telemetry)'}")
    print(f"Class      : {cls or '(auto-detect from PI)'}")
    print(f"Discipline : {disc}")

    fw = args.front_weight
    if fw is None:
        raw = input("Front weight %% (read it off the upgrade/telemetry screen, e.g. 48): ").strip()
        try:
            fw = float(raw)
        except ValueError:
            print("Could not parse front weight; defaulting to 50%.")
            fw = 50.0

    print(f"Confirm in-game: Data Out ON, IP 127.0.0.1, Port {args.port}.")

    cfg = Config(
        car=args.car, car_class=cls, discipline=disc, front_weight_pct=fw,
        drivetrain=args.drivetrain, port=args.port, manual_vision=args.manual_vision,
        verify_tune=args.verify_tune, skip_validation=args.skip_validation,
        telemetry_unit_system=args.telemetry_units,
        max_iters=args.max_iters, started_iso=_now_iso(),
    )
    try:
        status = run(cfg, UI())
    except KeyboardInterrupt:
        print("\nInterrupted. Partial session not saved.")
        return 130
    print(f"\nDone ({status}). Re-run with new --car to tune the next car.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
