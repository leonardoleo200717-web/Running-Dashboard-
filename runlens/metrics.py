"""KPIs and trends (spec section 4). Pure functions on plain data.

All computations use timer time, never elapsed time (spec 2.1): record
slices are taken between segment start/end and pauses are respected via
the pause list from ingest.timer_pauses().
"""

from __future__ import annotations

import math
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

def backfill_segment_hr(segments: list[dict], records: list[dict]) -> None:
    """Fill avg/max HR on segments that lack them (split-derived segments
    carry no HR) from the 1 Hz records in their window. In place."""
    for seg in segments:
        if seg.get("avg_hr") is not None or not seg.get("start_time") or not seg.get("end_time"):
            continue
        hrs = [r["heart_rate"] for r in _records_in(records, seg["start_time"], seg["end_time"])
               if r.get("heart_rate")]
        if hrs:
            seg["avg_hr"] = round(statistics.mean(hrs), 1)
            if seg.get("max_hr") is None:
                seg["max_hr"] = max(hrs)


# ------------------------------------------------------------ efficiency

REGRESSION_MIN_REPS = 4
REGRESSION_MIN_SPREAD_S_KM = 15.0
REGRESSION_MIN_R2 = 0.6
REI_COMPARE_PACE_TOL_S_KM = 10.0
REI_CHANGE_NOTABLE_PCT = 1.0
TYPICAL_PACE_COST_BPM = 5.0   # bpm per 10 s/km, threshold-range default


def session_efficiency(rep_efforts: list[dict]) -> dict:
    """Drift-aware REI summary for one session's work reps.

    Early-session REI (first ceil(n/3) valid reps) is the primary
    cross-session value: HR drifts upward across reps at constant pace,
    so a full-session mean conflates drift with efficiency."""
    valid = [e for e in rep_efforts if e.get("rei")]
    valid.sort(key=lambda e: e.get("rep_index") or 0)
    out = {"n_reps": len(rep_efforts), "n_hr_valid": len(valid),
           "rei_early": None, "rei_full": None, "domain": None,
           "median_pace_s_km": None}
    paces = [e["pace_s_km"] for e in rep_efforts if e.get("pace_s_km")]
    if paces:
        out["median_pace_s_km"] = round(statistics.median(paces), 1)
    if rep_efforts:
        domains = [e["domain"] for e in rep_efforts]
        out["domain"] = statistics.mode(domains)
    out["mean_hr"] = None
    if valid:
        early_n = math.ceil(len(valid) / 3)
        out["rei_early"] = round(statistics.mean(e["rei"] for e in valid[:early_n]), 5)
        out["rei_full"] = round(statistics.mean(e["rei"] for e in valid), 5)
        out["mean_hr"] = round(statistics.mean(e["hr_median"] for e in valid), 1)
    return out


def gated_regression(rep_efforts: list[dict], ref_hr: float,
                     ref_pace_s_km: float | None) -> dict:
    """Within-session HR<->pace regression, only when the session
    supports it (>= 4 valid same-domain reps, pace spread >= 15 s/km,
    R^2 >= 0.6). Otherwise: not computable — no silent garbage."""
    valid = [e for e in rep_efforts if e.get("rei")]
    out = {"valid": False, "reason": None, "pace_cost": None,
           "expected_hr_at_ref_pace": None, "expected_pace_at_ref_hr": None,
           "r2": None, "n": len(valid)}
    if len(valid) < REGRESSION_MIN_REPS:
        out["reason"] = f"needs ≥{REGRESSION_MIN_REPS} reps with stabilized HR (has {len(valid)})"
        return out
    if len({e["domain"] for e in valid}) > 1:
        out["reason"] = "reps span multiple effort domains"
        return out
    paces = [e["pace_s_km"] for e in valid]
    hrs = [e["hr_median"] for e in valid]
    spread = max(paces) - min(paces)
    if spread < REGRESSION_MIN_SPREAD_S_KM:
        out["reason"] = f"single-pace session (pace spread {spread:.0f} s/km < {REGRESSION_MIN_SPREAD_S_KM:.0f})"
        return out
    reg = linear_regression(paces, hrs)
    if reg is None:
        out["reason"] = "degenerate fit"
        return out
    slope, intercept = reg
    fitted = [slope * p + intercept for p in paces]
    ss_res = sum((h - f) ** 2 for h, f in zip(hrs, fitted))
    mean_hr = statistics.mean(hrs)
    ss_tot = sum((h - mean_hr) ** 2 for h in hrs)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    if r2 < REGRESSION_MIN_R2:
        out["reason"] = f"fit too noisy (R² {r2:.2f} < {REGRESSION_MIN_R2})"
        return out
    out.update({
        "valid": True,
        "r2": round(r2, 2),
        # slope is bpm per s/km; pace cost = ΔHR per 10 s/km faster
        "pace_cost": round(-slope * 10, 1),
    })
    if slope:
        out["expected_pace_at_ref_hr"] = round((ref_hr - intercept) / slope, 1)
    if ref_pace_s_km is not None:
        lo, hi = min(paces), max(paces)
        out["ref_pace_in_range"] = lo - 5 <= ref_pace_s_km <= hi + 5
        out["expected_hr_at_ref_pace"] = round(slope * ref_pace_s_km + intercept, 1)
    return out


def compare_sessions(cur: dict, prev: dict, cur_meta: dict, prev_meta: dict) -> dict:
    """The answer to "am I improving?" between two similar sessions.

    cur/prev are session_efficiency() outputs; *_meta carries date +
    hot flag. Returns verdict + evidence-carrying insights. Refuses to
    compare across domains or pace ranges (invalid comparison beats a
    wrong one)."""
    insights: list[str] = []
    verdict, delta_pct = "not_comparable", None
    explained = False  # a model-based branch already judged the pace gap

    if not cur["n_reps"] or not prev["n_reps"]:
        return {"verdict": verdict, "delta_pct": None,
                "insights": ["No work reps to compare."]}
    if cur["domain"] != prev["domain"]:
        return {"verdict": verdict, "delta_pct": None, "insights": [
            f"Different effort domains ({cur['domain']} vs {prev['domain']}) — "
            "efficiency comparison is not valid across domains."]}

    pace_d = None
    if cur["median_pace_s_km"] and prev["median_pace_s_km"]:
        pace_d = cur["median_pace_s_km"] - prev["median_pace_s_km"]  # <0 faster

    if cur["rei_early"] and prev["rei_early"]:
        if pace_d is not None and abs(pace_d) > REI_COMPARE_PACE_TOL_S_KM:
            # Paces too far apart for raw REI (speed/HR is not linear
            # across intensities). Judge with the expected pace cost
            # instead: ~5 bpm per 10 s/km at these intensities.
            hr_d = (cur["mean_hr"] or 0) - (prev["mean_hr"] or 0)
            expected_rise = (-pace_d / 10.0) * TYPICAL_PACE_COST_BPM
            margin = 2.0
            explained = True
            if pace_d < 0:  # current session faster
                if hr_d <= expected_rise - margin:
                    verdict = "improving"
                    insights.append(
                        f"Faster by {abs(pace_d):.0f} s/km with only "
                        f"{hr_d:+.0f} bpm (expected ~{expected_rise:+.0f} bpm "
                        f"at typical pace cost) — efficiency gain vs "
                        f"{prev_meta.get('date')}.")
                elif hr_d >= expected_rise + margin:
                    verdict = "similar"
                    insights.append(
                        f"Faster by {abs(pace_d):.0f} s/km but {hr_d:+.0f} bpm "
                        f"(more than the ~{expected_rise:+.0f} expected) — "
                        "mostly higher cardiovascular effort, not fitness.")
                else:
                    verdict = "similar"
                    insights.append(
                        f"Faster by {abs(pace_d):.0f} s/km at roughly the "
                        f"expected heart-rate cost ({hr_d:+.0f} bpm) — "
                        "consistent effort scaling, no clear efficiency change.")
            else:  # current session slower
                if hr_d <= expected_rise - margin:
                    verdict = "improving"
                    insights.append(
                        f"Slower by {pace_d:.0f} s/km but HR dropped "
                        f"{hr_d:+.0f} bpm (more than expected) — "
                        "lower cost at easier pace.")
                else:
                    verdict = "similar"
                    insights.append(
                        f"Slower session at proportionally lower effort "
                        f"({hr_d:+.0f} bpm) — no efficiency signal.")
        else:
            delta_pct = round(100 * (cur["rei_early"] - prev["rei_early"])
                              / prev["rei_early"], 1)
            if delta_pct >= REI_CHANGE_NOTABLE_PCT:
                verdict = "improving"
                insights.append(
                    f"Running efficiency improved {delta_pct:+.1f}% vs "
                    f"{prev_meta.get('date')} (early-session REI, "
                    f"n={cur['n_hr_valid']} vs {prev['n_hr_valid']} reps, "
                    f"{cur['domain']} domain).")
            elif delta_pct <= -REI_CHANGE_NOTABLE_PCT:
                verdict = "declining"
                insights.append(
                    f"Running efficiency decreased {delta_pct:+.1f}% vs "
                    f"{prev_meta.get('date')} (early-session REI, "
                    f"n={cur['n_hr_valid']} vs {prev['n_hr_valid']} reps).")
            else:
                verdict = "similar"
                insights.append(
                    f"Efficiency within noise of {prev_meta.get('date')} "
                    f"({delta_pct:+.1f}%).")

    # Plain-language pace/HR evidence (skipped when the expected-cost
    # model already explained the pace gap above).
    cur_hr = _mean_valid_hr(cur)
    prev_hr = _mean_valid_hr(prev)
    if not explained and pace_d is not None and cur_hr and prev_hr:
        hr_d = cur_hr - prev_hr
        if abs(pace_d) <= 5 and hr_d <= -3:
            insights.append(f"Same pace, {abs(hr_d):.0f} bpm lower heart rate.")
        elif abs(pace_d) <= 5 and hr_d >= 3:
            insights.append(f"Same pace, {hr_d:.0f} bpm higher heart rate.")
        elif pace_d <= -5 and hr_d >= 3 and (delta_pct is None or delta_pct < REI_CHANGE_NOTABLE_PCT):
            insights.append(
                f"Faster by {abs(pace_d):.0f} s/km but {hr_d:.0f} bpm higher — "
                "mostly explained by higher cardiovascular effort.")
        elif pace_d <= -5 and hr_d <= 0:
            insights.append(
                f"Faster by {abs(pace_d):.0f} s/km at no extra heart-rate cost.")

    if verdict == "not_comparable" and cur["n_hr_valid"] == 0:
        insights.append(
            "No stabilized HR available for these reps (< 90 s or flagged) — "
            "pace-only comparison: "
            f"{fmt_pace(prev['median_pace_s_km'])} → {fmt_pace(cur['median_pace_s_km'])}.")
        if pace_d is not None:
            verdict = "improving" if pace_d < -2 else ("declining" if pace_d > 2 else "similar")

    if cur_meta.get("hot") or prev_meta.get("hot"):
        insights.append("⚠ One of the sessions is heat-flagged — HR "
                        "comparison is confounded by temperature.")
    return {"verdict": verdict, "delta_pct": delta_pct, "insights": insights}


def _mean_valid_hr(eff: dict) -> float | None:
    return eff.get("mean_hr")


def cluster_reference_pace(all_rep_efforts: list[dict], domain: str,
                           days: int = 60) -> float | None:
    """Data-derived reference pace: rolling 60-day median rep pace of a
    domain (never an aspirational constant)."""
    paces = [e["pace_s_km"] for e in all_rep_efforts
             if e.get("domain") == domain and e.get("pace_s_km")
             and e.get("recent", True)]
    return round(statistics.median(paces), 1) if paces else None


# ------------------------------------------------------------ race prediction

BEST_EFFORT_DISTANCES_M = (1000, 1609, 3000, 5000, 10000)
RACE_DISTANCES_M = (("5K", 5000.0), ("10K", 10000.0),
                    ("Half marathon", 21097.5), ("Marathon", 42195.0))


def best_efforts(records: list[dict],
                 targets=BEST_EFFORT_DISTANCES_M) -> dict[int, float]:
    """Fastest time (s) covering each target distance, from cumulative
    1 Hz records. Elapsed-time based, so pauses only make it slower —
    never optimistic."""
    pts = [(r["timestamp"], r["distance"]) for r in records
           if r.get("distance") is not None and r.get("timestamp") is not None]
    out: dict[int, float] = {}
    for target in targets:
        best = None
        j = 0
        for i in range(len(pts)):
            while j < i and pts[i][1] - pts[j + 1][1] >= target:
                j += 1
            if pts[i][1] - pts[j][1] >= target:
                t = (pts[i][0] - pts[j][0]).total_seconds()
                if t > 0:
                    scaled = t * target / (pts[i][1] - pts[j][1])
                    best = min(best, scaled) if best else scaled
        if best:
            out[target] = round(best, 1)
    return out


def predict_riegel(t1: float, d1: float, d2: float) -> float:
    """Riegel (1981) endurance formula: T2 = T1 * (D2/D1)^1.06."""
    return t1 * (d2 / d1) ** 1.06


def predict_cameron(t1: float, d1: float, d2: float) -> float:
    """Dave Cameron's model (empirical fit on elite times), miles-based."""
    def f(miles: float) -> float:
        return 13.49681 - 0.048865 * miles + 2.438936 / (miles ** 0.7905)
    m1, m2 = d1 / 1609.344, d2 / 1609.344
    return (t1 / m1) * (f(m1) / f(m2)) * m2


def _vo2(v_m_per_min: float) -> float:
    return -4.60 + 0.182258 * v_m_per_min + 0.000104 * v_m_per_min ** 2


def _pct_vo2max(t_min: float) -> float:
    return (0.8 + 0.1894393 * math.exp(-0.012778 * t_min)
            + 0.2989558 * math.exp(-0.1932605 * t_min))


def vdot_from(t1: float, d1: float) -> float:
    """Daniels-Gilbert VDOT from a performance (t seconds, d meters)."""
    t_min = t1 / 60.0
    return _vo2(d1 / t_min) / _pct_vo2max(t_min)


def predict_vdot(t1: float, d1: float, d2: float) -> float:
    """Daniels-Gilbert: find the time at d2 whose implied VDOT matches."""
    vdot = vdot_from(t1, d1)
    lo, hi = 2.0, 600.0  # minutes
    for _ in range(60):
        mid = (lo + hi) / 2
        implied = _vo2(d2 / mid) / _pct_vo2max(mid)
        if implied > vdot:
            lo = mid      # implied effort too hard -> allow more time
        else:
            hi = mid
    return mid * 60.0


def race_predictions(t1: float, d1: float) -> list[dict]:
    """Predictions for the standard race distances from anchor (t1, d1)
    with the three classic models."""
    rows = []
    for label, d2 in RACE_DISTANCES_M:
        r = predict_riegel(t1, d1, d2)
        v = predict_vdot(t1, d1, d2)
        c = predict_cameron(t1, d1, d2)
        rows.append({
            "label": label, "distance_m": d2,
            "riegel_s": round(r), "vdot_s": round(v), "cameron_s": round(c),
            "mean_s": round((r + v + c) / 3),
        })
    return rows


def fmt_hms(seconds: float | None) -> str:
    if not seconds:
        return "–"
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


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
