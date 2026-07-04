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
    rec_dists = [173.5, 176.4, 163.6, 159.8, 171.3, 159.9, 170.0, 124.6,
                 148.8, 127.1, 133.6, 134.8, 122.8, 114.2, 150.0]
    for i, (dur, dist) in enumerate(zip(durs, dists)):
        b.lap(dur, dist, hr=170, trigger="manual")
        b.lap(60.4, rec_dists[i], hr=155, trigger="manual")
    b.lap(340, 1000, hr=145, trigger="distance")
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert len(reps) == 15
    assert result["structure"] == "15x1' R1'"


# --- real-file regressions (sessions of 2026-05-26, 04-02, 04-23, 04-26) ---

def test_nested_sets_with_botched_first_rep():
    """Real 5x(400+300+200) file: rep 1 was pressed late (644 m at slower
    pace, demoted); the remaining 4 complete + 1 truncated sets must still
    read as 5 sets."""
    b = _free_builder(26)
    b.lap(300, 1000, hr=132, trigger="distance")
    b.lap(295, 1000, hr=142, trigger="distance")
    b.lap(155, 645, hr=154, trigger="manual")          # botched first 400
    first = True
    for i in range(5):
        if not first:
            b.lap(84 - i, 400 + 3 * i, hr=170, trigger="manual")
        first = False
        b.lap(46, 105, hr=160, trigger="manual")       # p100
        b.lap(60 - i, 300 + 2 * i, hr=170, trigger="manual")
        b.lap(50, 103, hr=163, trigger="manual")       # p100
        b.lap(38, 204, hr=172, trigger="manual")
        if i < 4:
            b.lap(170, 420, hr=145, trigger="manual")  # p400 set recovery
    b.lap(310, 1000, hr=150, trigger="distance")
    result = intervals.resolve(b.done())
    assert result["structure"] == "5x(400m + 300m + 200m) R100m SR400m"
    assert result["session_type"] == "intervals"


def test_short_first_rep_and_sub5s_junk():
    """Real 6x1000 file: rep 1 measured 901 m / 206 s (late press) and a
    2.6 s / 8.6 m double-press lap sits mid-block. Junk must go, rep 1
    must group with its 1000 m neighbours."""
    b = _free_builder(2)
    for d in (293, 298, 299, 267):
        b.lap(d, 1000, hr=150, trigger="distance")     # warmup (last one brisk)
    b.lap(206.5, 901.5, hr=172, trigger="manual")      # rep 1, short
    b.lap(79.8, 214.5, hr=148, trigger="manual")
    b.lap(230.7, 1000, hr=173, trigger="distance")
    b.lap(2.6, 8.6, hr=178, trigger="manual")          # double press
    recs = ((89.2, 208.4), (93.0, 220.1), (81.5, 211.2), (89.4, 215.1))
    for (rd, rm), d in zip(recs, (226.9, 227.3, 229.5, 229.4)):
        b.lap(rd, rm, hr=151, trigger="manual")
        b.lap(d, 1000 if d != 229.4 else 999.6, hr=173, trigger="distance")
    b.lap(321, 1000, hr=138, trigger="distance")
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert len(reps) == 6
    assert result["structure"] == "6x1000m R200m"
    assert result["session_type"] == "intervals"


def test_strides_do_not_make_an_easy_run_intervals():
    """Real easy-75'-plus-5x100m file: strides are detected and labeled,
    but 115 s of reps in an 80' run is still an easy run."""
    b = _free_builder(23)
    for _ in range(11):
        b.lap(286, 1000, hr=150, trigger="distance")
    b.lap(101, 344, hr=155, trigger="manual")
    for i in range(5):
        b.lap(22 + 0.5 * i, 104 + i, hr=168, trigger="manual")   # stride
        b.lap(28.5, 105, hr=171, trigger="manual")               # float back
    for _ in range(3):
        b.lap(286, 1000, hr=156, trigger="distance")
    result = intervals.resolve(b.done())
    assert result["structure"] == "5x100m R100m"
    assert result["session_type"] == "easy"


def test_user_created_workout_warmup_cooldown_marked_active():
    """Real 5x1.5km p3' file: user-created workouts carry
    intensity='active' on the open warmup/cooldown steps, and autolap
    splits each 1500 m step into 1000+500 laps."""
    from runlens.tests.fixtures import Builder
    from datetime import datetime, timezone
    b = Builder(datetime(2026, 4, 26, 8, 48, tzinfo=timezone.utc),
                structured=True, wkt_name="5x1.5km p3'")
    b.step(duration_type="open", intensity="active", target_type="open")     # warmup!
    b.step(duration_type="distance", duration_distance=1500, intensity="active",
           target_type="speed")
    b.step(duration_type="time", duration_time=180, intensity="recovery")
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=5)
    b.step(duration_type="open", intensity="active", target_type="open")     # cooldown!
    for _ in range(4):
        b.lap(280, 1000, hr=155, trigger="distance", wkt_step_index=0)
    for _ in range(5):
        b.lap(226, 1000, hr=185, trigger="distance", wkt_step_index=1)
        b.lap(112, 500, hr=188, trigger="distance", wkt_step_index=1)
        b.lap(180, 600, hr=168, trigger="time", wkt_step_index=2)
    for _ in range(5):
        b.lap(275, 1000, hr=162, trigger="distance", wkt_step_index=4)
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert len(reps) == 5
    assert all(r["distance_m"] == pytest.approx(1500) for r in reps)
    assert result["structure"] == "5x1500m R3'"
    kinds = [s["kind"] for s in result["segments"]]
    assert kinds[0] == "warmup" and kinds[-1] == "cooldown"


# --- real-file regressions (sessions of 2026-06-04, 02-22) ---

def test_structured_step_tail_laps_are_not_junk():
    """Real 3x8' file: autolap fires just before a time step ends, leaving
    2-4 s tail laps that carry the step index. They must merge back into
    the step (dropping them read 3x8' as 3x7'58")."""
    from runlens.tests.fixtures import Builder
    from datetime import datetime, timezone
    b = Builder(datetime(2026, 6, 4, 16, 20, tzinfo=timezone.utc),
                structured=True, wkt_name="3x8'")
    b.step(duration_type="open", intensity="warmup")
    b.step(duration_type="time", duration_time=480, intensity="active")
    b.step(duration_type="time", duration_time=120, intensity="recovery")
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=3)
    b.step(duration_type="open", intensity="cooldown")
    b.lap(900, 3100, hr=145, trigger="manual", wkt_step_index=0)
    for d1, d2, tail in ((238.7, 237.2, 4.1), (239.8, 238.2, 2.0),
                         (238.4, 239.2, 2.4)):
        b.lap(d1, 1000, hr=175, trigger="distance", wkt_step_index=1)
        b.lap(d2, 1000, hr=180, trigger="distance", wkt_step_index=1)
        b.lap(tail, tail * 4, hr=182, trigger="time", wkt_step_index=1)
        b.lap(120, 350, hr=160, trigger="time", wkt_step_index=2)
    b.lap(600, 2000, hr=150, trigger="manual", wkt_step_index=4)
    result = intervals.resolve(b.done())
    reps = reps_of(result)
    assert [round(r["timer_s"]) for r in reps] == [480, 480, 480]
    assert result["structure"] == "3x8' R2'"


def test_suggested_workout_warmup_has_slower_target_band():
    """Real 'Training some speed' files: Garmin suggested workouts type
    the warmup as a distance step with intensity 'active' but an easy
    target band strictly below the rep targets."""
    from runlens.tests.fixtures import Builder
    from datetime import datetime, timezone
    b = Builder(datetime(2026, 2, 22, 16, 6, tzinfo=timezone.utc),
                structured=True, wkt_name="Training some speed")
    b.step(duration_type="distance", duration_distance=4000, intensity="active",
           target_type="speed", custom_target_speed_low=3.3,
           custom_target_speed_high=3.448)                       # warmup!
    b.step(duration_type="distance", duration_distance=1500, intensity="active",
           target_type="speed", custom_target_speed_low=3.774,
           custom_target_speed_high=4.0)
    b.step(duration_type="distance", duration_distance=500, intensity="recovery",
           target_type="speed")
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=6)
    b.step(duration_type="open", intensity="active", target_type="speed")  # cooldown
    for _ in range(4):
        b.lap(290, 1000, hr=143, trigger="distance", wkt_step_index=0)
    for _ in range(6):
        b.lap(247, 1000, hr=160, trigger="distance", wkt_step_index=1)
        b.lap(123, 500, hr=165, trigger="distance", wkt_step_index=1)
        b.lap(148, 500, hr=155, trigger="distance", wkt_step_index=2)
    b.lap(176, 609, hr=155, trigger="distance", wkt_step_index=4)
    result = intervals.resolve(b.done())
    assert result["segments"][0]["kind"] == "warmup"
    assert result["segments"][-1]["kind"] == "cooldown"
    reps = reps_of(result)
    assert len(reps) == 6
    assert result["structure"] == "6x1500m R500m"


# --- race prediction models ---

def test_race_prediction_models_sane():
    from runlens import metrics
    # Anchor: 10 km in 41:21 (2481 s). Predicting the anchor distance
    # must return ~the anchor time for every model.
    for model in (metrics.predict_riegel, metrics.predict_vdot,
                  metrics.predict_cameron):
        assert model(2481, 10000, 10000) == pytest.approx(2481, rel=0.01)
    # 5K must be faster than half of the 10K time is slower-paced… i.e.
    # 5K time > 10K/2 * 0.9 and < 10K/2 (positive endurance slowdown).
    for model in (metrics.predict_riegel, metrics.predict_vdot,
                  metrics.predict_cameron):
        t5 = model(2481, 10000, 5000)
        assert 2481 / 2 * 0.90 < t5 < 2481 / 2
        tm = model(2481, 10000, 42195)
        assert 2481 * 4.2 < tm < 2481 * 5.2  # marathon in a plausible band
    # VDOT for 41:21 10K is ~50 (Daniels' tables).
    assert metrics.vdot_from(2481, 10000) == pytest.approx(50.0, abs=0.8)


def test_best_efforts_two_pointer():
    from runlens import metrics
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # 20 min at 5:00/km then 10 min at 4:00/km -> best 1k = 240 s
    records, dist = [], 0.0
    for i in range(1800):
        speed = 10 / 3 if i < 1200 else 25 / 6
        dist += speed
        records.append({"timestamp": t0 + timedelta(seconds=i), "distance": dist})
    eff = metrics.best_efforts(records, targets=(1000, 3000))
    assert eff[1000] == pytest.approx(240, abs=2)
    assert eff[3000] < 3000 / (10 / 3)  # faster than pure easy pace


# --- every session records provenance ---

@pytest.mark.parametrize("name,build", sorted(fixtures.ALL.items()))
def test_provenance_always_recorded(name, build):
    result = intervals.resolve(build())
    assert result["detection_source"] in (
        "structured_workout", "garmin_splits", "lap_inference", "unclassified")
    assert result["confidence"] in ("high", "medium", "low")
