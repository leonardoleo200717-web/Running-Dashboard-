# Running.MD — RunLens Functional & Software Specification

**Purpose of this file**: a self-contained specification an AI coding agent
can use to regenerate, extend, or port this dashboard without needing the
rest of the conversation history. It states *what the product does*, *why
each non-obvious rule exists*, and *how it is built*, so a fresh agent
reading only this file can make correct decisions. Where a rule looks
arbitrary, the reason is given — do not "simplify" these away.

Companion files in this repo:
- `CLAUDE.md` — working log / agent operating instructions for this
  specific codebase (git branch, session history, terse ledger format).
- `PROPOSAL.md` — the design proposal for the efficiency/statistics layer
  (this file's §7–§9 describe what was actually built from it).

This file is the durable spec. If regenerating the dashboard elsewhere
(new stack, new language), **this is the file to hand to the agent.**

---

## 1. Product definition

**RunLens** is a local-first running-training dashboard for a single
athlete who trains with structured intervals as well as easy/long runs,
using a Garmin watch (verified against a Forerunner 265; the parsing
layer is Garmin-FIT-format-general, not device-specific beyond what's
noted). No cloud, no accounts, no multi-user support. All data lives in
one local file. The athlete uploads `.FIT` files; the dashboard extracts
workout structure, computes training-load and efficiency metrics, and
answers one central question well: **"between two similar workouts, am I
actually getting fitter, or just trying harder?"**

### 1.1 Design mandate
- **Keep it simple.** One user, no auth, no network calls except an
  optional CDN asset (vendor it instead — see §8).
- **Never show a misleading number.** A missing/inconclusive statistic
  must render as "not computable" with the reason, not a guess. This
  mandate shaped nearly every threshold and guard in this document —
  when extending the system, preserve it over adding a metric.
- **Raw data first, inference last**, and every inferred fact must state
  its confidence and provenance.

---

## 2. Core pipeline

Each stage is a pure function on plain data structures (dicts/records);
the only place allowed to touch a database is the storage layer. No stage
may reach back into raw source files after ingestion — everything needed
downstream must be in the normalized structure ingestion produces.

```
ingest  →  origin detection  →  interval engine  →  effort extraction (hygiene + domains)
  →  efficiency metrics  →  storage  →  presentation
```

Time-first discipline: **all segmentation and alignment uses timestamps**
(UTC internally; convert to local display timezone only at render time),
never distance alone — distance-based segmentation is provably ambiguous
(see §3.5). All duration/pace computation uses **timer time** (moving
time), never elapsed time, except when explicitly analyzing pauses.

---

## 3. FIT ingestion — verified ground truth

This section encodes facts empirically verified against real device
files (Garmin FR265, ~20 real session files analyzed during development).
**Treat these as constraints, not assumptions** — they were each
discovered because a naive implementation produced a wrong answer on a
real file.

### 3.1 Timestamps and sampling
- All timestamps are UTC. Convert to local display timezone only when
  rendering.
- Position/motion samples ("records") are at 1 Hz, with gaps during
  timer pauses.
- Timer pauses are detectable two ways, and both should agree:
  timer-event start/stop markers, and `elapsed_time > timer_time` on a
  lap/session summary.

### 3.2 Two file flavors, one bug
- **Watch-direct exports** (activity files transferred directly from the
  device) have correct per-lap start/end timestamps.
- **Cloud-export files** (re-exported from a companion phone app/cloud
  service) carry a **bogus lap end-timestamp bug**: every lap in the file
  reports the *session start time* as its timestamp field, not its own
  end time. If unhandled, every derived lap boundary collapses to the
  session start and every downstream time-windowed computation (HR
  backfill, recovery-quality windows, everything) breaks.
  **Fix:** always derive lap end as `lap.start_time + lap.total_elapsed_time`,
  never trust a stored end-timestamp field on laps.

### 3.3 Structured workout vs free run (origin detection)
- A session is a **structured workout** if and only if the file contains
  a workout-plan message (step definitions the athlete programmed or the
  watch suggested). This is the single reliable signal — do not infer
  structure from lap count or regularity.
- Otherwise it is a **free run**, and lap-level "intensity" tags in the
  file are **not usable** — devices stamp a meaningless constant value on
  every lap of a free run. Never read this field for free runs.

### 3.4 Structured workout quirks (all real, all verified)
- **Autolap keeps firing inside a workout step** that is longer than the
  device's autolap distance (e.g. 1 km): a single 15-minute programmed
  step can arrive as several separate lap records (e.g. 1000+1000+
  1000+664 m) that all share the same step-index tag. **Fix: merge
  consecutive laps sharing a step index into one logical rep/segment**
  (weighted-average HR, summed time/distance). True repeats never merge
  incorrectly by this rule, because a distinct recovery-step lap always
  sits between them.
- **Tail laps**: autolap can fire moments before a *time-based* step
  ends, leaving a real but very short (2–4 s) lap that still carries the
  step index. **Never discard a lap as "junk" if it carries a step
  index** — dropping it truncates the step's recorded duration (a
  programmed 8-minute step would read as 7:58).
- **Ambiguous warmup/cooldown tagging**: user-created workouts often tag
  the free-form warmup/cooldown steps with the *same* generic "active"
  intent as work reps (because the watch's step editor defaults to
  that). Distinguish by position: an open-ended "active" step outside
  the repeat block, occurring before the block, is a warmup; after, a
  cooldown.
- **Suggested-workout warmups can look like a rep**: watch-suggested
  workouts sometimes tag the warmup as a *distance-defined*, "active"
  step (not visibly a warmup) but give it a materially easier target
  pace/speed band than the actual work steps. **Fix: compare the
  target-intensity band of ambiguous steps against the work steps'
  band; a step with a target band entirely below the work steps, sitting
  outside the repeat block, is the warmup** (same logic, cooldown after).
- A step's **duration type is definitional and must win display
  formatting**: a step defined as "12 minutes" must always display as a
  time value even if its covered distance happens to round to a clean
  number — never silently relabel a time-defined step by distance.

### 3.5 Free-run (lap-inferred) interval detection
Free runs have no workout-plan message, so intervals must be inferred
from lap boundaries and pace/HR patterns:
- **Lap trigger types coexist within one workout**: autolap-triggered and
  manually-triggered laps appear interleaved in the same free-run
  session (e.g. some reps trigger by hitting exactly 1000 m, others by
  early manual button-press at 991–997 m because the athlete pressed
  slightly early). **Never assume one trigger type per session.**
- **Distance-based lap grouping is provably ambiguous**: an autolap fired
  at a round distance can coincide with a rep boundary at the same
  distance. Segmentation must use pace/HR alternation patterns, never
  distance alone, to tell "this is an autolap inside a long steady
  effort" from "this is a programmed rep."
- **Junk laps**: extremely short laps (well under ~5 s duration, or a
  handful of meters) are almost always accidental double button-presses
  and should be dropped — **but only if they carry no structured-step
  index** (see §3.4's tail-lap rule — those are real and must be kept).
- **Same duration, different meaning**: time-based reps and their
  recoveries can have *numerically identical* durations (e.g. a
  15×1-minute-on/1-minute-off session). Never discriminate rep vs.
  recovery by duration; use **pace or HR alternation** between
  consecutive laps.
- **Three-speed-level trap**: a free interval session can have three
  distinct paces present at once (recovery jog, an initial warmup pace
  that's faster than recovery but slower than the reps, and the actual
  rep pace). A naive fastest/slowest binary split can misclassify the
  warmup pace bucket as "recovery" or bleed into "rep." **Fix: after an
  initial fast/slow split, demote any "rep" whose pace is materially
  slower (e.g. below ~88% of the rep-cluster's median pace) — it's
  warmup, not a rep — and extend the true rep block to include
  autolap-triggered laps immediately adjacent that match true rep pace.**
- **A rep can be short because the athlete pressed the lap button
  early** (e.g. a 6×1000 m session where rep 1 reads 901 m because of an
  early press). Group reps by **pace similarity** (not just distance) so
  a mis-measured rep still joins its group; label the group by whichever
  measurement (distance or duration) the majority of the group actually
  snaps to.
- **GPS under-measures short track reps** by a few percent typically (a
  300 m rep might read ~289 m). Distance "snapping" to a canonical value
  for labeling purposes should tolerate up to ~4% for reps; recovery
  jogs are even less precise and deserve a looser tolerance (~15%) since
  they're not paced precisely by the athlete anyway.
- **Strides tacked onto an otherwise easy run** (e.g. a handful of
  ~100 m pickups after 70+ minutes easy) should be *detected and
  labeled* as their own short rep group, but must **not** flip the whole
  session's classification from "easy" to "intervals" — gate
  session-type classification on reps occupying a nontrivial share
  (e.g. ≥5%) of total session time.

### 3.6 Garmin's own auto-detected splits (secondary signal, unreliable alone)
Devices also emit their own auto-detected interval segmentation
("splits"), independent of the athlete's laps. This is a useful
cross-check but is **empirically unreliable as a sole source**:
- A single split spanning almost the entire session (a "degenerate"
  split) shows up on long, unstructured runs and must be discarded, not
  treated as one giant rep.
- Splits can **merge two or more real reps into one artificially large
  one**, or **invent extra reps that don't correspond to any real lap**
  (verified on real files — e.g. a device merged a rep+recovery+rep
  sequence into a single inflated "active" split).
- **Validation rule**: cross-check the device's split-derived rep count
  and durations against the independently-cleaned lap data. If the
  splits imply *more* real-looking reps than the laps do, or a
  significant fraction (e.g. more than half) of split "actives" don't
  line up with any real lap, **discard the split-derived segmentation
  and fall back to lap-based inference.** Splits are corroborating
  evidence, never primary truth.
- Split-derived segments carry no per-segment heart-rate data natively
  in some device firmwares — backfill HR for these segments from the
  underlying 1 Hz record stream, windowed by the segment's timestamps.

### 3.7 Never trust device-computed averages
Device-reported average pace/speed fields are sometimes null or
computed inconsistently. **Always recompute distance ÷ time yourself**
from the lap/segment's own distance and timer-time fields rather than
trusting a stored average field, and treat a missing average field as
"must recompute," never as "unavailable."

---

## 4. Interval-structure engine — hierarchical resolution

Given a parsed session, resolve its interval structure by trying levels
in order and using the **first level that produces a usable result**.
Every session's stored result must record **which level resolved it**
and a **confidence** (high / medium / low), because downstream consumers
(especially the efficiency module, §7) must be able to discount
low-confidence structure.

1. **Structured workout** (§3.3–3.4 apply) → confidence **high**.
   Exact mapping from the workout plan; step-merging and
   warmup/cooldown-repositioning rules apply.
2. **Device auto-splits** (§3.6), after discarding degenerate cases and
   cross-validating against cleaned laps → confidence **high** if the
   split count agrees with the independent lap count, else **medium**;
   discard entirely (fall through) if the cross-check fails badly.
3. **Lap-based inference** (§3.5) on free runs → confidence **medium**.
4. **Unclassified fallback** — if nothing above produces a confident
   segmentation, mark the session **low confidence, flagged for manual
   review**, rather than guessing. A stub "no structure found" result is
   correct behavior here, not a bug.

### 4.1 Normalization and human-readable labeling
- Canonical distance snapping: reps snap to standard track/road
  distances (100 m increments up through 10K) within ~4% tolerance;
  recovery-jog distances snap within a much looser ~15% tolerance (they
  aren't precisely paced).
- Time-based labels are **never rounded in storage** (store exact
  seconds), but for **display**, snap to the nearest 5 seconds when
  within ~2 seconds of a round value (a rep measured at 61.4 s that was
  programmed as "1 minute" should display as "1′", not "1′01.4″").
- **Group labeling with tolerance for one bad measurement**: if ~80%+ of
  a rep group snaps to the same canonical value, label the whole group
  by that majority value rather than fragmenting because one
  measurement was noisy. Otherwise, pick whichever dimension (distance
  vs. duration) has the tighter coefficient of variation across the
  group and label by that; single-member groups get no "1×" prefix.
- **Recovery labeling**: label by whichever dimension (distance or time)
  the athlete actually held constant across the recoveries (lower
  coefficient of variation wins); if the source is a structured
  workout, its declared duration-type decides directly, no inference
  needed.
- **Set structures** (repeats of a repeats, e.g. "2 sets of 10×1-minute
  with a long recovery between sets"): detect the longer between-set
  recovery by finding the largest gap in recovery durations across the
  session (the gap must be materially larger than the short recoveries
  and than a fixed minimum, to avoid false positives on sessions with
  no sets at all).
- Canonical structure-string grammar (human-readable summary), e.g.:
  `8×400m R200m`, `15×1′ R1′`, `2×(10×1′) R1′ SR4′` (R = recovery
  between reps, SR = recovery between sets).

### 4.2 Session-type classification
Classify each session as one of: `race` / `intervals` / `fartlek`
(irregular, lap-inferred structure with no clean regular distances or
durations) / `long` (distance or duration above a threshold) / `easy` /
`unknown`. Apply the strides carve-out (§3.5) before all other rules.
**Classification must always be user-overridable, and a manual
override must persist across re-imports/re-parses of the same file**
(store overrides keyed by a stable identity — e.g. segment start
timestamp — not by array position, since re-parsing can shift indices).

---

## 5. Effort extraction — hygiene layer and the Steady Effort Store

This is the foundation every efficiency/comparison statistic reads from.
**No statistic anywhere in the product may read a raw, unfiltered
session average.** Everything downstream reads only from this
extracted, cleaned effort store.

### 5.1 Data hygiene (must run before any efficiency computation)
Consumer optical wrist heart-rate sensors have well-known failure modes
that will silently corrupt every downstream statistic if not filtered:
- **Impossible jumps**: a heart-rate delta between consecutive 1 Hz
  samples larger than a physiological maximum (e.g. >8 bpm in one
  second) is a sensor artifact, not a real change. Short gaps (a few
  seconds) should be linearly interpolated from the surrounding good
  samples; longer bad stretches should be flagged invalid rather than
  guessed.
- **Cadence lock**: the optical sensor can lock onto the athlete's
  running cadence and report cadence as if it were heart rate for a
  sustained period. Detect by comparing HR against cadence (steps/min)
  directly — if they're nearly equal for a sustained stretch (tens of
  seconds), flag that stretch's HR as suspect and exclude it from
  heart-rate-based statistics (pace-only statistics still apply).
- **Pace smoothing**: GPS-derived instantaneous speed is noisy at 1 Hz;
  use a short rolling-window smoothed speed for any per-sample pace
  computation rather than raw instantaneous values.
- **Stabilized value = median, not mean**, of the qualifying samples in
  a window — a median is robust to any residual spike hygiene didn't
  catch.

### 5.2 What gets extracted as an "effort"
Two kinds of effort are extracted from each session, and **nothing
else contributes to efficiency statistics**:
- **Work reps** (from the interval engine's segmentation): pace is
  always available. Heart rate is usable **only from the stabilized
  portion** — discard roughly the first 45 seconds of each rep (this is
  approximately the physiological lag before heart rate catches up to
  a pace change), then take the median of the remainder. **A rep
  shorter than ~90 seconds never contributes heart-rate statistics at
  all** — there isn't enough stabilized signal left after discarding the
  lag period, so pretending otherwise produces noise dressed as data.
  Short reps still contribute their pace.
- **Steady continuous blocks** from easy/long/tempo running: reuse the
  existing "aerobic progression" eligibility rule — a continuous block
  of at least 10 minutes of moving time, no pause inside it, and pace
  coefficient of variation under ~5% (i.e. genuinely steady, not
  drifting or surging).
- **Explicitly excluded from all aggregate statistics, forever**:
  warmups, cooldowns, recoveries, transition segments, and anything
  junk-flagged. They remain visible in the raw per-session view, but
  never enter an average, a trend, or a comparison.

### 5.3 Effort domains (the anti-mixing guardrail)
Every extracted effort is tagged with an **effort domain** — easy /
steady / threshold / hard — and **the cardinal rule of this entire
system is that efficiency statistics are never compared across
domains.** Comparing a hard 400m-repeat session's efficiency number
against an easy long run's is meaningless (heart-rate-to-pace
relationships are not linear across intensity, and a smaller/larger
number doesn't mean fitter/less-fit across domains) — the system must
refuse that comparison outright rather than produce a number.

- Domain boundaries are derived as **percentages of the athlete's
  lactate-threshold heart rate (LTHR)**, not fixed bpm values, so that
  updating the athlete's LTHR (after a fitness test) automatically
  moves every boundary consistently, everywhere, at once.
- A **pace-based fallback** domain assignment applies whenever
  stabilized heart rate isn't available for an effort (too short, or
  hygiene-flagged): use pace bands derived from the same
  threshold-pace reference instead. This keeps very short high-intensity
  reps (which never get stabilized HR) still domain-classifiable from
  pace alone.
- Record the domain-boundary version an effort was tagged under. If the
  athlete's threshold values are later recalibrated, don't silently
  re-bucket historical data — the historical tag should reflect what
  was true at the time.

---

## 6. Physiological efficiency metrics

Built entirely on top of the Steady Effort Store (§5). The goal: answer
*"faster/slower — because fitter, or because trying harder?"*

### 6.1 Running Efficiency Index (REI)
```
REI = speed (distance/time) ÷ stabilized heart rate
```
Higher is better (more speed per heartbeat). Computed per effort where
stabilized HR is available.

**Drift-aware session summary — this is the important subtlety.**
Heart rate drifts upward across a session even at constant pace (classic
aerobic decoupling). A single whole-session-average REI therefore
conflates "getting less efficient within this one session" with "how
efficient this session was overall," and penalizes longer sessions
unfairly. Report **two numbers**:
- **Early-session REI**: mean REI over roughly the first third of valid
  work efforts. **This is the primary number for cross-session
  comparison** — it approximates the athlete's efficiency before
  within-session fatigue sets in, making different-length sessions
  comparable.
- **Full-session REI**: mean over all valid efforts, shown alongside,
  explicitly labeled as "drift-inclusive" — useful context, not the
  primary comparison metric.

### 6.2 Efficiency Factor (EF)
Same formula as REI, applied to steady continuous blocks rather than
discrete reps — this is one metric at two granularities (rep-level vs.
block-level), not two different definitions; keep one implementation
shared between them.

### 6.3 Gated within-session normalization (HR ↔ pace regression)
For sessions with enough pace variation across reps, fit a simple linear
relationship between pace and heart rate within that single session.
This lets you state things like "predicted heart rate at your reference
pace" or "predicted pace at your reference heart rate," comparable
across sessions run at different average intensities.

**This must be gated — refuse to compute it, and say why, when its
statistical preconditions fail**, rather than fitting a regression to
noise:
- Requires a minimum number of valid-HR efforts (e.g. at least 4).
- Requires the efforts to all be in the **same domain** (§5.3) — a
  regression across domains is physiologically invalid.
- Requires a **minimum pace spread** across the reps (e.g. at least
  ~15 seconds/km between the fastest and slowest rep) — a session run
  at one prescribed pace the whole way through has no pace variation to
  regress against; fitting a line to near-constant x-values fits noise,
  not physiology.
- Requires a **minimum fit quality** (R² above some threshold, e.g.
  0.6) — even with enough spread, a genuinely noisy relationship should
  be rejected rather than reported with false confidence.
- When any guard fails, report **"not computable"** with the specific
  reason (e.g. "single-pace session," "insufficient stabilized-HR
  reps"), and fall back to the simpler REI-based comparison instead.
  This message is itself useful information to the athlete.

From a valid regression, derive:
- **Predicted heart rate at a reference pace**, and **predicted pace at
  a reference heart rate**. Reference values should be **data-derived**
  (e.g. a rolling recent median pace for the relevant domain), not
  aspirational constants baked into the code — an arbitrary fixed
  reference pace far outside what the athlete has actually been running
  extrapolates the fitted line beyond its valid range and amplifies
  noise.
- **Pace cost**: the regression's slope, reframed as "heart-rate cost
  per unit of pace improvement" (e.g. "+4 bpm per 10 sec/km faster").
  This single number is the crux of the "fitter vs. trying harder"
  question: a session that's faster at a *lower or equal* pace-cost
  than before indicates a real efficiency gain; faster at a *higher*
  pace-cost indicates the same underlying capability, just pushed
  harder.

### 6.4 Heart-rate drift and durability
Track heart-rate drift within a session — first-third vs. last-third of
reps at stable pace, or first-half vs. second-half of a steady block.
Falling drift over time (at comparable pace/domain) indicates improving
aerobic durability, independent of raw pace or REI.

### 6.5 Environmental confounder: heat
Heat (and humidity) measurably raises heart rate at a given pace,
independent of fitness — a hot-weather block of training could look
like a fitness *regression* in the efficiency trend if not flagged.
Store ambient temperature from the file when available, and provide a
simple one-tap manual "hot" flag on any session (temperature isn't
always reliably logged). Heat-flagged sessions should be visually
annotated wherever they appear in a trend, and any automated
"improving/not improving" verdict that involves a heat-flagged session
must say so explicitly rather than silently absorbing the confound.

### 6.6 Power-based efficiency (optional, when the device provides
running power)
Prefer **power ÷ heart rate** as an efficiency numerator over
speed-derived metrics when power data is present — modeled running
power already accounts for grade and (partially) wind, making it more
terrain-robust than raw pace. Caveat this in the UI: on most consumer
watches, "running power" is itself a *model* derived substantially from
pace and grade, not an independent physical measurement — so this is a
grade-corrected refinement, not a categorically different signal.

### 6.7 The session-vs-session comparison verdict (the product's
central answer)

Given a current session and a candidate "previous similar session" to
compare against, produce one of exactly these outcomes, always with
plain-language supporting evidence:

- **`not_comparable`** — e.g. domains differ, or neither session has
  any valid work-effort data. State *why* explicitly (e.g. "different
  effort domains — comparison isn't valid").
- **`improving` / `declining` / `similar`** — determined by:
  1. If both sessions' rep paces are within a small tolerance of each
     other (e.g. within ~10 sec/km): compare **early-session REI**
     directly. A percentage change above a small noise threshold
     (e.g. ±1%) in either direction is `improving`/`declining`;
     otherwise `similar`.
  2. If paces differ by more than that tolerance: raw REI comparison is
     not reliable (REI isn't linear across a big pace gap), so instead
     compare the **actual heart-rate change against the heart-rate
     change that would be *expected* purely from the pace change**,
     using a typical pace-cost rate (see §6.3) as the reference. Faster
     with heart rate below the expected increase → `improving`
     (efficiency gain). Faster with heart rate above/at the expected
     increase → efficiency-neutral, effort explains the speed, not
     fitness (`similar`, with an explanatory note — do not report
     `improving` here even though the pace was faster).
  3. If neither session has valid heart-rate data at all: fall back to
     a pace-only verdict (faster/slower beyond a small tolerance), and
     say explicitly that this is pace-only, no efficiency signal.
- Always **annotate a heat-flagged session** in the verdict's evidence.
- Every verdict must be paired with **evidence in plain language,
  citing which session dates were compared and how many efforts backed
  the number** — a bare "improving" with no supporting reasoning is not
  acceptable output anywhere in this product.

**Comparison-partner selection**: when comparing a session against
"the previous similar one," prefer the most recent **same-domain**
prior session in that similarity group over a simple chronological
"previous session" — comparing across domains produces
`not_comparable` and wastes the comparison. If no same-domain partner
exists in the immediate history, search further back in the group
rather than giving up, and if truly nothing comparable exists, say so.

---

## 7. Similar-session clustering ("fuzzy matching")

Group sessions the athlete would consider "the same kind of workout"
even when exact rep count/distance differs, so trends and comparisons
have something to compare against:
- **Distance-repeat sessions** cluster by the *set of rep distances*
  present, regardless of how many reps (a 6×1000m session and an
  8×1000m session belong together; a session with 400+300+200m repeats
  is its own cluster distinct from pure 1000m sessions).
- **Time-based interval sessions** cluster by total *work time* within
  a tolerance (e.g. ±15%) — a 3×12′ session and a 4×10′ session are
  comparable total training stimulus even though rep count/length
  differ.
- **Plain runs** (no interval structure) cluster by total distance
  within a tolerance (e.g. ±10%) — a 20 km run and a 22 km run belong
  together.
- Clustering must be usable both for **generating comparison groups**
  (§6.7 operates within a cluster) and for a **standalone
  "similar sessions" browse view**.

---

## 8. Race performance prediction

**Critical constraint: an anchor performance must be race-equivalent.**
A rolling best-effort window computed over an interval session (which
necessarily includes recoveries between reps) is *not* a valid race
anchor — it will be materially slower than a true continuous
race-pace effort of that duration, and every prediction built on it
inherits that error. This was a real defect in an earlier version of
this dashboard; do not reintroduce it.

Use a **tiered predictor**, each tier stating its own confidence, never
silently blending tiers:

1. **Tier 1 — confirmed races / time trials.** Only actual races or
   explicitly-tagged all-out time-trial efforts (user-confirmed, never
   auto-inferred from training data) anchor predictions. From a
   confirmed anchor performance, apply multiple independent
   distance-prediction models (e.g. a power-law model, a
   physiologically-grounded VO2-based model, and at least one other
   independent empirical model) and present a **consensus** (their
   average) alongside each individual model's number — showing model
   disagreement is itself informative, don't hide it behind a single
   number.
2. **Tier 2 — Critical Speed model.** A more athlete-specific model
   fit from **multiple confirmed maximal efforts of genuinely different
   durations** (e.g. spanning roughly 2 to 15 minutes) — this requires
   real test/race data at multiple durations, not training reps.
   **Training reps at a prescribed pace must be excluded from this
   model by design**: they are deliberately submaximal (that's the
   point of interval training) and would bias the fitted curve toward
   pessimism. When there isn't yet enough qualifying data (e.g. fewer
   than 3 confirmed maximal efforts spanning enough duration range),
   state exactly what's missing and what kind of test effort would
   unlock it, rather than silently falling back to a worse model.
3. **Tier 3 — volume-corrected long-distance estimate.** Longer-distance
   predictions (particularly marathon) from a shorter anchor
   performance should be **gated by actual recent training volume** —
   an endurance-distance prediction extrapolated from a 10K anchor is
   only trustworthy if weekly volume actually supports the endurance
   demands of the target distance. When volume is below a stated
   threshold, present a **wider, explicitly-labeled range** (not a
   single confident number) reflecting the extra uncertainty, and state
   the volume threshold that would remove the caveat.

Any legacy "best rolling-window effort" table may still be shown, but
strictly as **informational context**, never as something that can feed
directly into a prediction without going through the confirmed-anchor
gate above.

---

## 9. User-facing functional surfaces (reference implementation)

The reference build is a server-rendered web dashboard. Any
regeneration should provide at least these views/capabilities, but the
UI framework itself is not prescriptive — the logic in §1–§8 is.

1. **Sessions list** — grouped into weekly/monthly/yearly time blocks
   with per-block totals; drag-and-drop file import; re-importing the
   same file must be **idempotent** (dedup by a stable file identity,
   e.g. device serial + creation timestamp — re-processing an existing
   session updates its analysis in place without duplicating it or
   discarding the athlete's manual overrides/confirmations).
2. **Session detail** — key stats; a phase breakdown (warmup / work /
   recoveries / cooldown with distance, time, pace, HR per phase); full
   rep table; intra-session drift stats; recovery-quality (HR drop
   during recoveries); pace and HR time-series charts; the
   physiological-efficiency card (§6) with its own comparison verdict
   against the most recent same-domain similar session; a **manual
   segment-kind override control** (reclassify any segment as
   active/recovery/warmup/cooldown, which must recompute structure and
   all dependent stats immediately, and persist across re-import); a
   session-level manual override (type/structure); a one-tap heat flag.
3. **Trends** — per-domain efficiency trend charts (never mixed across
   domains, per §5.3), each with a rolling average and a linear-fit
   trend line, plus a plain "is this improving?" verdict with supporting
   numbers; legacy/simple band-sample statistics may be kept available
   one level down for audit, clearly labeled as superseded.
4. **Aerobic progression tracker** — a single dual-axis (pace + HR)
   time series of qualifying steady efforts (§5.2's steady-block rule),
   with manually-seeded historical anchor points, where newly-detected
   qualifying blocks are **proposed** and only enter the tracker after
   explicit one-click user confirmation (never auto-added silently).
5. **Similar-sessions browser** (§7) — list of clusters; a **compare
   view** for any cluster that is the product's centerpiece:
   - A **verdict panel** at the top (§6.7's outcome) with plain-language
     evidence, and a fallback to the most recent comparable pair when
     the newest session has no same-domain match in the group.
   - An **efficiency-over-time chart** across the cluster, with points
     colored by effort domain (only same-colored points are validly
     comparable — this coloring is itself a correctness cue to the
     user, not decoration).
   - **Overlaid pace and heart-rate charts**, rep-by-rep, across
     multiple sessions in the cluster, controlled by a **single
     selection UI** (e.g. clickable chips per session) that toggles
     that session's visibility in *both* charts simultaneously — the
     user should never have to manage pace and HR series
     independently. Support an axis toggle between "rep number" and
     "cumulative work time," since directly comparable sessions can
     have different rep counts.
6. **Race predictor** (§8) — tiered display; a form to add a confirmed
   race/time-trial result; explicit statements of what's missing to
   unlock the next tier.

---

## 10. Data model (reference; adapt to any storage engine)

Store, at minimum:
- **Sessions**: start time, sport/sub-type, total distance/time,
  detected origin, detected structure + confidence + which engine level
  resolved it, session type, ambient temperature, heat flag, and a
  serialized KPI summary.
- **Segments/intervals per session**: ordered list, each with kind
  (warmup/work-rep/recovery/cooldown/other), sequence position, rep
  index (when applicable), start/end time, distance, duration, average
  speed, average/max heart rate, an outlier flag, and its detection
  source. Manual per-segment kind overrides must be identifiable by a
  **stable key independent of array position** (e.g. the segment's
  start timestamp) so re-parsing doesn't silently orphan an override.
- **Records**: full 1 Hz time series (timestamp, distance, speed,
  heart rate, cadence, power) — store at full resolution; a season's
  worth of running is small enough that downsampling isn't necessary,
  but design the schema so downsampling could be added later without a
  breaking migration.
- **Efforts** (the Steady Effort Store, §5): one row per extracted rep
  or steady block — kind, timing, pace, stabilized HR (nullable),
  HR-validity flag, HR-suspect flag, effort domain + how the domain was
  derived (HR-based or pace-fallback), REI, average power (nullable),
  and the domain-boundary version active when it was tagged.
- **User overrides**: session-level (type, structure) and per-segment
  (kind), keyed by stable identity, applied at read time so they always
  win over freshly (re-)computed values and survive re-import.
- **Progression points**: proposed / confirmed / rejected steady-effort
  data points for the aerobic-progression tracker, plus manually-seeded
  historical anchors.
- **Athlete settings**: LTHR, max HR, domain-boundary values/version,
  reference pace/HR for normalization — user-editable, not hardcoded.
- **Confirmed races/time-trials**: date, distance, time, label — the
  only valid inputs to Tier-1/2 race prediction (§8).

---

## 11. Software architecture principles

- **Pure-function pipeline stages**, plain data structures in and out;
  isolate all persistence behind a single storage-layer module/class —
  nothing else touches the database directly.
- **Every inferred fact carries its confidence and provenance**
  (detection source/level, domain-derivation method, etc.) all the way
  through to the UI — a number without a confidence attached is a code
  smell in this system.
- **Manual overrides always win, at every layer, forever** (session
  type, structure, per-segment kind, confirmed/rejected progression
  points, confirmed races) — and must survive re-processing of the same
  source file. Key overrides by stable identity, never array index.
- **Idempotent ingestion**: reprocessing an already-known file (by a
  stable file identity, e.g. device serial + creation time) must update
  in place, never duplicate, and must not clobber user overrides or
  confirmations made since the last import.
- **Never silently guess**: any statistic with unmet preconditions must
  render as "not computable" with the specific reason, not a fabricated
  or defaulted value. This is the single most important behavioral rule
  in the whole system — prioritize it over adding new metrics.
- **Additive, guarded schema migrations**: new fields/tables should be
  addable without breaking old data; guard migration statements so
  re-running them against an already-migrated database is a no-op, not
  an error.
- **Offline-first**: vendor any charting/UI library locally rather than
  loading from a CDN, so the whole thing works with no network access.
- **No stage reaches backward**: once ingestion has normalized a file
  into the internal representation, no later stage may re-read the
  original source file — everything needed must already be captured.

### 11.1 Reference tech stack (not prescriptive, but proven for this
scope)
Small server-rendered web app (single-process web framework), one
embedded/file-based relational database, vendored charting library,
drag-and-drop file upload, no build step. This scope (single user,
local files, moderate data volume — low thousands of sessions at most)
does not need a client-side SPA framework, a hosted database, or a
build pipeline; adding any of those would be over-engineering relative
to the stated design mandate (§1.1).

---

## 12. Testing requirements

- Maintain a **ground-truth validation ledger**: for every real source
  file used during development, record the human-confirmed correct
  interval structure/session type, and assert the pipeline reproduces
  it exactly. Any pipeline change that breaks a previously-passing
  ledger entry is a regression, full stop — the ledger is the
  acceptance contract.
- Since real device files typically shouldn't be committed to a public
  repo (they may contain identifying location data), build **synthetic
  fixture generators** that reproduce the *exact verified quirks*
  (§3.2–3.7) — e.g. a fixture builder that constructs a lap sequence
  with the Garmin-Connect timestamp bug, another with in-step autolap
  splitting, another with the three-speed-level trap — so the test
  suite doesn't depend on committing real personal data, but still
  exercises every documented real-world edge case.
- **Fixtures must carry realistic variance.** A synthetic fixture with
  perfectly constant values (identical pace on every "identical" rep,
  for instance) will pass validity checks that use coefficient-of-
  variation thresholds in ways real noisy data never would, silently
  hiding bugs. Inject small realistic jitter into fixtures.
- Unit-test the guard conditions themselves directly (e.g.: does the
  regression correctly refuse a single-pace session; does the
  domain-mismatch check correctly refuse a cross-domain comparison;
  does a too-short rep correctly contribute pace but not heart rate) —
  these guards are the core value proposition (§1.1's "never
  misleading" mandate) and deserve direct test coverage, not just
  incidental coverage through end-to-end fixtures.
- Include at least one end-to-end test that exercises the full pipeline
  through the storage layer and the presentation layer together (import
  → compute → persist → render), to catch integration seams the unit
  tests miss.

---

## 13. Known limitations / explicit non-goals (current version)

- Single user, single local database file. No multi-user, no cloud
  sync, no authentication.
- No GPS map rendering, no running-dynamics deep analysis beyond what's
  described above.
- Only one manufacturer's file format has been empirically verified
  (§3). A field for the recording device's manufacturer should be
  carried through ingestion so a second manufacturer's quirks can be
  added later without redesigning the pipeline, but no second
  manufacturer's parsing rules exist yet — do not assume they generalize.
- The historical long-window personal efficiency model (a rolling
  best-fit "expected efficiency curve" from all historical steady
  efforts, scored against fresh sessions) is a natural extension of §6
  but is **not required for a first implementation** — build the
  pairwise comparison (§6.7) first; it delivers the core "am I
  improving" answer on its own.
- Domain-boundary "hysteresis" (avoiding a workout cluster's trend
  flapping back and forth across a domain boundary session-to-session)
  and versioned-recalibration UI markers are desirable refinements, not
  required for correctness of the core comparisons.

---

## 14. Suggested future improvements (prioritized)

Ordered roughly by value-per-effort for an athlete training toward a
race goal with a mix of intervals and endurance work:

1. **Personal historical efficiency model** (extension of §6.7): fit a
   rolling, recency-weighted expected-efficiency curve per domain from
   *all* historical steady efforts (not just pairwise comparisons), and
   score each new session against what the model expected — "you
   performed 2% above your recent expected efficiency" is a stronger
   long-term fitness signal than any single pairwise comparison, and it
   should always show its own uncertainty band so a small delta isn't
   over-read as a breakthrough.
2. **Grade-Adjusted Pace (GAP)**: incorporate elevation change into the
   pace used for REI/EF wherever meaningful grade is present in the
   record stream — hilly steady runs currently get penalized in
   efficiency metrics purely for terrain, which confounds the "fitter
   vs. not" question exactly the way heat does (§6.5).
3. **Auto-detection of race-equivalent efforts** for Tier 1 prediction
   (§8): a continuous effort at sustained near-maximal heart rate for
   15+ minutes, at a pace consistent with the athlete's best recent
   pace for that duration, is very likely an unlabeled race or hard
   time trial — propose it to the athlete for one-tap confirmation
   (same UX pattern as the progression tracker's propose/confirm flow),
   rather than requiring 100% manual race-logging.
4. **Domain-boundary hysteresis and versioned recalibration markers**
   (§13): once the athlete updates their LTHR/max-HR after a fitness
   test, mark the transition visually on trend charts (a vertical
   marker), and prevent single sessions right at a boundary from
   flapping a workout cluster's assigned domain back and forth.
5. **A second device-manufacturer's quirk set**: extend §3 with the
   equivalent empirically-verified ground truth for a second
   manufacturer, gated behind the manufacturer field already carried
   through ingestion, with its own validation ledger (§12) — do not
   guess at another manufacturer's quirks without real files to verify
   against; every rule in §3 exists because a real file broke a naive
   assumption.
6. **Weather-service backfill for ambient temperature/humidity** on
   sessions where the device didn't log it, to reduce reliance on the
   manual heat-flag tap.
7. **Export/reporting**: a shareable read-only summary (e.g. a
   season-progress report) generated from the same efficiency and
   volume data already computed — useful for sharing with a coach,
   without turning this into a multi-user product (§13).
8. **Configurable domain count/boundaries per athlete**: some athletes
   may want a 5- or 6-zone model instead of the 4-domain default (§5.3)
   — keep the "never compare across domains" rule invariant, but let
   the domain *definitions* be a per-athlete setting rather than a
   fixed constant, since the underlying LTHR-percentage approach
   already generalizes to this.

---

## 15. How to use this file to regenerate a dashboard

If asked to build a similar dashboard from scratch (same domain, new
codebase/stack), read sections in this order:

1. §1–§2 for product intent and pipeline shape.
2. §3 for the FIT/device parsing ground truth — **do not re-derive these
   from first principles or from documentation alone; they were each
   discovered from a real file breaking a naive assumption.** If
   porting to a different device manufacturer, treat §3 as "here is the
   *shape* of quirks to expect," verify empirically against that
   manufacturer's real files, and write an equivalent section before
   trusting any interval-detection output.
3. §4 for the interval-structure resolution logic and its confidence
   model.
4. §5–§7 for the effort-hygiene, domain, and efficiency-comparison
   logic — this is the most novel and most important part of the
   product; the "never misleading" mandate (§1.1) is enforced almost
   entirely by the guards described here.
5. §8 for race prediction, keeping the race-equivalent-anchor
   constraint front and center.
6. §9–§12 for the reference UI surfaces, data model, and testing
   approach — adapt freely to a different stack, but preserve the
   *behavioral* guarantees (idempotent import, overrides always win,
   never-guess, confidence propagation).
7. §13–§14 for what's deliberately out of scope now and what to build
   next, so effort isn't wasted re-solving already-decided scope
   questions.
