# RunLens — Statistics Redesign Proposal (v2)

Status: **IMPLEMENTED (first release)** — see per-section notes below.

Implemented in this release: Layer 0 hygiene (spike interpolation,
cadence-lock detection, smoothed pace, median stabilized HR), the Steady
Effort Store with LTHR-derived domains and pace fallback, drift-aware
REI (early/full), gated within-session regression + pace cost,
domain-guarded session comparison with expected-pace-cost judgment and
evidence-carrying insights, the compare view (verdict panel with
last-comparable-pair fallback, domain-colored efficiency trend, single
selection bar over pace+HR overlays, rep#/work-time axis switch),
per-domain trend charts (legacy band stats kept one level down), the
tier-1/2/3 race predictor (confirmed races only, Leiden 2:54 seeded,
CS model gated on ≥3 maximal 2–15' efforts, volume-gated marathon with
1.06–1.15 exponent band), `power`/`temperature`/heat-flag schema v2.

Not yet implemented (next): domain hysteresis across workout clusters,
versioned recalibration markers, the 90-day historical model (§4.8),
auto-detection of race-equivalent efforts, GAP, the §9 validation plan
backtests, legacy-toggle removal.

Goal: every number the dashboard shows must be physiologically meaningful
for an interval-heavy training load. No statistic may silently mix reps,
recoveries, warmups and easy running — and no statistic may be computed
when its statistical or physiological preconditions are not met.

**v2 changes vs v1:** data-hygiene layer added as layer 0; within-session
regression gated by validity guards; Critical Speed restricted to truly
maximal efforts; domains anchored to LTHR with hysteresis and versioned
recalibration; environmental (heat) flagging; references derived from
data instead of aspirational constants; drift-aware REI; validation and
migration plans added.

---

## 1. Diagnosis: why the current stats can mislead

| Statistic | Problem |
|---|---|
| Pace @ HR / HR @ pace | Filters 1 Hz samples by HR band, but on interval days the band catches transients: the first ~40 s of a rep (HR still climbing while running fast → flatters) and the start of recoveries (HR still high while jogging → penalizes). The trend slope is partly built on these transients. |
| Race predictor anchor | Best rolling 10 km window spans reps *and* recoveries of an interval session — not a race-equivalent effort. Everything extrapolated from it inherits the error. |
| Trend lines | Linear fit over mixed-context points models noise, not trajectory. |
| Session avg pace in lists | Meaningless for interval sessions (mixes 3:45 reps with 6:30 jogs). |

The **aerobic progression tracker** is already built correctly (steady
blocks ≥ 10', pace CV ≤ 5%, no pauses, user-confirmed) — this proposal
extends that same discipline to every other statistic.

---

## 2. Layer 0: Data hygiene (new — runs before everything)

Optical wrist HR (FR265) has documented failure modes that would poison
every downstream metric if not filtered at ingest:

- **Cadence lock**: sensor locks onto cadence (~170–185 "bpm") — phantom
  readings that land exactly in the threshold/hard bands. Detection:
  |HR − cadence(spm)| < 3 for ≥ 30 s while pace/context implies easy
  effort → flag segment `hr_suspect`.
- **Dropouts / spikes**: physiologically impossible deltas
  (|ΔHR| > 8 bpm between consecutive 1 Hz samples) → interpolate ≤ 5 s
  gaps, flag longer gaps.
- **GPS pace noise**: pace derived from a 5–10 s rolling smoothed
  distance, never instantaneous samples.
- **Stabilized HR is a median**, never a mean (see 3.0) — robust to
  residual spikes.

Segments flagged `hr_suspect` contribute pace-only statistics; their HR
never enters any aggregate. The session detail page shows hygiene flags
so nothing is silently discarded.

---

## 3. Foundation: the Steady Effort Store

One canonical dataset, extracted at ingest, after Layer 0. All
statistics read from it; nothing reads raw session averages again.

Extracted per session:

- **Reps** (exact bounds from the interval engine)
  - pace: always
  - HR: **only the stabilized part** — discard the first ~45 s of each
    rep (HR time constant ≈ 30–45 s); stabilized HR = **median** of the
    remainder. Reps shorter than ~90 s contribute pace but **no
    HR-based statistics**.
  - **rep index** stored with every rep (needed for drift-aware
    efficiency, see 4.1).
- **Steady continuous blocks** — the existing ≥ 600 s / CV ≤ 5% /
  no-pause rule, from easy, long and tempo running.
- **Excluded from all aggregates**: warmups, cooldowns, recoveries,
  transitions, junk. (They remain visible in the session detail.)
- **Conditions**: temperature stored per session when present in the
  FIT file; user can flag a session `hot` / `humid` manually (one tap).
  Heat-flagged sessions are down-weighted in trend fits and excluded
  from the historical model (7.x) — heat can cost 5–10 bpm at pace and
  would otherwise masquerade as fitness regression across a summer
  build toward an October race.

### 3.1 Effort domains

Each effort is tagged with an **effort domain**. Boundaries are derived
from **LTHR** (percentages, not fixed bpm), so a recalibration updates
all boundaries consistently:

| Domain | Definition | Current default (LTHR 176, max 190) | Typical content |
|---|---|---|---|
| easy | ≤ Z2 ceiling | ≤ 143 bpm | easy/long runs |
| steady | Z2 ceiling – 93% LTHR | 144–163 bpm | steady blocks, long-run surges |
| threshold | 93–102% LTHR | 164–180 bpm | tempo blocks, long reps |
| hard | > 102% LTHR | > 180 bpm | short reps, races |

Defaults above are prefilled from the athlete's current Garmin values
(max HR 190, LTHR ≈ 176 bpm @ 3:55/km, Z2 125–143) — to be confirmed,
not invented.

**Robustness rules (new):**

- **Hysteresis**: an effort within ±2 bpm of a boundary is assigned the
  domain of the majority of its samples; a *workout cluster* keeps its
  domain unless two consecutive sessions cross the boundary (prevents
  trend lines flapping between charts).
- **Pace-based fallback**: reps < 90 s (no stabilized HR) and
  `hr_suspect` segments are domain-tagged by pace bands derived from
  the same recalibration (pace-only statistics).
- **Versioned recalibration**: when LTHR/zones are updated (e.g. after
  a threshold test), boundaries change *from that date forward*; each
  data point stores the boundary version it was tagged under, and trend
  charts mark recalibration events with a vertical marker instead of
  silently re-bucketing history.

**Rule: metrics are never compared across domains.** Corollary: when a
workout cluster migrates domains due to improved fitness, the chart
shows a marked discontinuity, not a fake regression.

**Known coverage gap (accepted trade-off):** short-rep sessions
(e.g. 10×400) produce no HR-based efficiency data under the ≥ 90 s
rule. The hard domain will be structurally sparse; its trend chart
states its n and shows pace-only progression alongside.

---

## 4. Physiological Efficiency Analysis module

New module on top of the similar-sessions comparison. Motivation: raw
pace + HR overlay cannot distinguish *fitter* from *trying harder*.

> Session A: 3:40/km @ 180 bpm
> Session B: 3:35/km @ 184 bpm — faster, or just pushed harder?
> Session C: 3:35/km @ 176 bpm — clearly improved efficiency.

### 4.1 Running Efficiency Index (REI) — per rep, drift-aware

```
REI = speed (m/s) / stabilized HR (bpm)
```

Higher = more efficient. Computed for every rep with valid stabilized
HR (≥ 90 s, not `hr_suspect`). Because HR drifts upward across reps at
constant pace, a naive session average penalizes longer sessions and
conflates drift with efficiency. Therefore:

- **Early-session REI**: mean REI of the first ⌈n/3⌉ work reps —
  the primary cross-session comparison value.
- **Full-session REI**: mean over all valid reps — shown alongside,
  labeled as drift-inclusive.
- REI trend across similar workouts (same cluster, same domain), using
  early-session REI.
- % change vs the previous comparable session, only when rep counts and
  domain match.

REI values are only compared within a domain **and** within a similar
pace range (±10 s/km of the comparison session's median rep pace) —
speed/HR is not linear across intensities, so cross-intensity REI
comparison is invalid even inside one domain.

### 4.2 Efficiency Factor (EF) — per steady block

Same formula as REI (TrainingPeaks convention), applied to steady
continuous blocks; upgraded to **GAP / HR** if grade-adjusted pace is
added later (elevation is already in the FIT records). REI and EF are
one underlying metric at two granularities — one definition in code,
two surfaces.

### 4.3 Normalized performance (within-session regression) — gated

Per session, fit a linear regression HR ↔ pace across all valid reps —
**only when the session supports it**:

- ≥ 4 valid reps (stabilized HR, same domain)
- rep pace spread ≥ 15 s/km (a 6×1000 all at target pace has ~5 s/km
  spread — the slope would be fit on noise)
- fit quality R² ≥ 0.6

When the guards fail (which will be the case for most single-pace
prescribed sessions), the module reports **"not computable — single-
pace session"** and falls back to REI comparison only. No silent
garbage.

When valid, report at fixed references:

- **expected HR at reference pace**
- **expected pace at reference HR**

**References are data-derived, not aspirational**: default reference
pace = rolling 60-day median rep pace of the domain (currently ≈
3:50–3:55/km, not 3:40 — extrapolating the regression 15 s/km beyond
the observed range amplifies noise). Default reference HR = LTHR − 1
(175 bpm). Both user-configurable; the UI warns when a chosen reference
lies outside the session's observed pace range.

### 4.4 Pace cost

```
pace cost = ΔHR per 10 s/km of speed  (slope of the 4.3 regression)
```

Only reported when the 4.3 guards pass. Distinguishes *faster because
fitter* (cost stable/falling while pace improves) from *faster because
harder* (cost paid in HR).

### 4.5 Heart-rate drift

Extend the existing intra-set drift: HR trajectory across reps at
stable pace (and, for steady blocks, first-half vs second-half HR at
equal pace — the classic Pw:Hr decoupling). Lower drift over time =
improved aerobic durability. Reported per session and as a cluster
trend. Heat-flagged sessions annotated in the drift trend (heat is the
dominant drift confounder).

### 4.6 Power-based efficiency (when power present)

FR265 records carry running power (already in the verified record
fields; currently not stored). **Caveat stated in the UI**: Garmin
running power is a *model* derived largely from pace, grade and wind
estimation — it is a GAP proxy, not an independent measurement.
Consequences:

- **Power / HR** — useful as a terrain-corrected EF (effectively
  GAP/HR without implementing GAP). Adopted.
- **Speed / Power** ("running effectiveness") — nearly constant by
  construction on Garmin-modeled power; **dropped from v2**.

Add `power` to the records table; Power/HR becomes the preferred
steady-block efficiency numerator where available. Requires re-import
(see 10).

### 4.7 Automatic insights

Natural-language findings generated on the session page and the compare
page, only when statistically supportable (minimum rep counts, same
domain, same pace range, stabilized HR, guards of 4.3 where relevant):

- "Running efficiency improved 3.2% vs your previous similar session."
- "Same pace, 5 bpm lower heart rate."
- "Today's faster pace was mostly explained by higher cardiovascular
  effort (pace cost ↑)." *(only on regression-valid sessions)*
- "HR drift decreased 30% — improved endurance."
- "Predicted pace at 175 bpm improved from 3:47/km to 3:40/km."
  *(only on regression-valid sessions)*

Every insight carries its evidence inline (n reps, domain, comparison
session date). Insights that would rest on a failed guard are not
generated.

### 4.8 Personal historical HR↔pace model (enhancement)

Beyond pairwise comparison: fit a personal efficiency curve from steady
efforts over a **defined rolling window (90 days)**, per domain,
exponentially time-weighted (half-life 30 days), power-based when
available. Fitting excludes heat-flagged and `hr_suspect` data.
Minimum data guard: ≥ 8 efforts in the window for the domain,
otherwise the model reports "insufficient data" rather than a curve.

Each new session is scored against the model's expectation:
*"performed 2.1% above your expected efficiency."* The score always
shows the residual distribution (how noisy the model currently is), so
a +2% on a ±4% model is presented as noise, not a breakthrough. This
becomes the primary long-term fitness signal; the current band-sample
trend charts remain accessible one level down (the model must be
auditable against raw data, not replace it invisibly).

---

## 5. Trends page redesign

- Per-domain EF/REI trend lines (rolling average + regression), one
  chart per domain — never mixed. Recalibration events and heat-flagged
  points visually marked.
- "Am I improving?" verdicts recomputed from the per-domain trends and
  the historical model of 4.8, with minimum-data guards; every verdict
  states its n and window.
- Weekly volume and intensity distribution stay as-is (volume is
  volume).

---

## 6. Race predictor redesign

Anchors must be race-equivalent. Three tiers, each labeled with its
confidence:

1. **Races / time trials** — user-tagged, or auto-detected (continuous
   ≥ 15', sustained ≥ 95% LTHR **and** pace within 5% of best pace for
   the duration) → **proposed to the user for one-click confirmation,
   never silently anchored** (same philosophy as progression points).
   Confirmed anchors → full-confidence predictions (Riegel + Daniels
   VDOT retained for 5K–HM).
2. **Critical Speed model** — fit the hyperbolic speed–duration curve
   **only from confirmed maximal efforts** (races, user-tagged time
   trials/tests) of different durations. **Training reps are excluded
   by default**: prescribed-pace reps with recoveries are submaximal by
   design and would systematically corrupt CS/D′. When the model lacks
   duration diversity (needs ≥ 3 efforts spanning ~2–15'), the page
   says so and suggests which test effort would unlock it. Yields CS
   (≈ threshold pace) + D′; 5K/10K from the curve; marathon as a
   CS-fraction with an explicit uncertainty band, calibrated against
   the athlete's actual races (Leiden 2:54, May 2025; mid-July HM test
   when run).
3. **Volume-corrected marathon** — gate the marathon line with the
   Vickers–Vertosick adjustment (Riegel exponent inflated at low weekly
   mileage). The page states plainly: *"Marathon 2:5X–3:0Y — supported
   by current volume: no (needs ~X km/wk)"* instead of one confident
   wrong number.

Every prediction shows the anchor effort it derives from; anchors can
be rejected like progression points. The current rolling-window "best
efforts" table stays, but only as information — never as an anchor
unless the window is a continuous confirmed effort.

---

## 7. Compare view control

- One **selection bar** above the charts: a chip per session (color
  swatch + date); click toggles the session in *all* charts at once;
  double-click solos it; All / None buttons.
- Pace + HR unified: hovering rep *n* shows both values for every
  visible session (synchronized tooltip across the stacked charts).
- X-axis switch: rep # ↔ cumulative work time (aligns 6x1500 vs
  5x1500).
- The efficiency module (section 4) renders on the same page below the
  overlay, driven by the same selection; comparisons across sessions of
  different domains are disabled with an explanatory tooltip rather
  than rendered wrong.

---

## 8. What stays unchanged

Rep tables, phases, structure detection, segment reclassification,
progression tracker, weekly volume, drag-and-drop, time blocks.

---

## 9. Validation plan (new)

Before the new statistics replace anything user-facing:

1. **Backtest against golden anchors**: the two confirmed progression
   points (June 2025: 4:02/km @ 173 bpm; June 2026: 3:52/km @ 175 bpm)
   must be reproduced by the historical model within its stated
   uncertainty.
2. **Race backtest**: predictor tier 1–2 fed with data available
   *before* Leiden (May 2025) must bracket 2:54; the mid-July HM
   becomes a prospective test.
3. **Guard audit**: run the full history through the pipeline and
   report how many sessions fail each guard (regression validity,
   hygiene flags, domain hysteresis) — if the guards reject almost
   everything, thresholds are retuned before launch, explicitly.
4. **Insight dry run**: generate insights over the full history in a
   log-only mode; spot-check for statements that would have been
   misleading.

---

## 10. Migration & schema (new)

- Schema version bump: add `power`, `temperature`, `hr_suspect` flags,
  `rep_index`, `boundary_version` to the store.
- One full historical re-import path (idempotent; existing confirmed
  progression points and tags preserved by session hash, not row id).
- Old statistics remain readable behind a "legacy" toggle during one
  transition release, then removed.

---

## 11. Inputs needed from Leonardo

1. Confirm domain boundary defaults derived from current values
   (max HR 190, LTHR 176 @ 3:55/km, Z2 ceiling 143) — see 3.1.
2. Confirm data-derived references for normalized performance
   (reference HR 175 bpm; reference pace = rolling domain median).
3. **Race history**: Leiden 2:54 (May 2025) is in; add any earlier
   races; tag the mid-July HM as a test effort when run.
4. Whether power-based metrics are wanted in v1 of the module
   (requires the section-10 re-import).
5. Willingness to occasionally tag test efforts (one 12–15' max effort
   per block) — this is what unlocks the CS model.

## 12. Implementation order (on approval)

1. Layer 0 hygiene + Steady Effort Store + domains (+ `power`,
   `temperature`, schema v2) — foundation.
2. Efficiency module: REI (drift-aware), EF, gated normalization, pace
   cost, drift, insights with evidence.
3. Compare-view selection bar + unified tooltips + domain-mismatch
   guard.
4. Trends per domain + historical model (with residual display).
5. Race predictor tiers (confirmed-anchor flow, CS model, volume gate).
6. Validation plan (section 9) executed before the legacy toggle is
   removed.
