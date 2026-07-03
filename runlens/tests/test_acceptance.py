"""Acceptance tests for the validation set (spec 5.4).

A change that breaks any expected structure fails CI.
"""

import pytest

from runlens import intervals
from runlens.tests import fixtures


def reps_of(result):
    return [s for s in result["segments"] if s["kind"] == "rep" and not s["outlier"]]


# --- 2026-07-02: 8x400 + 6x300 + 4x200, R200m — structured, exact ---

def test_structured_400_300_200():
    result = intervals.resolve(fixtures.structured_400_300_200())
    assert result["origin"] == "structured"
    assert result["detection_source"] == "structured_workout"
    assert result["confidence"] == "high"
    reps = reps_of(result)
    assert len(reps) == 18
    snapped = [intervals.snap_distance(r["distance_m"]) for r in reps]
    assert snapped == [400] * 8 + [300] * 6 + [200] * 4
    assert result["structure"] == "8x400m + 6x300m + 4x200m R200m"
    assert result["session_type"] == "intervals"


# --- 2026-07-01: easy run, no intervals ---

def test_easy_run():
    result = intervals.resolve(fixtures.easy_run())
    assert result["origin"] == "free"
    assert reps_of(result) == []
    assert result["session_type"] == "easy"
    assert result["structure"] is None


# --- 2026-06-30: 8x1000 R~200m — free, mixed autolap+manual, junk laps ---

def test_free_8x1000():
    activity = fixtures.free_8x1000()
    result = intervals.resolve(activity)
    assert result["origin"] == "free"
    # Garmin splits lost a rep (7 actives, first anomalous) — the engine
    # must not settle for them; laps carry the true structure.
    reps = reps_of(result)
    assert len(reps) == 8
    assert all(intervals.snap_distance(r["distance_m"]) == 1000 for r in reps)
    assert result["structure"] == "8x1000m R200m"
    assert result["session_type"] == "intervals"
    # Junk laps removed before analysis.
    cleaned = intervals.clean_laps(activity["laps"])
    assert len(cleaned) == len(activity["laps"]) - 2


# --- 2026-06-27: long run — degenerate split discarded, no intervals ---

def test_long_run():
    result = intervals.resolve(fixtures.long_run())
    assert result["origin"] == "free"
    assert reps_of(result) == []
    assert result["detection_source"] != "garmin_splits"  # degenerate discarded
    assert result["session_type"] == "long"


# --- 2026-06-25: 15x1' — rep/recovery by pace alternation ---

def test_free_15x1():
    result = intervals.resolve(fixtures.free_15x1())
    assert result["origin"] == "free"
    reps = reps_of(result)
    assert len(reps) == 15
    # Reps and recoveries have identical 60s durations: discrimination must
    # come from pace, and every rep must be the fast lap.
    assert all(r["timer_s"] == pytest.approx(60) for r in reps)
    assert all(r["avg_speed_ms"] > 4 for r in reps)
    assert result["structure"] == "15x1' R1'"
    assert result["session_type"] == "intervals"


# --- 2026-06-20: 6x5' — structured ---

def test_structured_6x5():
    result = intervals.resolve(fixtures.structured_6x5())
    assert result["detection_source"] == "structured_workout"
    assert result["confidence"] == "high"
    reps = reps_of(result)
    assert len(reps) == 6
    assert all(r["timer_s"] == pytest.approx(300) for r in reps)
    assert result["structure"] == "6x5' R1'30\""
    assert result["session_type"] == "intervals"


# --- 2026-05-24: 2x(10x1') R1' SR4' — 4' block must NOT count as rep ---

def test_structured_2x10x1():
    result = intervals.resolve(fixtures.structured_2x10x1())
    assert result["detection_source"] == "structured_workout"
    reps = reps_of(result)
    assert len(reps) == 20
    # The 4' inter-block segment is a recovery, never a rep.
    four_min = [s for s in result["segments"]
                if s["timer_s"] == pytest.approx(240)]
    assert four_min and all(s["kind"] == "recovery" for s in four_min)
    assert result["structure"] == "2x(10x1') R1' SR4'"
    assert result["session_type"] == "intervals"


# --- real-file regression: autolap splits laps INSIDE long workout steps ---

def test_structured_step_spanning_multiple_autolaps():
    """Verified on a real FR265 file ("15' 10' 8'"): a 15' tempo step
    arrives as 1000+1000+1000+664 m laps sharing one wkt_step_index.
    They must merge into a single rep, not count as four."""
    from runlens.tests.fixtures import Builder
    from datetime import datetime, timezone

    b = Builder(datetime(2026, 5, 17, 8, 11, tzinfo=timezone.utc),
                structured=True, wkt_name="15' 10' 8'")
    b.step(duration_type="open", intensity="warmup")                    # 0
    b.step(duration_type="time", duration_time=900, intensity="active") # 1
    b.step(duration_type="time", duration_time=180, intensity="recovery")  # 2
    b.step(duration_type="time", duration_time=600, intensity="active") # 3
    b.step(duration_type="time", duration_time=180, intensity="recovery")  # 4
    b.step(duration_type="time", duration_time=480, intensity="active") # 5
    b.step(duration_type="open", intensity="cooldown")                  # 6

    b.lap(1200, 3400, hr=140, trigger="manual", wkt_step_index=0)
    for dist, dur in ((1000, 244), (1000, 245), (1000, 248), (664, 163)):
        b.lap(dur, dist, hr=175, trigger="distance", wkt_step_index=1)
    b.lap(180, 500, hr=150, trigger="manual", wkt_step_index=2)
    for dist, dur in ((1000, 239), (1000, 243), (496, 118)):
        b.lap(dur, dist, hr=178, trigger="distance", wkt_step_index=3)
    b.lap(180, 490, hr=152, trigger="manual", wkt_step_index=4)
    for dist, dur in ((1000, 234), (1000, 235), (45, 11)):
        b.lap(dur, dist, hr=181, trigger="distance", wkt_step_index=5)
    b.lap(900, 2600, hr=142, trigger="manual", wkt_step_index=6)
    activity = b.done()

    result = intervals.resolve(activity)
    assert result["detection_source"] == "structured_workout"
    reps = reps_of(result)
    assert len(reps) == 3
    assert [round(r["timer_s"]) for r in reps] == [900, 600, 480]
    assert result["structure"] == "15' + 10' + 8' R3'"


# --- real-file regressions (sessions of 2026-06-30, 06-25, 06-02) ---

def _free_builder(day):
    from runlens.tests.fixtures import Builder
    from datetime import datetime, timezone
    return Builder(datetime(2026, 6, day, 19, 0, tzinfo=timezone.utc))


def test_warmup_pace_between_recovery_and_rep_pace():
    """Real 8x1000 file: warmup autolaps at 4:50/km sit between recovery
    (6:00+) and rep (3:45) pace. They must not be swallowed as reps by the
    fast/slow threshold, and the distance-triggered reps adjacent to the
    manual block must still be picked up."""
    b = _free_builder(30)
    b.lap(297, 1000, hr=132, trigger="distance")   # warmup ~4:57/km
    b.lap(288, 1000, hr=139, trigger="distance")   # warmup ~4:48/km
    b.lap(226, 829, hr=142, trigger="manual")      # pre-rep manual, 4:32/km
    b.lap(224, 1000, hr=162, trigger="distance")   # rep 1 (autolap!)
    recs = ((75, 209), (83, 202), (86, 212), (70, 204), (81, 206), (79, 210), (75, 211))
    for (rd, rm), d in zip(recs, (231, 226, 227, 226, 224, 228, 215)):
        b.lap(rd, rm, hr=150, trigger="manual")    # ~200 m jog, varying time
        b.lap(d, 1000, hr=172, trigger="manual" if d % 2 else "distance")
    b.lap(360, 1000, hr=142, trigger="distance")   # cooldown 6:00/km
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert len(reps) == 8
    assert result["structure"] == "8x1000m R200m"
    assert result["session_type"] == "intervals"


def test_composite_sets_with_short_and_set_recoveries():
    """Real 5x(1000 p100 + 300) p400 file: Garmin merged whole sets into
    single ~1400 m actives; splits must be rejected (they match no lap) and
    lap inference must recover the set structure."""
    b = _free_builder(2)
    for _ in range(4):
        b.lap(295, 1000, hr=130, trigger="distance")   # warmup
    short_recs = ((31.2, 104.9), (33.7, 96.0), (38.3, 112.2), (35.4, 109.0), (41.3, 112.4))
    set_recs = ((155.7, 422.3), (157.0, 415.7), (153.3, 416.5), (156.9, 421.8))
    for i in range(5):
        b.lap(222, 1000, hr=170, trigger="manual")     # 1000 rep
        b.lap(*short_recs[i], hr=170, trigger="manual")        # ~100 m rec
        b.lap(63, 289 + 3 * i, hr=176, trigger="manual")       # 300 rep
        if i < 4:
            b.lap(*set_recs[i], hr=145, trigger="manual")      # ~400 m set rec
    b.lap(343, 1000, hr=139, trigger="distance")       # cooldown
    # Garmin's broken merged splits.
    b.split("interval_warmup", 1180, 4000, start=b.start)
    for _ in range(3):
        b.split("interval_active", 318, 1395)
        b.split("interval_recovery", 155, 418)
    b.split("interval_active", 318, 1395)
    result = intervals.resolve(b.done())
    assert result["detection_source"] == "lap_inference"
    reps = reps_of(result)
    assert len(reps) == 10
    assert result["structure"] == "5x(1000m + 300m) R100m SR400m"


def test_time_labels_absorb_measurement_noise():
    """Real 15x1' file: reps timed 59.1-62.2 s must all read as 1', and
    inconsistent distance snapping must not fragment the group."""
    b = _free_builder(25)
    b.lap(300, 1000, hr=140, trigger="distance")
    durs = [60.6, 60.5, 60.1, 62.0, 62.2, 59.4, 60.1, 60.5,
            60.1, 59.6, 61.0, 61.0, 61.4, 60.0, 59.1]
    dists = [284.8, 278.5, 276.8, 286.8, 300.3, 279.9, 299.5, 290.3,
             276.4, 292.6, 302.5, 314.7, 307.0, 290.0, 287.7]
    for dur, dist in zip(durs, dists):
        b.lap(dur, dist, hr=170, trigger="manual")
        b.lap(60.4, 150, hr=155, trigger="manual")
    b.lap(340, 1000, hr=145, trigger="distance")
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert len(reps) == 15
    assert result["structure"] == "15x1' R1'"


# --- every session records provenance ---

@pytest.mark.parametrize("name,build", sorted(fixtures.ALL.items()))
def test_provenance_always_recorded(name, build):
    result = intervals.resolve(build())
    assert result["detection_source"] in (
        "structured_workout", "garmin_splits", "lap_inference", "unclassified")
    assert result["confidence"] in ("high", "medium", "low")
