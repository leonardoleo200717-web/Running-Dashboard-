"""Hierarchical interval engine (spec section 3).

Apply the first level that succeeds; every result records which level was
used (detection_source) and a confidence (high/medium/low).

  1. structured_workout  — wkt_step_index mapping, exact, high
  2. garmin_splits       — interval_* auto-splits, high/medium
  3. lap_inference       — junk-lap cleaning + pace/HR alternation, medium
  4. unclassified        — stub, low ("tag manually")

Manual overrides always win; they are applied in store/app, not here —
this module stays a pure function over the activity dict.

Result dict:
    {
      'detection_source': str,
      'confidence': 'high'|'medium'|'low',
      'segments': [segment, ...],     # ordered; kinds: warmup/rep/recovery/cooldown/other
      'structure': str | None,        # e.g. "8x400m R200m", "2x(10x1') R1' SR4'"
      'session_type': str,            # easy/long/intervals/fartlek/race/unknown
      'needs_manual_tag': bool,
    }

Segment dict:
    { 'kind', 'rep_index' (reps only), 'start_time', 'end_time',
      'timer_s', 'distance_m', 'avg_speed_ms', 'avg_hr', 'max_hr',
      'outlier': bool, 'source': str }
"""

from __future__ import annotations

import statistics
from collections import Counter

from . import origin as origin_mod

# --- junk laps (spec 2.6): double button presses ---
JUNK_MAX_DURATION_S = 2.0
JUNK_MAX_DISTANCE_M = 5.0

# --- normalization (spec 3.1) ---
CANONICAL_DISTANCES_M = (
    100, 150, 200, 300, 400, 500, 600, 800, 1000,
    1200, 1500, 1600, 2000, 3000, 5000, 10000,
)
# Spec said ±3%, but real files show GPS under-reading track reps by up
# to ~4% (288.8 m for a 300 m rep). 4% still never overlaps two canonical
# values. Recoveries are approximate jogs ("~200 m"), so their labels get
# a much looser snap.
SNAP_TOLERANCE = 0.04
RECOVERY_SNAP_TOLERANCE = 0.15

_INTENSITY_TO_KIND = {
    "warmup": "warmup",
    "active": "rep",
    "interval": "rep",
    "recovery": "recovery",
    "rest": "recovery",
    "cooldown": "cooldown",
}

_SPLIT_TYPE_TO_KIND = {
    "interval_warmup": "warmup",
    "interval_active": "rep",
    "interval_recovery": "recovery",
    "interval_cooldown": "cooldown",
}

# splits also contain rwd_run/rwd_walk/rwd_stand — never used for interval
# detection (spec 2.5).

OUTLIER_FACTOR = 1.5          # vs modal value within the set (spec 2.5)
DEGENERATE_COVERAGE = 0.85    # single active spanning ~whole session
SET_RECOVERY_FACTOR = 2.0     # recovery this much longer than modal = set recovery


# ---------------------------------------------------------------- helpers

def clean_laps(laps: list[dict]) -> list[dict]:
    """Drop junk laps (duration < 2 s or distance < 5 m) before analysis."""
    out = []
    for lap in laps:
        dur = lap.get("total_timer_time") or 0.0
        dist = lap.get("total_distance")
        if dur < JUNK_MAX_DURATION_S:
            continue
        if dist is not None and dist < JUNK_MAX_DISTANCE_M:
            continue
        out.append(lap)
    return out


def _lap_speed(lap: dict) -> float | None:
    """Never trust device averages blindly (spec 5.3): recompute from
    distance/timer when the stored average is missing."""
    for key in ("enhanced_avg_speed", "avg_speed"):
        v = lap.get(key)
        if v:
            return v
    dist, dur = lap.get("total_distance"), lap.get("total_timer_time")
    if dist and dur:
        return dist / dur
    return None


def _segment_from_lap(lap: dict, kind: str, source: str) -> dict:
    return {
        "kind": kind,
        "rep_index": None,
        "start_time": lap.get("start_time"),
        "end_time": lap.get("timestamp"),
        "timer_s": lap.get("total_timer_time"),
        "distance_m": lap.get("total_distance"),
        "avg_speed_ms": _lap_speed(lap),
        "avg_hr": lap.get("avg_heart_rate"),
        "max_hr": lap.get("max_heart_rate"),
        "outlier": False,
        "source": source,
    }


def _segment_from_split(split: dict, kind: str) -> dict:
    dist, dur = split.get("total_distance"), split.get("total_timer_time")
    speed = split.get("avg_speed") or (dist / dur if dist and dur else None)
    return {
        "kind": kind,
        "rep_index": None,
        "start_time": split.get("start_time"),
        "end_time": split.get("end_time"),
        "timer_s": dur,
        "distance_m": dist,
        "avg_speed_ms": speed,
        "avg_hr": None,   # splits carry no HR; filled from records by metrics
        "max_hr": None,
        "outlier": False,
        "source": "garmin_splits",
    }


def _number_reps(segments: list[dict]) -> None:
    i = 0
    for seg in segments:
        if seg["kind"] == "rep" and not seg["outlier"]:
            i += 1
            seg["rep_index"] = i


def _modal(values: list[float], bucket: float) -> float | None:
    """Modal value after bucketing (robust 'most common' for noisy floats)."""
    vals = [v for v in values if v]
    if not vals:
        return None
    buckets = Counter(round(v / bucket) for v in vals)
    top = buckets.most_common(1)[0][0]
    members = [v for v in vals if round(v / bucket) == top]
    return statistics.median(members)


# ------------------------------------------------------------ level 1

def _merge_lap_group(group: list[dict], kind: str) -> dict:
    """One workout-step execution can span several laps (autolap keeps
    firing inside steps longer than 1 km — verified on real files: a 15'
    tempo step arrives as 1000+1000+1000+664 m laps sharing one
    wkt_step_index). Merge them into a single segment."""
    if len(group) == 1:
        seg = _segment_from_lap(group[0], kind, "structured_workout")
        seg["wkt_step_index"] = group[0].get("wkt_step_index")
        return seg
    timer = sum(l.get("total_timer_time") or 0 for l in group)
    dist = sum(l.get("total_distance") or 0 for l in group)
    hr_weighted = [(l["avg_heart_rate"], l.get("total_timer_time") or 0)
                   for l in group if l.get("avg_heart_rate")]
    hr_t = sum(t for _, t in hr_weighted)
    max_hrs = [l["max_heart_rate"] for l in group if l.get("max_heart_rate")]
    return {
        "kind": kind,
        "rep_index": None,
        "start_time": group[0].get("start_time"),
        "end_time": group[-1].get("timestamp"),
        "timer_s": timer,
        "distance_m": dist,
        "avg_speed_ms": dist / timer if timer else None,
        "avg_hr": sum(h * t for h, t in hr_weighted) / hr_t if hr_t else None,
        "max_hr": max(max_hrs) if max_hrs else None,
        "outlier": False,
        "source": "structured_workout",
        "wkt_step_index": group[0].get("wkt_step_index"),
    }


def _level_structured(activity: dict, laps: list[dict]) -> dict | None:
    steps = {s.get("message_index"): s for s in activity.get("workout_steps", [])}
    if not steps:
        return None
    mapped = [l for l in laps if l.get("wkt_step_index") is not None]
    if not mapped:
        return None

    # Group consecutive laps sharing a wkt_step_index: same step execution.
    # Repeated steps (8x400: every rep is index 1) never merge because a
    # recovery lap with a different index always sits between them.
    groups: list[list[dict]] = []
    for lap in laps:
        idx = lap.get("wkt_step_index")
        if (groups and idx is not None
                and groups[-1][-1].get("wkt_step_index") == idx):
            groups[-1].append(lap)
        else:
            groups.append([lap])

    segments = []
    for group in groups:
        idx = group[0].get("wkt_step_index")
        step = steps.get(idx)
        if step is None:
            kind = "other"
        else:
            kind = _INTENSITY_TO_KIND.get(step.get("intensity"), "other")
        seg = _merge_lap_group(group, kind)
        # Structured steps say exactly how they're defined: a time-based
        # step must be labeled by time even if its meters happen to snap
        # to a canonical distance (a 12' block covering ~3000 m is "12'",
        # never "3000m").
        seg["duration_hint"] = step.get("duration_type") if step else None
        segments.append(seg)

    _number_reps(segments)
    return {
        "detection_source": "structured_workout",
        "confidence": "high",
        "segments": segments,
        "needs_manual_tag": False,
    }


# ------------------------------------------------------------ level 2

def _rep_like_laps(laps: list[dict], modal_dist: float | None,
                   modal_dur: float | None) -> list[dict]:
    """Cleaned laps that match the modal active-split profile. Distance is
    preferred as discriminator: time reps and recoveries can share identical
    durations (15x1' — spec 2.6), but their distances differ."""
    out = []
    for lap in laps:
        dist = lap.get("total_distance")
        dur = lap.get("total_timer_time")
        if modal_dist and dist is not None:
            if abs(dist - modal_dist) <= 0.15 * modal_dist:
                out.append(lap)
        elif modal_dur and dur is not None:
            if abs(dur - modal_dur) <= 0.10 * modal_dur:
                out.append(lap)
    return out


def _level_splits(activity: dict, laps: list[dict]) -> dict | None:
    interval_splits = [
        s for s in activity.get("splits", [])
        if s.get("split_type") in _SPLIT_TYPE_TO_KIND
    ]
    actives = [s for s in interval_splits if s["split_type"] == "interval_active"]
    if not actives:
        return None

    # Degenerate case: a single active spanning ~the whole session (long
    # runs produce this) — discard, fall through (spec 2.5).
    session_timer = (activity.get("session") or {}).get("total_timer_time")
    if len(actives) == 1 and session_timer:
        if (actives[0].get("total_timer_time") or 0) >= DEGENERATE_COVERAGE * session_timer:
            return None

    segments = [_segment_from_split(s, _SPLIT_TYPE_TO_KIND[s["split_type"]])
                for s in interval_splits]

    # Flag outlier actives: duration or distance > 1.5x modal within the set.
    active_segs = [s for s in segments if s["kind"] == "rep"]
    modal_dur = _modal([s["timer_s"] for s in active_segs], bucket=5.0)
    modal_dist = _modal([s["distance_m"] for s in active_segs], bucket=50.0)
    for seg in active_segs:
        if modal_dur and (seg["timer_s"] or 0) > OUTLIER_FACTOR * modal_dur:
            seg["outlier"] = True
        if modal_dist and (seg["distance_m"] or 0) > OUTLIER_FACTOR * modal_dist:
            seg["outlier"] = True

    clean_actives = [s for s in active_segs if not s["outlier"]]
    if not clean_actives:
        return None

    # Cross-validate count against cleaned laps (spec 2.5). More rep-like
    # laps than active splits means Garmin merged/lost reps (verified on
    # the 8x1000 file) — the laps are the richer source, fall through to
    # lap inference.
    modal_dur_c = _modal([s["timer_s"] for s in clean_actives], bucket=5.0)
    modal_dist_c = _modal([s["distance_m"] for s in clean_actives], bucket=50.0)
    rep_like = _rep_like_laps(laps, modal_dist_c, modal_dur_c)
    if len(rep_like) > len(clean_actives):
        return None
    # Splits that match (almost) no lap at all are equally suspect: on a
    # real 5x(1000+300) file Garmin merged rep+recovery+rep into single
    # ~1400 m actives that correspond to nothing the runner did. Only
    # trust the laps instead when there are enough of them to work with.
    if len(rep_like) < 0.5 * len(clean_actives) and len(laps) >= len(clean_actives):
        return None

    _number_reps(segments)
    return {
        "detection_source": "garmin_splits",
        "confidence": "high" if len(rep_like) == len(clean_actives) else "medium",
        "segments": segments,
        "needs_manual_tag": False,
    }


# ------------------------------------------------------------ level 3

def _alternates(kinds: list[str]) -> float:
    """Fraction of adjacent pairs that alternate."""
    if len(kinds) < 2:
        return 0.0
    flips = sum(1 for a, b in zip(kinds, kinds[1:]) if a != b)
    return flips / (len(kinds) - 1)


def _level_lap_inference(activity: dict, laps: list[dict]) -> dict | None:
    if not laps:
        return None

    manual = [l for l in laps if l.get("lap_trigger") == "manual"]

    # No manual-lap structure: a plain autolap run — valid "no intervals"
    # result, not a failure.
    if len(manual) < 4:
        segments = [_segment_from_lap(l, "other", "lap_inference") for l in laps]
        return {
            "detection_source": "lap_inference",
            "confidence": "medium",
            "segments": segments,
            "needs_manual_tag": False,
        }

    # Contiguous manual block = the interval set; discriminate rep vs
    # recovery by pace (never by duration — 15x1' has identical durations,
    # spec 2.6; never lap.intensity on free runs, spec 2.4).
    first_manual = laps.index(manual[0])
    last_manual = laps.index(manual[-1])
    block = laps[first_manual:last_manual + 1]

    speeds = [_lap_speed(l) for l in block]
    if any(s is None for s in speeds):
        hrs = [l.get("avg_heart_rate") for l in block]
        if any(h is None for h in hrs):
            return None  # can't discriminate -> level 4
        values = [float(h) for h in hrs]
    else:
        values = [float(s) for s in speeds]

    # Two-cluster split at the largest gap in sorted values.
    ordered = sorted(values)
    gaps = [(ordered[i + 1] - ordered[i], i) for i in range(len(ordered) - 1)]
    best_gap, gap_i = max(gaps)
    threshold = (ordered[gap_i] + ordered[gap_i + 1]) / 2
    spread = ordered[-1] - ordered[0]
    if spread <= 0 or best_gap < 0.3 * spread:
        # No clear fast/slow separation: treat as unstructured.
        segments = [_segment_from_lap(l, "other", "lap_inference") for l in laps]
        return {
            "detection_source": "lap_inference",
            "confidence": "medium",
            "segments": segments,
            "needs_manual_tag": False,
        }

    kinds_block = ["rep" if v > threshold else "recovery" for v in values]
    if _alternates(kinds_block) < 0.6:
        segments = [_segment_from_lap(l, "other", "lap_inference") for l in laps]
        return {
            "detection_source": "lap_inference",
            "confidence": "medium",
            "segments": segments,
            "needs_manual_tag": False,
        }

    kinds: dict[int, str] = {first_manual + k: kind
                             for k, kind in enumerate(kinds_block)}

    if all(s is not None for s in speeds):
        # The largest-gap threshold can land below warmup pace when the
        # session has three speed levels (recovery < warmup < rep — real
        # 8x1000 file: 6:00 / 4:50 / 3:45 per km). True reps are tightly
        # clustered, so demote "reps" clearly slower than the rep cluster.
        rep_speeds = sorted(v for v, k in zip(values, kinds_block) if k == "rep")
        rep_floor = 0.88 * rep_speeds[len(rep_speeds) // 2] if rep_speeds else None
        if rep_floor:
            for i in list(kinds):
                if kinds[i] == "rep" and _lap_speed(laps[i]) < rep_floor:
                    kinds[i] = "other"

        # Autolap and manual laps coexist inside the rep block (spec 2.6) —
        # extend over adjacent laps at true rep pace, so a
        # distance-triggered first/last rep isn't misread as warmup/cooldown.
        if rep_floor:
            i = first_manual - 1
            while i >= 0:
                v = _lap_speed(laps[i])
                if v is not None and v >= rep_floor:
                    kinds[i] = "rep"
                    i -= 1
                else:
                    break
            j = last_manual + 1
            while j < len(laps):
                v = _lap_speed(laps[j])
                if v is not None and v >= rep_floor:
                    kinds[j] = "rep"
                    j += 1
                else:
                    break

    lo, hi = min(kinds), max(kinds)
    segments = []
    for i, lap in enumerate(laps):
        if i < lo:
            kind = "warmup"      # leading autolap block at easy pace
        elif i > hi:
            kind = "cooldown"    # trailing block
        else:
            kind = kinds.get(i, "other")
        segments.append(_segment_from_lap(lap, kind, "lap_inference"))

    # A trailing "recovery" after the last rep is really cooldown.
    for seg in reversed(segments):
        if seg["kind"] == "rep":
            break
        if seg["kind"] == "recovery":
            seg["kind"] = "cooldown"

    _number_reps(segments)
    return {
        "detection_source": "lap_inference",
        "confidence": "medium",
        "segments": segments,
        "needs_manual_tag": False,
    }


# ------------------------------------------------------------ level 4

def _level_unclassified(activity: dict) -> dict:
    """MVP stub (spec 3, level 4): record-level change-point detection is
    deferred; sessions landing here are flagged for manual tagging."""
    return {
        "detection_source": "unclassified",
        "confidence": "low",
        "segments": [],
        "needs_manual_tag": True,
    }


# ------------------------------------------------------------ entry point

def resolve(activity: dict) -> dict:
    """Run the hierarchical engine over a normalized activity dict."""
    info = origin_mod.detect_origin(activity)
    laps = clean_laps(activity.get("laps", []))

    result = None
    if info["origin"] == origin_mod.STRUCTURED:
        result = _level_structured(activity, laps)
    if result is None:
        result = _level_splits(activity, laps)
    if result is None:
        result = _level_lap_inference(activity, laps)
    if result is None:
        result = _level_unclassified(activity)

    result["origin"] = info["origin"]
    result["wkt_name"] = info["wkt_name"]
    result["structure"] = structure_string(result["segments"])
    result["session_type"] = classify_session(activity, result)
    return result


# ------------------------------------------------------------ normalization

def snap_distance(meters: float | None, tolerance: float = SNAP_TOLERANCE) -> int | None:
    """Snap to a canonical rep distance when within tolerance (398 -> 400)."""
    if not meters:
        return None
    for canon in CANONICAL_DISTANCES_M:
        if abs(meters - canon) / canon <= tolerance:
            return canon
    return None


def _fmt_seconds(seconds: float) -> str:
    """60 -> 1', 90 -> 1'30". Stored values stay exact (spec 3.1); for
    display only, measurement noise is absorbed by snapping to the nearest
    5 s when within 2 s (a rep timed at 61.4 s was programmed as 1')."""
    s = int(round(seconds))
    nearest5 = 5 * round(seconds / 5)
    if abs(seconds - nearest5) <= 2:
        s = int(nearest5)
    minutes, rem = divmod(s, 60)
    if minutes and rem:
        return f"{minutes}'{rem:02d}\""
    if minutes:
        return f"{minutes}'"
    return f"{rem}\""


def _seg_snap(seg: dict) -> int | None:
    """Canonical distance for labeling — suppressed for segments known to
    be time-defined (structured duration_type hint)."""
    if seg.get("duration_hint") == "time":
        return None
    return snap_distance(seg["distance_m"])


def _rep_label(group: list[dict]) -> str:
    """Label one run of consecutive same-shaped reps. A group of one gets
    no count prefix: '15'' reads better than '1x15''."""
    n = f"{len(group)}x" if len(group) > 1 else ""
    snaps = [_seg_snap(s) for s in group]
    if all(snaps) and len(set(snaps)) == 1:
        return f"{n}{snaps[0]}m"
    durations = [s["timer_s"] for s in group if s["timer_s"]]
    if durations:
        modal = _modal(durations, bucket=5.0)
        if modal and all(abs(d - modal) <= max(3, 0.05 * modal) for d in durations):
            return f"{n}{_fmt_seconds(modal)}"
    # heterogeneous group: label reps individually
    parts = []
    for s in group:
        snap = _seg_snap(s)
        parts.append(f"{snap}m" if snap else _fmt_seconds(s["timer_s"] or 0))
    return " + ".join(parts)


def _cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.mean(values)
    return statistics.pstdev(values) / mean if mean else 0.0


def _recovery_label(recoveries: list[dict], prefix: str = "R") -> str | None:
    """Recoveries are approximate: label by whichever dimension the runner
    actually kept constant. 202-212 m jogged in 70-86 s is "R200m"; 114-176 m
    jogged in 59-62 s is "R1'". Structured steps say which explicitly."""
    if not recoveries:
        return None
    hints = {s.get("duration_hint") for s in recoveries}
    durs = [s["timer_s"] for s in recoveries if s["timer_s"]]
    dists = [s["distance_m"] for s in recoveries if s["distance_m"]]

    if "distance" in hints:
        prefer_dist = True
    elif "time" in hints:
        prefer_dist = False
    else:
        prefer_dist = bool(dists) and (not durs or _cv(dists) < _cv(durs))

    if prefer_dist and dists:
        snap = snap_distance(statistics.median(dists), RECOVERY_SNAP_TOLERANCE)
        if snap:
            return f"{prefix}{snap}m"
    if durs:
        modal = _modal(durs, bucket=5.0)
        if modal:
            return f"{prefix}{_fmt_seconds(modal)}"
    if dists:
        snap = snap_distance(statistics.median(dists), RECOVERY_SNAP_TOLERANCE)
        if snap:
            return f"{prefix}{snap}m"
    return None


def _group_reps(reps: list[dict]) -> list[list[dict]]:
    """Split an ordered rep list into runs of same-shaped reps."""
    groups: list[list[dict]] = []
    for rep in reps:
        if groups:
            prev = groups[-1][-1]
            same_dist = (_seg_snap(rep) is not None
                         and _seg_snap(rep) == _seg_snap(prev))
            # Duration similarity groups on its own: reps measured
            # 276-315 m / 59-62 s (real 15x1' file) must not fragment just
            # because some distances happen to snap and others don't.
            same_time = (rep["timer_s"] and prev["timer_s"]
                         and abs(rep["timer_s"] - prev["timer_s"]) <= max(3, 0.05 * prev["timer_s"]))
            if same_dist or same_time:
                groups[-1].append(rep)
                continue
        groups.append([rep])
    return groups


def structure_string(segments: list[dict]) -> str | None:
    """Canonical structure string: '8x400m R200m', '15x1\\' R1\\'',
    '2x(10x1\\') R1\\' SR4\\''."""
    reps = [s for s in segments if s["kind"] == "rep" and not s["outlier"]]
    if not reps:
        return None

    # Recoveries strictly between two reps.
    order = [s for s in segments if s["kind"] in ("rep", "recovery") and not s["outlier"]]
    inner: list[dict] = []
    for i, seg in enumerate(order):
        if seg["kind"] != "recovery":
            continue
        before = any(s["kind"] == "rep" for s in order[:i])
        after = any(s["kind"] == "rep" for s in order[i + 1:])
        if before and after:
            inner.append(seg)

    # Set recoveries: split recovery durations at their largest gap. A
    # modal-bucket rule fails when set recoveries outnumber any single
    # bucket of jogged short recoveries (real 5x(1000+300) file: four
    # ~156 s set recoveries vs 100 m jogs spread over 31-41 s).
    set_recs: list[dict] = []
    normal_recs = list(inner)
    if len(inner) >= 2:
        ds = sorted((r["timer_s"] or 0) for r in inner)
        gap, gap_i = max((ds[i + 1] - ds[i], i) for i in range(len(ds) - 1))
        short_median = statistics.median(ds[:gap_i + 1])
        if gap >= max(30, short_median):
            cut = (ds[gap_i] + ds[gap_i + 1]) / 2
            set_recs = [r for r in inner if (r["timer_s"] or 0) > cut]
            normal_recs = [r for r in inner if (r["timer_s"] or 0) <= cut]

    if set_recs:
        # Split reps into sets at each set recovery.
        sets: list[list[dict]] = [[]]
        for seg in order:
            if seg in set_recs:
                sets.append([])
            elif seg["kind"] == "rep":
                sets[-1].append(seg)
        sets = [s for s in sets if s]
        labels = {_rep_label(s) for s in sets}
        if len(sets) > 1 and len(labels) == 1:
            base = f"{len(sets)}x({labels.pop()})"
            r = _recovery_label(normal_recs, "R")
            sr = _recovery_label(set_recs, "SR")
            return " ".join(p for p in (base, r, sr) if p)

    groups = _group_reps(reps)
    body = " + ".join(_rep_label(g) for g in groups)
    r = _recovery_label(normal_recs, "R")
    return f"{body} {r}" if r else body


# ------------------------------------------------------------ classification

LONG_RUN_MIN_M = 15000
LONG_RUN_MIN_S = 80 * 60


def classify_session(activity: dict, result: dict) -> str:
    """easy / long / intervals / fartlek / race / unknown (spec 3.2).
    Always user-overridable; overrides are applied at the store layer."""
    session = activity.get("session") or {}
    if session.get("sub_sport") == "race":
        return "race"

    reps = [s for s in result["segments"] if s["kind"] == "rep" and not s["outlier"]]
    if reps:
        # Fartlek: interval-ish but irregular (no clean structure string groups)
        if result.get("detection_source") == "lap_inference":
            snaps = [snap_distance(r["distance_m"]) for r in reps]
            durs = [r["timer_s"] for r in reps if r["timer_s"]]
            modal = _modal(durs, bucket=5.0)
            regular_time = modal and all(abs(d - modal) <= max(5, 0.1 * modal) for d in durs)
            regular_dist = all(snaps) and len(set(snaps)) <= 3
            if not regular_time and not regular_dist:
                return "fartlek"
        return "intervals"

    if result["detection_source"] == "unclassified":
        return "unknown"

    dist = session.get("total_distance") or 0
    dur = session.get("total_timer_time") or 0
    if dist >= LONG_RUN_MIN_M or dur >= LONG_RUN_MIN_S:
        return "long"
    return "easy"
