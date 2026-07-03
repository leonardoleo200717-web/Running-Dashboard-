"""Origin detection: structured workout vs free run.

Single reliable rule (spec 2.4): an activity is a structured workout iff a
`workout` message exists. Laps then carry non-null wkt_step_index, making
interval classification exact.

The manufacturer is passed through from file_id to leave room for COROS
support later; no COROS code paths exist in v1 (spec 5.5).
"""

from __future__ import annotations

STRUCTURED = "structured"
FREE = "free"


def detect_origin(activity: dict) -> dict:
    """Return {'origin', 'manufacturer', 'wkt_name'} for an activity dict."""
    fid = activity.get("file_id") or {}
    manufacturer = fid.get("manufacturer")

    workout = activity.get("workout")
    is_structured = workout is not None or bool(activity.get("workout_steps"))

    return {
        "origin": STRUCTURED if is_structured else FREE,
        "manufacturer": manufacturer,
        "wkt_name": (workout or {}).get("wkt_name"),
    }
