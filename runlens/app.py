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

@app.route("/")
def index():
    con = db()
    try:
        sessions = store.list_sessions(con)
    finally:
        con.close()
    return render_template("index.html", sessions=sessions)


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


@app.route("/session/<int:session_id>")
def session_detail(session_id):
    con = db()
    try:
        session = store.get_session(con, session_id)
        if session is None:
            return "Not found", 404
        segs = store.get_intervals(con, session_id)
        records = store.get_records(con, session_id)
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
                           series=series)


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
    return render_template("trends.html", points_hr=points_hr,
                           points_pace=points_pace, weekly=weekly,
                           hr_band=metrics.DEFAULT_HR_BAND,
                           pace_band=metrics.DEFAULT_PACE_BAND_S)


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
