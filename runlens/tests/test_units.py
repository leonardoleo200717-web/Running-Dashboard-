"""Unit tests: normalization, metrics, store idempotency, overrides,
progression eligibility, end-to-end Flask pipeline."""

import pytest

from runlens import ingest, intervals, metrics, store
from runlens.tests import fixtures


# ------------------------------------------------------------ normalization

def test_snap_distance():
    assert intervals.snap_distance(398) == 400     # spec 3.1 example
    assert intervals.snap_distance(992) == 1000    # spec 3.1 example
    assert intervals.snap_distance(1000) == 1000
    assert intervals.snap_distance(330) is None    # 1' rep at 5.5 m/s: no snap
    assert intervals.snap_distance(None) is None


def test_time_formatting_never_rounds():
    assert intervals._fmt_seconds(60) == "1'"
    assert intervals._fmt_seconds(90) == "1'30\""
    assert intervals._fmt_seconds(300) == "5'"
    assert intervals._fmt_seconds(45) == "45\""


def test_junk_lap_cleaning():
    laps = [
        {"total_timer_time": 0.5, "total_distance": 1.0},    # junk: both
        {"total_timer_time": 60, "total_distance": 3.0},     # junk: distance
        {"total_timer_time": 1.0, "total_distance": 100.0},  # junk: duration
        {"total_timer_time": 60, "total_distance": 330.0},   # keep
    ]
    assert intervals.clean_laps(laps) == [laps[3]]


def test_lap_intensity_never_used_on_free_runs():
    # Fixture laps all carry intensity='interval' (the meaningless default);
    # the easy run must still classify with zero reps.
    activity = fixtures.easy_run()
    assert all(l["intensity"] == "interval" for l in activity["laps"])
    result = intervals.resolve(activity)
    assert not [s for s in result["segments"] if s["kind"] == "rep"]


# ------------------------------------------------------------ metrics

def test_rep_table_and_drift():
    activity = fixtures.free_15x1()
    result = intervals.resolve(activity)
    rows = metrics.rep_table(result["segments"], activity["records"])
    assert len(rows) == 15
    assert all(r["avg_hr"] is not None for r in rows)
    drift = metrics.intra_set_drift(rows)
    assert drift["hr_drift_pct"] is not None


def test_pace_at_hr_band():
    activity = fixtures.long_run()  # steady 152 bpm, 3.51 m/s
    pace = metrics.pace_at_hr(activity["records"], band=(145, 160))
    assert pace == pytest.approx(285, abs=2)  # 1000 m / 3.5088 m/s
    assert metrics.pace_at_hr(activity["records"], band=(180, 190)) is None


def test_progression_candidate_from_continuous_run():
    activity = fixtures.long_run()
    result = intervals.resolve(activity)
    pauses = ingest.timer_pauses(activity)
    cands = metrics.find_progression_candidates(
        result["segments"], activity["records"], pauses)
    assert len(cands) == 1
    assert cands[0]["timer_s"] >= 600
    assert cands[0]["pace_cv"] <= 0.05
    assert cands[0]["avg_hr"] == pytest.approx(152, abs=1)


def test_progression_never_from_short_reps_or_recoveries():
    activity = fixtures.free_15x1()  # all reps 60s < 600s
    result = intervals.resolve(activity)
    cands = metrics.find_progression_candidates(
        result["segments"], activity["records"], [])
    assert cands == []


def test_weekly_volume():
    sessions = [{"start_time_utc": f["session"]["start_time"],
                 "total_distance_m": f["session"]["total_distance"]}
                for f in (fixtures.easy_run(), fixtures.long_run())]
    weeks = metrics.weekly_volume(sessions)
    assert sum(w["n_sessions"] for w in weeks) == 2
    assert all(w["km"] > 0 for w in weeks)


# ------------------------------------------------------------ store

@pytest.fixture
def con(tmp_path):
    c = store.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


def _save(con, activity):
    result = intervals.resolve(activity)
    reps = metrics.rep_table(result["segments"], activity["records"])
    kpis = {"rep_table": reps, "drift": metrics.intra_set_drift(reps)}
    cands = metrics.find_progression_candidates(
        result["segments"], activity["records"], ingest.timer_pauses(activity))
    return store.save_session(con, ingest.dedup_key(activity), activity,
                              result, kpis, cands)


def test_idempotent_reupload(con):
    activity = fixtures.free_8x1000()
    sid1, created1 = _save(con, activity)
    sid2, created2 = _save(con, activity)
    assert created1 and not created2
    assert sid1 == sid2
    assert len(store.list_sessions(con)) == 1
    # intervals not duplicated
    segs = store.get_intervals(con, sid1)
    assert len([s for s in segs if s["kind"] == "rep"]) == 8


def test_override_wins_and_persists_reparse(con):
    activity = fixtures.easy_run()
    sid, _ = _save(con, activity)
    store.set_override(con, sid, "session_type", "race")
    assert store.get_session(con, sid)["session_type"] == "race"
    _save(con, activity)  # re-parse
    assert store.get_session(con, sid)["session_type"] == "race"


def test_anchor_points_seeded(con):
    pts = store.progression_points(con, "confirmed")
    assert len(pts) == 2
    assert {p["pace_s_km"] for p in pts} == {242.0, 232.0}


def test_progression_confirm_flow(con):
    activity = fixtures.long_run()
    sid, _ = _save(con, activity)
    proposed = store.progression_points(con, "proposed")
    assert len(proposed) == 1
    store.set_progression_status(con, proposed[0]["id"], "confirmed")
    assert len(store.progression_points(con, "confirmed")) == 3
    # re-parse must not re-propose a confirmed block
    _save(con, activity)
    assert store.progression_points(con, "proposed") == []


# ------------------------------------------------------------ app (end to end)

@pytest.fixture
def client(tmp_path, monkeypatch):
    from runlens import app as app_mod
    monkeypatch.setattr(app_mod, "DB_PATH", str(tmp_path / "app.db"))
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client()


def test_pipeline_and_pages(client):
    from runlens import app as app_mod
    for build in fixtures.ALL.values():
        app_mod.process_activity(build())

    r = client.get("/")
    assert r.status_code == 200
    assert b"8x400m + 6x300m + 4x200m R200m" in r.data
    assert b"2x(10x1&#39;) R1&#39; SR4&#39;" in r.data or b"2x(10x1') R1' SR4'" in r.data

    con = store.connect(app_mod.DB_PATH)
    sid = store.list_sessions(con)[0]["id"]
    con.close()
    assert client.get(f"/session/{sid}").status_code == 200
    assert client.get("/trends").status_code == 200
    assert client.get("/progression").status_code == 200

    r = client.post(f"/session/{sid}/override",
                    data={"session_type": "race"}, follow_redirects=True)
    assert r.status_code == 200
