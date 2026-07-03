# RunLens — CLAUDE.md (v1.0)

Local-first running dashboard for Garmin FIT files. Successor to the interval
training dashboard; redesigned from scratch around **empirically verified FIT
structure** (7 real files from a Garmin Forerunner 265, May–July 2026).

**Design mandate: keep it simple.** One user, local files, no auth, no cloud.
Every feature not in MVP scope is explicitly deferred.

---

## 1. Core principle

Raw FIT content first, inference last. Pipeline is strictly ordered:

```
FIT ingestion → origin detection → interval engine → metric computation → dashboard
```

The system is **time-first**: all segmentation and alignment is done on UTC
timestamps, never on distance alone.

---

## 2. Verified FIT structure (ground truth — do not re-derive)

Findings from real FR265 files. Treat as constraints, not assumptions.

### 2.1 Timestamps
- All timestamps are UTC (`datetime` with tzinfo). Convert to local
  (Europe/Amsterdam) only at display time.
- `record` messages are sampled at **1 Hz** (verified; tolerate gaps from
  timer pauses).
- Timer pauses are detectable two ways (both must agree):
  - `event` messages: `event='timer'`, `event_type='start'/'stop_all'`
  - `total_elapsed_time > total_timer_time` on laps/session
- All computations use **timer time**, never elapsed time, unless explicitly
  analyzing pauses.

### 2.2 Message types present (FR265 activity files)
`file_id`, `session`, `lap`, `event`, `record`, `split`, `split_summary`,
`workout` + `workout_step` + `training_file` (structured workouts only),
`time_in_zone`, `hrv`, `gps_metadata`, `user_profile`, `zones_target`.
Many `unknown_*` messages exist — ignore them, never crash on them.

### 2.3 Record fields available
`timestamp`, `distance`, `enhanced_speed`, `heart_rate`, `cadence`, `power`,
`enhanced_altitude`, `step_length`, `vertical_oscillation`, `vertical_ratio`,
`stance_time`, `position_lat/long`, `enhanced_respiration_rate`.
MVP uses: timestamp, distance, enhanced_speed, heart_rate, cadence.

### 2.4 Origin detection (single reliable rule)
An activity is a **structured workout** iff a `workout` message exists
(equivalently: `training_file` present, and laps carry non-null
`wkt_step_index`). Otherwise it is a **free run**.

Structured workout metadata:
- `workout.wkt_name` — human-readable name (e.g. `"8x400 + 6x300 4x200 p200"`)
- `workout_step` list: `duration_type` (`distance`/`time`/`open`/
  `repeat_until_steps_cmplt`), `duration_distance`/`duration_time`,
  `intensity` (`warmup`/`active`/`recovery`/`cooldown`), target speed range,
  and repeat encoding (`duration_step` = index to jump back to,
  `repeat_steps` = count).
- Each lap's `wkt_step_index` maps it to its step → interval classification
  is **exact, zero inference**.

Free run signatures:
- `wkt_step_index` is `None` on all laps.
- `lap.intensity` is always `'interval'` — **a meaningless default. Never
  use lap.intensity on free runs.**

### 2.5 Garmin auto-splits (`split` / `split_summary`) — second truth source
Garmin generates its own interval segmentation, **even on free runs**:
`split_type` ∈ `interval_warmup`, `interval_active`, `interval_recovery`,
`interval_cooldown` (plus `rwd_run/rwd_walk/rwd_stand` — ignore rwd_* for
interval detection). Each split has start/end time, timer time, distance,
avg speed.

Verified accuracy on the validation set:
- 15×1′ free run → exactly 15 active splits ✓
- 6×5′ structured → exactly 6 ✓
- 8×400+6×300+4×200 structured → exactly 18 ✓
- 2×(10×1′) → 21 actives: the 4′ inter-block segment misclassified as
  active ✗
- 8×1000 free → 7 actives, first one anomalous (1209 m / 298 s): one rep
  merged/lost ✗
- Long run → 1 active spanning the whole session (degenerate) ✗

**Rules:** discard the degenerate case (single active ≈ whole session);
cross-validate split count and durations against laps; flag outlier splits
(duration or distance > 1.5× the modal value within the set).

### 2.6 Lap-level traps (verified)
- `lap_trigger` values: `distance` (autolap), `manual`, `session_end`.
- **Autolap coexists with manual laps in the same session**, including
  inside the rep block (8×1000 session: reps triggered by `distance` when
  exactly 1000 m, by `manual` when pressed early at 991–997 m).
- Autolap 1 km can fire at the same distance as the reps → distance-based
  lap grouping is ambiguous by construction. Never classify by distance
  alone.
- **Junk laps** from double button presses: duration < 2 s or distance
  < 5 m. Drop them before any analysis.
- Time-based reps and recoveries can have identical durations (15×1′:
  both ~60 s). Discriminate by pace/HR alternation, not duration.

---

## 3. Interval engine — hierarchical resolution

Apply the first level that succeeds; record which level was used
(`detection_source`) and a `confidence` (high/medium/low) on every session.

1. **Structured workout** (`workout_step` + `wkt_step_index`): exact
   mapping. Confidence: high. Structured data fully overrides lap
   interpretation.
2. **Garmin auto-splits**: use `interval_*` splits after discarding
   degenerate/outlier cases and cross-checking count against cleaned laps.
   Confidence: high if splits and laps agree, medium otherwise.
3. **Lap inference** (free runs where splits fail): clean junk laps →
   segment by pace/HR alternation on manual-lap sequences → identify
   warmup (leading autolap block at easy pace) and cooldown (trailing).
   Confidence: medium.
4. **Record-level fallback**: change-point detection on 1 Hz speed/HR
   series. Confidence: low. MVP: implement as stub that returns
   "unclassified — tag manually".

**Manual override always wins.** User tags (session type, rep structure)
are stored and take precedence over any inference on re-parse.

### 3.1 Normalization
- Distance reps: snap to canonical values when within ±3% (398→400 m,
  992→1000 m). `1km` = `1000m` = `1 km`.
- Time reps: **never round**. 1′ is 1′, stored in seconds.
- Structure string format: `8x400m R200m`, `15x1' R1'`, `2x(10x1') R1' SR4'`
  (R = recovery, SR = set recovery).

### 3.2 Session classification
`easy` / `long` / `intervals` / `fartlek` / `race` / `unknown`, inferred
from: presence of interval structure, distance, avg pace vs HR, session
`sub_sport`. Always user-overridable. Overrides persist.

---

## 4. KPIs (MVP — deliberately minimal)

Per session:
- Rep table: distance/duration, pace, avg/max HR per rep
- Intra-set drift: HR drift and pace drift across reps (first vs last third)
- Recovery quality: HR drop during recoveries; `recovery_hr` events
  (present in files) as supporting data

Trends (the core value — aerobic efficiency toward the 2h43 goal):
- **Pace @ HR**: pace at fixed HR bands (e.g. 165–175 bpm) over time,
  computed from record data of comparable sessions
- **HR @ pace**: avg HR at fixed pace bands (e.g. 3:50–3:55/km) over time
- Weekly volume (km) and intensity distribution (time in easy / moderate /
  hard, from HR zones in `time_in_zone` or user zone config)

### 4.1 Aerobic progression tracker (replaces the external React artifact)
Single time-series chart: pace (y1) and avg HR (y2) per data point, with
existing anchor points seeded manually (Jun 2025: 4:02/km @ 173 bpm;
Jun 2026: 3:52/km @ 175 bpm).

**Eligibility rule — a data point is created only from a continuous block
≥ 10 minutes of timer time** at steady effort. Concretely, a segment
qualifies iff:
- duration ≥ 600 s of timer time, no timer pause inside the block
- steady effort: pace coefficient of variation within the block ≤ 5%
  (computed on 1 Hz records)
- it is a rep/tempo block or a continuous run segment — never a recovery,
  never stitched across recoveries

Qualifying blocks are auto-detected and **proposed**, not auto-added: the
user confirms each point (one click) before it enters the tracker. This
keeps the series clean and comparable, which was the whole point of the
manual artifact.

**Explicitly no comparison table.** The previous dashboard's
session-comparison table was noisy and is not rebuilt. The tracker chart +
per-session rep tables are the only comparison surfaces in v1.

Deferred to v2 (do not build now): race prediction ensemble, multi-model
fatigue, similar-session comparison engine, target pace curve tracking.

---

## 5. Software requirements

### 5.1 Stack (keep it boring)
- **Python 3.11+**, single small package
- FIT parsing: `fitdecode`
- Storage: **SQLite**, one file (`runlens.db`). Tables: `sessions`,
  `intervals`, `records` (downsampled or on-demand), `user_overrides`
- Web: **Flask** (single app file acceptable), server-rendered pages +
  **Chart.js** via CDN. No build step, no npm, no framework
- Ingestion: drag-and-drop upload or watched folder; re-upload of the same
  file (dedup by `file_id.serial_number` + `time_created`) is idempotent

### 5.2 Architecture
```
runlens/
  ingest.py      # FIT → normalized message dicts (pure, no DB)
  origin.py      # structured vs free classification
  intervals.py   # hierarchical engine (levels 1–4)
  metrics.py     # KPIs, trends
  store.py       # SQLite layer
  app.py         # Flask routes + templates
  tests/         # pytest against the validation set
```
Each stage is a pure function on plain data structures; DB access only in
`store.py`. No stage may reach back into raw FIT after `ingest.py`.

### 5.3 Error handling
- Unknown/corrupt messages: skip and log, never crash
- Missing fields (`avg_speed` null etc.): compute manually from
  `total_distance / total_timer_time` — never trust device averages blindly
- Every parsed session persists `detection_source` and `confidence`;
  low-confidence sessions are visually flagged for manual tagging

### 5.4 Validation set (must-pass acceptance tests)
| File (date) | Expected result |
|---|---|
| 2026-07-02 | 8×400 + 6×300 + 4×200, R200 m — structured, exact |
| 2026-07-01 | easy run, no intervals |
| 2026-06-30 | 8×1000 R~200 m — free run, mixed autolap+manual, junk laps removed |
| 2026-06-27 | long run — degenerate split discarded, no intervals |
| 2026-06-25 | 15×1′ — free run, manual laps, rep/recovery by pace alternation |
| 2026-06-20 | 6×5′ — structured |
| 2026-05-24 | 2×(10×1′) R1′ SR4′ — structured; 4′ block must NOT count as rep |

pytest fixtures = these 7 files. A change that breaks any expected
structure fails CI.

### 5.5 Non-goals (v1)
COROS support (design leaves room: `origin.py` takes a manufacturer field
from `file_id`, but no COROS code paths), multi-user, cloud sync, GPS maps,
running dynamics analysis, race prediction.

---

## 6. Open decisions (resolve with Leonardo before coding)

1. HR improvement definition: default proposal = pace @ 170 bpm on
   comparable continuous blocks, monthly aggregate. Confirm band.
2. Records storage: full 1 Hz (~5–6k rows/session) vs 5 s downsample.
   Proposal: full, SQLite handles it easily at this volume.
3. Watched folder vs manual upload only. Proposal: manual upload for MVP.
