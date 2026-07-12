// Node test suite for the JS engine — ports the key Python acceptance
// and efficiency tests. Run: node --test web/tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyActivity, timerPauses } from '../js/ingest.js';
import { resolve, snapDistance, fmtSeconds, cleanLaps } from '../js/intervals.js';
import { cleanHeartRate, extractEfforts, domainForHr, DEFAULT_SETTINGS } from '../js/efforts.js';
import { sessionEfficiency, gatedRegression, compareSessions,
         predictRiegel, predictVdot, predictCameron, vdotFrom,
         bestEfforts, findProgressionCandidates } from '../js/metrics.js';

// ---------------------------------------------------------- fixtures

function builder(startIso, { structured = false, wktName = null } = {}) {
  const a = emptyActivity();
  let t = new Date(startIso);
  const start = new Date(t);
  let dist = 0;
  a.file_id = { serial_number: Math.floor(start.getTime() / 1000),
                time_created: start, manufacturer: 'garmin' };
  if (structured) a.workout = { wkt_name: wktName };
  a.events.push({ timestamp: start, event: 'timer', event_type: 'start', data: null });

  const api = {
    activity: a,
    step(fields) {
      a.workout_steps.push({ message_index: a.workout_steps.length,
        duration_type: null, duration_distance: null, duration_time: null,
        duration_step: null, repeat_steps: null, intensity: null,
        target_type: null, custom_target_speed_low: null,
        custom_target_speed_high: null, wkt_step_name: null, ...fields });
      return api;
    },
    lap(durS, distM, { hr = null, trigger = 'manual', wkt = null,
                       records = true } = {}) {
      const s = new Date(t);
      const e = new Date(t.getTime() + durS * 1000);
      const speed = durS ? distM / durS : 0;
      a.laps.push({ start_time: s, timestamp: e, total_distance: distM,
        total_timer_time: durS, total_elapsed_time: durS,
        avg_speed: speed, enhanced_avg_speed: speed,
        avg_heart_rate: hr, max_heart_rate: hr ? hr + 6 : null,
        lap_trigger: trigger, wkt_step_index: wkt, intensity: 'interval',
        message_index: a.laps.length });
      if (records && durS) {
        for (let i = 0; i < Math.floor(durS); i++) {
          dist += speed;
          a.records.push({ timestamp: new Date(s.getTime() + i * 1000),
            distance: dist, enhanced_speed: speed,
            heart_rate: hr ? Math.round(hr) : null, cadence: 170, power: null });
        }
      }
      t = e;
      return api;
    },
    split(type, durS, distM, startAt = null) {
      const last = a.splits[a.splits.length - 1];
      const s = startAt ?? (last ? last.end_time : start);
      a.splits.push({ split_type: type, start_time: s,
        end_time: new Date(s.getTime() + durS * 1000),
        total_timer_time: durS, total_elapsed_time: durS,
        total_distance: distM, avg_speed: durS ? distM / durS : null });
      return api;
    },
    done() {
      a.events.push({ timestamp: t, event: 'timer', event_type: 'stop_all', data: null });
      const timer = a.laps.reduce((x, l) => x + l.total_timer_time, 0);
      const d = a.laps.reduce((x, l) => x + l.total_distance, 0);
      const hrs = a.laps.map(l => l.avg_heart_rate).filter(Boolean);
      a.session = { start_time: start, timestamp: t, sport: 'running',
        sub_sport: 'generic', total_distance: d, total_timer_time: timer,
        total_elapsed_time: timer, avg_speed: timer ? d / timer : null,
        avg_heart_rate: hrs.length ? hrs.reduce((x, y) => x + y, 0) / hrs.length : null,
        num_laps: a.laps.length };
      return a;
    },
  };
  return api;
}

// ---------------------------------------------------------- engine

test('structured 8x400+6x300+4x200 exact mapping', () => {
  const b = builder('2026-07-02T17:30:00Z', { structured: true, wktName: '8x400' });
  b.step({ duration_type: 'time', duration_time: 900, intensity: 'warmup' });
  b.step({ duration_type: 'distance', duration_distance: 400, intensity: 'active' });
  b.step({ duration_type: 'distance', duration_distance: 200, intensity: 'recovery' });
  b.step({ duration_type: 'repeat_until_steps_cmplt', duration_step: 1, repeat_steps: 8 });
  b.step({ duration_type: 'distance', duration_distance: 300, intensity: 'active' });
  b.step({ duration_type: 'distance', duration_distance: 200, intensity: 'recovery' });
  b.step({ duration_type: 'repeat_until_steps_cmplt', duration_step: 4, repeat_steps: 6 });
  b.step({ duration_type: 'distance', duration_distance: 200, intensity: 'active' });
  b.step({ duration_type: 'distance', duration_distance: 200, intensity: 'recovery' });
  b.step({ duration_type: 'repeat_until_steps_cmplt', duration_step: 7, repeat_steps: 4 });
  b.step({ duration_type: 'open', intensity: 'cooldown' });
  b.lap(900, 2500, { hr: 138, wkt: 0 });
  for (let i = 0; i < 8; i++) { b.lap(78, 401, { hr: 172, wkt: 1 }); b.lap(80, 201, { hr: 150, wkt: 2 }); }
  for (let i = 0; i < 6; i++) { b.lap(57, 300, { hr: 174, wkt: 4 }); b.lap(80, 202, { hr: 152, wkt: 5 }); }
  for (let i = 0; i < 4; i++) { b.lap(36, 199, { hr: 175, wkt: 7 }); b.lap(80, 200, { hr: 153, wkt: 8 }); }
  b.lap(600, 1700, { hr: 140, wkt: 10 });
  const r = resolve(b.done());
  assert.equal(r.detection_source, 'structured_workout');
  assert.equal(r.structure, '8x400m + 6x300m + 4x200m R200m');
  assert.equal(r.segments.filter(s => s.kind === 'rep' && !s.outlier).length, 18);
  assert.equal(r.session_type, 'intervals');
});

test('free 15x1: pace alternation, identical durations', () => {
  const b = builder('2026-06-25T17:40:00Z');
  b.lap(340, 1000, { hr: 139, trigger: 'distance' });
  b.lap(340, 1000, { hr: 141, trigger: 'distance' });
  const recDists = [173, 176, 163, 159, 171, 160, 170, 124, 148, 127, 133, 135, 122, 114, 150];
  for (let i = 0; i < 15; i++) {
    b.lap(60, 330, { hr: 172 });
    b.lap(60, recDists[i], { hr: 150 });
  }
  b.lap(340, 1000, { hr: 143, trigger: 'distance' });
  b.split('interval_warmup', 680, 2000, new Date('2026-06-25T17:40:00Z'));
  for (let i = 0; i < 15; i++) { b.split('interval_active', 60, 330); b.split('interval_recovery', 60, 132); }
  const r = resolve(b.done());
  assert.equal(r.structure, "15x1' R1'");
  assert.equal(r.segments.filter(s => s.kind === 'rep' && !s.outlier).length, 15);
});

test('junk laps dropped unless they carry a step index', () => {
  const laps = [
    { total_timer_time: 2.6, total_distance: 8.6, wkt_step_index: null },
    { total_timer_time: 2.4, total_distance: 9.6, wkt_step_index: 1 },
    { total_timer_time: 60, total_distance: 330, wkt_step_index: null },
  ];
  const cleaned = cleanLaps(laps);
  assert.equal(cleaned.length, 2);
  assert.ok(cleaned.some(l => l.wkt_step_index === 1));
});

test('snap and time label rules', () => {
  assert.equal(snapDistance(398), 400);
  assert.equal(snapDistance(992), 1000);
  assert.equal(snapDistance(288.8), 300);   // 4% GPS under-read
  assert.equal(snapDistance(330), null);
  assert.equal(fmtSeconds(61.4), "1'");     // display snapping
  assert.equal(fmtSeconds(90), '1\'30"');
  assert.equal(fmtSeconds(480), "8'");
});

// ---------------------------------------------------------- efforts

function recs1hz(spec, startIso = '2026-07-01T17:00:00Z') {
  const out = [];
  let t = new Date(startIso).getTime(), dist = 0;
  for (const [dur, speed, hr, cad] of spec) {
    for (let i = 0; i < dur; i++) {
      dist += speed;
      out.push({ timestamp: new Date(t), distance: dist,
        enhanced_speed: speed, heart_rate: hr, cadence: cad, power: null });
      t += 1000;
    }
  }
  return out;
}

test('hygiene: spike interpolated, cadence lock flagged', () => {
  const r = recs1hz([[60, 3, 150, 168]]);
  r[30].heart_rate = 190;
  const cleaned = cleanHeartRate(r);
  assert.ok(cleaned[30].hr_ok);
  assert.ok(Math.abs(cleaned[30].heart_rate - 150) <= 2);

  const r2 = recs1hz([[60, 3, 140, 168], [60, 3, 172, 172], [60, 3, 140, 168]]);
  const c2 = cleanHeartRate(r2);
  assert.ok(c2[70].hr_suspect);
  assert.ok(!c2[70].hr_ok);
});

test('stabilized HR: first 45s dropped; <90s reps get no HR stats', () => {
  const seg = (startS, dur, dist) => ({ kind: 'rep', rep_index: 1, outlier: false,
    start_time: new Date(Date.parse('2026-07-01T17:00:00Z') + startS * 1000),
    end_time: new Date(Date.parse('2026-07-01T17:00:00Z') + (startS + dur) * 1000),
    timer_s: dur, distance_m: dist, avg_speed_ms: dist / dur });
  const r = cleanHeartRate(recs1hz([[45, 4.4, 150, 168], [135, 4.4, 178, 168]]));
  const eff = extractEfforts([seg(0, 180, 792)], r, []);
  assert.ok(eff[0].hr_valid);
  assert.ok(Math.abs(eff[0].hr_median - 178) <= 1);
  assert.equal(eff[0].domain, 'threshold');

  const r2 = cleanHeartRate(recs1hz([[60, 5.0, 175, 180]]));
  const e2 = extractEfforts([seg(0, 60, 300)], r2, []);
  assert.equal(e2[0].hr_median, null);
  assert.equal(e2[0].rei, null);
  assert.equal(e2[0].domain, 'hard');       // pace fallback
});

test('domain boundaries from LTHR', () => {
  const s = DEFAULT_SETTINGS;
  assert.equal(domainForHr(140, s), 'easy');
  assert.equal(domainForHr(155, s), 'steady');
  assert.equal(domainForHr(172, s), 'threshold');
  assert.equal(domainForHr(183, s), 'hard');
});

// ---------------------------------------------------------- efficiency

const effRep = (i, pace, hr, domain = 'threshold') => ({
  kind: 'rep', rep_index: i, pace_s_km: pace, speed_ms: 1000 / pace,
  hr_median: hr, hr_valid: true, domain, rei: (1000 / pace) / hr });

test('early REI is drift-aware', () => {
  const reps = Array.from({ length: 6 }, (_, i) => effRep(i + 1, 240, 170 + 2 * i));
  const s = sessionEfficiency(reps);
  assert.ok(s.rei_early > s.rei_full);
});

test('regression gated on single-pace; valid with spread', () => {
  const single = Array.from({ length: 6 }, (_, i) => effRep(i + 1, 240 + (i % 2), 172));
  const g = gatedRegression(single, 175, 235);
  assert.ok(!g.valid && g.reason.includes('single-pace'));

  const spread = [250, 240, 230, 220, 210].map((p, i) =>
    effRep(i + 1, p, 180 - 0.4 * (p - 210)));
  const v = gatedRegression(spread, 175, 235);
  assert.ok(v.valid && v.r2 >= 0.95);
  assert.ok(Math.abs(v.pace_cost - 4) <= 0.3);
});

test('compare: proposal A/B/C examples + cross-domain refusal', () => {
  const A = sessionEfficiency([1, 2, 3, 4, 5].map(i => effRep(i, 220, 180)));
  const B = sessionEfficiency([1, 2, 3, 4, 5].map(i => effRep(i, 215, 184)));
  const C = sessionEfficiency([1, 2, 3, 4, 5].map(i => effRep(i, 215, 176)));
  const meta = { date: '2026-07-01', hot: 0 };
  assert.ok(['similar', 'declining'].includes(compareSessions(B, A, meta, meta).verdict));
  assert.equal(compareSessions(C, A, meta, meta).verdict, 'improving');

  const X = sessionEfficiency([effRep(1, 220, 180, 'hard')]);
  const Y = sessionEfficiency([effRep(1, 260, 160, 'steady')]);
  assert.equal(compareSessions(X, Y, meta, meta).verdict, 'not_comparable');
});

// ---------------------------------------------------------- prediction

test('race models sane and consistent with Daniels', () => {
  for (const model of [predictRiegel, predictVdot, predictCameron]) {
    assert.ok(Math.abs(model(2481, 10000, 10000) - 2481) / 2481 < 0.01);
    const t5 = model(2481, 10000, 5000);
    assert.ok(t5 > 2481 / 2 * 0.9 && t5 < 2481 / 2);
  }
  assert.ok(Math.abs(vdotFrom(2481, 10000) - 50) < 0.8);
});

test('best-effort two-pointer', () => {
  const r = recs1hz([[1200, 10 / 3, 150, 168], [600, 25 / 6, 165, 172]]);
  const eff = bestEfforts(r, [1000]);
  assert.ok(Math.abs(eff[1000] - 240) <= 2);
});

test('progression: continuous block qualifies, short reps never', () => {
  const r = recs1hz([[900, 3.5, 152, 168]]);
  const c = findProgressionCandidates([], r, []);
  assert.equal(c.length, 1);
  assert.ok(c[0].timer_s >= 600);
});
