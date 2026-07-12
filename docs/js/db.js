// IndexedDB storage — the only module that touches persistence.
// One database, per-session blobs (single user, moderate volume).
// Dates are stored as epoch ms and revived on read.

const DB_NAME = 'runlens';
const DB_VERSION = 1;

let dbPromise = null;

export function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolveP, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore('sessions', { keyPath: 'dedup_key' });
      db.createObjectStore('records', { keyPath: 'dedup_key' });
      db.createObjectStore('segments', { keyPath: 'dedup_key' });
      db.createObjectStore('efforts', { keyPath: 'dedup_key' });
      db.createObjectStore('overrides', { keyPath: 'dedup_key' });
      db.createObjectStore('settings', { keyPath: 'key' });
      db.createObjectStore('races', { keyPath: 'id', autoIncrement: true });
      db.createObjectStore('progression', { keyPath: 'id', autoIncrement: true });
    };
    req.onsuccess = () => resolveP(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}

function tx(db, store, mode = 'readonly') {
  return db.transaction(store, mode).objectStore(store);
}
const wrap = (req) => new Promise((res, rej) => {
  req.onsuccess = () => res(req.result);
  req.onerror = () => rej(req.error);
});

export async function put(store, value) {
  const db = await openDb();
  return wrap(tx(db, store, 'readwrite').put(value));
}
export async function get(store, key) {
  const db = await openDb();
  return wrap(tx(db, store).get(key));
}
export async function getAll(store) {
  const db = await openDb();
  return wrap(tx(db, store).getAll());
}
export async function del(store, key) {
  const db = await openDb();
  return wrap(tx(db, store, 'readwrite').delete(key));
}

// --------------------------------------------------------- serialization

const D = (v) => v instanceof Date ? v.getTime() : v;
const R = (v) => typeof v === 'number' && v > 1e12 ? new Date(v) : v;

export function packSegments(segments) {
  return segments.map(s => ({ ...s, start_time: D(s.start_time), end_time: D(s.end_time) }));
}
export function unpackSegments(segments) {
  return (segments || []).map(s => ({ ...s, start_time: R(s.start_time), end_time: R(s.end_time) }));
}
export function packRecords(records) {
  return records.map(r => ({ t: D(r.timestamp), d: r.distance, s: r.enhanced_speed,
    h: r.heart_rate, c: r.cadence, p: r.power }));
}
export function unpackRecords(records) {
  return (records || []).map(r => ({ timestamp: new Date(r.t), distance: r.d,
    enhanced_speed: r.s, heart_rate: r.h, cadence: r.c, power: r.p }));
}
export const packEfforts = packSegments;
export const unpackEfforts = unpackSegments;

// --------------------------------------------------------- settings/seed

import { DEFAULT_SETTINGS } from './efforts.js';

export async function getSettings() {
  const rows = await getAll('settings');
  const stored = {};
  for (const r of rows) stored[r.key] = r.value;
  return { ...DEFAULT_SETTINGS, ...stored };
}
export async function setSetting(key, value) {
  return put('settings', { key, value });
}

const SEED_ANCHORS = [
  { date: '2025-06-15', pace_s_km: 242, avg_hr: 173, status: 'confirmed',
    source: 'anchor', label: 'Jun 2025 anchor (4:02/km @ 173)' },
  { date: '2026-06-15', pace_s_km: 232, avg_hr: 175, status: 'confirmed',
    source: 'anchor', label: 'Jun 2026 anchor (3:52/km @ 175)' },
];
const SEED_RACES = [
  { date: '2025-05-11', distance_m: 42195, time_s: 10440, label: 'Leiden Marathon 2:54' },
];

export async function seedOnce() {
  const prog = await getAll('progression');
  if (!prog.some(p => p.source === 'anchor')) {
    for (const a of SEED_ANCHORS) await put('progression', a);
  }
  const races = await getAll('races');
  if (!races.length) {
    for (const r of SEED_RACES) await put('races', r);
  }
}

// --------------------------------------------------------- overrides

export async function getOverrides(dedupKey) {
  return (await get('overrides', dedupKey)) ||
    { dedup_key: dedupKey, session_type: null, structure: null, segkinds: {} };
}
export async function setOverride(dedupKey, patch) {
  const ov = await getOverrides(dedupKey);
  await put('overrides', { ...ov, ...patch,
    segkinds: { ...ov.segkinds, ...(patch.segkinds || {}) } });
}

/** Segments with kind overrides applied + rep indices renumbered. */
export async function getSegments(dedupKey) {
  const row = await get('segments', dedupKey);
  const segs = unpackSegments(row ? row.segments : []);
  const ov = await getOverrides(dedupKey);
  for (const s of segs) {
    const key = s.start_time instanceof Date ? s.start_time.toISOString() : String(s.start_time);
    if (ov.segkinds && ov.segkinds[key]) {
      s.kind = ov.segkinds[key];
      s.overridden_kind = true;
    }
  }
  let n = 0;
  for (const s of segs) {
    s.rep_index = (s.kind === 'rep' && !s.outlier) ? ++n : null;
  }
  return segs;
}

export async function listSessions() {
  const sessions = await getAll('sessions');
  for (const s of sessions) {
    const ov = await getOverrides(s.dedup_key);
    if (ov.session_type) { s.session_type = ov.session_type; s.confidence = 'high'; s.overridden = true; }
    if (ov.structure) { s.structure = ov.structure; s.overridden = true; }
  }
  sessions.sort((a, b) => (a.start_time_utc < b.start_time_utc ? 1 : -1));
  return sessions;
}
