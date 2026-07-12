// Pipeline glue: FIT buffer -> engine -> storage. Mirrors runlens/app.py's
// process_activity/_recompute_session. Idempotent by dedup key; user
// overrides and confirmed progression points always survive re-import.

import { parseActivity, dedupKey, timerPauses } from './ingest.js';
import { resolve, structureString, snapDistance } from './intervals.js';
import { cleanHeartRate, smoothSpeed, extractEfforts } from './efforts.js';
import * as M from './metrics.js';
import * as db from './db.js';

export async function importFit(buffer) {
  const activity = parseActivity(buffer);
  const key = dedupKey(activity);
  const result = resolve(activity);
  const records = activity.records;
  const pauses = timerPauses(activity);

  M.backfillSegmentHr(result.segments, records);
  const reps = M.repTable(result.segments, records);
  const kpis = {
    rep_table: reps,
    drift: M.intraSetDrift(reps),
    recovery: M.recoveryQuality(result.segments, records),
    pace_at_hr: M.paceAtHr(records),
    hr_at_pace: M.hrAtPace(records),
    best_efforts: M.bestEfforts(records),
  };
  const settings = await db.getSettings();
  const cleaned = smoothSpeed(cleanHeartRate(records));
  const efforts = extractEfforts(result.segments, cleaned, pauses, settings);

  const existing = await db.get('sessions', key);
  const s = activity.session || {};
  await db.put('sessions', {
    dedup_key: key,
    start_time_utc: s.start_time ? s.start_time.toISOString()
      : new Date().toISOString(),
    sport: s.sport ?? null, sub_sport: s.sub_sport ?? null,
    total_distance_m: s.total_distance ?? null,
    total_timer_s: s.total_timer_time ?? null,
    total_elapsed_s: s.total_elapsed_time ?? null,
    avg_hr: s.avg_heart_rate ?? null, max_hr: s.max_heart_rate ?? null,
    temperature: s.avg_temperature ?? null,
    hot: existing ? (existing.hot ?? 0) : 0,
    origin: result.origin, detection_source: result.detection_source,
    confidence: result.confidence, session_type: result.session_type,
    structure: result.structure, wkt_name: result.wkt_name,
    needs_manual_tag: !!result.needs_manual_tag, kpis,
  });
  await db.put('segments', { dedup_key: key, segments: db.packSegments(result.segments) });
  await db.put('records', { dedup_key: key, records: db.packRecords(records) });
  await db.put('efforts', { dedup_key: key, efforts: db.packEfforts(efforts) });

  // Progression proposals (never re-propose confirmed/rejected labels).
  const existingPoints = (await db.getAll('progression'))
    .filter(p => p.dedup_key === key);
  for (const p of existingPoints.filter(p => p.status === 'proposed')) {
    await db.del('progression', p.id);
  }
  const keep = new Set(existingPoints.filter(p => p.status !== 'proposed')
    .map(p => p.label));
  for (const c of M.findProgressionCandidates(result.segments, records, pauses)) {
    if (keep.has(c.label)) continue;
    await db.put('progression', {
      dedup_key: key, date: c.start_time.toISOString().slice(0, 10),
      pace_s_km: c.pace_s_km, avg_hr: c.avg_hr,
      status: 'proposed', source: 'auto', label: c.label });
  }
  // Re-apply per-segment overrides after re-import.
  const ov = await db.getOverrides(key);
  if (ov.segkinds && Object.keys(ov.segkinds).length) await recompute(key);
  return { key, created: !existing };
}

/** Rebuild structure/KPIs/efforts after a segment-kind override. */
export async function recompute(key) {
  const segs = await db.getSegments(key);
  const recRow = await db.get('records', key);
  const records = db.unpackRecords(recRow ? recRow.records : []);
  const session = await db.get('sessions', key);
  if (!session) return;
  const reps = M.repTable(segs, records);
  session.kpis = { ...session.kpis, rep_table: reps,
    drift: M.intraSetDrift(reps),
    recovery: M.recoveryQuality(segs, records) };
  session.structure = structureString(segs);
  session.needs_manual_tag = false;
  await db.put('sessions', session);
  const settings = await db.getSettings();
  const cleaned = smoothSpeed(cleanHeartRate(records));
  await db.put('efforts', { dedup_key: key,
    efforts: db.packEfforts(extractEfforts(segs, cleaned, [], settings)) });
}

export async function clusterEntries() {
  const sessions = await db.listSessions();
  const entries = [];
  for (const s of sessions) {
    entries.push({ session: { ...s, id: s.dedup_key },
                   segments: await db.getSegments(s.dedup_key) });
  }
  return M.clusterSessions(entries, snapDistance);
}

export async function repEffortsOf(key) {
  const row = await db.get('efforts', key);
  return db.unpackEfforts(row ? row.efforts : []).filter(e => e.kind === 'rep');
}
