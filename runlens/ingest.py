"""FIT ingestion: binary FIT file -> normalized plain-dict activity.

Pure functions, no DB access. No downstream stage may reach back into raw
FIT after this module: everything the pipeline needs is in the activity
dict returned by parse_fit().

Activity dict shape (all timestamps are UTC datetimes, per verified FIT
structure — convert to local only at display time):

    {
      'file_id':       {'serial_number', 'time_created', 'manufacturer', 'product'},
      'session':       {...session summary fields...},
      'laps':          [{...}, ...],          # ordered by start_time
      'events':        [{...}, ...],          # timer + recovery_hr events
      'records':       [{...}, ...],          # 1 Hz samples, ordered
      'splits':        [{...}, ...],          # Garmin auto-splits
      'workout':       {'wkt_name': ...} | None,
      'workout_steps': [{...}, ...],          # ordered by message_index
      'time_in_zone':  [{...}, ...],
    }
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# MVP record fields (spec 2.3). Other fields exist in the files but are
# deliberately not carried forward.
_RECORD_FIELDS = ("timestamp", "distance", "enhanced_speed", "heart_rate", "cadence")

_SESSION_FIELDS = (
    "start_time", "timestamp", "sport", "sub_sport",
    "total_distance", "total_timer_time", "total_elapsed_time",
    "avg_speed", "enhanced_avg_speed", "avg_heart_rate", "max_heart_rate",
    "total_ascent", "total_descent", "num_laps",
)

_LAP_FIELDS = (
    "start_time", "timestamp", "total_distance",
    "total_timer_time", "total_elapsed_time",
    "avg_speed", "enhanced_avg_speed", "avg_heart_rate", "max_heart_rate",
    "lap_trigger", "wkt_step_index", "intensity", "message_index",
)

_SPLIT_FIELDS = (
    "split_type", "start_time", "end_time",
    "total_timer_time", "total_elapsed_time", "total_distance", "avg_speed",
)

_EVENT_FIELDS = ("timestamp", "event", "event_type", "data")

_WORKOUT_STEP_FIELDS = (
    "message_index", "duration_type", "duration_distance", "duration_time",
    "duration_step", "repeat_steps", "intensity",
    "target_type", "custom_target_speed_low", "custom_target_speed_high",
    "wkt_step_name", "notes",
)

_FILE_ID_FIELDS = ("serial_number", "time_created", "manufacturer", "product", "type")

_TIME_IN_ZONE_FIELDS = ("timestamp", "reference_mesg", "time_in_hr_zone", "hr_zone_high_boundary")


def empty_activity() -> dict:
    return {
        "file_id": {},
        "session": {},
        "laps": [],
        "events": [],
        "records": [],
        "splits": [],
        "workout": None,
        "workout_steps": [],
        "time_in_zone": [],
    }


def _extract(frame, fields) -> dict:
    out = {}
    for name in fields:
        try:
            out[name] = frame.get_value(name, fallback=None)
        except Exception:  # unknown/corrupt field: skip, never crash (spec 5.3)
            out[name] = None
    return out


def parse_fit(source) -> dict:
    """Parse a FIT file (path, bytes or file-like) into an activity dict.

    Unknown or corrupt messages are skipped and logged, never fatal.
    """
    import fitdecode

    activity = empty_activity()

    with fitdecode.FitReader(source, check_crc=fitdecode.CrcCheck.WARN) as reader:
        for frame in reader:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            name = frame.name
            if name.startswith("unknown"):
                continue  # many unknown_* messages exist; ignore (spec 2.2)
            try:
                _dispatch(activity, name, frame)
            except Exception:
                log.warning("skipping unreadable %s message", name, exc_info=True)

    _finalize(activity)
    return activity


def _dispatch(activity: dict, name: str, frame) -> None:
    if name == "record":
        rec = _extract(frame, _RECORD_FIELDS)
        if rec.get("timestamp") is not None:
            activity["records"].append(rec)
    elif name == "lap":
        activity["laps"].append(_extract(frame, _LAP_FIELDS))
    elif name == "event":
        activity["events"].append(_extract(frame, _EVENT_FIELDS))
    elif name == "split":
        activity["splits"].append(_extract(frame, _SPLIT_FIELDS))
    elif name == "session":
        activity["session"] = _extract(frame, _SESSION_FIELDS)
    elif name == "file_id":
        activity["file_id"] = _extract(frame, _FILE_ID_FIELDS)
    elif name == "workout":
        activity["workout"] = {"wkt_name": frame.get_value("wkt_name", fallback=None)}
    elif name == "workout_step":
        activity["workout_steps"].append(_extract(frame, _WORKOUT_STEP_FIELDS))
    elif name == "time_in_zone":
        activity["time_in_zone"].append(_extract(frame, _TIME_IN_ZONE_FIELDS))
    # everything else (hrv, gps_metadata, user_profile, zones_target, ...)
    # is present in the files but unused by the MVP pipeline.


def _finalize(activity: dict) -> None:
    activity["records"].sort(key=lambda r: r["timestamp"])
    activity["laps"].sort(key=lambda l: (l.get("start_time") or _MIN_KEY,))
    activity["workout_steps"].sort(key=lambda s: s.get("message_index") or 0)
    splits = activity["splits"]
    if splits and all(s.get("start_time") is not None for s in splits):
        splits.sort(key=lambda s: s["start_time"])


class _MinKey:
    def __lt__(self, other):
        return True

    def __gt__(self, other):
        return False


_MIN_KEY = _MinKey()


def dedup_key(activity: dict) -> str:
    """Idempotent-upload identity: file_id.serial_number + time_created."""
    fid = activity.get("file_id") or {}
    return f"{fid.get('serial_number')}:{fid.get('time_created')}"


def timer_pauses(activity: dict) -> list[tuple]:
    """(stop_time, restart_time) pairs from timer events.

    Detected from event messages (event='timer', stop_all/start); callers
    can cross-check against total_elapsed_time > total_timer_time.
    """
    pauses = []
    stop_at = None
    for ev in activity["events"]:
        if ev.get("event") != "timer" or ev.get("timestamp") is None:
            continue
        et = ev.get("event_type")
        if et in ("stop_all", "stop") and stop_at is None:
            stop_at = ev["timestamp"]
        elif et in ("start",) and stop_at is not None:
            pauses.append((stop_at, ev["timestamp"]))
            stop_at = None
    return pauses
