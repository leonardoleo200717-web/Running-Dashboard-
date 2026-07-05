"""Layer 0 data hygiene + the Steady Effort Store (PROPOSAL.md v2).

Pure functions. Everything the efficiency statistics read comes from
here — nothing downstream reads raw session averages or unfiltered HR.

Hygiene (optical wrist-HR failure modes):
- spikes: |ΔHR| > 8 bpm between consecutive 1 Hz samples → interpolate
  gaps ≤ 5 s, flag longer stretches invalid
- cadence lock: |HR − cadence(spm)| < 3 for ≥ 30 s → flagged suspect
- stabilized HR is a median, never a mean

Efforts:
- reps (engine bounds): pace always; HR only stabilized (first 45 s
  dropped, median of the rest); reps < 90 s contribute no HR statistics
- steady blocks: the progression rule (≥ 600 s, pace CV ≤ 5%, no pause)
- everything else (warmup/cooldown/recovery/junk) never aggregates

Domains (percent of LTHR, so recalibration moves all boundaries):
  easy ≤ Z2 ceiling < steady < 93% LTHR ≤ threshold ≤ 102% LTHR < hard
"""

from __future__ import annotations

import math
import statistics
from datetime import timedelta

HR_SPIKE_DELTA = 8          # bpm between consecutive 1 Hz samples
HR_GAP_INTERPOLATE_S = 5
CADENCE_LOCK_TOL = 3        # bpm vs steps/min
CADENCE_LOCK_MIN_S = 30
HR_STABILIZE_S = 45         # HR time constant ~30-45 s
HR_MIN_REP_S = 90           # below this a rep has no stabilized HR
HR_MIN_SAMPLES = 20

DEFAULT_SETTINGS = {
    "lthr": 176.0,           # @ 3:55/km (Garmin, to confirm)
    "max_hr": 190.0,
    "z2_ceiling": 143.0,
    "ref_hr": 175.0,         # LTHR - 1
    "lthr_pace_s_km": 235.0, # 3:55/km, for the pace-based fallback
    "boundary_version": 1,
}

DOMAINS = ("easy", "steady", "threshold", "hard")


# ------------------------------------------------------------ hygiene

def clean_heart_rate(records: list[dict]) -> list[dict]:
    """Return records with an added 'hr_ok' flag and spike-interpolated
    HR. Original list is not mutated."""
    out = [dict(r) for r in records]

    # Spikes / dropouts: physiologically impossible deltas.
    last_good = None
    bad_run: list[int] = []
    for i, r in enumerate(out):
        hr = r.get("heart_rate")
        ok = hr is not None and 30 <= hr <= 230
        if ok and last_good is not None:
            prev_hr, prev_i = last_good
            dt = max(1, (out[i]["timestamp"] - out[prev_i]["timestamp"]).total_seconds())
            if abs(hr - prev_hr) > HR_SPIKE_DELTA * dt:
                ok = False
        if ok:
            if bad_run and last_good is not None:
                prev_hr, prev_i = last_good
                gap = (out[i]["timestamp"] - out[prev_i]["timestamp"]).total_seconds()
                if gap <= HR_GAP_INTERPOLATE_S:
                    for k, j in enumerate(bad_run, 1):
                        frac = k / (len(bad_run) + 1)
                        out[j]["heart_rate"] = round(prev_hr + frac * (hr - prev_hr))
                        out[j]["hr_ok"] = True
            bad_run = []
            last_good = (hr, i)
            r["hr_ok"] = True
        else:
            r.setdefault("hr_ok", False)
            bad_run.append(i)

    # Cadence lock: HR glued to steps/min for a sustained stretch.
    run_start = None
    for i, r in enumerate(out + [None]):
        locked = (r is not None and r.get("hr_ok")
                  and r.get("heart_rate") and r.get("cadence")
                  and abs(r["heart_rate"] - r["cadence"]) < CADENCE_LOCK_TOL)
        if locked and run_start is None:
            run_start = i
        elif not locked and run_start is not None:
            span = (out[i - 1]["timestamp"] - out[run_start]["timestamp"]).total_seconds()
            if span >= CADENCE_LOCK_MIN_S:
                for j in range(run_start, i):
                    out[j]["hr_ok"] = False
                    out[j]["hr_suspect"] = True
            run_start = None
    return out


def smooth_speed(records: list[dict], window: int = 7) -> list[dict]:
    """Rolling-mean speed (GPS pace is noise at 1 Hz). Adds 'speed_smooth'."""
    speeds = [r.get("enhanced_speed") for r in records]
    half = window // 2
    for i, r in enumerate(records):
        chunk = [s for s in speeds[max(0, i - half):i + half + 1] if s is not None]
        r["speed_smooth"] = statistics.mean(chunk) if chunk else None
    return records


# ------------------------------------------------------------ domains

def domain_boundaries(settings: dict) -> dict:
    lthr = settings["lthr"]
    return {
        "easy_max": settings["z2_ceiling"],
        "steady_max": 0.93 * lthr,
        "threshold_max": 1.02 * lthr,
    }


def domain_for_hr(hr: float, settings: dict) -> str:
    b = domain_boundaries(settings)
    if hr <= b["easy_max"]:
        return "easy"
    if hr < b["steady_max"]:
        return "steady"
    if hr <= b["threshold_max"]:
        return "threshold"
    return "hard"


def domain_for_pace(pace_s_km: float, settings: dict) -> str:
    """Pace-based fallback (reps < 90 s, hr_suspect): bands derived from
    LTHR pace. Rough by design — pace-only statistics."""
    ltp = settings["lthr_pace_s_km"]
    if pace_s_km < 0.95 * ltp:
        return "hard"
    if pace_s_km <= 1.05 * ltp:
        return "threshold"
    if pace_s_km <= 1.15 * ltp:
        return "steady"
    return "easy"


# ------------------------------------------------------------ extraction

def _records_between(records, start, end):
    return [r for r in records if start <= r["timestamp"] <= end]


def _stabilized_hr(records: list[dict], start, end) -> tuple[float | None, bool, int]:
    """(median stabilized HR, suspect_majority, n_samples)."""
    window = _records_between(records, start + timedelta(seconds=HR_STABILIZE_S), end)
    good = [r["heart_rate"] for r in window if r.get("hr_ok")]
    suspect = sum(1 for r in window if r.get("hr_suspect"))
    if len(good) < HR_MIN_SAMPLES:
        return None, suspect > len(window) / 2 if window else False, len(good)
    return float(statistics.median(good)), suspect > len(window) / 2, len(good)


def extract_efforts(segments: list[dict], records: list[dict],
                    pauses: list[tuple], settings: dict | None = None) -> list[dict]:
    """The Steady Effort Store for one session.

    records must have passed clean_heart_rate() and smooth_speed().
    Returns effort dicts: kind rep|steady, pace, stabilized HR (or None),
    hr_valid, domain (+basis), REI, avg power.
    """
    from . import metrics as _m
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    efforts: list[dict] = []

    reps = [s for s in segments if s["kind"] == "rep" and not s.get("outlier")]
    for seg in reps:
        start, end = seg.get("start_time"), seg.get("end_time")
        timer = seg.get("timer_s") or 0
        if not start or not end or not timer:
            continue
        speed = seg.get("avg_speed_ms") or (
            (seg.get("distance_m") or 0) / timer if timer else None)
        if not speed:
            continue
        pace = 1000.0 / speed
        hr_med, suspect, n_hr = (None, False, 0)
        if timer >= HR_MIN_REP_S:
            hr_med, suspect, n_hr = _stabilized_hr(records, start, end)
        hr_valid = hr_med is not None and not suspect
        window = _records_between(records, start, end)
        powers = [r["power"] for r in window if r.get("power")]
        if hr_valid:
            dom, basis = domain_for_hr(hr_med, settings), "hr"
        else:
            dom, basis = domain_for_pace(pace, settings), "pace"
        efforts.append({
            "kind": "rep",
            "rep_index": seg.get("rep_index"),
            "start_time": start, "end_time": end,
            "timer_s": timer, "distance_m": seg.get("distance_m"),
            "speed_ms": speed, "pace_s_km": round(pace, 1),
            "hr_median": round(hr_med, 1) if hr_med is not None else None,
            "hr_valid": hr_valid, "hr_suspect": suspect,
            "domain": dom, "domain_basis": basis,
            "rei": round(speed / hr_med, 5) if hr_valid else None,
            "avg_power": round(statistics.mean(powers), 1) if powers else None,
            "boundary_version": settings["boundary_version"],
        })

    # Steady continuous blocks (never from interval sessions' recoveries;
    # the progression rule already guarantees that).
    for cand in _m.find_progression_candidates(segments, records, pauses):
        start, end = cand["start_time"], cand["end_time"]
        hr_med, suspect, _ = _stabilized_hr(records, start, end)
        hr_valid = hr_med is not None and not suspect
        window = _records_between(records, start, end)
        powers = [r["power"] for r in window if r.get("power")]
        speed = 1000.0 / cand["pace_s_km"]
        if hr_valid:
            dom, basis = domain_for_hr(hr_med, settings), "hr"
        else:
            dom, basis = domain_for_pace(cand["pace_s_km"], settings), "pace"
        efforts.append({
            "kind": "steady",
            "rep_index": None,
            "start_time": start, "end_time": end,
            "timer_s": cand["timer_s"], "distance_m": None,
            "speed_ms": round(speed, 3), "pace_s_km": cand["pace_s_km"],
            "hr_median": round(hr_med, 1) if hr_med is not None else None,
            "hr_valid": hr_valid, "hr_suspect": suspect,
            "domain": dom, "domain_basis": basis,
            "rei": round(speed / hr_med, 5) if hr_valid else None,
            "avg_power": round(statistics.mean(powers), 1) if powers else None,
            "boundary_version": settings["boundary_version"],
        })
    return efforts
