"""Persistence: per-car session JSON + human-readable final tune + share format,
plus shareable exports, a support zip bundle, and a cumulative tune log."""
from __future__ import annotations

import glob
import json
import os
import re
import zipfile
from dataclasses import asdict
from typing import List, Optional

from .tune_state import TuneState, Tune
from .. import PRODUCT_NAME

SESSIONS_DIR = os.environ.get("FH6_SESSIONS_DIR", "sessions")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def set_sessions_dir(path: str) -> None:
    """Point all outputs at `path` (the GUI sets this to its data dir)."""
    global SESSIONS_DIR
    SESSIONS_DIR = path
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_path(car: str, discipline: str) -> str:
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, f"{_slug(car)}_{_slug(discipline)}.json")


def session_log_path(car: str, discipline: str) -> str:
    """Path of the INCREMENTAL per-session log (written progressively so a session is
    recoverable even after a crash / force-close)."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, f"{_slug(car)}_{_slug(discipline)}_session.log")


def save_session(state: TuneState, *, car: str, car_class: str, discipline: str,
                 front_weight_pct: float, drivetrain: str, baseline: Tune,
                 stats_log: list, started_iso: str, status: str,
                 limits=None, best_lap_s: Optional[float] = None,
                 finished_iso: Optional[str] = None,
                 final_tune: Optional[Tune] = None) -> str:
    """Write the per-car session JSON. `final_tune` (the BEST CONFIRMED tune) overrides
    state.current when given, so the saved tune is never a mid-flight reverted state.
    Writes atomically (temp + replace) so a crash mid-write can't corrupt the file."""
    path = session_path(car, discipline)
    out = (final_tune if final_tune is not None else state.current)
    payload = {
        "car": car,
        "class": car_class,
        "discipline": discipline,
        "drivetrain": drivetrain,
        "front_weight_pct": front_weight_pct,
        "car_limits": limits.as_dict() if limits is not None else None,
        "started": started_iso,
        "finished": finished_iso,
        "status": status,
        "best_lap_s": best_lap_s,
        "iterations": state.iteration,
        "baseline": baseline.as_dict(),
        "final_tune": out.as_dict(),
        "diff_from_baseline": {k: list(v) for k, v in state.diff_from_baseline(baseline).items()}
        if final_tune is None else
        {k: (baseline.as_dict()[k], v) for k, v in out.as_dict().items()
         if baseline.as_dict().get(k) != v},
        "converged_levers": sorted(state.converged_levers),
        "history": [h.as_dict() for h in state.history],
        "test_stats_log": stats_log,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)               # atomic on Windows + POSIX
    return path


def load_session(car: str, discipline: str) -> Optional[dict]:
    path = session_path(car, discipline)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_final_tune_txt(t: Tune, *, car: str, car_class: str, discipline: str,
                        front_weight_pct: float, drivetrain: str) -> str:
    from ..knowledge.baseline import format_checklist
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, f"{_slug(car)}_{_slug(discipline)}_final_tune.txt")
    body = format_checklist(t, car, car_class, discipline, front_weight_pct, drivetrain)
    body = body.replace("INITIAL TUNE", "FINAL TUNE", 1)
    share = _optn_club_block(t, drivetrain)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + "\n\n" + share + "\n")
    return path


def _stockable(v: float, fmt: str) -> str:
    from ..state.tune_state import STOCK
    return "stock" if v == STOCK else format(v, fmt)


def _optn_club_block(t: Tune, drivetrain: str) -> str:
    """A flat key:value block roughly matching optn.club/formatter/forza/horizon6/v1.

    Not the exact binary share code (that is generated on their site) but a
    clean, paste-friendly value list with the same field set.
    """
    L = ["--- share format (optn.club/formatter/forza/horizon6/v1) ---",
         f"tires.front_psi: {t.pressure_f:.1f}",
         f"tires.rear_psi: {t.pressure_r:.1f}",
         f"gears.final_drive: {_stockable(t.final_drive, '.2f')}",
         f"align.camber_front: {t.camber_f:+.1f}",
         f"align.camber_rear: {t.camber_r:+.1f}",
         f"align.toe_front: {t.toe_f:+.1f}",
         f"align.toe_rear: {t.toe_r:+.1f}",
         f"align.caster: {t.caster:.1f}",
         f"arb.front: {t.arb_f:.0f}",
         f"arb.rear: {t.arb_r:.0f}",
         f"springs.front: {t.spring_f:.1f}",
         f"springs.rear: {t.spring_r:.1f}",
         f"ride_height.front: {t.ride_height_f:.1f}",
         f"ride_height.rear: {t.ride_height_r:.1f}",
         f"damping.rebound_front: {t.rebound_f:.1f}",
         f"damping.rebound_rear: {t.rebound_r:.1f}",
         f"damping.bump_front: {t.bump_f:.1f}",
         f"damping.bump_rear: {t.bump_r:.1f}",
         f"brakes.pressure: {t.brake_pressure:.0f}",
         f"brakes.balance: {t.brake_balance:.0f}",
         f"diff.center: {t.diff_center:.0f}",
         f"diff.rear_accel: {t.diff_rear_accel:.0f}",
         f"diff.rear_decel: {t.diff_rear_decel:.0f}",
         f"diff.front_accel: {t.diff_front_accel:.0f}",
         f"diff.front_decel: {t.diff_front_decel:.0f}",
         f"aero.front: {_stockable(t.aero_front, '.0f')}",
         f"aero.rear: {_stockable(t.aero_rear, '.0f')}",
         f"drivetrain: {drivetrain}"]
    return "\n".join(L)


# === shareable output ========================================================

_SHARE_DISCLAIMER = (
    "NOTE: these are VALUES to enter by hand in the FH6 tune menu. This is NOT an\n"
    "in-game FH6 share code (those are generated by the game and can't be made here).")


def share_text(t: Tune, *, car: str, car_class: str, discipline: str,
               front_weight_pct: float, drivetrain: str,
               best_lap_s: Optional[float] = None) -> str:
    """The full human-readable value sheet + optn.club block + disclaimer. This is
    exactly what the 'copy to clipboard' button copies."""
    from ..knowledge.baseline import format_checklist
    body = format_checklist(t, car, car_class, discipline, front_weight_pct, drivetrain)
    body = body.replace("INITIAL TUNE", "FINAL TUNE", 1)
    if best_lap_s:
        body += f"\n\nBest lap this session: {best_lap_s:.2f}s"
    return body + "\n\n" + _optn_club_block(t, drivetrain) + "\n\n" + _SHARE_DISCLAIMER


def export_tune(state: TuneState, *, car: str, car_class: str, discipline: str,
                front_weight_pct: float, drivetrain: str,
                best_lap_s: Optional[float] = None,
                final_tune: Optional[Tune] = None) -> dict:
    """Write the shareable bundle for a final tune into SESSIONS_DIR and return the
    paths. Produces: a value sheet (.txt with optn.club block) and a clean JSON of
    the final values + meta. `final_tune` (the BEST CONFIRMED tune) overrides
    state.current. Returns {folder, txt, json, share_text}."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    out = (final_tune if final_tune is not None else state.current)
    base = f"{_slug(car)}_{_slug(discipline)}"
    txt_path = os.path.join(SESSIONS_DIR, base + "_final_tune.txt")
    json_path = os.path.join(SESSIONS_DIR, base + "_tune.json")
    text = share_text(out, car=car, car_class=car_class, discipline=discipline,
                      front_weight_pct=front_weight_pct, drivetrain=drivetrain,
                      best_lap_s=best_lap_s)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    payload = {
        "car": car, "class": car_class, "discipline": discipline,
        "drivetrain": drivetrain, "front_weight_pct": front_weight_pct,
        "best_lap_s": best_lap_s, "values": out.as_dict(),
        "note": "manual values for the FH6 tune menu - not an in-game share code",
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return {"folder": os.path.abspath(SESSIONS_DIR), "txt": txt_path,
            "json": json_path, "share_text": text}


def list_sessions() -> List[dict]:
    """Summaries of every saved tune session (for the Previous Tunes tab): car,
    class, discipline, date, best lap, status + file paths. Newest first."""
    out: List[dict] = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        if p.endswith("_tune.json"):
            continue                          # the compact share JSON, not a session
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if "final_tune" not in d:
            continue
        base = os.path.splitext(os.path.basename(p))[0]
        txt = os.path.join(SESSIONS_DIR, base + "_final_tune.txt")
        tune_json = os.path.join(SESSIONS_DIR, base + "_tune.json")
        out.append({
            "car": d.get("car", "?"), "class": d.get("class", "?"),
            "discipline": d.get("discipline", "?"), "date": d.get("started", ""),
            "finished": d.get("finished", ""),
            "duration_s": _duration_s(d.get("started"), d.get("finished")),
            "best_lap_s": d.get("best_lap_s"), "status": d.get("status", ""),
            "iterations": d.get("iterations", 0),
            "drivetrain": d.get("drivetrain", ""),
            "session_json": p,
            "final_txt": txt if os.path.exists(txt) else None,
            "tune_json": tune_json if os.path.exists(tune_json) else None,
        })
    out.sort(key=lambda s: s.get("date") or "", reverse=True)
    return out


def _duration_s(started: Optional[str], finished: Optional[str]) -> Optional[float]:
    if not started or not finished:
        return None
    try:
        import datetime as _dt
        a = _dt.datetime.fromisoformat(started)
        b = _dt.datetime.fromisoformat(finished)
        s = (b - a).total_seconds()
        return s if s >= 0 else None
    except (ValueError, TypeError):
        return None


def stats_summary() -> dict:
    """Aggregate dashboard stats from the saved sessions: totals, counts by
    car/discipline/class, best lap per car, total iterations + time spent."""
    sessions = list_sessions()
    by_car: dict = {}
    by_disc: dict = {}
    by_class: dict = {}
    best_by_car: dict = {}
    total_iters = 0
    total_time = 0.0
    have_time = False
    for s in sessions:
        by_car[s["car"]] = by_car.get(s["car"], 0) + 1
        by_disc[s["discipline"]] = by_disc.get(s["discipline"], 0) + 1
        by_class[s["class"]] = by_class.get(s["class"], 0) + 1
        total_iters += int(s.get("iterations") or 0)
        bl = s.get("best_lap_s")
        if bl:
            cur = best_by_car.get(s["car"])
            best_by_car[s["car"]] = bl if cur is None else min(cur, bl)
        dur = s.get("duration_s")
        if dur:
            total_time += dur
            have_time = True
    return {
        "total_tunes": len(sessions),
        "by_car": by_car,
        "by_discipline": by_disc,
        "by_class": by_class,
        "best_lap_by_car": best_by_car,
        "total_iterations": total_iters,
        "total_time_s": total_time if have_time else None,
        "recent": sessions[:8],
    }


# === cumulative tune log (paste into an LLM to refine the method) ============

def append_cumulative_log(state: TuneState, baseline: Tune, *, car: str,
                          car_class: str, discipline: str, drivetrain: str,
                          started_iso: str, best_lap_s: Optional[float] = None,
                          baseline_lap_s: Optional[float] = None) -> str:
    """Append one record per completed tune to a growing markdown log. The format
    is designed to be pasted into an LLM to refine the tuning method: it lists
    each change, its keep/revert verdict, the evidence (reason) behind it, and the
    net lap delta."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, "cumulative_tune_log.md")
    new_file = not os.path.exists(path)
    kept = [h for h in state.history if h.verdict == "kept"]
    reverted = [h for h in state.history if h.verdict == "reverted"]
    L: List[str] = []
    if new_file:
        L.append(f"# {PRODUCT_NAME} cumulative log")
        L.append("One record per completed tune. Paste into an LLM to refine the "
                 "tuning method (which evidence -> change rules helped vs hurt).\n")
    L.append(f"## {car}  |  {car_class}  |  {discipline.upper()}  |  {drivetrain}  |  {started_iso}")
    if baseline_lap_s and best_lap_s:
        L.append(f"- Lap: baseline {baseline_lap_s:.2f}s -> best {best_lap_s:.2f}s "
                 f"({best_lap_s - baseline_lap_s:+.2f}s)")
    elif best_lap_s:
        L.append(f"- Best lap: {best_lap_s:.2f}s")
    L.append(f"- Iterations: {state.iteration}; kept {len(kept)}, reverted {len(reverted)}")
    if kept:
        L.append("- Changes KEPT (helped):")
        for h in kept:
            sets = ", ".join(f"{k}->{v}" for k, v in h.fields.items())
            L.append(f"    - {h.lever_group}: {sets}  | why: {h.reason}")
    if reverted:
        L.append("- Changes REVERTED (hurt/locked):")
        for h in reverted:
            sets = ", ".join(f"{k}->{v}" for k, v in h.fields.items())
            L.append(f"    - {h.lever_group}: {sets}  | why: {h.reason}")
    diffs = state.diff_from_baseline(baseline)
    if diffs:
        L.append("- Net change from baseline:")
        for k, (b, c) in sorted(diffs.items()):
            L.append(f"    - {k}: {b} -> {c}")
    L.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


# === support bundle (one zip a user can send for help) =======================

def write_support_bundle(*, car: str, discipline: str, env: dict,
                         app_log: Optional[str] = None,
                         heat_frames: Optional[List[str]] = None,
                         max_log_bytes: int = 400_000,
                         max_frames: int = 6) -> str:
    """Write ONE shareable zip with everything needed to diagnose a run: the per-session
    DECISION log (the primary debugging artefact - changes, rules, drivetrain, A/B/A,
    final tune), the session JSON, the final tune sheet, an environment summary, recent
    Heat frames, and a (now small, raw-free) tail of app.log. The raw per-packet
    telemetry dumps are deliberately NOT included."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    base = f"{_slug(car)}_{_slug(discipline)}"
    zip_path = os.path.join(SESSIONS_DIR, base + "_support.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("environment.json", json.dumps(env, indent=2))
        # THE decision log for this session, full + start-to-finish.
        slog = session_log_path(car, discipline)
        if os.path.exists(slog):
            z.write(slog, "session_decision_log.txt")
        sj = session_path(car, discipline)
        if os.path.exists(sj):
            z.write(sj, "session.json")
        for suffix in ("_final_tune.txt", "_tune.json"):
            p = os.path.join(SESSIONS_DIR, base + suffix)
            if os.path.exists(p):
                z.write(p, os.path.basename(p))
        if app_log and os.path.exists(app_log):
            try:
                with open(app_log, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - max_log_bytes))
                    tail = f.read()
                z.writestr("app.log", tail)
            except OSError:
                pass
        for fp in (heat_frames or [])[:max_frames]:
            if fp and os.path.exists(fp):
                z.write(fp, os.path.join("heat_frames", os.path.basename(fp)))
    return zip_path
