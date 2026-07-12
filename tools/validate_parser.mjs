// Cross-validates the JS FIT parser (web/js/fit.js + ingest.js) against
// the Python fitdecode ground-truth dump, field by field, on real files.
// Usage: node tools/validate_parser.mjs <fit-dir> <py_truth.json>

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { parseActivity } from '../docs/js/ingest.js';

const [dir, truthPath] = process.argv.slice(2);
const truth = JSON.parse(readFileSync(truthPath, 'utf8'));

let files = 0, failures = 0;
const close = (a, b, tol = 0.11) => Math.abs((a ?? 0) - (b ?? 0)) <= tol;

for (const name of readdirSync(dir).filter(f => f.endsWith('.fit')).sort()) {
  const t = truth[name];
  if (!t) continue;
  files++;
  const buf = readFileSync(join(dir, name));
  const a = parseActivity(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const errs = [];

  if (a.laps.length !== t.n_laps) errs.push(`laps ${a.laps.length} != ${t.n_laps}`);
  if (a.records.length !== t.n_records) errs.push(`records ${a.records.length} != ${t.n_records}`);
  a.laps.forEach((l, i) => {
    if (!close(l.total_distance, t.lap_dists[i])) errs.push(`lap${i} dist ${l.total_distance} != ${t.lap_dists[i]}`);
    if (!close(l.total_timer_time, t.lap_timers[i])) errs.push(`lap${i} timer ${l.total_timer_time} != ${t.lap_timers[i]}`);
    if (String(l.lap_trigger) !== t.lap_triggers[i]) errs.push(`lap${i} trigger ${l.lap_trigger} != ${t.lap_triggers[i]}`);
    if ((l.wkt_step_index ?? null) !== (t.lap_wkt[i] ?? null)) errs.push(`lap${i} wkt ${l.wkt_step_index} != ${t.lap_wkt[i]}`);
    if (l.start_time.toISOString().slice(0, 19) !== t.lap_starts[i].slice(0, 19)) errs.push(`lap${i} start ${l.start_time.toISOString()} != ${t.lap_starts[i]}`);
    if (l.timestamp.toISOString().slice(0, 19) !== t.lap_ends[i].slice(0, 19)) errs.push(`lap${i} end ${l.timestamp.toISOString()} != ${t.lap_ends[i]}`);
  });
  const st = {};
  for (const s of a.splits) { const k = String(s.split_type); st[k] = (st[k] || 0) + 1; }
  for (const [k, n] of Object.entries(t.split_types)) {
    if ((st[k] || 0) !== n) errs.push(`split ${k}: ${st[k] || 0} != ${n}`);
  }
  const wname = a.workout ? a.workout.wkt_name : null;
  const tname = t.workout ? t.workout.wkt_name : null;
  if (wname !== tname) errs.push(`workout ${wname} != ${tname}`);
  if (a.workout_steps.length !== t.steps.length) {
    errs.push(`steps ${a.workout_steps.length} != ${t.steps.length}`);
  } else {
    a.workout_steps.forEach((s, i) => {
      const ts = t.steps[i];
      for (const f of ['duration_type', 'intensity', 'target_type',
                       'duration_step', 'repeat_steps']) {
        if (String(s[f] ?? null) !== String(ts[f] ?? null)) errs.push(`step${i}.${f} ${s[f]} != ${ts[f]}`);
      }
      for (const f of ['duration_distance', 'duration_time',
                       'custom_target_speed_low', 'custom_target_speed_high']) {
        if (!close(s[f], ts[f], 0.01)) errs.push(`step${i}.${f} ${s[f]} != ${ts[f]}`);
      }
    });
  }
  if (!close(a.session.total_distance, t.session.total_distance, 0.2)) errs.push(`sess dist ${a.session.total_distance} != ${t.session.total_distance}`);
  if (!close(a.session.total_timer_time, t.session.total_timer_time, 0.2)) errs.push(`sess timer ${a.session.total_timer_time} != ${t.session.total_timer_time}`);
  if (String(a.session.sport) !== String(t.session.sport)) errs.push(`sport ${a.session.sport} != ${t.session.sport}`);
  const hr = a.records.slice(1000, 1005).map(r => r.heart_rate);
  if (JSON.stringify(hr) !== JSON.stringify(t.hr_sample)) errs.push(`hr sample ${hr} != ${t.hr_sample}`);
  const ds = a.records.slice(1000, 1005).map(r => Math.round((r.distance || 0) * 100) / 100);
  if (!ds.every((d, i) => close(d, t.dist_sample[i], 0.02))) errs.push(`dist sample ${ds} != ${t.dist_sample}`);

  if (errs.length) {
    failures++;
    console.log(`✗ ${name}`);
    errs.slice(0, 8).forEach(e => console.log(`   ${e}`));
  } else {
    console.log(`✓ ${name}`);
  }
}
console.log(`\n${files - failures}/${files} files parse identically`);
process.exit(failures ? 1 : 0);
