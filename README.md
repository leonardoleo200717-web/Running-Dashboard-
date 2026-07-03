# RunLens

Local-first running dashboard for Garmin FIT files (Forerunner 265).
One user, local files, no auth, no cloud. Built to the spec in
[CLAUDE.md](CLAUDE.md).

## Quick start

```bash
pip install -r requirements.txt
python -m flask --app runlens.app run
# open http://127.0.0.1:5000 and drag your .FIT files into the upload form
```

The database is a single SQLite file (`runlens.db`, created on first run).
Re-uploading the same file is idempotent (dedup by
`file_id.serial_number` + `time_created`).

## What it does

Pipeline (strictly ordered, each stage a pure function):

```
FIT ingestion → origin detection → interval engine → metric computation → dashboard
```

- **Origin detection** — structured workout iff a `workout` message exists;
  otherwise free run. `lap.intensity` is never used on free runs.
- **Interval engine** — hierarchical resolution, first level that succeeds
  wins; every session records `detection_source` + `confidence`:
  1. `structured_workout` — exact `wkt_step_index` mapping (high)
  2. `garmin_splits` — `interval_*` auto-splits, degenerate/outlier cases
     discarded, count cross-validated against cleaned laps (high/medium).
     If Garmin merged/lost reps, falls through to lap inference.
  3. `lap_inference` — junk laps dropped (<2 s or <5 m), rep/recovery by
     pace alternation on manual laps, mixed autolap+manual handled (medium)
  4. `unclassified` — stub, flagged "tag manually" (low)
- **Manual overrides always win** and persist across re-parses.
- **KPIs** — per-session rep table, intra-set HR/pace drift, recovery HR
  drop; trends: pace @ HR 165–175, HR @ pace 3:50–3:55, weekly volume,
  intensity distribution.
- **Aerobic progression tracker** — steady blocks (≥10 min timer time, pace
  CV ≤ 5 %, no pauses, never a recovery) are auto-detected and *proposed*;
  each point enters the tracker only after one-click confirmation. Seeded
  with the Jun 2025 (4:02/km @ 173) and Jun 2026 (3:52/km @ 175) anchors.

## Layout

```
runlens/
  ingest.py      # FIT → normalized message dicts (pure, no DB)
  origin.py      # structured vs free classification
  intervals.py   # hierarchical engine (levels 1–4)
  metrics.py     # KPIs, trends, progression eligibility
  store.py       # SQLite layer (the only module touching the DB)
  app.py         # Flask routes
  templates/     # server-rendered pages
  static/        # vendored Chart.js (single file — works offline)
  tests/         # pytest: acceptance tests for the 7-file validation set
```

## Tests

```bash
python -m pytest runlens/tests/
```

The acceptance tests encode the validation set from CLAUDE.md §5.4 with
synthetic fixtures reproducing the verified FIT structures (mixed
autolap+manual triggers, junk laps, degenerate splits, Garmin's merged-rep
split anomaly, the 4′ inter-block segment that must not count as a rep).
When the real FIT files are available, drop them in `runlens/tests/data/`
and point the same assertions at `ingest.parse_fit()` output.

## Notes / deviations from spec

- Chart.js is vendored in `runlens/static/` instead of loaded from a CDN:
  same single-file, no-build-step simplicity, but the dashboard keeps
  working offline, which fits local-first. (Swap the `<script>` tag in
  `templates/base.html` back to the CDN if preferred.)
- Section 6 open decisions taken with the spec's own proposals: HR band
  165–175 bpm, full 1 Hz record storage, manual upload only.
