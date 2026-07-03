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


# --- every session records provenance ---

@pytest.mark.parametrize("name,build", sorted(fixtures.ALL.items()))
def test_provenance_always_recorded(name, build):
    result = intervals.resolve(build())
    assert result["detection_source"] in (
        "structured_workout", "garmin_splits", "lap_inference", "unclassified")
    assert result["confidence"] in ("high", "medium", "low")
