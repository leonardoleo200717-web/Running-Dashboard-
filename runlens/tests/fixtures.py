"""Synthetic fixtures reproducing the verified FIT structure of the
validation set (spec 5.4).

The real 7 FR265 files are not committed to the repo; these builders
recreate their verified message-level structure (mixed autolap+manual
triggers, junk laps, degenerate splits, anomalous split merging) so the
acceptance tests encode the same expected results. When the real files
are available, drop them in tests/data/ and point the same assertions at
ingest.parse_fit() output — the activity dict shape is identical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from runlens.ingest import empty_activity

UTC = timezone.utc


class Builder:
    """Sequentially appends laps/records/splits along a timeline."""

    def __init__(self, start: datetime, structured: bool = False,
                 wkt_name: str | None = None, sub_sport: str = "generic"):
        self.a = empty_activity()
        self.t = start
        self.start = start
        self.distance = 0.0
        self.a["file_id"] = {
            "serial_number": int(start.timestamp()),
            "time_created": start,
            "manufacturer": "garmin",
            "product": "fr265",
            "type": "activity",
        }
        if structured:
            self.a["workout"] = {"wkt_name": wkt_name}
        self.sub_sport = sub_sport
        self.a["events"].append({"timestamp": start, "event": "timer",
                                 "event_type": "start", "data": None})

    def step(self, **kw) -> "Builder":
        kw.setdefault("message_index", len(self.a["workout_steps"]))
        for key in ("duration_type", "duration_distance", "duration_time",
                    "duration_step", "repeat_steps", "intensity",
                    "target_type", "custom_target_speed_low",
                    "custom_target_speed_high", "wkt_step_name", "notes"):
            kw.setdefault(key, None)
        self.a["workout_steps"].append(kw)
        return self

    def lap(self, duration_s: float, distance_m: float, speed: float | None = None,
            hr: float | None = None, trigger: str = "manual",
            wkt_step_index: int | None = None, records: bool = True) -> "Builder":
        start = self.t
        end = start + timedelta(seconds=duration_s)
        speed = speed if speed is not None else (distance_m / duration_s if duration_s else 0)
        self.a["laps"].append({
            "start_time": start, "timestamp": end,
            "total_distance": distance_m,
            "total_timer_time": duration_s, "total_elapsed_time": duration_s,
            "avg_speed": speed, "enhanced_avg_speed": speed,
            "avg_heart_rate": hr, "max_heart_rate": (hr + 6) if hr else None,
            "lap_trigger": trigger, "wkt_step_index": wkt_step_index,
            # free-run lap.intensity is a meaningless 'interval' default
            # (spec 2.4) — set it to prove the engine never reads it
            "intensity": "interval",
            "message_index": len(self.a["laps"]),
        })
        if records and duration_s:
            n = int(duration_s)
            for i in range(n):
                self.distance += speed
                self.a["records"].append({
                    "timestamp": start + timedelta(seconds=i),
                    "distance": self.distance,
                    "enhanced_speed": speed,
                    "heart_rate": int(hr) if hr else None,
                    "cadence": 170,
                })
        self.t = end
        return self

    def junk_lap(self) -> "Builder":
        """Double button press: sub-2s, sub-5m lap (spec 2.6)."""
        return self.lap(0.6, 1.5, hr=150, trigger="manual", records=False)

    def split(self, split_type: str, duration_s: float, distance_m: float,
              start: datetime | None = None) -> "Builder":
        s = start if start is not None else self._last_split_end()
        self.a["splits"].append({
            "split_type": split_type,
            "start_time": s, "end_time": s + timedelta(seconds=duration_s),
            "total_timer_time": duration_s, "total_elapsed_time": duration_s,
            "total_distance": distance_m,
            "avg_speed": distance_m / duration_s if duration_s else None,
        })
        return self

    def _last_split_end(self) -> datetime:
        if self.a["splits"]:
            return self.a["splits"][-1]["end_time"]
        return self.start

    def done(self) -> dict:
        self.a["events"].append({"timestamp": self.t, "event": "timer",
                                 "event_type": "stop_all", "data": None})
        timer = sum(l["total_timer_time"] for l in self.a["laps"])
        dist = sum(l["total_distance"] for l in self.a["laps"])
        hrs = [l["avg_heart_rate"] for l in self.a["laps"] if l["avg_heart_rate"]]
        self.a["session"] = {
            "start_time": self.start, "timestamp": self.t,
            "sport": "running", "sub_sport": self.sub_sport,
            "total_distance": dist, "total_timer_time": timer,
            "total_elapsed_time": timer,
            "avg_speed": dist / timer if timer else None,
            "enhanced_avg_speed": dist / timer if timer else None,
            "avg_heart_rate": sum(hrs) / len(hrs) if hrs else None,
            "max_heart_rate": max(hrs) + 6 if hrs else None,
            "num_laps": len(self.a["laps"]),
        }
        return self.a


# ------------------------------------------------------------ 2026-07-02
# 8x400 + 6x300 + 4x200, R200m — structured, exact.

def structured_400_300_200() -> dict:
    b = Builder(datetime(2026, 7, 2, 17, 30, tzinfo=UTC), structured=True,
                wkt_name="8x400 + 6x300 4x200 p200")
    b.step(duration_type="time", duration_time=900, intensity="warmup")           # 0
    b.step(duration_type="distance", duration_distance=400, intensity="active")   # 1
    b.step(duration_type="distance", duration_distance=200, intensity="recovery") # 2
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=8)   # 3
    b.step(duration_type="distance", duration_distance=300, intensity="active")   # 4
    b.step(duration_type="distance", duration_distance=200, intensity="recovery") # 5
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=4, repeat_steps=6)   # 6
    b.step(duration_type="distance", duration_distance=200, intensity="active")   # 7
    b.step(duration_type="distance", duration_distance=200, intensity="recovery") # 8
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=7, repeat_steps=4)   # 9
    b.step(duration_type="open", intensity="cooldown")                            # 10

    b.lap(900, 2500, hr=138, trigger="manual", wkt_step_index=0)
    for _ in range(8):
        b.lap(78, 401, hr=172, trigger="manual", wkt_step_index=1)
        b.lap(80, 201, hr=150, trigger="manual", wkt_step_index=2)
    for _ in range(6):
        b.lap(57, 300, hr=174, trigger="manual", wkt_step_index=4)
        b.lap(80, 202, hr=152, trigger="manual", wkt_step_index=5)
    for _ in range(4):
        b.lap(36, 199, hr=175, trigger="manual", wkt_step_index=7)
        b.lap(80, 200, hr=153, trigger="manual", wkt_step_index=8)
    b.lap(600, 1700, hr=140, trigger="manual", wkt_step_index=10)

    # Garmin also emits splits for structured sessions — 18 actives verified.
    for _ in range(18):
        b.split("interval_active", 60, 300)
        b.split("interval_recovery", 80, 200)
    return b.done()


# ------------------------------------------------------------ 2026-07-01
# Easy run, no intervals.

def easy_run() -> dict:
    b = Builder(datetime(2026, 7, 1, 17, 0, tzinfo=UTC))
    for _ in range(9):
        b.lap(285, 1000, hr=143, trigger="distance")
    b.lap(140, 490, hr=144, trigger="session_end")
    # Degenerate auto-split: one active spanning ~the whole session.
    b.split("interval_active", 2705, 9490, start=b.start)
    return b.done()


# ------------------------------------------------------------ 2026-06-30
# 8x1000 R~200m — free run, mixed autolap+manual triggers, junk laps,
# Garmin splits merged rep 1+2 (7 actives, first anomalous).

def free_8x1000() -> dict:
    b = Builder(datetime(2026, 6, 30, 17, 45, tzinfo=UTC))
    b.lap(300, 1000, hr=138, trigger="distance")   # warmup autolaps @ 1 km
    b.lap(300, 1000, hr=141, trigger="distance")
    rep_specs = [  # (trigger, distance): exact 1 km = autolap, early press = manual
        ("distance", 1000), ("manual", 995), ("distance", 1000), ("manual", 991),
        ("manual", 997), ("distance", 1000), ("manual", 993), ("distance", 1000),
    ]
    # Recoveries are distance-defined (~200 m) so the jogged duration
    # varies — matches the real file (202-212 m in 70-86 s).
    rec_specs = [(75, 209), (83, 202), (86, 212), (70, 204),
                 (81, 206), (79, 210), (75, 211), (77, 205)]
    for i, (trigger, dist) in enumerate(rep_specs):
        b.lap(180, dist, hr=176, trigger=trigger)
        if i == 3:
            b.junk_lap()                            # double press after rep 4
        rec_dur, rec_dist = rec_specs[i]
        b.lap(rec_dur, rec_dist, hr=152, trigger="manual")
    b.junk_lap()
    b.lap(300, 1000, hr=142, trigger="distance")    # cooldown
    b.lap(150, 500, hr=140, trigger="session_end")

    # Verified split anomaly: 7 actives, first one merged (1209 m / 298 s).
    b.split("interval_warmup", 600, 2000, start=b.start)
    b.split("interval_active", 298, 1209)
    b.split("interval_recovery", 75, 200)
    for _ in range(6):
        b.split("interval_active", 180, 1000)
        b.split("interval_recovery", 75, 200)
    return b.done()


# ------------------------------------------------------------ 2026-06-27
# Long run — degenerate split discarded, no intervals.

def long_run() -> dict:
    b = Builder(datetime(2026, 6, 27, 7, 30, tzinfo=UTC))
    for _ in range(24):
        b.lap(285, 1000, hr=152, trigger="distance")
    b.lap(60, 210, hr=152, trigger="session_end")
    b.split("interval_active", 6900, 24210, start=b.start)   # degenerate
    return b.done()


# ------------------------------------------------------------ 2026-06-25
# 15x1' R1' — free run, manual laps, rep/recovery discriminated by pace
# alternation (identical durations — spec 2.6).

def free_15x1() -> dict:
    b = Builder(datetime(2026, 6, 25, 17, 40, tzinfo=UTC))
    b.lap(340, 1000, hr=139, trigger="distance")   # warmup
    b.lap(340, 1000, hr=141, trigger="distance")
    # Recoveries are time-defined (1') so the jogged distance varies —
    # matches the real file (114-176 m, all ~60 s).
    rec_dists = [173, 176, 163, 159, 171, 160, 170, 124,
                 148, 127, 133, 135, 122, 114, 150]
    for i in range(15):
        b.lap(60, 330, hr=172, trigger="manual")   # rep: fast
        b.lap(60, rec_dists[i], hr=150, trigger="manual")  # recovery: slow, same duration
    b.lap(340, 1000, hr=143, trigger="distance")   # cooldown
    b.lap(100, 290, hr=141, trigger="session_end")

    # Verified: exactly 15 active splits.
    b.split("interval_warmup", 680, 2000, start=b.start)
    for _ in range(15):
        b.split("interval_active", 60, 330)
        b.split("interval_recovery", 60, 132)
    b.split("interval_cooldown", 440, 1290)
    return b.done()


# ------------------------------------------------------------ 2026-06-20
# 6x5' — structured.

def structured_6x5() -> dict:
    b = Builder(datetime(2026, 6, 20, 8, 0, tzinfo=UTC), structured=True,
                wkt_name="6x5' p1'30")
    b.step(duration_type="open", intensity="warmup")                              # 0
    b.step(duration_type="time", duration_time=300, intensity="active")           # 1
    b.step(duration_type="time", duration_time=90, intensity="recovery")          # 2
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=6)   # 3
    b.step(duration_type="open", intensity="cooldown")                            # 4

    b.lap(900, 2600, hr=137, trigger="manual", wkt_step_index=0)
    for _ in range(6):
        b.lap(300, 1450, hr=171, trigger="manual", wkt_step_index=1)
        b.lap(90, 250, hr=149, trigger="manual", wkt_step_index=2)
    b.lap(700, 1900, hr=139, trigger="manual", wkt_step_index=4)

    for _ in range(6):
        b.split("interval_active", 300, 1450)
        b.split("interval_recovery", 90, 250)
    return b.done()


# ------------------------------------------------------------ 2026-05-24
# 2x(10x1') R1' SR4' — structured; the 4' inter-block segment must NOT
# count as a rep (Garmin's own splits misclassified it — verified).

def structured_2x10x1() -> dict:
    b = Builder(datetime(2026, 5, 24, 8, 15, tzinfo=UTC), structured=True,
                wkt_name="2x(10x1') p1' P4'")
    b.step(duration_type="open", intensity="warmup")                              # 0
    b.step(duration_type="time", duration_time=60, intensity="active")            # 1
    b.step(duration_type="time", duration_time=60, intensity="recovery")          # 2
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=1, repeat_steps=10)  # 3
    b.step(duration_type="time", duration_time=240, intensity="rest")             # 4 set recovery
    b.step(duration_type="time", duration_time=60, intensity="active")            # 5
    b.step(duration_type="time", duration_time=60, intensity="recovery")          # 6
    b.step(duration_type="repeat_until_steps_cmplt", duration_step=5, repeat_steps=10)  # 7
    b.step(duration_type="open", intensity="cooldown")                            # 8

    b.lap(880, 2500, hr=136, trigger="manual", wkt_step_index=0)
    for _ in range(10):
        b.lap(60, 335, hr=170, trigger="manual", wkt_step_index=1)
        b.lap(60, 130, hr=149, trigger="manual", wkt_step_index=2)
    b.lap(240, 560, hr=140, trigger="manual", wkt_step_index=4)   # 4' set recovery
    for _ in range(10):
        b.lap(60, 332, hr=173, trigger="manual", wkt_step_index=5)
        b.lap(60, 128, hr=151, trigger="manual", wkt_step_index=6)
    b.lap(650, 1800, hr=138, trigger="manual", wkt_step_index=8)

    # Verified split bug: 21 actives — the 4' block misclassified as active.
    for _ in range(10):
        b.split("interval_active", 60, 335)
        b.split("interval_recovery", 60, 130)
    b.split("interval_active", 240, 560)   # the misclassified 4' block
    for _ in range(10):
        b.split("interval_active", 60, 332)
        b.split("interval_recovery", 60, 128)
    return b.done()


ALL = {
    "2026-07-02": structured_400_300_200,
    "2026-07-01": easy_run,
    "2026-06-30": free_8x1000,
    "2026-06-27": long_run,
    "2026-06-25": free_15x1,
    "2026-06-20": structured_6x5,
    "2026-05-24": structured_2x10x1,
}
