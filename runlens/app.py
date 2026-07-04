"""Flask app: routes + server-rendered templates (spec 5.1).

Single app file, Chart.js via CDN, no build step. Manual upload only
(section 6 decision #3). Run with:  python -m runlens.app
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from flask import Flask, flash, redirect, render_template, request, url_for

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
    finally:
        con.close()
    return session_id, created, result


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
                           cluster_idx=cluster_idx)


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
    return render_template("trends.html", points_hr=points_hr,
                           points_pace=points_pace, weekly=weekly,
                           trend_hr=trend_hr, trend_pace=trend_pace,
                           stats=stats,
                           hr_band=metrics.DEFAULT_HR_BAND,
                           pace_band=metrics.DEFAULT_PACE_BAND_S)


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
    overlay = []
    for m in members:
        segs = m["segments"]
        if cluster["kind"] in ("dist", "time"):
            sel = [s for s in segs if s["kind"] == "rep" and not s["outlier"]]
        else:
            sel = [s for s in segs if (s["timer_s"] or 0) > 60][:30]
        paces = [round(metrics.pace_s_per_km(s["avg_speed_ms"]), 1)
                 if s["avg_speed_ms"] else None for s in sel]
        overlay.append({
            "session_id": m["session"]["id"],
            "date": (m["session"]["start_time_utc"] or "")[:10],
            "structure": m["session"].get("structure"),
            "paces": paces,
        })
    x_max = max((len(o["paces"]) for o in overlay), default=0)
    return render_template("cluster.html", cluster=cluster,
                           cluster_idx=cluster_idx, overlay=overlay,
                           x_max=x_max,
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
