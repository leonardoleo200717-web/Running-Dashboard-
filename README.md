# RunLens

Local-first running dashboard for Garmin FIT files (Forerunner 265).
One user, local files, no auth, no cloud. Built to the spec in
[CLAUDE.md](CLAUDE.md); the durable regeneration spec is
[Running.MD](Running.MD).

Two equivalent implementations share the same validated engine logic:

| | Python (`runlens/`) | PWA (`docs/`) |
|---|---|---|
| Runs on | your computer (Flask + SQLite) | **any phone or computer, in the browser** |
| Install | `pip install`, run server | open the page once, "Add to Home Screen" |
| Data | `runlens.db` file | IndexedDB, on-device, never leaves it |
| FIT parsing | fitdecode | own parser, byte-validated against fitdecode |
| Engine tests | 52 pytest | 13 node tests + 13/13 real-file ledger |

## PWA (Android / iPhone / desktop — no server)

The `docs/` folder is a static Progressive Web App: FIT parsing, the
full interval engine, the efficiency module, clustering, predictor and
charts all run **in the browser**. Nothing executes server-side; your
files and data stay on the device (IndexedDB), and it works fully
offline after the first visit.

**Host it free on GitHub Pages** (repo → Settings → Pages → "Deploy from
a branch" → branch `claude/dashboard-md-file-ea34nj`, folder `/docs`).
Note GitHub Pages requires the repository to be public (or a paid plan
for private repos). Then open the URL on your phone, tap the browser
menu → **Add to Home Screen** — it installs like an app. On the phone,
Garmin Connect → activity → export/share the FIT straight into it, or
pick files from Downloads.

To develop locally: `python3 -m http.server -d docs 8000` and open
http://localhost:8000 (ES modules need http, not file://).

Validation tooling (Node ≥ 20, no npm install needed):
```bash
node --test docs/tests/engine.test.mjs        # engine unit tests
node tools/validate_ledger.mjs <dir-of-fits>  # 13-session ground truth
node tools/validate_parser.mjs <dir> <truth>  # parser vs fitdecode dump
```

## Python version — quick start

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
- **Time blocks** — the session list groups into weekly / monthly / yearly
  blocks with volume totals; FIT files can be dragged & dropped anywhere
  on the page to import.
- **Phase overview** — every session shows warmup / work / recoveries /
  cooldown with distance, duration, pace and HR.
- **Trend modeling** — the trends charts carry a 5-point rolling average
  and a linear-regression fit, plus an "Am I improving?" panel with
  yes/no verdicts (pace @ HR slope, HR @ pace slope, 4-week volume).
- **Similar sessions** — fuzzy clustering groups comparable workouts
  (same rep distances regardless of count, time intervals within ±15 %
  total work, runs within ±10 % distance) with an overlapped
  pace-per-rep comparison chart.

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
