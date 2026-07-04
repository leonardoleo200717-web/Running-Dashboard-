# RunLens — Statistics Redesign Proposal (v1)

Status: **proposal only — no code changes yet.**
Goal: every number the dashboard shows must be physiologically meaningful
for an interval-heavy training load. No statistic may silently mix reps,
recoveries, warmups and easy running.

---

## 1. Diagnosis: why the current stats can mislead

| Statistic | Problem |
|---|---|
| Pace @ HR / HR @ pace | Filters 1 Hz samples by HR band, but on interval days the band catches transients: the first ~40 s of a rep (HR still climbing while running 3:45/km → flatters) and the start of recoveries (HR still high while jogging → penalizes). The trend slope is partly built on these transients. |
| Race predictor anchor | Best rolling 10 km window spans reps *and* recoveries of an interval session — not a race-equivalent effort. Everything extrapolated from it inherits the error. |
| Trend lines | Linear fit over mixed-context points models noise, not trajectory. |
| Session avg pace in lists | Meaningless for interval sessions (mixes 3:45 reps with 6:30 jogs). |

The **aerobic progression tracker** is already built correctly (steady
blocks ≥ 10', pace CV ≤ 5%, no pauses, user-confirmed) — this proposal
extends that same discipline to every other statistic.

---

## 2. Foundation: the Steady Effort Store

One canonical dataset, extracted at ingest. All statistics read from it;
nothing reads raw session averages again.

Extracted per session:

- **Reps** (exact bounds from the interval engine)
  - pace: always
  - HR: **only the stabilized part** — discard the first ~45 s of each
    rep (HR time constant ≈ 30–45 s). Reps shorter than ~90 s contribute
    pace but **no HR-based statistics** (HR never stabilizes in a 1' rep).
- **Steady continuous blocks** — the existing ≥ 600 s / CV ≤ 5% /
  no-pause rule, from easy, long and tempo running.
- **Excluded from all aggregates**: warmups, cooldowns, recoveries,
  transitions, junk. (They remain visible in the session detail.)

Each effort is tagged with an **effort domain** by stabilized HR
(boundaries from user's max HR / LTHR — one-time setting, defaults below):

| Domain | Default band | Typical content |
|---|---|---|
| easy | ≤ 150 bpm | easy/long runs |
| steady | 150–165 bpm | steady blocks, long-run surges |
| threshold | 165–178 bpm | tempo blocks, long reps |
| hard | > 178 bpm | short reps, races |

**Rule: metrics are never compared across domains.**

---

## 3. Physiological Efficiency Analysis module

New module on top of the similar-sessions comparison. Motivation: raw
pace + HR overlay cannot distinguish *fitter* from *trying harder*.

> Session A: 3:40/km @ 180 bpm
> Session B: 3:35/km @ 184 bpm — faster, or just pushed harder?
> Session C: 3:35/km @ 176 bpm — clearly improved efficiency.

### 3.1 Running Efficiency Index (REI) — per rep

```
REI = speed (m/s) / stabilized HR (bpm)
```

Higher = more efficient. Computed for every rep with valid stabilized HR
(≥ 90 s reps). Displayed as:

- average REI per session (work reps only)
- REI trend across similar workouts (same cluster, same domain)
- % change vs the previous comparable session

### 3.2 Efficiency Factor (EF) — per steady block

Same formula as REI (TrainingPeaks convention), applied to steady
continuous blocks rather than reps; upgraded to **GAP / HR** if grade-
adjusted pace is added later (elevation is already in the FIT records).
REI and EF are one underlying metric at two granularities — one
definition in code, two surfaces.

### 3.3 Normalized performance (within-session regression)

Per session, fit a simple linear regression HR ↔ pace across all valid
reps, then report at fixed references:

- **expected HR at reference pace** (default 3:40/km)
- **expected pace at reference HR** (default 175 bpm)

Enables statements like *"at 175 bpm your predicted pace improved from
3:47/km to 3:40/km"* — comparable across sessions even when the sessions
were run at different intensities. References user-configurable.

### 3.4 Pace cost

```
pace cost = ΔHR per 10 s/km of speed  (slope of the 3.3 regression)
```

Distinguishes *faster because fitter* (cost stable/falling while pace
improves) from *faster because harder* (cost paid in HR).

### 3.5 Heart-rate drift

Extend the existing intra-set drift: HR trajectory across reps at stable
pace (and, for steady blocks, first-half vs second-half HR at equal
pace — the classic Pw:Hr decoupling). Lower drift over time = improved
aerobic durability. Reported per session and as a cluster trend.

### 3.6 Power-based efficiency (when power present)

FR265 records carry running power (already in the verified record
fields; currently not stored). Add `power` to the records table, then:

- **Power / HR** (robust EF — terrain-independent)
- **Speed / Power** (running effectiveness)

Power becomes the preferred efficiency numerator wherever available.

### 3.7 Automatic insights

Natural-language findings generated on the session page and the compare
page, only when statistically supportable (minimum rep counts, same
domain, stabilized HR):

- "Running efficiency improved 3.2% vs your previous similar session."
- "Same pace, 5 bpm lower heart rate."
- "Today's faster pace was mostly explained by higher cardiovascular
  effort (pace cost ↑)."
- "HR drift decreased 30% — improved endurance."
- "Predicted pace at 175 bpm improved from 3:47/km to 3:40/km."

### 3.8 Personal historical HR↔pace model (enhancement)

Beyond pairwise comparison: fit a personal efficiency curve from **all**
steady efforts over a rolling window (per domain, time-weighted, power-
based when available). Each new session is scored against the model's
expectation: *"performed 2.1% above your expected efficiency."* This
becomes the primary long-term fitness signal, replacing the current
band-sample trend charts.

---

## 4. Trends page redesign

- Per-domain EF/REI trend lines (rolling average + regression), one
  chart per domain — never mixed.
- "Am I improving?" verdicts recomputed from the per-domain trends and
  the historical model of 3.8, with minimum-data guards.
- Weekly volume and intensity distribution stay as-is (volume is volume).

---

## 5. Race predictor redesign

Anchors must be race-equivalent. Three tiers, each labeled with its
confidence:

1. **Races / time trials** (user-tagged, or auto-detected continuous
   ≥ 15' at sustained near-max HR) → full-confidence predictions
   (Riegel + Daniels VDOT retained for 5K–HM).
2. **Critical Speed model** — the right model for interval-trained
   runners: fit the hyperbolic speed–duration curve from maximal efforts
   of *different durations* (400s, 1000s, 1500s, 12–15' blocks are ideal
   inputs). Yields CS (≈ threshold pace) + D′; 5K/10K from the curve;
   marathon as a CS-fraction with an explicit uncertainty band,
   calibrated against the athlete's actual races when available.
3. **Volume-corrected marathon** — gate the marathon line with the
   Vickers–Vertosick adjustment (Riegel exponent inflated at low weekly
   mileage). The page states plainly: *"Marathon 2:5X–3:0Y — supported
   by current volume: no (needs ~X km/wk)"* instead of one confident
   wrong number.

Every prediction shows the anchor effort it derives from; anchors can be
rejected like progression points. The current rolling-window "best
efforts" table stays, but only as information — never as an anchor
unless the window is a continuous effort.

---

## 6. Compare view control

- One **selection bar** above the charts: a chip per session (color
  swatch + date); click toggles the session in *all* charts at once;
  double-click solos it; All / None buttons.
- Pace + HR unified: hovering rep *n* shows both values for every
  visible session (synchronized tooltip across the stacked charts).
- X-axis switch: rep # ↔ cumulative work time (aligns 6x1500 vs 5x1500).
- The efficiency module (section 3) renders on the same page below the
  overlay, driven by the same selection.

---

## 7. What stays unchanged

Rep tables, phases, structure detection, segment reclassification,
progression tracker, weekly volume, drag-and-drop, time blocks.

---

## 8. Inputs needed from Leonardo

1. **Max HR and/or LTHR** → domain boundaries (or confirm the defaults
   in section 2).
2. Confirm reference pace (3:40/km) and reference HR (175 bpm) for
   normalized performance.
3. **Race history** (date, distance, time — even one race) to calibrate
   the CS marathon fraction and validate the predictor.
4. Whether power-based metrics are wanted in v1 of the module (requires
   re-import to store the power field).

## 9. Implementation order (on approval)

1. Steady Effort Store + domains (+ store `power`) — foundation.
2. Efficiency module: REI/EF, normalization, pace cost, drift, insights.
3. Compare-view selection bar + unified tooltips.
4. Trends per domain + historical model.
5. Race predictor tiers (CS model, volume gate, race tagging).
