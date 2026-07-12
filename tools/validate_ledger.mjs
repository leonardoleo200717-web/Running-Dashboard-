// The acceptance contract: the JS engine must reproduce the user-
// confirmed ground truth (CLAUDE.md §7 ledger) on the 13 real files.
// Usage: node tools/validate_ledger.mjs <fit-dir>

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { parseActivity } from '../docs/js/ingest.js';
import { resolve } from '../docs/js/intervals.js';

const LEDGER = {
  '31dbf241': { type: 'intervals', structure: '4x1500m R500m', reps: 4 },
  '2178eafa': { type: 'intervals', structure: '6x1500m R500m', reps: 6 },
  '4dbfd499': { type: 'intervals', structure: '6x1000m R200m', reps: 6 },
  'f161a7ff': { type: 'easy', structure: '5x100m R100m', reps: 5 },
  '42934276': { type: 'intervals', structure: "5x1500m R3'", reps: 5 },
  'a157427a': { type: 'easy', structure: null, reps: 0 },
  '225052bf': { type: 'intervals', structure: "15' + 10' + 8' R3'", reps: 3 },
  '866a65d9': { type: 'intervals', structure: '5x(400m + 300m + 200m) R100m SR400m', reps: 14 },
  '90170db0': { type: 'intervals', structure: '5x(1000m + 300m) R100m SR400m', reps: 10 },
  '3fa3e427': { type: 'intervals', structure: "3x8' + 3x3' + 3x1' R1'", reps: 9 },
  'ca4ddb56': { type: 'intervals', structure: "2x12' + 5x1' R1'", reps: 7 },
  'a006c103': { type: 'intervals', structure: "15x1' R1'", reps: 15 },
  'dc1db5cd': { type: 'intervals', structure: '8x1000m R200m', reps: 8 },
};

const dir = process.argv[2];
let ok = 0, total = 0;
for (const name of readdirSync(dir).filter(f => f.endsWith('.fit')).sort()) {
  const key = name.slice(0, 8);
  const truth = LEDGER[key];
  if (!truth) continue;
  total++;
  const buf = readFileSync(join(dir, name));
  const a = parseActivity(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
  const r = resolve(a);
  const reps = r.segments.filter(s => s.kind === 'rep' && !s.outlier).length;
  const good = r.session_type === truth.type &&
    (r.structure ?? null) === truth.structure && reps === truth.reps;
  if (good) { ok++; console.log(`✓ ${key} ${r.structure ?? '(no structure)'}`); }
  else console.log(`✗ ${key} got type=${r.session_type} structure=${JSON.stringify(r.structure)} reps=${reps}` +
                   ` want type=${truth.type} structure=${JSON.stringify(truth.structure)} reps=${truth.reps}` +
                   ` [${r.detection_source}]`);
}
console.log(`\n${ok}/${total} ledger entries reproduced`);
process.exit(ok === total ? 0 : 1);
