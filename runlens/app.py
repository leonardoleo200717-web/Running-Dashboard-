"""Flask app: routes + server-rendered templates (spec 5.1).

Single app file, Chart.js via CDN, no build step. Manual upload only
(section 6 decision #3). Run with:  python -m runlens.app
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from flask import Flask, flash, redirect, render_template, request, url_for

from . import efforts as efforts_mod
from . import ingest, intervals, metrics, store

DISPLAY_TZ = ZoneInfo("Europe/Amsterdam")  # UTC internally, local at display time

app = Flask(__name__)
app.secret_key = os.environ.get("RUNLENS_SECRET", "runlens-local")

DB_PATH = os.environ.get("RUNLENS_DB", store.DB_FILENAME)


def db():
    return store.connect(DB_PATH)


# ------------------------------------------------------------ pipeline

def process_activity(activity: dict) -> tuple[int, bool, dict]:
    """FIT ingestion -> origin -> interval engine -> metrics -> store."""
    result = intervals.resolve(activity)
    records = activity.get("records", [])
    pauses = ingest.timer_pauses(activity)

    metrics.backfill_segment_hr(result["segments"], records)
    reps = metrics.rep_table(result["segments"], records)
    kpis = {
        "rep_table": reps,
        "drift": metrics.intra_set_drift(reps),
        "recovery": metrics.recovery_quality(result["segments"], records,
                                             activity.get("events")),
        "pace_at_hr": metrics.pace_at_hr(records),
        "hr_at_pace": metrics.hr_at_pace(records),
        "intensity": metrics.intensity_distribution(activity.get("time_in_zone", [])),
    }
    candidates = metrics.find_progression_candidates(result["segments"], records, pauses)

    con = db()
    try:
        session_id, created = store.save_session(
            con, ingest.dedup_key(activity), activity, result, kpis, candidates)
        # Steady Effort Store: hygiene first, then extraction (PROPOSAL v2).
        settings = store.get_settings(con)
        cleaned = efforts_mod.clean_heart_rate(records)
        efforts_mod.smooth_speed(cleaned)
        eff = efforts_mod.extract_efforts(result["segments"], cleaned, pauses, settings)
        store.save_efforts(con, session_id, eff)
        # Re-apply per-segment kind overrides after a re-parse.
        if any(k.startswith("segkind:") for k in store.get_overrides(con, session_id)):
            _recompute_session(con, session_id)
    finally:
        con.close()
    return session_id, created, result


def _stored_segments_and_records(con, session_id):
    """Stored intervals/records converted back to engine-shaped dicts."""
    segs = []
    for s in store.get_intervals(con, session_id):
        segs.append({**s,
                     "start_time": store.parse_ts(s["start_time"]),
                     "end_time": store.parse_ts(s["end_time"]),
                     "outlier": bool(s["outlier"])})
    records = [{"timestamp": store.parse_ts(r["ts"]),
                "distance": r["distance_m"],
                "enhanced_speed": r["speed_ms"],
                "heart_rate": r["hr"], "cadence": r["cadence"],
                "power": r["power"]}
               for r in store.get_records(con, session_id)]
    return segs, records


def _recompute_session(con, session_id):
    """Rebuild structure and KPIs from the (override-applied) stored
    segments, so per-segment reclassification updates everything."""
    import json
    segs, records = _stored_segments_and_records(con, session_id)
    reps = metrics.rep_table(segs, records)
    kpis = {
        "rep_table": reps,
        "drift": metrics.intra_set_drift(reps),
        "recovery": metrics.recovery_quality(segs, records),
        "pace_at_hr": metrics.pace_at_hr(records),
        "hr_at_pace": metrics.hr_at_pace(records),
        "intensity": None,
    }
    structure = intervals.structure_string(segs)
    con.execute(
        "UPDATE sessions SET structure = ?, kpis_json = ?, needs_manual_tag = 0"
        " WHERE id = ?",
        (structure, json.dumps(kpis, default=str), session_id))
    con.commit()
    # Reclassified segments change the effort store too.
    settings = store.get_settings(con)
    cleaned = efforts_mod.clean_heart_rate(records)
    efforts_mod.smooth_speed(cleaned)
    eff = efforts_mod.extract_efforts(segs, cleaned, [], settings)
    store.save_efforts(con, session_id, eff)


# ------------------------------------------------------------ template helpers

@app.template_filter("localdt")
def localdt(value, fmt="%a %d %b %Y %H:%M"):
    dt = store.parse_ts(value) if isinstance(value, str) else value
    if dt is None:
        return "–"
    return dt.astimezone(DISPLAY_TZ).strftime(fmt)


@app.template_filter("km")
def km(meters):
    return f"{(meters or 0) / 1000:.1f} km"


@app.template_filter("hms")
def hms(seconds):
    if not seconds:
        return "–"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@app.template_filter("pace")
def pace_filter(seconds_per_km):
    return metrics.fmt_pace(seconds_per_km)


# ------------------------------------------------------------ routes

_GROUP_MODES = ("week", "month", "year")


def _group_label(dt, mode: str) -> str:
    local = dt.astimezone(DISPLAY_TZ)
    if mode == "week":
        iso = local.isocalendar()
        return f"{iso[0]} · Week {iso[1]:02d}"
    if mode == "month":
        return local.strftime("%B %Y")
    return local.strftime("%Y")


@app.route("/")
def index():
    mode = request.args.get("group", "week")
    if mode not in _GROUP_MODES:
        mode = "week"
    con = db()
    try:
        sessions = store.list_sessions(con)
    finally:
        con.close()

    # Time blocks: sessions grouped into weekly/monthly/yearly buckets,
    # newest first, each with volume totals.
    groups: list[dict] = []
    for s in sessions:
        dt = store.parse_ts(s["start_time_utc"])
        label = _group_label(dt, mode) if dt else "unknown"
        if not groups or groups[-1]["label"] != label:
            groups.append({"label": label, "sessions": [],
                           "km": 0.0, "timer_s": 0.0})
        g = groups[-1]
        g["sessions"].append(s)
        g["km"] += (s.get("total_distance_m") or 0) / 1000
        g["timer_s"] += s.get("total_timer_s") or 0
    for g in groups:
        g["km"] = round(g["km"], 1)
    return render_template("index.html", groups=groups, mode=mode,
                           n_sessions=len(sessions))


@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("fit_files")
    ok, failed = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            activity = ingest.parse_fit(f.stream.read())
            process_activity(activity)
            ok += 1
        except Exception as exc:  # corrupt file: report, never crash (spec 5.3)
            failed.append(f"{f.filename}: {exc}")
    if ok:
        flash(f"Imported {ok} file(s).")
    for msg in failed:
        flash(f"Failed: {msg}")
    return redirect(url_for("index"))


def _phase_summary(segs: list[dict]) -> dict:
    """Warmup / work / recovery / cooldown totals for the overview."""
    phases = {}
    for key, kinds in (("warmup", ("warmup",)), ("work", ("rep",)),
                       ("recovery", ("recovery",)), ("cooldown", ("cooldown",))):
        sel = [s for s in segs if s["kind"] in kinds and not s["outlier"]]
        if not sel:
            continue
        dist = sum(s["distance_m"] or 0 for s in sel)
        timer = sum(s["timer_s"] or 0 for s in sel)
        hr_w = [(s["avg_hr"], s["timer_s"] or 0) for s in sel if s["avg_hr"]]
        hr_t = sum(t for _, t in hr_w)
        phases[key] = {
            "n": len(sel),
            "distance_m": dist,
            "timer_s": timer,
            "pace_s_km": metrics.pace_s_per_km(dist / timer) if timer and dist else None,
            "avg_hr": round(sum(h * t for h, t in hr_w) / hr_t) if hr_t else None,
        }
    return phases


def _all_cluster_entries(con) -> list[dict]:
    return [{"session": s, "segments": store.get_intervals(con, s["id"])}
            for s in store.list_sessions(con)]


@app.route("/session/<int:session_id>")
def session_detail(session_id):
    con = db()
    try:
        session = store.get_session(con, session_id)
        if session is None:
            return "Not found", 404
        segs = store.get_intervals(con, session_id)
        records = store.get_records(con, session_id)
        clusters = metrics.cluster_sessions(_all_cluster_entries(con))
    finally:
        con.close()

    phases = _phase_summary(segs)
    similar, cluster_idx = [], None
    for ci, c in enumerate(clusters):
        ids = [m["session"]["id"] for m in c["members"]]
        if session_id in ids and len(ids) > 1:
            cluster_idx = ci
            similar = [m["session"] for m in c["members"]
                       if m["session"]["id"] != session_id]
            break

    # Physiological efficiency (PROPOSAL v2 §4).
    con = db()
    try:
        settings = store.get_settings(con)
        rep_effs = store.get_efforts(con, session_id, "rep")
        efficiency = metrics.session_efficiency(rep_effs)
        ref_pace = None
        if efficiency["domain"]:
            cutoff = store.parse_ts(session["start_time_utc"])
            all_eff = store.all_efforts_with_dates(con)
            recent = [e for e in all_eff
                      if e["kind"] == "rep" and e["domain"] == efficiency["domain"]
                      and e["session_date"] and cutoff
                      and 0 <= (cutoff - store.parse_ts(e["session_date"])).days <= 60]
            paces = [e["pace_s_km"] for e in recent if e["pace_s_km"]]
            if paces:
                import statistics as _st
                ref_pace = round(_st.median(paces), 1)
        regression = metrics.gated_regression(rep_effs, settings["ref_hr"], ref_pace)
        # Verdict vs previous similar session.
        verdict = None
        if similar:
            older = [s for s in similar
                     if (s["start_time_utc"] or "") < (session["start_time_utc"] or "")]
            if older:
                older.sort(key=lambda s: s["start_time_utc"] or "", reverse=True)
                prev, prev_eff = older[0], None
                for cand in older:  # prefer the latest same-domain session
                    ce = metrics.session_efficiency(
                        store.get_efforts(con, cand["id"], "rep"))
                    if ce["domain"] == efficiency["domain"]:
                        prev, prev_eff = cand, ce
                        break
                if prev_eff is None:
                    prev_eff = metrics.session_efficiency(
                        store.get_efforts(con, prev["id"], "rep"))
                verdict = metrics.compare_sessions(
                    efficiency, prev_eff,
                    {"date": (session["start_time_utc"] or "")[:10],
                     "hot": session.get("hot")},
                    {"date": (prev["start_time_utc"] or "")[:10],
                     "hot": prev.get("hot")})
    finally:
        con.close()

    # 1 Hz series for the pace/HR charts, downsampled for the wire (~600 pts).
    step = max(1, len(records) // 600)
    series = []
    for r in records[::step]:
        ts = store.parse_ts(r["ts"])
        p = metrics.pace_s_per_km(r["speed_ms"])
        series.append({
            "t": ts.astimezone(DISPLAY_TZ).strftime("%H:%M:%S") if ts else None,
            "pace": round(p, 1) if p and p < 900 else None,
            "hr": r["hr"],
        })
    return render_template("session.html", session=session, segments=segs,
                           series=series, phases=phases, similar=similar,
                           cluster_idx=cluster_idx, segment_kinds=_SEGMENT_KINDS,
                           efficiency=efficiency, regression=regression,
                           ref_pace=ref_pace, ref_hr=settings["ref_hr"],
                           verdict=verdict)


@app.route("/session/<int:session_id>/hot", methods=["POST"])
def toggle_hot(session_id):
    con = db()
    try:
        con.execute("UPDATE sessions SET hot = 1 - COALESCE(hot, 0) WHERE id = ?",
                    (session_id,))
        con.commit()
    finally:
        con.close()
    return redirect(url_for("session_detail", session_id=session_id))


_SEGMENT_KINDS = {"rep": "Active", "recovery": "Recovery",
                  "warmup": "WU", "cooldown": "CD"}


@app.route("/session/<int:session_id>/segment", methods=["POST"])
def segment_override(session_id):
    seq = request.form.get("seq", type=int)
    kind = request.form.get("kind")
    if seq is None or kind not in _SEGMENT_KINDS:
        return "Bad request", 400
    con = db()
    try:
        row = con.execute(
            "SELECT start_time FROM intervals WHERE session_id = ? AND seq = ?",
            (session_id, seq)).fetchone()
        if row is None:
            return "Not found", 404
        store.set_override(con, session_id, f"segkind:{row['start_time']}", kind)
        _recompute_session(con, session_id)
    finally:
        con.close()
    flash("Segment reclassified — structure and KPIs recomputed.")
    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/session/<int:session_id>/override", methods=["POST"])
def override(session_id):
    con = db()
    try:
        for key in ("session_type", "structure"):
            value = (request.form.get(key) or "").strip()
            if value:
                store.set_override(con, session_id, key, value)
    finally:
        con.close()
    flash("Override saved — it will persist across re-parses.")
    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/trends")
def trends():
    con = db()
    try:
        sessions = store.list_sessions(con)
        points_hr, points_pace = [], []
        for s in sessions:
            full = store.get_session(con, s["id"])
            kpis = full.get("kpis", {})
            date = (s["start_time_utc"] or "")[:10]
            if kpis.get("pace_at_hr"):
                points_hr.append({"date": date, "value": round(kpis["pace_at_hr"], 1),
                                  "label": metrics.fmt_pace(kpis["pace_at_hr"])})
            if kpis.get("hr_at_pace"):
                points_pace.append({"date": date, "value": kpis["hr_at_pace"]})
        weekly = metrics.weekly_volume(sessions)
    finally:
        con.close()
    points_hr.sort(key=lambda p: p["date"])
    points_pace.sort(key=lambda p: p["date"])
    trend_hr = metrics.trend_series(points_hr)
    trend_pace = metrics.trend_series(points_pace)
    stats = metrics.improvement_stats(points_hr, points_pace, weekly)

    # Per-domain efficiency trends (PROPOSAL v2 §5): early-session REI
    # per session per domain, never mixed across domains.
    import math as _math
    from collections import defaultdict
    con = db()
    try:
        all_eff = store.all_efforts_with_dates(con)
    finally:
        con.close()
    grouped: dict = defaultdict(list)
    for e in all_eff:
        if e["rei"] and e["session_date"]:
            grouped[(e["domain"], e["session_id"], e["session_date"][:10],
                     bool(e["session_hot"]))].append(e)
    domain_points: dict = defaultdict(list)
    for (dom, sid, date, hot), effs in grouped.items():
        reps = sorted([e for e in effs if e["kind"] == "rep"],
                      key=lambda e: e["rep_index"] or 0)
        pool = reps[:_math.ceil(len(reps) / 3)] if reps else effs
        value = sum(e["rei"] for e in pool) / len(pool)
        domain_points[dom].append({"date": date, "value": round(value * 1000, 2),
                                   "hot": hot, "session_id": sid})
    domain_trends = {}
    for dom in ("easy", "steady", "threshold", "hard"):
        pts = sorted(domain_points.get(dom, []), key=lambda p: p["date"])
        if pts:
            domain_trends[dom] = {"points": pts,
                                  "trend": metrics.trend_series(pts)}

    return render_template("trends.html", points_hr=points_hr,
                           points_pace=points_pace, weekly=weekly,
                           trend_hr=trend_hr, trend_pace=trend_pace,
                           stats=stats, domain_trends=domain_trends,
                           hr_band=metrics.DEFAULT_HR_BAND,
                           pace_band=metrics.DEFAULT_PACE_BAND_S)


@app.route("/predictor")
def predictor():
    from datetime import timedelta
    window_days = 120
    con = db()
    try:
        sessions = store.list_sessions(con)
        bests: dict[int, dict] = {}
        latest = store.parse_ts(sessions[0]["start_time_utc"]) if sessions else None
        for s in sessions:
            dt = store.parse_ts(s["start_time_utc"])
            if dt is None or (latest - dt).days > window_days:
                continue
            _, records = _stored_segments_and_records(con, s["id"])
            for d, t in metrics.best_efforts(records).items():
                if d not in bests or t < bests[d]["t"]:
                    bests[d] = {"t": t, "session_id": s["id"],
                                "date": (s["start_time_utc"] or "")[:10]}
    finally:
        con.close()

    # Tier 1 (PROPOSAL v2 §6): confirmed races only — never a rolling
    # window over interval sessions.
    con = db()
    try:
        races = store.list_races(con)
        all_sessions = store.list_sessions(con)
    finally:
        con.close()
    weekly = metrics.weekly_volume(all_sessions)
    last4 = round(sum(w["km"] for w in weekly[-4:]) / max(1, len(weekly[-4:])), 1) \
        if weekly else 0.0

    anchor_race, predictions, vdot, marathon_band = None, [], None, None
    MARATHON_SUPPORT_KM_WK = 65  # stated assumption, adjustable
    volume_supported = last4 >= MARATHON_SUPPORT_KM_WK
    if races:
        anchor_race = max(races, key=lambda r: r["date"])
        t1, d1 = anchor_race["time_s"], anchor_race["distance_m"]
        predictions = metrics.race_predictions(t1, d1)
        vdot = round(metrics.vdot_from(t1, d1), 1)
        if d1 < 42195 and not volume_supported:
            # Volume-corrected marathon band: Riegel 1.06 (trained
            # endurance) .. 1.15 (low-volume bound, Vickers-Vertosick
            # finding for recreational marathoners).
            marathon_band = {
                "low": round(t1 * (42195 / d1) ** 1.06),
                "high": round(t1 * (42195 / d1) ** 1.15),
            }

    # Critical Speed needs >= 3 confirmed maximal efforts spanning ~2-15'.
    cs_efforts = [r for r in races if 120 <= r["time_s"] <= 1500]
    cs_status = (None if len(cs_efforts) >= 3 else
                 "needs ≥ 3 confirmed maximal efforts of 2–15' (has "
                 f"{len(cs_efforts)}) — tag a 12–15' max test to unlock")

    return render_template("predictor.html", bests=bests, races=races,
                           anchor_race=anchor_race, predictions=predictions,
                           vdot=vdot, window_days=window_days,
                           last4=last4, volume_supported=volume_supported,
                           support_km=MARATHON_SUPPORT_KM_WK,
                           marathon_band=marathon_band, cs_status=cs_status)


@app.route("/predictor/race", methods=["POST"])
def add_race():
    date = (request.form.get("date") or "").strip()
    label = (request.form.get("label") or "").strip()
    try:
        distance_m = float(request.form.get("distance_km")) * 1000
        parts = [int(p) for p in (request.form.get("time") or "").split(":")]
        time_s = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 \
            else parts[0] * 60 + parts[1]
    except (TypeError, ValueError, IndexError):
        flash("Race not added — need date, distance (km) and time (h:mm:ss).")
        return redirect(url_for("predictor"))
    con = db()
    try:
        store.add_race(con, date, distance_m, time_s, label or "race")
    finally:
        con.close()
    flash("Race added — predictor re-anchored.")
    return redirect(url_for("predictor"))


@app.route("/clusters")
def clusters_page():
    con = db()
    try:
        clusters = metrics.cluster_sessions(_all_cluster_entries(con))
    finally:
        con.close()
    return render_template("clusters.html", clusters=clusters)


@app.route("/clusters/<int:cluster_idx>")
def cluster_detail(cluster_idx):
    con = db()
    try:
        clusters = metrics.cluster_sessions(_all_cluster_entries(con))
    finally:
        con.close()
    if cluster_idx < 0 or cluster_idx >= len(clusters):
        return "Not found", 404
    cluster = clusters[cluster_idx]

    # Overlay series: rep paces per session for interval clusters, km-lap
    # paces for run clusters. Capped at 8 sessions (the categorical
    # palette's fixed slot count) — newest 8 win.
    members = cluster["members"][-8:]
    con = db()
    try:
        overlay, trend_points, sess_eff = [], [], {}
        for m in members:
            sid = m["session"]["id"]
            segs = m["segments"]
            if cluster["kind"] in ("dist", "time"):
                sel = [s for s in segs if s["kind"] == "rep" and not s["outlier"]]
            else:
                sel = [s for s in segs if (s["timer_s"] or 0) > 60][:30]
            paces = [round(metrics.pace_s_per_km(s["avg_speed_ms"]), 1)
                     if s["avg_speed_ms"] else None for s in sel]
            hrs = [round(s["avg_hr"]) if s.get("avg_hr") else None for s in sel]
            cum, total = [], 0.0
            for s in sel:
                total += (s["timer_s"] or 0) / 60.0
                cum.append(round(total, 1))
            eff = metrics.session_efficiency(store.get_efforts(con, sid, "rep"))
            sess_eff[sid] = eff
            date = (m["session"]["start_time_utc"] or "")[:10]
            overlay.append({
                "session_id": sid, "date": date,
                "structure": m["session"].get("structure"),
                "domain": eff["domain"],
                "paces": paces, "hrs": hrs, "cum_min": cum,
            })
            trend_points.append({
                "date": date, "session_id": sid,
                "rei": round(eff["rei_early"] * 1000, 2) if eff["rei_early"] else None,
                "pace": eff["median_pace_s_km"],
                "n_hr": eff["n_hr_valid"],
                "domain": eff["domain"],
            })

        # The verdict: newest vs the most recent SAME-DOMAIN session (a
        # hard 5x1500 must not be judged against a steady 6x1500); fall
        # back to the immediate previous with an explanation.
        verdict, compared = None, None
        if len(members) >= 2:
            cur_m = members[-1]
            cur_dom = sess_eff[cur_m["session"]["id"]]["domain"]
            prev_m = next((m for m in reversed(members[:-1])
                           if sess_eff[m["session"]["id"]]["domain"] == cur_dom),
                          members[-2])
            verdict = metrics.compare_sessions(
                sess_eff[cur_m["session"]["id"]],
                sess_eff[prev_m["session"]["id"]],
                {"date": (cur_m["session"]["start_time_utc"] or "")[:10],
                 "hot": cur_m["session"].get("hot")},
                {"date": (prev_m["session"]["start_time_utc"] or "")[:10],
                 "hot": prev_m["session"].get("hot")})
            compared = {"cur": (cur_m["session"]["start_time_utc"] or "")[:10],
                        "prev": (prev_m["session"]["start_time_utc"] or "")[:10]}

        # If the newest session has no same-domain partner, still answer
        # the question with the most recent comparable PAIR in the group.
        pair_verdict, pair_compared = None, None
        if verdict and verdict["verdict"] == "not_comparable" and len(members) >= 2:
            for i in range(len(members) - 1, 0, -1):
                di = sess_eff[members[i]["session"]["id"]]["domain"]
                for j in range(i - 1, -1, -1):
                    if sess_eff[members[j]["session"]["id"]]["domain"] == di:
                        pv = metrics.compare_sessions(
                            sess_eff[members[i]["session"]["id"]],
                            sess_eff[members[j]["session"]["id"]],
                            {"date": (members[i]["session"]["start_time_utc"] or "")[:10],
                             "hot": members[i]["session"].get("hot")},
                            {"date": (members[j]["session"]["start_time_utc"] or "")[:10],
                             "hot": members[j]["session"].get("hot")})
                        if pv["verdict"] != "not_comparable":
                            pair_verdict = pv
                            pair_compared = {
                                "cur": (members[i]["session"]["start_time_utc"] or "")[:10],
                                "prev": (members[j]["session"]["start_time_utc"] or "")[:10]}
                            break
                if pair_verdict:
                    break
    finally:
        con.close()

    x_max = max((len(o["paces"]) for o in overlay), default=0)
    return render_template("cluster.html", cluster=cluster,
                           cluster_idx=cluster_idx, overlay=overlay,
                           trend_points=trend_points, verdict=verdict,
                           compared=compared, pair_verdict=pair_verdict,
                           pair_compared=pair_compared, x_max=x_max,
                           x_label="Rep" if cluster["kind"] in ("dist", "time") else "Segment")


@app.route("/progression")
def progression():
    con = db()
    try:
        confirmed = store.progression_points(con, "confirmed")
        proposed = store.progression_points(con, "proposed")
    finally:
        con.close()
    chart = [{"date": p["date"], "pace": p["pace_s_km"], "hr": p["avg_hr"],
              "label": p["label"] or ""} for p in confirmed]
    return render_template("progression.html", chart=chart,
                           proposed=proposed, confirmed=confirmed)


@app.route("/progression/<int:point_id>/<action>", methods=["POST"])
def progression_action(point_id, action):
    status = {"confirm": "confirmed", "reject": "rejected"}.get(action)
    if status is None:
        return "Bad action", 400
    con = db()
    try:
        store.set_progression_status(con, point_id, status)
    finally:
        con.close()
    return redirect(url_for("progression"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
