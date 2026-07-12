// Hierarchical interval engine — 1:1 port of runlens/intervals.py.
// Every rule here exists because a real file broke a naive assumption
// (see Running.MD §3–§4). Do not simplify without re-running the ledger.

import { detectOrigin, STRUCTURED } from './origin.js';

export const JUNK_MAX_DURATION_S = 5.0;
export const JUNK_MAX_DISTANCE_M = 5.0;
export const CANONICAL_DISTANCES_M = [100, 150, 200, 300, 400, 500, 600,
  800, 1000, 1200, 1500, 1600, 2000, 3000, 5000, 10000];
export const SNAP_TOLERANCE = 0.04;
export const RECOVERY_SNAP_TOLERANCE = 0.15;

const INTENSITY_TO_KIND = { warmup: 'warmup', active: 'rep', interval: 'rep',
  recovery: 'recovery', rest: 'recovery', cooldown: 'cooldown' };
const SPLIT_TYPE_TO_KIND = { interval_warmup: 'warmup',
  interval_active: 'rep', interval_recovery: 'recovery',
  interval_cooldown: 'cooldown' };

const OUTLIER_FACTOR = 1.5;
const DEGENERATE_COVERAGE = 0.85;
export const LONG_RUN_MIN_M = 18000;
export const LONG_RUN_MIN_S = 95 * 60;

// ------------------------------------------------------------ helpers

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
};
const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;

export function cleanLaps(laps) {
  // Laps carrying a wkt_step_index are never junk (step tail laps).
  return laps.filter(lap => {
    if (lap.wkt_step_index !== null && lap.wkt_step_index !== undefined) return true;
    const dur = lap.total_timer_time ?? 0;
    const dist = lap.total_distance;
    if (dur < JUNK_MAX_DURATION_S) return false;
    if (dist !== null && dist !== undefined && dist < JUNK_MAX_DISTANCE_M) return false;
    return true;
  });
}

function lapSpeed(lap) {
  for (const k of ['enhanced_avg_speed', 'avg_speed']) {
    if (lap[k]) return lap[k];
  }
  if (lap.total_distance && lap.total_timer_time) {
    return lap.total_distance / lap.total_timer_time;
  }
  return null;
}

function segmentFromLap(lap, kind, source) {
  return { kind, rep_index: null,
    start_time: lap.start_time ?? null, end_time: lap.timestamp ?? null,
    timer_s: lap.total_timer_time ?? null, distance_m: lap.total_distance ?? null,
    avg_speed_ms: lapSpeed(lap),
    avg_hr: lap.avg_heart_rate ?? null, max_hr: lap.max_heart_rate ?? null,
    outlier: false, source };
}

function segmentFromSplit(split, kind) {
  const dist = split.total_distance, dur = split.total_timer_time;
  const speed = split.avg_speed ?? ((dist && dur) ? dist / dur : null);
  return { kind, rep_index: null,
    start_time: split.start_time ?? null, end_time: split.end_time ?? null,
    timer_s: dur ?? null, distance_m: dist ?? null, avg_speed_ms: speed,
    avg_hr: null, max_hr: null, outlier: false, source: 'garmin_splits' };
}

function numberReps(segments) {
  let i = 0;
  for (const s of segments) {
    if (s.kind === 'rep' && !s.outlier) { i += 1; s.rep_index = i; }
  }
}

function modal(values, bucket) {
  const vals = values.filter(v => v);
  if (!vals.length) return null;
  const buckets = new Map();
  for (const v of vals) {
    const k = Math.round(v / bucket);
    buckets.set(k, (buckets.get(k) || 0) + 1);
  }
  let top = null, topN = -1;
  for (const [k, n] of buckets) if (n > topN) { top = k; topN = n; }
  return median(vals.filter(v => Math.round(v / bucket) === top));
}

// ------------------------------------------------------------ level 1

function stepKinds(steps) {
  const kinds = {};
  for (const [i, s] of Object.entries(steps)) {
    kinds[i] = INTENSITY_TO_KIND[s.intensity] ?? 'other';
  }
  const covered = new Set();
  for (const [i, s] of Object.entries(steps)) {
    if (s.duration_type === 'repeat_until_steps_cmplt' &&
        s.duration_step !== null && s.duration_step !== undefined) {
      for (let k = s.duration_step; k < Number(i); k++) covered.add(k);
    }
  }
  if (covered.size) {
    const repLows = Object.entries(steps)
      .filter(([i, s]) => covered.has(Number(i)) && kinds[i] === 'rep' &&
                          s.custom_target_speed_low)
      .map(([, s]) => s.custom_target_speed_low);
    const refLow = repLows.length ? Math.min(...repLows) : null;
    const lo = Math.min(...covered), hi = Math.max(...covered);
    for (const [i, s] of Object.entries(steps)) {
      const idx = Number(i);
      if (covered.has(idx) || kinds[i] !== 'rep') continue;
      const isOpen = s.duration_type === 'open' && s.intensity === 'active';
      const h = s.custom_target_speed_high;
      const slower = refLow !== null && h !== null && h !== undefined && h < refLow;
      if (isOpen || slower) {
        if (idx < lo) kinds[i] = 'warmup';
        else if (idx > hi) kinds[i] = 'cooldown';
      }
    }
  }
  return kinds;
}

function mergeLapGroup(group, kind) {
  if (group.length === 1) {
    const seg = segmentFromLap(group[0], kind, 'structured_workout');
    seg.wkt_step_index = group[0].wkt_step_index ?? null;
    return seg;
  }
  const timer = group.reduce((a, l) => a + (l.total_timer_time ?? 0), 0);
  const dist = group.reduce((a, l) => a + (l.total_distance ?? 0), 0);
  const hrW = group.filter(l => l.avg_heart_rate)
    .map(l => [l.avg_heart_rate, l.total_timer_time ?? 0]);
  const hrT = hrW.reduce((a, [, t]) => a + t, 0);
  const maxHrs = group.filter(l => l.max_heart_rate).map(l => l.max_heart_rate);
  return { kind, rep_index: null,
    start_time: group[0].start_time ?? null,
    end_time: group[group.length - 1].timestamp ?? null,
    timer_s: timer, distance_m: dist,
    avg_speed_ms: timer ? dist / timer : null,
    avg_hr: hrT ? hrW.reduce((a, [h, t]) => a + h * t, 0) / hrT : null,
    max_hr: maxHrs.length ? Math.max(...maxHrs) : null,
    outlier: false, source: 'structured_workout',
    wkt_step_index: group[0].wkt_step_index ?? null };
}

function levelStructured(activity, laps) {
  const steps = {};
  for (const s of activity.workout_steps || []) steps[s.message_index] = s;
  if (!Object.keys(steps).length) return null;
  if (!laps.some(l => l.wkt_step_index !== null && l.wkt_step_index !== undefined)) return null;
  const kindsByStep = stepKinds(steps);

  const groups = [];
  for (const lap of laps) {
    const idx = lap.wkt_step_index ?? null;
    const last = groups[groups.length - 1];
    if (last && idx !== null && last[last.length - 1].wkt_step_index === idx) {
      last.push(lap);
    } else groups.push([lap]);
  }
  const segments = [];
  for (const group of groups) {
    const idx = group[0].wkt_step_index ?? null;
    const step = steps[idx];
    const kind = step !== undefined ? (kindsByStep[idx] ?? 'other') : 'other';
    const seg = mergeLapGroup(group, kind);
    seg.duration_hint = step ? step.duration_type : null;
    segments.push(seg);
  }
  numberReps(segments);
  return { detection_source: 'structured_workout', confidence: 'high',
           segments, needs_manual_tag: false };
}

// ------------------------------------------------------------ level 2

function repLikeLaps(laps, modalDist, modalDur) {
  return laps.filter(lap => {
    const dist = lap.total_distance, dur = lap.total_timer_time;
    if (modalDist && dist !== null && dist !== undefined) {
      return Math.abs(dist - modalDist) <= 0.15 * modalDist;
    }
    if (modalDur && dur !== null && dur !== undefined) {
      return Math.abs(dur - modalDur) <= 0.10 * modalDur;
    }
    return false;
  });
}

function levelSplits(activity, laps) {
  const intervalSplits = (activity.splits || [])
    .filter(s => SPLIT_TYPE_TO_KIND[s.split_type]);
  const actives = intervalSplits.filter(s => s.split_type === 'interval_active');
  if (!actives.length) return null;

  const sessionTimer = (activity.session || {}).total_timer_time;
  if (actives.length === 1 && sessionTimer &&
      (actives[0].total_timer_time ?? 0) >= DEGENERATE_COVERAGE * sessionTimer) {
    return null;
  }
  const segments = intervalSplits.map(s =>
    segmentFromSplit(s, SPLIT_TYPE_TO_KIND[s.split_type]));

  const activeSegs = segments.filter(s => s.kind === 'rep');
  const modalDur = modal(activeSegs.map(s => s.timer_s), 5.0);
  const modalDist = modal(activeSegs.map(s => s.distance_m), 50.0);
  for (const seg of activeSegs) {
    if (modalDur && (seg.timer_s ?? 0) > OUTLIER_FACTOR * modalDur) seg.outlier = true;
    if (modalDist && (seg.distance_m ?? 0) > OUTLIER_FACTOR * modalDist) seg.outlier = true;
  }
  const cleanActives = activeSegs.filter(s => !s.outlier);
  if (!cleanActives.length) return null;

  const mDur = modal(cleanActives.map(s => s.timer_s), 5.0);
  const mDist = modal(cleanActives.map(s => s.distance_m), 50.0);
  const repLike = repLikeLaps(laps, mDist, mDur);
  if (repLike.length > cleanActives.length) return null;
  if (repLike.length < 0.5 * cleanActives.length && laps.length >= cleanActives.length) {
    return null;
  }
  numberReps(segments);
  return { detection_source: 'garmin_splits',
           confidence: repLike.length === cleanActives.length ? 'high' : 'medium',
           segments, needs_manual_tag: false };
}

// ------------------------------------------------------------ level 3

function alternates(kinds) {
  if (kinds.length < 2) return 0;
  let flips = 0;
  for (let i = 1; i < kinds.length; i++) if (kinds[i] !== kinds[i - 1]) flips++;
  return flips / (kinds.length - 1);
}

function plainResult(laps) {
  return { detection_source: 'lap_inference', confidence: 'medium',
           segments: laps.map(l => segmentFromLap(l, 'other', 'lap_inference')),
           needs_manual_tag: false };
}

function levelLapInference(activity, laps) {
  if (!laps.length) return null;
  const manual = laps.filter(l => l.lap_trigger === 'manual');
  if (manual.length < 4) return plainResult(laps);

  const firstManual = laps.indexOf(manual[0]);
  const lastManual = laps.indexOf(manual[manual.length - 1]);
  const block = laps.slice(firstManual, lastManual + 1);

  const speeds = block.map(lapSpeed);
  let values;
  if (speeds.some(s => s === null)) {
    const hrs = block.map(l => l.avg_heart_rate);
    if (hrs.some(h => h === null || h === undefined)) return null;
    values = hrs.map(Number);
  } else values = speeds.map(Number);

  const ordered = [...values].sort((a, b) => a - b);
  let bestGap = -1, gapI = 0;
  for (let i = 0; i < ordered.length - 1; i++) {
    const g = ordered[i + 1] - ordered[i];
    if (g > bestGap) { bestGap = g; gapI = i; }
  }
  const threshold = (ordered[gapI] + ordered[gapI + 1]) / 2;
  const spread = ordered[ordered.length - 1] - ordered[0];
  if (spread <= 0 || bestGap < 0.3 * spread) return plainResult(laps);

  const kindsBlock = values.map(v => v > threshold ? 'rep' : 'recovery');
  if (alternates(kindsBlock) < 0.6) return plainResult(laps);

  const kinds = {};
  kindsBlock.forEach((k, i) => { kinds[firstManual + i] = k; });

  if (!speeds.some(s => s === null)) {
    // Demote "reps" slower than the rep cluster (three-pace-level trap),
    // then extend over adjacent autolap reps at true rep pace.
    const repSpeeds = values.filter((v, k) => kindsBlock[k] === 'rep')
      .sort((a, b) => a - b);
    const repFloor = repSpeeds.length
      ? 0.88 * repSpeeds[Math.floor(repSpeeds.length / 2)] : null;
    if (repFloor) {
      for (const i of Object.keys(kinds)) {
        if (kinds[i] === 'rep' && lapSpeed(laps[i]) < repFloor) kinds[i] = 'other';
      }
      let i = firstManual - 1;
      while (i >= 0) {
        const v = lapSpeed(laps[i]);
        if (v !== null && v >= repFloor) { kinds[i] = 'rep'; i--; } else break;
      }
      let j = lastManual + 1;
      while (j < laps.length) {
        const v = lapSpeed(laps[j]);
        if (v !== null && v >= repFloor) { kinds[j] = 'rep'; j++; } else break;
      }
    }
  }
  const idxs = Object.keys(kinds).map(Number);
  const lo = Math.min(...idxs), hi = Math.max(...idxs);
  const segments = laps.map((lap, i) => {
    let kind;
    if (i < lo) kind = 'warmup';
    else if (i > hi) kind = 'cooldown';
    else kind = kinds[i] ?? 'other';
    return segmentFromLap(lap, kind, 'lap_inference');
  });
  for (let i = segments.length - 1; i >= 0; i--) {
    if (segments[i].kind === 'rep') break;
    if (segments[i].kind === 'recovery') segments[i].kind = 'cooldown';
  }
  numberReps(segments);
  return { detection_source: 'lap_inference', confidence: 'medium',
           segments, needs_manual_tag: false };
}

// ------------------------------------------------------------ resolve

export function resolve(activity) {
  const info = detectOrigin(activity);
  const laps = cleanLaps(activity.laps || []);
  let result = null;
  if (info.origin === STRUCTURED) result = levelStructured(activity, laps);
  if (!result) result = levelSplits(activity, laps);
  if (!result) result = levelLapInference(activity, laps);
  if (!result) result = { detection_source: 'unclassified', confidence: 'low',
                          segments: [], needs_manual_tag: true };
  result.origin = info.origin;
  result.wkt_name = info.wkt_name;
  result.structure = structureString(result.segments);
  result.session_type = classifySession(activity, result);
  return result;
}

// ------------------------------------------------------------ labels

export function snapDistance(meters, tolerance = SNAP_TOLERANCE) {
  if (!meters) return null;
  for (const c of CANONICAL_DISTANCES_M) {
    if (Math.abs(meters - c) / c <= tolerance) return c;
  }
  return null;
}

export function fmtSeconds(seconds) {
  let s = Math.round(seconds);
  const nearest5 = 5 * Math.round(seconds / 5);
  if (Math.abs(seconds - nearest5) <= 2) s = nearest5;
  const m = Math.floor(s / 60), rem = s % 60;
  if (m && rem) return `${m}'${String(rem).padStart(2, '0')}"`;
  if (m) return `${m}'`;
  return `${rem}"`;
}

function cv(values) {
  if (values.length < 2) return 0;
  const m = mean(values);
  if (!m) return 0;
  const varr = mean(values.map(v => (v - m) ** 2));
  return Math.sqrt(varr) / m;
}

function segSnap(seg) {
  if (seg.duration_hint === 'time') return null;
  return snapDistance(seg.distance_m);
}

function repLabel(group) {
  const n = group.length > 1 ? `${group.length}x` : '';
  const snaps = group.map(segSnap);
  const nonNull = snaps.filter(s => s);
  if (nonNull.length && new Set(nonNull).size === 1 &&
      nonNull.length >= 0.8 * group.length) {
    return `${n}${nonNull[0]}m`;
  }
  const durations = group.map(s => s.timer_s).filter(Boolean);
  const dists = group.map(s => s.distance_m).filter(Boolean);
  const hintTime = group.some(s => s.duration_hint === 'time');
  if (!hintTime && dists.length && durations.length &&
      cv(dists) <= 0.08 && cv(dists) < cv(durations)) {
    const medSnap = snapDistance(median(dists), 0.10);
    if (medSnap) return `${n}${medSnap}m`;
  }
  if (durations.length) {
    const m = modal(durations, 5.0);
    if (m && durations.every(d => Math.abs(d - m) <= Math.max(3, 0.05 * m))) {
      return `${n}${fmtSeconds(m)}`;
    }
  }
  return group.map(s => {
    const snap = segSnap(s);
    return snap ? `${snap}m` : fmtSeconds(s.timer_s ?? 0);
  }).join(' + ');
}

function recoveryLabel(recoveries, prefix = 'R') {
  if (!recoveries.length) return null;
  const hints = new Set(recoveries.map(s => s.duration_hint));
  const durs = recoveries.map(s => s.timer_s).filter(Boolean);
  const dists = recoveries.map(s => s.distance_m).filter(Boolean);
  let preferDist;
  if (hints.has('distance')) preferDist = true;
  else if (hints.has('time')) preferDist = false;
  else if (!dists.length) preferDist = false;
  else if (!durs.length || cv(dists) < cv(durs)) preferDist = true;
  else preferDist = cv(dists) === cv(durs) &&
    snapDistance(median(dists), 0.08) !== null;

  if (preferDist && dists.length) {
    const snap = snapDistance(median(dists), RECOVERY_SNAP_TOLERANCE);
    if (snap) return `${prefix}${snap}m`;
  }
  if (durs.length) {
    const m = modal(durs, 5.0);
    if (m) return `${prefix}${fmtSeconds(m)}`;
  }
  if (dists.length) {
    const snap = snapDistance(median(dists), RECOVERY_SNAP_TOLERANCE);
    if (snap) return `${prefix}${snap}m`;
  }
  return null;
}

function groupReps(reps) {
  const groups = [];
  for (const rep of reps) {
    const last = groups[groups.length - 1];
    if (last) {
      const prev = last[last.length - 1];
      const sameDist = segSnap(rep) !== null && segSnap(rep) === segSnap(prev);
      const sameTime = rep.timer_s && prev.timer_s &&
        Math.abs(rep.timer_s - prev.timer_s) <= Math.max(3, 0.05 * prev.timer_s);
      const similarPace = rep.avg_speed_ms && prev.avg_speed_ms &&
        Math.abs(rep.avg_speed_ms - prev.avg_speed_ms) <= 0.08 * prev.avg_speed_ms;
      const closeTime = rep.timer_s && prev.timer_s &&
        Math.abs(rep.timer_s - prev.timer_s) <= 0.15 * prev.timer_s;
      if (sameDist || sameTime || (similarPace && closeTime)) {
        last.push(rep);
        continue;
      }
    }
    groups.push([rep]);
  }
  return groups;
}

export function structureString(segments) {
  const reps = segments.filter(s => s.kind === 'rep' && !s.outlier);
  if (!reps.length) return null;

  const order = segments.filter(s =>
    (s.kind === 'rep' || s.kind === 'recovery') && !s.outlier);
  const inner = [];
  order.forEach((seg, i) => {
    if (seg.kind !== 'recovery') return;
    const before = order.slice(0, i).some(s => s.kind === 'rep');
    const after = order.slice(i + 1).some(s => s.kind === 'rep');
    if (before && after) inner.push(seg);
  });

  let setRecs = [], normalRecs = [...inner];
  if (inner.length >= 2) {
    const ds = inner.map(r => r.timer_s ?? 0).sort((a, b) => a - b);
    let gap = -1, gapI = 0;
    for (let i = 0; i < ds.length - 1; i++) {
      if (ds[i + 1] - ds[i] > gap) { gap = ds[i + 1] - ds[i]; gapI = i; }
    }
    const shortMedian = median(ds.slice(0, gapI + 1));
    if (gap >= Math.max(30, shortMedian) && ds[gapI + 1] >= 60) {
      const cut = (ds[gapI] + ds[gapI + 1]) / 2;
      setRecs = inner.filter(r => (r.timer_s ?? 0) > cut);
      normalRecs = inner.filter(r => (r.timer_s ?? 0) <= cut);
    }
  }

  if (setRecs.length) {
    const sets = [[]];
    for (const seg of order) {
      if (setRecs.includes(seg)) sets.push([]);
      else if (seg.kind === 'rep') sets[sets.length - 1].push(seg);
    }
    const nonEmpty = sets.filter(s => s.length);
    const labels = nonEmpty.map(repLabel);
    const counts = new Map();
    for (const l of labels) counts.set(l, (counts.get(l) || 0) + 1);
    let topLabel = null, topN = -1;
    for (const [l, c] of counts) if (c > topN) { topLabel = l; topN = c; }
    const majoritySize = Math.max(...nonEmpty
      .filter((s, i) => labels[i] === topLabel).map(s => s.length));
    const uniform = topN === nonEmpty.length;
    const majority = topN >= 0.5 * nonEmpty.length &&
      nonEmpty.every((s, i) => labels[i] === topLabel || s.length < majoritySize);
    if (nonEmpty.length > 1 && (uniform || majority)) {
      const base = `${nonEmpty.length}x(${topLabel})`;
      const r = recoveryLabel(normalRecs, 'R');
      const sr = recoveryLabel(setRecs, 'SR');
      return [base, r, sr].filter(Boolean).join(' ');
    }
  }
  const body = groupReps(reps).map(repLabel).join(' + ');
  const r = recoveryLabel(normalRecs, 'R');
  return r ? `${body} ${r}` : body;
}

// ------------------------------------------------------------ classify

export function classifySession(activity, result) {
  const session = activity.session || {};
  if (session.sub_sport === 'race') return 'race';

  let reps = result.segments.filter(s => s.kind === 'rep' && !s.outlier);
  const sessionTimer = session.total_timer_time ?? 0;
  const repTimer = reps.reduce((a, s) => a + (s.timer_s ?? 0), 0);
  if (reps.length && sessionTimer && repTimer / sessionTimer < 0.05) reps = [];

  if (reps.length) {
    if (result.detection_source === 'lap_inference') {
      const snaps = reps.map(r => snapDistance(r.distance_m));
      const durs = reps.map(r => r.timer_s).filter(Boolean);
      const m = modal(durs, 5.0);
      const regularTime = m && durs.every(d => Math.abs(d - m) <= Math.max(5, 0.15 * m));
      const regularDist = snaps.every(s => s) && new Set(snaps).size <= 3;
      if (!regularTime && !regularDist) return 'fartlek';
    }
    return 'intervals';
  }
  if (result.detection_source === 'unclassified') return 'unknown';
  const dist = session.total_distance ?? 0;
  const dur = session.total_timer_time ?? 0;
  if (dist >= LONG_RUN_MIN_M || dur >= LONG_RUN_MIN_S) return 'long';
  return 'easy';
}
