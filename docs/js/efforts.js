// Layer 0 hygiene + Steady Effort Store — 1:1 port of runlens/efforts.py.

import { findProgressionCandidates } from './metrics.js';

export const HR_SPIKE_DELTA = 8;
export const HR_GAP_INTERPOLATE_S = 5;
export const CADENCE_LOCK_TOL = 3;
export const CADENCE_LOCK_MIN_S = 30;
export const HR_STABILIZE_S = 45;
export const HR_MIN_REP_S = 90;
export const HR_MIN_SAMPLES = 20;

export const DEFAULT_SETTINGS = {
  lthr: 176.0, max_hr: 190.0, z2_ceiling: 143.0, ref_hr: 175.0,
  lthr_pace_s_km: 235.0, boundary_version: 1,
};
export const DOMAINS = ['easy', 'steady', 'threshold', 'hard'];

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
};
const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;

export function cleanHeartRate(records) {
  const out = records.map(r => ({ ...r }));
  let lastGood = null;
  let badRun = [];
  for (let i = 0; i < out.length; i++) {
    const r = out[i];
    const hr = r.heart_rate;
    let ok = hr !== null && hr !== undefined && hr >= 30 && hr <= 230;
    if (ok && lastGood) {
      const [prevHr, prevI] = lastGood;
      const dt = Math.max(1, (out[i].timestamp - out[prevI].timestamp) / 1000);
      if (Math.abs(hr - prevHr) > HR_SPIKE_DELTA * dt) ok = false;
    }
    if (ok) {
      if (badRun.length && lastGood) {
        const [prevHr, prevI] = lastGood;
        const gap = (out[i].timestamp - out[prevI].timestamp) / 1000;
        if (gap <= HR_GAP_INTERPOLATE_S) {
          badRun.forEach((j, k) => {
            const frac = (k + 1) / (badRun.length + 1);
            out[j].heart_rate = Math.round(prevHr + frac * (hr - prevHr));
            out[j].hr_ok = true;
          });
        }
      }
      badRun = [];
      lastGood = [hr, i];
      r.hr_ok = true;
    } else {
      if (r.hr_ok === undefined) r.hr_ok = false;
      badRun.push(i);
    }
  }
  // Cadence lock detection.
  let runStart = null;
  for (let i = 0; i <= out.length; i++) {
    const r = out[i] ?? null;
    const locked = r && r.hr_ok && r.heart_rate && r.cadence &&
      Math.abs(r.heart_rate - r.cadence) < CADENCE_LOCK_TOL;
    if (locked && runStart === null) runStart = i;
    else if (!locked && runStart !== null) {
      const span = (out[i - 1].timestamp - out[runStart].timestamp) / 1000;
      if (span >= CADENCE_LOCK_MIN_S) {
        for (let j = runStart; j < i; j++) {
          out[j].hr_ok = false;
          out[j].hr_suspect = true;
        }
      }
      runStart = null;
    }
  }
  return out;
}

export function smoothSpeed(records, window = 7) {
  const speeds = records.map(r => r.enhanced_speed);
  const half = Math.floor(window / 2);
  records.forEach((r, i) => {
    const chunk = speeds.slice(Math.max(0, i - half), i + half + 1)
      .filter(s => s !== null && s !== undefined);
    r.speed_smooth = chunk.length ? mean(chunk) : null;
  });
  return records;
}

export function domainBoundaries(settings) {
  return { easy_max: settings.z2_ceiling,
           steady_max: 0.93 * settings.lthr,
           threshold_max: 1.02 * settings.lthr };
}

export function domainForHr(hr, settings) {
  const b = domainBoundaries(settings);
  if (hr <= b.easy_max) return 'easy';
  if (hr < b.steady_max) return 'steady';
  if (hr <= b.threshold_max) return 'threshold';
  return 'hard';
}

export function domainForPace(pace, settings) {
  const ltp = settings.lthr_pace_s_km;
  if (pace < 0.95 * ltp) return 'hard';
  if (pace <= 1.05 * ltp) return 'threshold';
  if (pace <= 1.15 * ltp) return 'steady';
  return 'easy';
}

function recordsBetween(records, start, end) {
  return records.filter(r => r.timestamp >= start && r.timestamp <= end);
}

function stabilizedHr(records, start, end) {
  const from = new Date(start.getTime() + HR_STABILIZE_S * 1000);
  const window = recordsBetween(records, from, end);
  const good = window.filter(r => r.hr_ok).map(r => r.heart_rate);
  const suspect = window.filter(r => r.hr_suspect).length;
  const suspectMaj = window.length ? suspect > window.length / 2 : false;
  if (good.length < HR_MIN_SAMPLES) return [null, suspectMaj, good.length];
  return [median(good), suspectMaj, good.length];
}

export function extractEfforts(segments, records, pauses, settings = null) {
  const s = { ...DEFAULT_SETTINGS, ...(settings || {}) };
  const efforts = [];

  const reps = segments.filter(x => x.kind === 'rep' && !x.outlier);
  for (const seg of reps) {
    const start = seg.start_time, end = seg.end_time;
    const timer = seg.timer_s ?? 0;
    if (!start || !end || !timer) continue;
    const speed = seg.avg_speed_ms ?? (timer ? (seg.distance_m ?? 0) / timer : null);
    if (!speed) continue;
    const pace = 1000 / speed;
    let [hrMed, suspect] = [null, false];
    if (timer >= HR_MIN_REP_S) {
      [hrMed, suspect] = stabilizedHr(records, start, end);
    }
    const hrValid = hrMed !== null && !suspect;
    const window = recordsBetween(records, start, end);
    const powers = window.map(r => r.power).filter(Boolean);
    efforts.push({
      kind: 'rep', rep_index: seg.rep_index ?? null,
      start_time: start, end_time: end,
      timer_s: timer, distance_m: seg.distance_m ?? null,
      speed_ms: speed, pace_s_km: Math.round(pace * 10) / 10,
      hr_median: hrMed !== null ? Math.round(hrMed * 10) / 10 : null,
      hr_valid: hrValid, hr_suspect: suspect,
      domain: hrValid ? domainForHr(hrMed, s) : domainForPace(pace, s),
      domain_basis: hrValid ? 'hr' : 'pace',
      rei: hrValid ? speed / hrMed : null,
      avg_power: powers.length ? Math.round(mean(powers) * 10) / 10 : null,
      boundary_version: s.boundary_version,
    });
  }

  for (const cand of findProgressionCandidates(segments, records, pauses)) {
    const [hrMed, suspect] = stabilizedHr(records, cand.start_time, cand.end_time);
    const hrValid = hrMed !== null && !suspect;
    const window = recordsBetween(records, cand.start_time, cand.end_time);
    const powers = window.map(r => r.power).filter(Boolean);
    const speed = 1000 / cand.pace_s_km;
    efforts.push({
      kind: 'steady', rep_index: null,
      start_time: cand.start_time, end_time: cand.end_time,
      timer_s: cand.timer_s, distance_m: null,
      speed_ms: Math.round(speed * 1000) / 1000, pace_s_km: cand.pace_s_km,
      hr_median: hrMed !== null ? Math.round(hrMed * 10) / 10 : null,
      hr_valid: hrValid, hr_suspect: suspect,
      domain: hrValid ? domainForHr(hrMed, s) : domainForPace(cand.pace_s_km, s),
      domain_basis: hrValid ? 'hr' : 'pace',
      rei: hrValid ? speed / hrMed : null,
      avg_power: powers.length ? Math.round(mean(powers) * 10) / 10 : null,
      boundary_version: s.boundary_version,
    });
  }
  return efforts;
}
