"""Tests for Layer 0 hygiene, the Steady Effort Store, and the
physiological efficiency module (PROPOSAL v2)."""

from datetime import datetime, timedelta, timezone

import pytest

from runlens import efforts, metrics

T0 = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)


def recs(spec):
    """spec: list of (duration_s, speed, hr, cadence). 1 Hz records."""
    out, t, dist = [], T0, 0.0
    for dur, speed, hr, cad in spec:
        for _ in range(int(dur)):
            dist += speed
            out.append({"timestamp": t, "distance": dist,
                        "enhanced_speed": speed, "heart_rate": hr,
                        "cadence": cad, "power": None})
            t += timedelta(seconds=1)
    return out


# ------------------------------------------------------------ hygiene

def test_hr_spike_interpolated():
    r = recs([(60, 3.0, 150, 168)])
    r[30]["heart_rate"] = 190      # impossible +40 in 1 s
    cleaned = efforts.clean_heart_rate(r)
    assert cleaned[30]["hr_ok"]
    assert abs(cleaned[30]["heart_rate"] - 150) <= 2
    assert not r[30].get("hr_ok")  # original untouched


def test_cadence_lock_flagged():
    # 60 s where "HR" equals cadence exactly -> optical lock.
    r = recs([(60, 3.0, 140, 168), (60, 3.0, 172, 172), (60, 3.0, 140, 168)])
    cleaned = efforts.clean_heart_rate(r)
    locked = cleaned[70]
    assert locked.get("hr_suspect")
    assert not locked["hr_ok"]


# ------------------------------------------------------------ extraction

def seg(start_s, dur, dist, kind="rep", rep_index=1):
    return {"kind": kind, "rep_index": rep_index, "outlier": False,
            "start_time": T0 + timedelta(seconds=start_s),
            "end_time": T0 + timedelta(seconds=start_s + dur),
            "timer_s": dur, "distance_m": dist,
            "avg_speed_ms": dist / dur, "avg_hr": None, "max_hr": None}


def test_stabilized_hr_drops_first_45s_and_uses_median():
    # Rep 0-180 s: HR climbs in the first 45 s, then 178 steady.
    # (cadence deliberately != HR: 178/178 would trigger the lock detector)
    r = recs([(45, 4.4, 150, 168), (135, 4.4, 178, 168)])
    cleaned = efforts.clean_heart_rate(r)
    eff = efforts.extract_efforts([seg(0, 180, 792)], cleaned, [])
    assert len(eff) == 1
    assert eff[0]["hr_valid"]
    assert eff[0]["hr_median"] == pytest.approx(178, abs=1)  # climb excluded
    assert eff[0]["domain"] == "threshold"


def test_short_reps_have_no_hr_statistics():
    r = recs([(60, 5.0, 175, 180)])
    cleaned = efforts.clean_heart_rate(r)
    eff = efforts.extract_efforts([seg(0, 60, 300)], cleaned, [])
    assert eff[0]["hr_median"] is None
    assert not eff[0]["hr_valid"]
    assert eff[0]["rei"] is None
    assert eff[0]["domain_basis"] == "pace"   # pace fallback (3:20 -> hard)
    assert eff[0]["domain"] == "hard"


def test_domain_boundaries_from_lthr():
    s = efforts.DEFAULT_SETTINGS
    assert efforts.domain_for_hr(140, s) == "easy"
    assert efforts.domain_for_hr(155, s) == "steady"
    assert efforts.domain_for_hr(172, s) == "threshold"
    assert efforts.domain_for_hr(183, s) == "hard"


# ------------------------------------------------------------ efficiency

def eff_rep(i, pace, hr, domain="threshold"):
    speed = 1000.0 / pace
    return {"kind": "rep", "rep_index": i, "pace_s_km": pace,
            "speed_ms": speed, "hr_median": hr, "hr_valid": True,
            "domain": domain, "rei": speed / hr}


def test_early_vs_full_rei_is_drift_aware():
    # Same pace, HR drifting 170 -> 180 across 6 reps.
    reps = [eff_rep(i + 1, 240, 170 + 2 * i) for i in range(6)]
    s = metrics.session_efficiency(reps)
    assert s["rei_early"] > s["rei_full"]      # drift excluded from primary
    assert s["n_hr_valid"] == 6


def test_regression_gated_on_single_pace_session():
    reps = [eff_rep(i + 1, 240 + (i % 2), 172) for i in range(6)]  # ~1 s/km spread
    r = metrics.gated_regression(reps, ref_hr=175, ref_pace_s_km=235)
    assert not r["valid"]
    assert "single-pace" in r["reason"]


def test_regression_valid_with_spread():
    # Clean linear relation: faster pace costs 4 bpm per 10 s/km.
    reps = [eff_rep(i + 1, p, 180 - 0.4 * (p - 210))
            for i, p in enumerate([250, 240, 230, 220, 210])]
    r = metrics.gated_regression(reps, ref_hr=175, ref_pace_s_km=235)
    assert r["valid"] and r["r2"] >= 0.95
    assert r["pace_cost"] == pytest.approx(4.0, abs=0.3)
    assert r["expected_hr_at_ref_pace"] == pytest.approx(170, abs=1)


def test_compare_sessions_proposal_examples():
    # A: 3:40/km @ 180 -> B: 3:35/km @ 184 (harder) vs C: 3:35/km @ 176 (fitter)
    A = metrics.session_efficiency([eff_rep(i + 1, 220, 180) for i in range(5)])
    B = metrics.session_efficiency([eff_rep(i + 1, 215, 184) for i in range(5)])
    C = metrics.session_efficiency([eff_rep(i + 1, 215, 176) for i in range(5)])
    meta = {"date": "2026-07-01", "hot": 0}

    b = metrics.compare_sessions(B, A, meta, meta)
    assert b["verdict"] in ("similar", "declining")   # NOT improving
    c = metrics.compare_sessions(C, A, meta, meta)
    assert c["verdict"] == "improving"
    assert any("efficiency improved" in s.lower() for s in c["insights"])


def test_compare_refuses_cross_domain():
    A = metrics.session_efficiency([eff_rep(1, 220, 180, domain="hard")])
    B = metrics.session_efficiency([eff_rep(1, 260, 160, domain="steady")])
    r = metrics.compare_sessions(A, B, {"date": "d1"}, {"date": "d2"})
    assert r["verdict"] == "not_comparable"


def test_heat_flag_annotates_comparison():
    A = metrics.session_efficiency([eff_rep(i + 1, 220, 175) for i in range(4)])
    B = metrics.session_efficiency([eff_rep(i + 1, 220, 170) for i in range(4)])
    r = metrics.compare_sessions(A, B, {"date": "d1", "hot": 1}, {"date": "d2"})
    assert any("heat" in s.lower() for s in r["insights"])


# ------------------------------------------------------------ store roundtrip

def test_efforts_persist_and_schema_migrates(tmp_path):
    from runlens import store
    con = store.connect(str(tmp_path / "t.db"))
    # migration adds power to records and hot/temperature to sessions
    cols = {r[1] for r in con.execute("PRAGMA table_info(records)")}
    assert "power" in cols
    cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    assert {"hot", "temperature"} <= cols
    # settings defaults + races seeded
    s = store.get_settings(con)
    assert s["lthr"] == 176.0
    races = store.list_races(con)
    assert any("Leiden" in (r["label"] or "") for r in races)
    con.close()
