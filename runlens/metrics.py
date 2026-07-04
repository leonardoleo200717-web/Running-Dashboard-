"""KPIs and trends (spec section 4). Pure functions on plain data.

All computations use timer time, never elapsed time (spec 2.1): record
slices are taken between segment start/end and pauses are respected via
the pause list from ingest.timer_pauses().
"""

from __future__ import annotations

import statistics
from datetime import timedelta

# Section 6 decisions (spec proposals adopted):
DEFAULT_HR_BAND = (165, 175)          # pace @ HR band, bpm
DEFAULT_PACE_BAND_S = (230, 235)      # HR @ pace band: 3:50-3:55 /km
MIN_BAND_SAMPLES = 300                # >= 5 min of 1 Hz samples in band

# Aerobic progression eligibility (spec 4.1):
PROGRESSION_MIN_TIMER_S = 600
PROGRESSION_MAX_PACE_CV = 0.05
MIN_MOVING_SPEED = 1.4                # m/s; below this = not running


def pace_s_per_km(speed_ms: float | None) -> float | None:
    if not speed_ms or speed_ms <= 0:
        return None
    return 1000.0 / speed_ms


def fmt_pace(seconds_per_km: float | None) -> str:
    if not seconds_per_km:
        return "–"
    m, s = divmod(int(round(seconds_per_km)), 60)
    return f"{m}:{s:02d}/km"


def _records_in(records: list[dict], start, end) -> list[dict]:
    return [r for r in records
            if r["timestamp"] is not None and start <= r["timestamp"] <= end]


# ------------------------------------------------------------ rep table

def rep_table(segments: list[dict], records: list[dict]) -> list[dict]:
    """Per-rep rows: distance/duration, pace, avg/max HR (spec 4).

    HR comes from the lap when present, else from the 1 Hz records in the
    segment window (splits carry no HR).
    """
    rows = []
    for seg in segments:
        if seg["kind"] != "rep":
            continue
        avg_hr, max_hr = seg.get("avg_hr"), seg.get("max_hr")
        if (avg_hr is None or max_hr is None) and seg["start_time"] and seg["end_time"]:
            hrs = [r["heart_rate"] for r in _records_in(records, seg["start_time"], seg["end_time"])
                   if r.get("heart_rate")]
            if hrs:
                avg_hr = avg_hr if avg_hr is not None else round(statistics.mean(hrs), 1)
                max_hr = max_hr if max_hr is not None else max(hrs)
        pace = pace_s_per_km(seg["avg_speed_ms"])
        rows.append({
            "rep_index": seg["rep_index"],
            "distance_m": seg["distance_m"],
            "timer_s": seg["timer_s"],
            "pace_s_km": pace,
            "pace": fmt_pace(pace),
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "outlier": seg["outlier"],
        })
    return rows


# ------------------------------------------------------------ drift

def _thirds(values: list[float]) -> tuple[list[float], list[float]]:
    n = max(1, len(values) // 3)
    return values[:n], values[-n:]


def intra_set_drift(rep_rows: list[dict]) -> dict:
    """HR and pace drift across reps: first vs last third (spec 4)."""
    rows = [r for r in rep_rows if not r["outlier"]]
    out = {"hr_drift_pct": None, "pace_drift_pct": None}
    if len(rows) < 3:
        return out

    hrs = [r["avg_hr"] for r in rows if r["avg_hr"]]
    if len(hrs) >= 3:
        first, last = _thirds(hrs)
        base = statistics.mean(first)
        if base:
            out["hr_drift_pct"] = round(100 * (statistics.mean(last) - base) / base, 1)

    paces = [r["pace_s_km"] for r in rows if r["pace_s_km"]]
    if len(paces) >= 3:
        first, last = _thirds(paces)
        base = statistics.mean(first)
        if base:
            out["pace_drift_pct"] = round(100 * (statistics.mean(last) - base) / base, 1)
    return out


# ------------------------------------------------------------ recovery quality

def recovery_quality(segments: list[dict], records: list[dict],
                     events: list[dict] | None = None) -> dict:
    """HR drop during recoveries; recovery_hr events as supporting data."""
    drops = []
    for seg in segments:
        if seg["kind"] != "recovery" or not seg["start_time"] or not seg["end_time"]:
            continue
        recs = _records_in(records, seg["start_time"], seg["end_time"])
        hrs = [r["heart_rate"] for r in recs if r.get("heart_rate")]
        if len(hrs) >= 5:
            start_hr = statistics.mean(hrs[:3])
            end_hr = statistics.mean(hrs[-3:])
            drops.append(start_hr - end_hr)

    recovery_hr_events = [
        {"timestamp": e["timestamp"], "hr": e.get("data")}
        for e in (events or [])
        if e.get("event") == "recovery_hr"
    ]
    return {
        "avg_hr_drop": round(statistics.mean(drops), 1) if drops else None,
        "n_recoveries": len(drops),
        "recovery_hr_events": recovery_hr_events,
    }


# ------------------------------------------------------------ trends

def _moving(records: list[dict]) -> list[dict]:
    return [r for r in records
            if (r.get("enhanced_speed") or 0) >= MIN_MOVING_SPEED]


def pace_at_hr(records: list[dict], band: tuple[int, int] = DEFAULT_HR_BAND,
               min_samples: int = MIN_BAND_SAMPLES) -> float | None:
    """Avg pace (s/km) over samples with HR inside the band, or None if the
    session doesn't spend enough time there."""
    lo, hi = band
    speeds = [r["enhanced_speed"] for r in _moving(records)
              if r.get("heart_rate") and lo <= r["heart_rate"] <= hi]
    if len(speeds) < min_samples:
        return None
    return pace_s_per_km(statistics.mean(speeds))


def hr_at_pace(records: list[dict], band_s: tuple[int, int] = DEFAULT_PACE_BAND_S,
               min_samples: int = MIN_BAND_SAMPLES) -> float | None:
    """Avg HR over samples with pace inside the band (s/km), or None."""
    lo, hi = band_s
    hrs = []
    for r in _moving(records):
        p = pace_s_per_km(r.get("enhanced_speed"))
        if p and lo <= p <= hi and r.get("heart_rate"):
            hrs.append(r["heart_rate"])
    if len(hrs) < min_samples:
        return None
    return round(statistics.mean(hrs), 1)


def weekly_volume(sessions: list[dict]) -> list[dict]:
    """[{'week': 'YYYY-Www', 'km': float, 'n_sessions': int}, ...] ordered."""
    weeks: dict[str, dict] = {}
    for s in sessions:
        start = s.get("start_time_utc") or s.get("start_time")
        if start is None:
            continue
        if isinstance(start, str):
            from datetime import datetime
            start = datetime.fromisoformat(start)
        iso = start.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        w = weeks.setdefault(key, {"week": key, "km": 0.0, "n_sessions": 0})
        w["km"] += (s.get("total_distance_m") or s.get("total_distance") or 0) / 1000.0
        w["n_sessions"] += 1
    out = sorted(weeks.values(), key=lambda w: w["week"])
    for w in out:
        w["km"] = round(w["km"], 1)
    return out


def intensity_distribution(time_in_zone: list[dict]) -> dict | None:
    """easy/moderate/hard split from the session-level time_in_zone message.
    Zones 1-2 easy, 3 moderate, 4-5 hard."""
    session_tiz = None
    for tiz in time_in_zone:
        if tiz.get("reference_mesg") in ("session", None):
            session_tiz = tiz.get("time_in_hr_zone")
            if tiz.get("reference_mesg") == "session":
                break
    if not session_tiz:
        return None
    z = list(session_tiz) + [0] * (6 - len(session_tiz))
    return {
        "easy_s": round(z[0] + z[1] + z[2], 1),   # below Z1 + Z1 + Z2
        "moderate_s": round(z[3], 1),
        "hard_s": round(z[4] + z[5], 1),
    }


# ------------------------------------------------------------ progression

# ------------------------------------------------------------ trend modeling

def linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Least-squares (slope, intercept), or None with <3 points."""
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if not denom:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return slope, my - slope * mx


def rolling_mean(values: list[float], window: int = 5) -> list[float]:
    """Trailing rolling average, same length as input."""
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1):i + 1]
        out.append(round(statistics.mean(chunk), 2))
    return out


def trend_series(points: list[dict], window: int = 5) -> dict:
    """Rolling average + linear-regression fit for [{'date','value'}...],
    plus the slope per 30 days. Dates are ISO 'YYYY-MM-DD'."""
    from datetime import date
    if len(points) < 3:
        return {"rolling": None, "fit": None, "slope_per_30d": None}
    xs = [date.fromisoformat(p["date"][:10]).toordinal() for p in points]
    ys = [p["value"] for p in points]
    reg = linear_regression([float(x) for x in xs], ys)
    fit = [round(reg[0] * x + reg[1], 2) for x in xs] if reg else None
    return {
        "rolling": rolling_mean(ys, window),
        "fit": fit,
        "slope_per_30d": round(reg[0] * 30, 2) if reg else None,
    }


def improvement_stats(points_hr: list[dict], points_pace: list[dict],
                      weekly: list[dict]) -> dict:
    """Yes/No fitness verdicts from the aerobic trend slopes and volume.

    - pace @ HR: improving iff pace drops (slope < -0.5 s/km per 30 days)
    - HR @ pace: improving iff HR drops (slope < -0.3 bpm per 30 days)
    - volume: last 4 full weeks vs the previous 4
    """
    hr_trend = trend_series(points_hr)
    pace_trend = trend_series(points_pace)

    def verdict(slope, threshold):
        if slope is None:
            return None
        return bool(slope < threshold)

    vol_change_pct = None
    if len(weekly) >= 2:
        kms = [w["km"] for w in weekly]
        recent = kms[-4:]
        previous = kms[-8:-4] or kms[:-len(recent)] or None
        if previous:
            prev_avg = statistics.mean(previous)
            if prev_avg:
                vol_change_pct = round(
                    100 * (statistics.mean(recent) - prev_avg) / prev_avg, 1)

    pace_at_hr_improving = verdict(hr_trend["slope_per_30d"], -0.5)
    hr_at_pace_improving = verdict(pace_trend["slope_per_30d"], -0.3)
    aerobic = [v for v in (pace_at_hr_improving, hr_at_pace_improving)
               if v is not None]
    return {
        "pace_at_hr_slope_30d": hr_trend["slope_per_30d"],       # s/km / 30d
        "pace_at_hr_improving": pace_at_hr_improving,
        "hr_at_pace_slope_30d": pace_trend["slope_per_30d"],     # bpm / 30d
        "hr_at_pace_improving": hr_at_pace_improving,
        "volume_change_pct": vol_change_pct,
        "volume_increasing": None if vol_change_pct is None else vol_change_pct > 0,
        "improving": any(aerobic) if aerobic else None,
    }


# ------------------------------------------------------------ clustering

CLUSTER_RUN_TOLERANCE = 0.10    # ±10% distance for easy/long runs
CLUSTER_TIME_TOLERANCE = 0.15   # work-time equivalence (3x12' ~ 4x10')


def session_signature(session: dict, segments: list[dict]) -> tuple:
    """('dist', (rep distances...)) / ('time', total work s) / ('run', km).

    Distance-rep sessions cluster by their rep-distance set regardless of
    count (6x1km groups with 8x1km); time-rep sessions by total work time;
    plain runs by total distance.
    """
    from . import intervals as _iv
    reps = [s for s in segments if s["kind"] == "rep" and not s["outlier"]]
    if not reps or (session.get("session_type") in ("easy", "long")):
        return ("run", session.get("total_distance_m") or 0)
    snaps = [_iv.snap_distance(r["distance_m"]) for r in reps]
    snapped = [s for s in snaps if s]
    if snapped and len(snapped) >= 0.8 * len(reps):
        return ("dist", tuple(sorted(set(snapped))))
    return ("time", sum(r["timer_s"] or 0 for r in reps))


def cluster_sessions(entries: list[dict]) -> list[dict]:
    """entries: [{'session': {...}, 'segments': [...]}]. Returns clusters
    of 'similar' sessions (fuzzy volume/time equivalence)."""
    tagged = []
    for e in entries:
        kind, value = session_signature(e["session"], e["segments"])
        tagged.append({"kind": kind, "value": value, **e})

    clusters: list[dict] = []

    dist_groups: dict[tuple, list] = {}
    for t in [t for t in tagged if t["kind"] == "dist"]:
        dist_groups.setdefault(t["value"], []).append(t)
    for reps_key, members in dist_groups.items():
        label = " + ".join(f"{d}m" for d in reps_key) + " reps"
        clusters.append({"kind": "dist", "label": label, "members": members})

    for kind, tol, fmt in (
        ("time", CLUSTER_TIME_TOLERANCE, lambda v: f"~{int(round(v / 60))}' work"),
        ("run", CLUSTER_RUN_TOLERANCE, lambda v: f"~{v / 1000:.0f} km run"),
    ):
        pool = sorted([t for t in tagged if t["kind"] == kind],
                      key=lambda t: t["value"])
        current: list = []
        for t in pool:
            if current and t["value"] > (1 + tol) * current[0]["value"]:
                clusters.append({"kind": kind,
                                 "label": fmt(statistics.median(c["value"] for c in current)),
                                 "members": current})
                current = []
            current.append(t)
        if current:
            clusters.append({"kind": kind,
                             "label": fmt(statistics.median(c["value"] for c in current)),
                             "members": current})

    for c in clusters:
        c["members"].sort(key=lambda m: m["session"].get("start_time_utc") or "")
    clusters.sort(key=lambda c: -len(c["members"]))
    return clusters


def _pace_cv(records: list[dict]) -> float | None:
    paces = [pace_s_per_km(r.get("enhanced_speed")) for r in records]
    paces = [p for p in paces if p]
    if len(paces) < 30:
        return None
    mean = statistics.mean(paces)
    if not mean:
        return None
    return statistics.pstdev(paces) / mean


def _has_pause_inside(start, end, pauses: list[tuple]) -> bool:
    return any(start < p_stop and p_start < end for p_stop, p_start in pauses)


def find_progression_candidates(segments: list[dict], records: list[dict],
                                pauses: list[tuple]) -> list[dict]:
    """Aerobic progression tracker candidates (spec 4.1).

    A block qualifies iff: >= 600 s timer time, no timer pause inside,
    pace CV <= 5% on the 1 Hz records, and it is a rep/tempo block or a
    continuous run segment — never a recovery, never stitched across
    recoveries. Candidates are proposed, not auto-added.
    """
    candidates = []

    def consider(start, end, timer_s, label):
        if timer_s is None or timer_s < PROGRESSION_MIN_TIMER_S:
            return
        if start is None or end is None or _has_pause_inside(start, end, pauses):
            return
        recs = _records_in(records, start, end)
        cv = _pace_cv(recs)
        if cv is None or cv > PROGRESSION_MAX_PACE_CV:
            return
        speeds = [r["enhanced_speed"] for r in recs if r.get("enhanced_speed")]
        hrs = [r["heart_rate"] for r in recs if r.get("heart_rate")]
        if not speeds or not hrs:
            return
        candidates.append({
            "start_time": start,
            "end_time": end,
            "timer_s": timer_s,
            "pace_s_km": round(pace_s_per_km(statistics.mean(speeds)), 1),
            "avg_hr": round(statistics.mean(hrs), 1),
            "pace_cv": round(cv, 4),
            "label": label,
        })

    reps = [s for s in segments if s["kind"] == "rep" and not s["outlier"]]
    for seg in reps:
        consider(seg["start_time"], seg["end_time"], seg["timer_s"],
                 f"rep {seg['rep_index']}")

    if not reps and records:
        # Continuous run: evaluate each pause-free stretch.
        bounds = [records[0]["timestamp"]]
        for p_stop, p_start in sorted(pauses):
            bounds += [p_stop, p_start]
        bounds.append(records[-1]["timestamp"])
        for i in range(0, len(bounds) - 1, 2):
            start, end = bounds[i], bounds[i + 1]
            timer_s = (end - start).total_seconds()
            consider(start, end, timer_s, "continuous block")

    return candidates
