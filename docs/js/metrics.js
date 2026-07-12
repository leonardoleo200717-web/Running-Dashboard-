// KPIs, trends, efficiency, clustering, race prediction — port of
// runlens/metrics.py. Pure functions on plain data.

export const DEFAULT_HR_BAND = [165, 175];
export const DEFAULT_PACE_BAND_S = [230, 235];
export const MIN_BAND_SAMPLES = 300;
export const PROGRESSION_MIN_TIMER_S = 600;
export const PROGRESSION_MAX_PACE_CV = 0.05;
export const MIN_MOVING_SPEED = 1.4;
export const REGRESSION_MIN_REPS = 4;
export const REGRESSION_MIN_SPREAD_S_KM = 15.0;
export const REGRESSION_MIN_R2 = 0.6;
export const REI_COMPARE_PACE_TOL_S_KM = 10.0;
export const REI_CHANGE_NOTABLE_PCT = 1.0;
export const TYPICAL_PACE_COST_BPM = 5.0;
export const CLUSTER_RUN_TOLERANCE = 0.10;
export const CLUSTER_TIME_TOLERANCE = 0.15;
export const RACE_DISTANCES_M = [['5K', 5000], ['10K', 10000],
  ['Half marathon', 21097.5], ['Marathon', 42195]];
export const BEST_EFFORT_DISTANCES_M = [1000, 1609, 3000, 5000, 10000];

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b);
  const n = s.length;
  return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null;
};
const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
const pstdev = (xs) => {
  const m = mean(xs);
  return m === null ? null : Math.sqrt(mean(xs.map(x => (x - m) ** 2)));
};
const mode = (xs) => {
  const c = new Map();
  let top = null, n = -1;
  for (const x of xs) {
    c.set(x, (c.get(x) || 0) + 1);
    if (c.get(x) > n) { n = c.get(x); top = x; }
  }
  return top;
};

export function paceSPerKm(speed) {
  return speed && speed > 0 ? 1000 / speed : null;
}

export function fmtPace(s) {
  if (!s) return '–';
  const m = Math.floor(Math.round(s) / 60), sec = Math.round(s) % 60;
  return `${m}:${String(sec).padStart(2, '0')}/km`;
}

export function fmtHms(seconds) {
  if (!seconds) return '–';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
           : `${m}:${String(sec).padStart(2, '0')}`;
}

const recordsIn = (records, start, end) =>
  records.filter(r => r.timestamp && r.timestamp >= start && r.timestamp <= end);

// ------------------------------------------------------------ rep table

export function repTable(segments, records) {
  const rows = [];
  for (const seg of segments) {
    if (seg.kind !== 'rep') continue;
    let avgHr = seg.avg_hr ?? null, maxHr = seg.max_hr ?? null;
    if ((avgHr === null || maxHr === null) && seg.start_time && seg.end_time) {
      const hrs = recordsIn(records, seg.start_time, seg.end_time)
        .map(r => r.heart_rate).filter(Boolean);
      if (hrs.length) {
        if (avgHr === null) avgHr = Math.round(mean(hrs) * 10) / 10;
        if (maxHr === null) maxHr = Math.max(...hrs);
      }
    }
    const pace = paceSPerKm(seg.avg_speed_ms);
    rows.push({ rep_index: seg.rep_index, distance_m: seg.distance_m,
      timer_s: seg.timer_s, pace_s_km: pace, pace: fmtPace(pace),
      avg_hr: avgHr, max_hr: maxHr, outlier: seg.outlier });
  }
  return rows;
}

export function intraSetDrift(repRows) {
  const rows = repRows.filter(r => !r.outlier);
  const out = { hr_drift_pct: null, pace_drift_pct: null };
  if (rows.length < 3) return out;
  const thirds = (vals) => {
    const n = Math.max(1, Math.floor(vals.length / 3));
    return [vals.slice(0, n), vals.slice(-n)];
  };
  const hrs = rows.map(r => r.avg_hr).filter(Boolean);
  if (hrs.length >= 3) {
    const [f, l] = thirds(hrs);
    const base = mean(f);
    if (base) out.hr_drift_pct = Math.round(1000 * (mean(l) - base) / base) / 10;
  }
  const paces = rows.map(r => r.pace_s_km).filter(Boolean);
  if (paces.length >= 3) {
    const [f, l] = thirds(paces);
    const base = mean(f);
    if (base) out.pace_drift_pct = Math.round(1000 * (mean(l) - base) / base) / 10;
  }
  return out;
}

export function recoveryQuality(segments, records) {
  const drops = [];
  for (const seg of segments) {
    if (seg.kind !== 'recovery' || !seg.start_time || !seg.end_time) continue;
    const hrs = recordsIn(records, seg.start_time, seg.end_time)
      .map(r => r.heart_rate).filter(Boolean);
    if (hrs.length >= 5) {
      drops.push(mean(hrs.slice(0, 3)) - mean(hrs.slice(-3)));
    }
  }
  return { avg_hr_drop: drops.length ? Math.round(mean(drops) * 10) / 10 : null,
           n_recoveries: drops.length };
}

// ------------------------------------------------------------ trends

const moving = (records) =>
  records.filter(r => (r.enhanced_speed ?? 0) >= MIN_MOVING_SPEED);

export function paceAtHr(records, band = DEFAULT_HR_BAND, minSamples = MIN_BAND_SAMPLES) {
  const [lo, hi] = band;
  const speeds = moving(records)
    .filter(r => r.heart_rate && r.heart_rate >= lo && r.heart_rate <= hi)
    .map(r => r.enhanced_speed);
  return speeds.length >= minSamples ? paceSPerKm(mean(speeds)) : null;
}

export function hrAtPace(records, bandS = DEFAULT_PACE_BAND_S, minSamples = MIN_BAND_SAMPLES) {
  const [lo, hi] = bandS;
  const hrs = [];
  for (const r of moving(records)) {
    const p = paceSPerKm(r.enhanced_speed);
    if (p && p >= lo && p <= hi && r.heart_rate) hrs.push(r.heart_rate);
  }
  return hrs.length >= minSamples ? Math.round(mean(hrs) * 10) / 10 : null;
}

export function weeklyVolume(sessions) {
  const weeks = new Map();
  for (const s of sessions) {
    const start = s.start_time_utc ? new Date(s.start_time_utc) : null;
    if (!start) continue;
    const key = isoWeek(start);
    const w = weeks.get(key) || { week: key, km: 0, n_sessions: 0 };
    w.km += (s.total_distance_m ?? 0) / 1000;
    w.n_sessions += 1;
    weeks.set(key, w);
  }
  const out = [...weeks.values()].sort((a, b) => a.week < b.week ? -1 : 1);
  out.forEach(w => { w.km = Math.round(w.km * 10) / 10; });
  return out;
}

export function isoWeek(d) {
  const date = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((date - yearStart) / 86400000 + 1) / 7);
  return `${date.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
}

// ------------------------------------------------------------ trend modeling

export function linearRegression(xs, ys) {
  const n = xs.length;
  if (n < 3 || ys.length !== n) return null;
  const mx = mean(xs), my = mean(ys);
  const denom = xs.reduce((a, x) => a + (x - mx) ** 2, 0);
  if (!denom) return null;
  const slope = xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0) / denom;
  return [slope, my - slope * mx];
}

export function rollingMean(values, window = 5) {
  return values.map((_, i) => {
    const chunk = values.slice(Math.max(0, i - window + 1), i + 1);
    return Math.round(mean(chunk) * 100) / 100;
  });
}

export function trendSeries(points, window = 5) {
  if (points.length < 3) return { rolling: null, fit: null, slope_per_30d: null };
  const xs = points.map(p => new Date(p.date).getTime() / 86400000);
  const ys = points.map(p => p.value);
  const reg = linearRegression(xs, ys);
  return {
    rolling: rollingMean(ys, window),
    fit: reg ? xs.map(x => Math.round((reg[0] * x + reg[1]) * 100) / 100) : null,
    slope_per_30d: reg ? Math.round(reg[0] * 30 * 100) / 100 : null,
  };
}

export function improvementStats(pointsHr, pointsPace, weekly) {
  const hrTrend = trendSeries(pointsHr);
  const paceTrend = trendSeries(pointsPace);
  const verdict = (slope, threshold) => slope === null ? null : slope < threshold;
  let volChange = null;
  if (weekly.length >= 2) {
    const kms = weekly.map(w => w.km);
    const recent = kms.slice(-4);
    const previous = kms.slice(-8, -4).length ? kms.slice(-8, -4)
      : (kms.slice(0, -recent.length).length ? kms.slice(0, -recent.length) : null);
    if (previous) {
      const prevAvg = mean(previous);
      if (prevAvg) volChange = Math.round(1000 * (mean(recent) - prevAvg) / prevAvg) / 10;
    }
  }
  const pah = verdict(hrTrend.slope_per_30d, -0.5);
  const hap = verdict(paceTrend.slope_per_30d, -0.3);
  const aerobic = [pah, hap].filter(v => v !== null);
  return { pace_at_hr_slope_30d: hrTrend.slope_per_30d, pace_at_hr_improving: pah,
    hr_at_pace_slope_30d: paceTrend.slope_per_30d, hr_at_pace_improving: hap,
    volume_change_pct: volChange,
    volume_increasing: volChange === null ? null : volChange > 0,
    improving: aerobic.length ? aerobic.some(v => v) : null };
}

// ------------------------------------------------------------ efficiency

export function sessionEfficiency(repEfforts) {
  const valid = repEfforts.filter(e => e.rei)
    .sort((a, b) => (a.rep_index ?? 0) - (b.rep_index ?? 0));
  const out = { n_reps: repEfforts.length, n_hr_valid: valid.length,
    rei_early: null, rei_full: null, domain: null,
    median_pace_s_km: null, mean_hr: null };
  const paces = repEfforts.map(e => e.pace_s_km).filter(Boolean);
  if (paces.length) out.median_pace_s_km = Math.round(median(paces) * 10) / 10;
  if (repEfforts.length) out.domain = mode(repEfforts.map(e => e.domain));
  if (valid.length) {
    const earlyN = Math.ceil(valid.length / 3);
    out.rei_early = mean(valid.slice(0, earlyN).map(e => e.rei));
    out.rei_full = mean(valid.map(e => e.rei));
    out.mean_hr = Math.round(mean(valid.map(e => e.hr_median)) * 10) / 10;
  }
  return out;
}

export function gatedRegression(repEfforts, refHr, refPace) {
  const valid = repEfforts.filter(e => e.rei);
  const out = { valid: false, reason: null, pace_cost: null,
    expected_hr_at_ref_pace: null, expected_pace_at_ref_hr: null,
    r2: null, n: valid.length };
  if (valid.length < REGRESSION_MIN_REPS) {
    out.reason = `needs ≥${REGRESSION_MIN_REPS} reps with stabilized HR (has ${valid.length})`;
    return out;
  }
  if (new Set(valid.map(e => e.domain)).size > 1) {
    out.reason = 'reps span multiple effort domains';
    return out;
  }
  const paces = valid.map(e => e.pace_s_km);
  const hrs = valid.map(e => e.hr_median);
  const spread = Math.max(...paces) - Math.min(...paces);
  if (spread < REGRESSION_MIN_SPREAD_S_KM) {
    out.reason = `single-pace session (pace spread ${Math.round(spread)} s/km < ${REGRESSION_MIN_SPREAD_S_KM})`;
    return out;
  }
  const reg = linearRegression(paces, hrs);
  if (!reg) { out.reason = 'degenerate fit'; return out; }
  const [slope, intercept] = reg;
  const fitted = paces.map(p => slope * p + intercept);
  const ssRes = hrs.reduce((a, h, i) => a + (h - fitted[i]) ** 2, 0);
  const mh = mean(hrs);
  const ssTot = hrs.reduce((a, h) => a + (h - mh) ** 2, 0);
  const r2 = ssTot ? 1 - ssRes / ssTot : 0;
  if (r2 < REGRESSION_MIN_R2) {
    out.reason = `fit too noisy (R² ${r2.toFixed(2)} < ${REGRESSION_MIN_R2})`;
    return out;
  }
  out.valid = true;
  out.r2 = Math.round(r2 * 100) / 100;
  out.pace_cost = Math.round(-slope * 100) / 10;
  if (slope) out.expected_pace_at_ref_hr = Math.round((refHr - intercept) / slope * 10) / 10;
  if (refPace !== null && refPace !== undefined) {
    out.ref_pace_in_range = Math.min(...paces) - 5 <= refPace &&
                            refPace <= Math.max(...paces) + 5;
    out.expected_hr_at_ref_pace = Math.round((slope * refPace + intercept) * 10) / 10;
  }
  return out;
}

export function compareSessions(cur, prev, curMeta, prevMeta) {
  const insights = [];
  let verdict = 'not_comparable', deltaPct = null, explained = false;

  if (!cur.n_reps || !prev.n_reps) {
    return { verdict, delta_pct: null, insights: ['No work reps to compare.'] };
  }
  if (cur.domain !== prev.domain) {
    return { verdict, delta_pct: null, insights: [
      `Different effort domains (${cur.domain} vs ${prev.domain}) — ` +
      'efficiency comparison is not valid across domains.'] };
  }
  let paceD = null;
  if (cur.median_pace_s_km && prev.median_pace_s_km) {
    paceD = cur.median_pace_s_km - prev.median_pace_s_km;
  }
  if (cur.rei_early && prev.rei_early) {
    if (paceD !== null && Math.abs(paceD) > REI_COMPARE_PACE_TOL_S_KM) {
      const hrD = (cur.mean_hr ?? 0) - (prev.mean_hr ?? 0);
      const expectedRise = (-paceD / 10) * TYPICAL_PACE_COST_BPM;
      const margin = 2;
      explained = true;
      const f = (x) => (x >= 0 ? '+' : '') + Math.round(x);
      if (paceD < 0) {
        if (hrD <= expectedRise - margin) {
          verdict = 'improving';
          insights.push(`Faster by ${Math.abs(Math.round(paceD))} s/km with only ` +
            `${f(hrD)} bpm (expected ~${f(expectedRise)} bpm at typical pace cost) — ` +
            `efficiency gain vs ${prevMeta.date}.`);
        } else if (hrD >= expectedRise + margin) {
          verdict = 'similar';
          insights.push(`Faster by ${Math.abs(Math.round(paceD))} s/km but ${f(hrD)} bpm ` +
            `(more than the ~${f(expectedRise)} expected) — mostly higher ` +
            'cardiovascular effort, not fitness.');
        } else {
          verdict = 'similar';
          insights.push(`Faster by ${Math.abs(Math.round(paceD))} s/km at roughly the ` +
            `expected heart-rate cost (${f(hrD)} bpm) — consistent effort ` +
            'scaling, no clear efficiency change.');
        }
      } else {
        if (hrD <= expectedRise - margin) {
          verdict = 'improving';
          insights.push(`Slower by ${Math.round(paceD)} s/km but HR dropped ${f(hrD)} bpm ` +
            '(more than expected) — lower cost at easier pace.');
        } else {
          verdict = 'similar';
          insights.push(`Slower session at proportionally lower effort ` +
            `(${f(hrD)} bpm) — no efficiency signal.`);
        }
      }
    } else {
      deltaPct = Math.round(1000 * (cur.rei_early - prev.rei_early) / prev.rei_early) / 10;
      if (deltaPct >= REI_CHANGE_NOTABLE_PCT) {
        verdict = 'improving';
        insights.push(`Running efficiency improved ${deltaPct > 0 ? '+' : ''}${deltaPct}% vs ` +
          `${prevMeta.date} (early-session REI, n=${cur.n_hr_valid} vs ` +
          `${prev.n_hr_valid} reps, ${cur.domain} domain).`);
      } else if (deltaPct <= -REI_CHANGE_NOTABLE_PCT) {
        verdict = 'declining';
        insights.push(`Running efficiency decreased ${deltaPct}% vs ${prevMeta.date} ` +
          `(early-session REI, n=${cur.n_hr_valid} vs ${prev.n_hr_valid} reps).`);
      } else {
        verdict = 'similar';
        insights.push(`Efficiency within noise of ${prevMeta.date} ` +
          `(${deltaPct >= 0 ? '+' : ''}${deltaPct}%).`);
      }
    }
  }
  if (!explained && paceD !== null && cur.mean_hr && prev.mean_hr) {
    const hrD = cur.mean_hr - prev.mean_hr;
    if (Math.abs(paceD) <= 5 && hrD <= -3) {
      insights.push(`Same pace, ${Math.abs(Math.round(hrD))} bpm lower heart rate.`);
    } else if (Math.abs(paceD) <= 5 && hrD >= 3) {
      insights.push(`Same pace, ${Math.round(hrD)} bpm higher heart rate.`);
    } else if (paceD <= -5 && hrD >= 3 &&
               (deltaPct === null || deltaPct < REI_CHANGE_NOTABLE_PCT)) {
      insights.push(`Faster by ${Math.abs(Math.round(paceD))} s/km but ` +
        `${Math.round(hrD)} bpm higher — mostly explained by higher cardiovascular effort.`);
    } else if (paceD <= -5 && hrD <= 0) {
      insights.push(`Faster by ${Math.abs(Math.round(paceD))} s/km at no extra heart-rate cost.`);
    }
  }
  if (verdict === 'not_comparable' && cur.n_hr_valid === 0) {
    insights.push('No stabilized HR available for these reps (< 90 s or flagged) — ' +
      `pace-only comparison: ${fmtPace(prev.median_pace_s_km)} → ${fmtPace(cur.median_pace_s_km)}.`);
    if (paceD !== null) {
      verdict = paceD < -2 ? 'improving' : (paceD > 2 ? 'declining' : 'similar');
    }
  }
  if (curMeta.hot || prevMeta.hot) {
    insights.push('⚠ One of the sessions is heat-flagged — HR comparison is ' +
      'confounded by temperature.');
  }
  return { verdict, delta_pct: deltaPct, insights };
}

// ------------------------------------------------------------ clustering

export function sessionSignature(session, segments, snapDistance) {
  const reps = segments.filter(s => s.kind === 'rep' && !s.outlier);
  if (!reps.length || ['easy', 'long'].includes(session.session_type)) {
    return ['run', session.total_distance_m ?? 0];
  }
  const snaps = reps.map(r => snapDistance(r.distance_m));
  const snapped = snaps.filter(Boolean);
  if (snapped.length && snapped.length >= 0.8 * reps.length) {
    return ['dist', [...new Set(snapped)].sort((a, b) => a - b).join('+')];
  }
  return ['time', reps.reduce((a, r) => a + (r.timer_s ?? 0), 0)];
}

export function clusterSessions(entries, snapDistance) {
  const tagged = entries.map(e => {
    const [kind, value] = sessionSignature(e.session, e.segments, snapDistance);
    return { kind, value, ...e };
  });
  const clusters = [];
  const distGroups = new Map();
  for (const t of tagged.filter(t => t.kind === 'dist')) {
    if (!distGroups.has(t.value)) distGroups.set(t.value, []);
    distGroups.get(t.value).push(t);
  }
  for (const [key, members] of distGroups) {
    const label = key.split('+').map(d => `${d}m`).join(' + ') + ' reps';
    clusters.push({ kind: 'dist', label, members });
  }
  for (const [kind, tol, fmt] of [
    ['time', CLUSTER_TIME_TOLERANCE, v => `~${Math.round(v / 60)}' work`],
    ['run', CLUSTER_RUN_TOLERANCE, v => `~${Math.round(v / 1000)} km run`]]) {
    const pool = tagged.filter(t => t.kind === kind).sort((a, b) => a.value - b.value);
    let current = [];
    for (const t of pool) {
      if (current.length && t.value > (1 + tol) * current[0].value) {
        clusters.push({ kind, label: fmt(median(current.map(c => c.value))), members: current });
        current = [];
      }
      current.push(t);
    }
    if (current.length) {
      clusters.push({ kind, label: fmt(median(current.map(c => c.value))), members: current });
    }
  }
  for (const c of clusters) {
    c.members.sort((a, b) =>
      (a.session.start_time_utc ?? '') < (b.session.start_time_utc ?? '') ? -1 : 1);
  }
  clusters.sort((a, b) => b.members.length - a.members.length);
  return clusters;
}

// ------------------------------------------------------------ progression

function paceCv(records) {
  const paces = records.map(r => paceSPerKm(r.enhanced_speed)).filter(Boolean);
  if (paces.length < 30) return null;
  const m = mean(paces);
  return m ? pstdev(paces) / m : null;
}

const hasPauseInside = (start, end, pauses) =>
  pauses.some(([pStop, pStart]) => start < pStop && pStart < end);

export function findProgressionCandidates(segments, records, pauses) {
  const candidates = [];
  const consider = (start, end, timerS, label) => {
    if (!timerS || timerS < PROGRESSION_MIN_TIMER_S) return;
    if (!start || !end || hasPauseInside(start, end, pauses)) return;
    const recs = recordsIn(records, start, end);
    const cvv = paceCv(recs);
    if (cvv === null || cvv > PROGRESSION_MAX_PACE_CV) return;
    const speeds = recs.map(r => r.enhanced_speed).filter(Boolean);
    const hrs = recs.map(r => r.heart_rate).filter(Boolean);
    if (!speeds.length || !hrs.length) return;
    candidates.push({ start_time: start, end_time: end, timer_s: timerS,
      pace_s_km: Math.round(paceSPerKm(mean(speeds)) * 10) / 10,
      avg_hr: Math.round(mean(hrs) * 10) / 10,
      pace_cv: Math.round(cvv * 10000) / 10000, label });
  };
  const reps = segments.filter(s => s.kind === 'rep' && !s.outlier);
  for (const seg of reps) {
    consider(seg.start_time, seg.end_time, seg.timer_s, `rep ${seg.rep_index}`);
  }
  if (!reps.length && records.length) {
    const bounds = [records[0].timestamp];
    for (const [pStop, pStart] of [...pauses].sort((a, b) => a[0] - b[0])) {
      bounds.push(pStop, pStart);
    }
    bounds.push(records[records.length - 1].timestamp);
    for (let i = 0; i < bounds.length - 1; i += 2) {
      const timerS = (bounds[i + 1] - bounds[i]) / 1000;
      consider(bounds[i], bounds[i + 1], timerS, 'continuous block');
    }
  }
  return candidates;
}

// ------------------------------------------------------------ race prediction

export function bestEfforts(records, targets = BEST_EFFORT_DISTANCES_M) {
  const pts = records.filter(r => r.distance !== null && r.distance !== undefined
    && r.timestamp).map(r => [r.timestamp.getTime(), r.distance]);
  const out = {};
  for (const target of targets) {
    let best = null, j = 0;
    for (let i = 0; i < pts.length; i++) {
      while (j < i && pts[i][1] - pts[j + 1][1] >= target) j++;
      if (pts[i][1] - pts[j][1] >= target) {
        const t = (pts[i][0] - pts[j][0]) / 1000;
        if (t > 0) {
          const scaled = t * target / (pts[i][1] - pts[j][1]);
          best = best ? Math.min(best, scaled) : scaled;
        }
      }
    }
    if (best) out[target] = Math.round(best * 10) / 10;
  }
  return out;
}

export const predictRiegel = (t1, d1, d2) => t1 * (d2 / d1) ** 1.06;

export function predictCameron(t1, d1, d2) {
  const f = (mi) => 13.49681 - 0.048865 * mi + 2.438936 / (mi ** 0.7905);
  const m1 = d1 / 1609.344, m2 = d2 / 1609.344;
  return (t1 / m1) * (f(m1) / f(m2)) * m2;
}

const vo2 = (v) => -4.60 + 0.182258 * v + 0.000104 * v * v;
const pctVo2max = (t) => 0.8 + 0.1894393 * Math.exp(-0.012778 * t)
  + 0.2989558 * Math.exp(-0.1932605 * t);

export function vdotFrom(t1, d1) {
  const tMin = t1 / 60;
  return vo2(d1 / tMin) / pctVo2max(tMin);
}

export function predictVdot(t1, d1, d2) {
  const vdot = vdotFrom(t1, d1);
  let lo = 2, hi = 600, mid = 0;
  for (let i = 0; i < 60; i++) {
    mid = (lo + hi) / 2;
    if (vo2(d2 / mid) / pctVo2max(mid) > vdot) lo = mid; else hi = mid;
  }
  return mid * 60;
}

export function racePredictions(t1, d1) {
  return RACE_DISTANCES_M.map(([label, d2]) => {
    const r = predictRiegel(t1, d1, d2);
    const v = predictVdot(t1, d1, d2);
    const c = predictCameron(t1, d1, d2);
    return { label, distance_m: d2, riegel_s: Math.round(r),
      vdot_s: Math.round(v), cameron_s: Math.round(c),
      mean_s: Math.round((r + v + c) / 3) };
  });
}

export function backfillSegmentHr(segments, records) {
  for (const seg of segments) {
    if (seg.avg_hr !== null && seg.avg_hr !== undefined) continue;
    if (!seg.start_time || !seg.end_time) continue;
    const hrs = recordsIn(records, seg.start_time, seg.end_time)
      .map(r => r.heart_rate).filter(Boolean);
    if (hrs.length) {
      seg.avg_hr = Math.round(mean(hrs) * 10) / 10;
      if (seg.max_hr === null || seg.max_hr === undefined) seg.max_hr = Math.max(...hrs);
    }
  }
}
