# RunLens — CLAUDE.md (v2.0)

Local-first running dashboard for Garmin FIT files (Forerunner 265).
One user (Leonardo, marathon goal 2h43), local files, no auth, no cloud.
**Design mandate: keep it simple.** v1 is BUILT and validated against
13 real FIT files (see §7). Next milestone is `PROPOSAL.md` (statistics
redesign — approved direction, awaiting user inputs, do not start
without them).

---

## 1. Core principle

Raw FIT content first, inference last. Pipeline strictly ordered, each
stage a pure function on plain dicts; DB access only in `store.py`:

```
ingest.py → origin.py → intervals.py → metrics.py → store.py → app.py
```

Time-first: all segmentation on UTC timestamps, never distance alone.
All computations use timer time, never elapsed (except pause analysis).
Display timezone: Europe/Amsterdam, conversion only at render.

---

## 2. Verified FIT ground truth (do not re-derive)

### 2.1 From the original FR265 analysis
- Timestamps UTC; records at 1 Hz (gaps at timer pauses).
- Pauses: `event` timer start/stop_all; cross-check
  `total_elapsed_time > total_timer_time`.
- Structured workout iff a `workout` message exists; laps then carry
  `wkt_step_index` (exact mapping). Otherwise free run.
- Free-run `lap.intensity` is always `'interval'` — meaningless, never use.
- `split`/`split_summary`: Garmin's own segmentation (`interval_*` types;
  ignore `rwd_*`). Useful but flawed (see 2.2).
- `lap_trigger`: `distance` (autolap), `manual`, `session_end`, `time`.
  Autolap and manual triggers coexist inside one rep block.
- Never classify laps by distance alone (autolap 1 km == rep 1 km).
- Time reps and recoveries can share identical durations (15x1'):
  discriminate by pace/HR alternation, never duration.

### 2.2 Discovered on real files during v1 (all covered by tests)
- **Garmin-Connect exports** (`YYYYMMDD_running_*.fit`) carry a bogus lap
  `timestamp` (= session start on every lap). Lap end must be derived as
  `start_time + total_elapsed_time`. Watch-direct `*_ACTIVITY.fit` files
  are fine. Handled in `ingest._finalize`.
- **Autolap keeps firing inside workout steps** longer than 1 km: one 15'
  step arrives as 1000+1000+1000+664 m laps sharing one
  `wkt_step_index`. Consecutive same-index laps = one step execution →
  merge. True repeats never merge (a recovery lap with another index
  always sits between reps).
- **Step tail laps**: autolap firing just before a time step ends leaves
  real 2–4 s laps carrying the step index. Never junk-drop laps that
  have a `wkt_step_index` (3x8' otherwise reads 3x7'58").
- **Junk laps** (double press): duration < 5 s (real 2.6 s case) or
  distance < 5 m, only for laps without a step index.
- **User-created workouts** mark warmup/cooldown steps
  `intensity='active'`, `duration_type='open'` → classify positionally
  (open active step outside the repeat block, before it = warmup,
  after = cooldown).
- **Garmin suggested workouts** ("Training some speed") type the warmup
  as a *distance* step, `active`, but with a target speed band strictly
  below the rep-step targets (e.g. 3.30–3.45 vs 3.77–4.0 m/s) → compare
  target bands.
- **Splits merge/lose reps**: 8x1000 → 7 actives (first anomalous
  1209 m); 5x(1000+300) → whole sets merged into ~1400 m actives
  matching no lap. Splits must be cross-validated against cleaned laps
  and rejected when they disagree (more rep-like laps than actives, or
  < 50% of actives match any lap).
- **GPS under-reads short track reps** by up to ~4% (288.8 m for a
  300 m rep) — rep snap tolerance is 4%; recoveries are approximate
  jogs — 15% tolerance for labels.
- **Three pace levels** exist in free interval sessions (recovery ~6:00,
  warmup ~4:50, reps ~3:45 /km): a fast/slow largest-gap threshold can
  land below warmup pace. Demote "reps" slower than 0.88 × the
  rep-cluster median speed.
- Rep 1 pressed late gives a short rep (901 m at rep pace in a 6x1000):
  group by pace similarity (±8%) + duration (±15%); label by majority
  snap (≥ 80%).
- Strides after an easy run (5x100m): detected, but < 5% of session
  timer in reps keeps session_type = easy/long.

---

## 3. Interval engine (implemented, `intervals.py`)

Hierarchical; first level that succeeds wins; every session records
`detection_source` + `confidence`:

1. `structured_workout` — step-kind map (with §2.2 warmup/cooldown
   corrections), consecutive same-index lap merging, `duration_hint`
   from `duration_type` (time-defined steps label by time even when
   meters snap: a 12' block is never "3000m"). High confidence.
2. `garmin_splits` — degenerate discarded (1 active ≥ 85% of session),
   outliers flagged (> 1.5× modal duration/distance), count
   cross-validated vs cleaned laps; falls through when splits lost or
   invented reps. High if counts match, else medium.
3. `lap_inference` — junk cleaning → manual-lap block → two-cluster
   pace split at largest gap (≥ 30% of spread, ≥ 60% alternation) →
   demote slow "reps" (0.88 × median) → extend block over adjacent
   autolap reps at rep pace → leading laps = warmup, trailing =
   cooldown, trailing recoveries = cooldown. Medium confidence.
4. `unclassified` — stub, low confidence, flagged "tag manually".

Normalization / labels:
- Canonical distances 100–10000 m, snap ±4% (reps), ±15% (recoveries).
- Time labels: stored exact; display snaps to nearest 5 s when within
  2 s (61.4 s rep of a programmed 1' reads 1').
- Rep-group labels: all-snap → `8x1000m`; majority snap (≥ 80%) covers
  one corrupted rep; else the tighter dimension wins (distance CV ≤ 8%
  and < duration CV → `5x100m`; else modal time → `15x1'`); single rep
  groups have no `1x` prefix (`15' + 10' + 8'`).
- Recovery labels: dimension the runner kept constant (CV comparison;
  structured `duration_hint` decides directly; tie → distance if it
  snaps at 8%).
- Sets: recovery durations split at the largest gap (gap ≥ max(30 s,
  short median), long cluster ≥ 60 s) → `SR`; uniform or
  majority-with-truncated sets → `5x(400m + 300m + 200m) R100m SR400m`.
- Structure grammar: `8x400m R200m`, `15x1' R1'`, `2x(10x1') R1' SR4'`.

Classification: race (sub_sport) / intervals / fartlek (lap_inference +
irregular durations >15% and no regular distances) / long (≥ 18 km or
≥ 95') / easy / unknown. Strides rule (< 5% rep time) before all.
**Manual override always wins and survives re-parse** — session type,
structure, and per-segment kind (`segkind:<start_time_iso>` overrides,
applied in `store.get_intervals`, KPIs+structure recomputed).

---

## 4. Functional state (v1 complete)

Pages (Flask, server-rendered, vendored Chart.js — works offline):
- **Sessions** — weekly/monthly/yearly time blocks with totals;
  drag-and-drop .FIT import anywhere on the page + file picker;
  idempotent re-upload (dedup `serial_number + time_created`;
  re-parse updates analysis, preserves overrides/confirmed points).
- **Session detail** — stat row; Phases table (warmup / work /
  recoveries / cooldown: distance, time, pace, HR); rep table; HR/pace
  drift (first vs last third); recovery HR drop; pace & HR charts
  (reversed pace axis, faster = up); Segments editor: per-segment
  dropdown Active/Recovery/WU/CD, auto-submit, recompute; Similar
  sessions box; manual override form.
- **Trends** — pace @ HR 165–175, HR @ pace 3:50–3:55, weekly volume;
  5-point rolling average + linear fit; "Am I improving?" verdict panel.
  (⚠ misleading on interval-heavy data — being redesigned, PROPOSAL.md.)
- **Progression** — spec 4.1 tracker (the one dual-axis chart), seeded
  anchors (Jun 2025 4:02/km @ 173, Jun 2026 3:52/km @ 175),
  propose→confirm/reject flow, ≥ 600 s / CV ≤ 5% / no-pause rule.
- **Similar sessions** — fuzzy clusters: distance-rep sessions by rep
  distance set regardless of count (6x1k with 8x1k), time reps by total
  work ± 15%, runs by distance ± 10%; compare page overlays pace AND HR
  per rep, one line per session (max 8, fixed categorical palette),
  avg rep pace table.
- **Race predictor** — best rolling-window efforts (1k/1609/3k/5k/10k,
  last 120 days), longest as anchor; Riegel, Daniels-Gilbert VDOT,
  Cameron + consensus for 5K/10K/HM/M; VDOT figure; caveats shown.
  (⚠ anchor not race-equivalent on interval data — PROPOSAL.md tier
  redesign pending.)

Split-derived segments get HR backfilled from records at ingest.

## 5. Software state

- Python 3.11, fitdecode, Flask, pytest; Chart.js vendored in
  `runlens/static/` (single UMD file — offline-first; CDN swap possible).
- SQLite `runlens.db`: `sessions` (incl. detection_source, confidence,
  structure, kpis_json), `intervals`, `records` (full 1 Hz),
  `user_overrides` (session-level + `segkind:` per-segment),
  `progression_points` (proposed/confirmed/rejected, seeded anchors).
- Layout: `runlens/{ingest,origin,intervals,metrics,store,app}.py`,
  `templates/`, `static/`, `tests/`.
- `metrics.py` also holds: trend series (rolling + regression),
  improvement stats, clustering, best-effort two-pointer, Riegel /
  VDOT / Cameron models, phase summaries, HR backfill.
- Section-6 decisions taken (spec's own proposals): HR band 165–175,
  full 1 Hz storage, manual upload (+ drag-drop).
- Records `power` field exists on FR265 but is NOT stored yet (needed
  for PROPOSAL.md §3.6 — schema tweak + re-import).

## 6. Tests — 40 passing, must stay green

`runlens/tests/`: original 7-case validation set (spec v1 §5.4, synthetic
fixtures reproducing verified structures) + regressions for every real
file finding + unit tests (normalization, junk, metrics, store
idempotency, override persistence, progression flow, prediction model
sanity vs Daniels tables, best-effort window) + end-to-end Flask test.
Fixtures must carry realistic variance (constant synthetic values create
CV ties real data never has). Real FIT files are NOT committed; builders
in `tests/fixtures.py` mimic them. A change that breaks any expected
structure fails CI.

## 7. Real-file validation ledger (ground truth, user-confirmed)

| Date | Truth | Engine result |
|---|---|---|
| 2025-12-27 | warmup + 4x1500 R500m (suggested wkt) | ✓ `4x1500m R500m` |
| 2026-02-22 | warmup + 6x1500 R500m (suggested wkt) | ✓ `6x1500m R500m` |
| 2026-04-02 | 6x1000 (rep 1 short-measured, junk lap) | ✓ `6x1000m R200m` |
| 2026-04-23 | easy 75' + 5x100m strides | ✓ easy, `5x100m R100m` |
| 2026-04-26 | watch wkt "5x1.5km p3'" (user said 5x1000 — FIT is unambiguous) | ✓ `5x1500m R3'` |
| 2026-05-16 | easy/long 16.2 km | ✓ easy, no intervals |
| 2026-05-17 | 15'+10'+8' R3' (structured, steps span autolaps) | ✓ `15' + 10' + 8' R3'` |
| 2026-05-26 | 5x(400+300+200) p100 SP400 (first 400 botched) | ✓ `5x(400m + 300m + 200m) R100m SR400m` |
| 2026-06-02 | 5x(1000 p100 + 300) p400 (splits merged sets) | ✓ `5x(1000m + 300m) R100m SR400m` |
| 2026-06-04 | 3x8' 3x3' 3x1' (step tail laps) | ✓ `3x8' + 3x3' + 3x1' R1'` |
| 2026-06-06 | 2x12' + 5x1' | ✓ `2x12' + 5x1' R1'` |
| 2026-06-25 | 15x1' R1' (pace alternation) | ✓ `15x1' R1'` |
| 2026-06-30 | 8x1000 R~200m (3 pace levels, split merge) | ✓ `8x1000m R200m` |

## 8. Next: PROPOSAL.md (approved direction, blocked on inputs)

Steady Effort Store (stabilized HR: drop first ~45 s of reps, no HR
stats from reps < 90 s) → effort domains → REI/EF efficiency module,
within-session HR↔pace regression (normalized performance, pace cost),
HR drift, automatic insights, personal historical model → per-domain
trends replacing current band trends → 3-tier race predictor (races /
Critical Speed / volume-corrected marathon) → compare-view selection
bar (chips toggling all charts, solo, rep#↔work-time axis).

**Blocked on user inputs (ask before building):** max HR / LTHR;
normalization references (3:40/km, 175 bpm); race history; power in v1.

## 9. Non-goals (unchanged)

COROS (manufacturer passthrough exists, no code paths), multi-user,
cloud sync, GPS maps, running dynamics. The old session-comparison
table stays dead; the tracker + rep tables + cluster compare are the
comparison surfaces.

## 10. Working agreements (learned)

- Validate every engine change against the §7 ledger before pushing.
- Never trust device averages; recompute from distance/timer.
- Prefer an honest "–" or "low confidence" over a misleading number.
- User feedback overrides spec text (spec said ±3% snap and never-round
  times; real data and the user said otherwise).
- git: work on `claude/dashboard-md-file-ea34nj` (repo default branch —
  the repo was empty at project start, so no PR flow exists).
