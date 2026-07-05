"""SQLite persistence — the only module that touches the database.

One file (runlens.db). Tables: sessions, intervals, records,
user_overrides, progression_points. Re-upload of the same file is
idempotent (dedup by file_id.serial_number + time_created).

Manual overrides always win (spec 3): apply_overrides() is called on every
read and on re-parse, so user tags survive re-ingestion.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

DB_FILENAME = "runlens.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT UNIQUE NOT NULL,
    start_time_utc TEXT NOT NULL,
    sport TEXT, sub_sport TEXT,
    total_distance_m REAL, total_timer_s REAL, total_elapsed_s REAL,
    avg_hr REAL, max_hr REAL,
    origin TEXT, detection_source TEXT, confidence TEXT,
    session_type TEXT, structure TEXT, wkt_name TEXT,
    needs_manual_tag INTEGER DEFAULT 0,
    kpis_json TEXT
);
CREATE TABLE IF NOT EXISTS intervals (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    rep_index INTEGER,
    start_time TEXT, end_time TEXT,
    timer_s REAL, distance_m REAL, avg_speed_ms REAL,
    avg_hr REAL, max_hr REAL,
    outlier INTEGER DEFAULT 0,
    source TEXT
);
CREATE TABLE IF NOT EXISTS records (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    distance_m REAL, speed_ms REAL, hr INTEGER, cadence INTEGER
);
CREATE INDEX IF NOT EXISTS idx_records_session ON records(session_id, ts);
CREATE TABLE IF NOT EXISTS user_overrides (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (session_id, key)
);
CREATE TABLE IF NOT EXISTS progression_points (
    id INTEGER PRIMARY KEY,
    session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    date TEXT NOT NULL,
    pace_s_km REAL NOT NULL,
    avg_hr REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',   -- proposed / confirmed / rejected
    source TEXT,
    label TEXT
);
CREATE TABLE IF NOT EXISTS efforts (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                 -- rep / steady
    rep_index INTEGER,
    start_time TEXT, end_time TEXT,
    timer_s REAL, distance_m REAL,
    speed_ms REAL, pace_s_km REAL,
    hr_median REAL, hr_valid INTEGER DEFAULT 0, hr_suspect INTEGER DEFAULT 0,
    domain TEXT, domain_basis TEXT,
    rei REAL, avg_power REAL,
    boundary_version INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_efforts_session ON efforts(session_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    distance_m REAL NOT NULL,
    time_s REAL NOT NULL,
    label TEXT,
    source TEXT DEFAULT 'user'          -- user / auto-proposed
);
"""

# Schema v2 additive columns (ALTER guarded — sqlite has no IF NOT EXISTS).
_MIGRATIONS = (
    "ALTER TABLE records ADD COLUMN power INTEGER",
    "ALTER TABLE sessions ADD COLUMN temperature REAL",
    "ALTER TABLE sessions ADD COLUMN hot INTEGER DEFAULT 0",
)

# Anchor points seeded from the previous manual artifact (spec 4.1).
_SEED_ANCHORS = (
    ("2025-06-15", 242.0, 173.0, "anchor", "Jun 2025 anchor (4:02/km @ 173)"),
    ("2026-06-15", 232.0, 175.0, "anchor", "Jun 2026 anchor (3:52/km @ 175)"),
)


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value)


_SEED_RACES = (
    ("2025-05-11", 42195.0, 10440.0, "Leiden Marathon 2:54"),
)


def connect(path: str = DB_FILENAME) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already there
    _seed_anchors(con)
    if not con.execute("SELECT 1 FROM races LIMIT 1").fetchone():
        con.executemany(
            "INSERT INTO races (date, distance_m, time_s, label) VALUES (?, ?, ?, ?)",
            _SEED_RACES)
    con.commit()
    return con


# ------------------------------------------------------------ settings / races

def get_settings(con: sqlite3.Connection) -> dict:
    from .efforts import DEFAULT_SETTINGS
    rows = con.execute("SELECT key, value FROM settings")
    stored = {r["key"]: float(r["value"]) for r in rows}
    return {**DEFAULT_SETTINGS, **stored}


def set_setting(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, str(value)))
    con.commit()


def list_races(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in
            con.execute("SELECT * FROM races ORDER BY date DESC")]


def add_race(con: sqlite3.Connection, date: str, distance_m: float,
             time_s: float, label: str) -> None:
    con.execute("INSERT INTO races (date, distance_m, time_s, label) VALUES (?, ?, ?, ?)",
                (date, distance_m, time_s, label))
    con.commit()


# ------------------------------------------------------------ efforts

def save_efforts(con: sqlite3.Connection, session_id: int,
                 efforts: list[dict]) -> None:
    con.execute("DELETE FROM efforts WHERE session_id = ?", (session_id,))
    con.executemany(
        "INSERT INTO efforts (session_id, kind, rep_index, start_time, end_time,"
        " timer_s, distance_m, speed_ms, pace_s_km, hr_median, hr_valid,"
        " hr_suspect, domain, domain_basis, rei, avg_power, boundary_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(session_id, e["kind"], e["rep_index"], _iso(e["start_time"]),
          _iso(e["end_time"]), e["timer_s"], e["distance_m"], e["speed_ms"],
          e["pace_s_km"], e["hr_median"], 1 if e["hr_valid"] else 0,
          1 if e["hr_suspect"] else 0, e["domain"], e["domain_basis"],
          e["rei"], e["avg_power"], e["boundary_version"])
         for e in efforts])
    con.commit()


def get_efforts(con: sqlite3.Connection, session_id: int,
                kind: str | None = None) -> list[dict]:
    if kind:
        rows = con.execute(
            "SELECT * FROM efforts WHERE session_id = ? AND kind = ?"
            " ORDER BY start_time", (session_id, kind))
    else:
        rows = con.execute(
            "SELECT * FROM efforts WHERE session_id = ? ORDER BY start_time",
            (session_id,))
    return [dict(r) for r in rows]


def all_efforts_with_dates(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT e.*, s.start_time_utc AS session_date, s.hot AS session_hot"
        " FROM efforts e JOIN sessions s ON s.id = e.session_id"
        " ORDER BY s.start_time_utc")
    return [dict(r) for r in rows]


def _seed_anchors(con: sqlite3.Connection) -> None:
    n = con.execute(
        "SELECT COUNT(*) FROM progression_points WHERE source = 'anchor'"
    ).fetchone()[0]
    if n:
        return
    con.executemany(
        "INSERT INTO progression_points (date, pace_s_km, avg_hr, status, source, label)"
        " VALUES (?, ?, ?, 'confirmed', ?, ?)",
        [(d, p, h, s, l) for d, p, h, s, l in _SEED_ANCHORS],
    )
    con.commit()


# ------------------------------------------------------------ save

def save_session(con: sqlite3.Connection, dedup_key: str, activity: dict,
                 result: dict, kpis: dict, candidates: list[dict]) -> tuple[int, bool]:
    """Persist a parsed session. Returns (session_id, created).

    Idempotent: an existing dedup_key is fully re-written (so re-parsing
    with improved logic updates data) but user overrides and confirmed
    progression points are preserved.
    """
    session = activity.get("session") or {}
    row = con.execute("SELECT id FROM sessions WHERE dedup_key = ?", (dedup_key,)).fetchone()
    created = row is None

    values = {
        "dedup_key": dedup_key,
        "start_time_utc": _iso(session.get("start_time")) or _iso(datetime.now(timezone.utc)),
        "sport": session.get("sport"),
        "sub_sport": session.get("sub_sport"),
        "total_distance_m": session.get("total_distance"),
        "total_timer_s": session.get("total_timer_time"),
        "total_elapsed_s": session.get("total_elapsed_time"),
        "avg_hr": session.get("avg_heart_rate"),
        "max_hr": session.get("max_heart_rate"),
        "origin": result.get("origin"),
        "detection_source": result.get("detection_source"),
        "confidence": result.get("confidence"),
        "session_type": result.get("session_type"),
        "structure": result.get("structure"),
        "wkt_name": result.get("wkt_name"),
        "needs_manual_tag": 1 if result.get("needs_manual_tag") else 0,
        "temperature": session.get("avg_temperature"),
        "kpis_json": json.dumps(kpis, default=str),
    }

    if created:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = con.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})",
                          tuple(values.values()))
        session_id = cur.lastrowid
    else:
        session_id = row["id"]
        sets = ", ".join(f"{k} = ?" for k in values)
        con.execute(f"UPDATE sessions SET {sets} WHERE id = ?",
                    (*values.values(), session_id))
        con.execute("DELETE FROM intervals WHERE session_id = ?", (session_id,))
        con.execute("DELETE FROM records WHERE session_id = ?", (session_id,))
        con.execute(
            "DELETE FROM progression_points WHERE session_id = ? AND status = 'proposed'",
            (session_id,))

    con.executemany(
        "INSERT INTO intervals (session_id, seq, kind, rep_index, start_time, end_time,"
        " timer_s, distance_m, avg_speed_ms, avg_hr, max_hr, outlier, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(session_id, i, s["kind"], s["rep_index"], _iso(s["start_time"]),
          _iso(s["end_time"]), s["timer_s"], s["distance_m"], s["avg_speed_ms"],
          s["avg_hr"], s["max_hr"], 1 if s["outlier"] else 0, s["source"])
         for i, s in enumerate(result.get("segments", []))],
    )

    # Full 1 Hz storage (section 6 decision #2), power included (schema v2).
    con.executemany(
        "INSERT INTO records (session_id, ts, distance_m, speed_ms, hr, cadence, power)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(session_id, _iso(r["timestamp"]), r.get("distance"),
          r.get("enhanced_speed"), r.get("heart_rate"), r.get("cadence"),
          r.get("power"))
         for r in activity.get("records", [])],
    )

    for c in candidates:
        # Skip if a confirmed point already exists for this window.
        dup = con.execute(
            "SELECT 1 FROM progression_points WHERE session_id = ? AND label = ?"
            " AND status != 'rejected'",
            (session_id, c["label"])).fetchone()
        if dup:
            continue
        con.execute(
            "INSERT INTO progression_points (session_id, date, pace_s_km, avg_hr,"
            " status, source, label) VALUES (?, ?, ?, ?, 'proposed', 'auto', ?)",
            (session_id, _iso(c["start_time"])[:10], c["pace_s_km"], c["avg_hr"],
             c["label"]),
        )

    con.commit()
    return session_id, created


# ------------------------------------------------------------ overrides

def set_override(con: sqlite3.Connection, session_id: int, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO user_overrides (session_id, key, value) VALUES (?, ?, ?)"
        " ON CONFLICT (session_id, key) DO UPDATE SET value = excluded.value",
        (session_id, key, value),
    )
    con.commit()


def get_overrides(con: sqlite3.Connection, session_id: int) -> dict:
    rows = con.execute(
        "SELECT key, value FROM user_overrides WHERE session_id = ?", (session_id,))
    return {r["key"]: r["value"] for r in rows}


def apply_overrides(con: sqlite3.Connection, session: dict) -> dict:
    """Manual override always wins (spec 3)."""
    ov = get_overrides(con, session["id"])
    if "session_type" in ov:
        session["session_type"] = ov["session_type"]
        session["confidence"] = "high"
        session["needs_manual_tag"] = 0
    if "structure" in ov:
        session["structure"] = ov["structure"]
    session["overridden"] = sorted(ov)
    return session


# ------------------------------------------------------------ read

def list_sessions(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute("SELECT * FROM sessions ORDER BY start_time_utc DESC").fetchall()
    return [apply_overrides(con, dict(r)) for r in rows]


def get_session(con: sqlite3.Connection, session_id: int) -> dict | None:
    row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    session = apply_overrides(con, dict(row))
    session["kpis"] = json.loads(session.get("kpis_json") or "{}")
    return session


def get_intervals(con: sqlite3.Connection, session_id: int) -> list[dict]:
    """Segments with per-segment kind overrides applied (keys
    'segkind:<start_time_iso>' — stable across re-parses) and rep indices
    renumbered accordingly. Manual override always wins (spec 3)."""
    rows = con.execute(
        "SELECT * FROM intervals WHERE session_id = ? ORDER BY seq", (session_id,))
    segs = [dict(r) for r in rows]
    overrides = get_overrides(con, session_id)
    kind_ov = {k.removeprefix("segkind:"): v
               for k, v in overrides.items() if k.startswith("segkind:")}
    if kind_ov:
        for s in segs:
            new = kind_ov.get(s.get("start_time") or "")
            if new:
                s["kind"] = new
                s["overridden_kind"] = True
    n = 0
    for s in segs:
        if s["kind"] == "rep" and not s["outlier"]:
            n += 1
            s["rep_index"] = n
        else:
            s["rep_index"] = None
    return segs


def get_records(con: sqlite3.Connection, session_id: int) -> list[dict]:
    rows = con.execute(
        "SELECT ts, distance_m, speed_ms, hr, cadence, power FROM records"
        " WHERE session_id = ? ORDER BY ts", (session_id,))
    return [dict(r) for r in rows]


def progression_points(con: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = con.execute(
            "SELECT * FROM progression_points WHERE status = ? ORDER BY date", (status,))
    else:
        rows = con.execute("SELECT * FROM progression_points ORDER BY date")
    return [dict(r) for r in rows]


def set_progression_status(con: sqlite3.Connection, point_id: int, status: str) -> None:
    if status not in ("proposed", "confirmed", "rejected"):
        raise ValueError(f"bad status: {status}")
    con.execute("UPDATE progression_points SET status = ? WHERE id = ?",
                (status, point_id))
    con.commit()
