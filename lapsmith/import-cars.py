"""CLI entry: import an FH6 car-name database into LapSmith.

    python -m lapsmith.import-cars <file>

<file> is the CSV / TSV / JSON you downloaded from the Nexus Mods
"Forza Horizon 6 Car ID List" page. Merge-only: names you set or edited are kept.
"""
import argparse
import sys

from . import car_import, ordinals


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m lapsmith.import-cars",
        description="Import a community FH6 car-name database (CSV/TSV/JSON) into "
                    f"LapSmith. Download one from {car_import.NEXUS_CAR_LIST_URL}")
    ap.add_argument("file", help="the CSV/TSV/JSON downloaded from the Nexus "
                                  "'Forza Horizon 6 Car ID List' page")
    ap.add_argument("--names-file", default=None,
                    help="car_names.json path (default: %%APPDATA%%/LapSmith/car_names.json)")
    args = ap.parse_args(argv)

    ordinals.set_store_path(args.names_file or car_import.default_names_path())
    try:
        s = car_import.import_file(args.file)
    except OSError as e:
        print(f"Could not read {args.file}: {e}", file=sys.stderr)
        return 2
    print(f"Imported {s['imported']} new car name(s) -> {ordinals.NAMES_PATH}")
    print(f"  already named (your names kept): {s['already']}")
    print(f"  malformed / skipped rows:        {s['malformed']}")
    if s["parsed"] == 0:
        print("  No car names recognised - is this the CSV/JSON from the Nexus page?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
