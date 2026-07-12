// Ingestion: raw FIT messages -> normalized activity object.
// Mirrors runlens/ingest.py exactly (Running.MD §3). Pure, no storage.

import { parseFit, decodeEnums } from './fit.js';

export function emptyActivity() {
  return { file_id: {}, session: {}, laps: [], events: [], records: [],
           splits: [], workout: null, workout_steps: [], time_in_zone: [] };
}

/** Parse a FIT ArrayBuffer into the normalized activity dict. */
export function parseActivity(buffer) {
  const msgs = decodeEnums(parseFit(buffer));
  const a = emptyActivity();

  a.file_id = (msgs.file_id && msgs.file_id[0]) || {};
  a.session = (msgs.session && msgs.session[0]) || {};
  a.events = (msgs.event || []).map(e => ({
    timestamp: e.timestamp ?? null, event: e.event ?? null,
    event_type: e.event_type ?? null, data: e.data ?? null }));
  a.records = (msgs.record || []).filter(r => r.timestamp).map(r => ({
    timestamp: r.timestamp,
    distance: r.distance ?? null,
    enhanced_speed: r.enhanced_speed ?? r.speed ?? null,
    heart_rate: r.heart_rate ?? null,
    cadence: r.cadence ?? null,
    power: r.power ?? null,
    temperature: r.temperature ?? null }));
  a.laps = (msgs.lap || []).map(l => ({
    start_time: l.start_time ?? null, timestamp: l.timestamp ?? null,
    total_distance: l.total_distance ?? null,
    total_timer_time: l.total_timer_time ?? null,
    total_elapsed_time: l.total_elapsed_time ?? null,
    avg_speed: l.avg_speed ?? null,
    enhanced_avg_speed: l.enhanced_avg_speed ?? l.avg_speed ?? null,
    avg_heart_rate: l.avg_heart_rate ?? null,
    max_heart_rate: l.max_heart_rate ?? null,
    lap_trigger: l.lap_trigger ?? null,
    wkt_step_index: l.wkt_step_index ?? null,
    intensity: l.intensity ?? null,
    message_index: l.message_index ?? null }));
  a.splits = (msgs.split || []).map(s => ({
    split_type: s.split_type ?? null,
    start_time: s.start_time ?? null, end_time: s.end_time ?? null,
    total_timer_time: s.total_timer_time ?? null,
    total_elapsed_time: s.total_elapsed_time ?? null,
    total_distance: s.total_distance ?? null,
    avg_speed: s.avg_speed ?? null }));
  if (msgs.workout && msgs.workout.length) {
    a.workout = { wkt_name: msgs.workout[0].wkt_name ?? null };
  }
  a.workout_steps = (msgs.workout_step || []).map(expandWorkoutStep);

  finalize(a);
  return a;
}

// FIT stores workout-step duration/target as raw values whose meaning
// depends on duration_type/target_type (dynamic subfields). Expand to
// the named fields the engine reads.
function expandWorkoutStep(s) {
  const out = {
    message_index: s.message_index ?? null,
    duration_type: s.duration_type ?? null,
    duration_distance: null, duration_time: null,
    duration_step: null, repeat_steps: null,
    intensity: s.intensity ?? null,
    target_type: s.target_type ?? null,
    custom_target_speed_low: null, custom_target_speed_high: null,
    wkt_step_name: s.wkt_step_name ?? null, notes: null };
  const v = s.duration_value;
  if (v !== null && v !== undefined) {
    if (out.duration_type === 'time') out.duration_time = v / 1000;
    else if (out.duration_type === 'distance') out.duration_distance = v / 100;
    else if (String(out.duration_type).startsWith('repeat')) {
      out.duration_step = v;
      out.repeat_steps = s.target_value ?? null;
    }
  }
  if (out.target_type === 'speed') {
    if (s.custom_target_value_low) out.custom_target_speed_low = s.custom_target_value_low / 1000;
    if (s.custom_target_value_high) out.custom_target_speed_high = s.custom_target_value_high / 1000;
  }
  return out;
}

function finalize(a) {
  a.records.sort((x, y) => x.timestamp - y.timestamp);
  a.laps.sort((x, y) => (x.start_time?.getTime() ?? -Infinity) -
                        (y.start_time?.getTime() ?? -Infinity));
  a.workout_steps.sort((x, y) => (x.message_index ?? 0) - (y.message_index ?? 0));
  if (a.splits.length && a.splits.every(s => s.start_time)) {
    a.splits.sort((x, y) => x.start_time - y.start_time);
  }
  // Garmin-Connect exports carry a bogus lap `timestamp` (= session start
  // on every lap; Running.MD §3.2). Derive lap end from start + elapsed.
  for (const lap of a.laps) {
    const dur = lap.total_elapsed_time ?? lap.total_timer_time;
    if (lap.start_time && dur &&
        (!lap.timestamp || lap.timestamp <= lap.start_time)) {
      lap.timestamp = new Date(lap.start_time.getTime() + dur * 1000);
    }
  }
  // Splits without end_time: derive the same way.
  for (const s of a.splits) {
    if (s.start_time && !s.end_time && (s.total_elapsed_time || s.total_timer_time)) {
      s.end_time = new Date(s.start_time.getTime() +
        (s.total_elapsed_time ?? s.total_timer_time) * 1000);
    }
  }
}

export function dedupKey(a) {
  const f = a.file_id || {};
  const t = f.time_created instanceof Date ? f.time_created.toISOString()
    : String(f.time_created);
  return `${f.serial_number}:${t}`;
}

/** (stopTime, restartTime) pairs from timer events. */
export function timerPauses(a) {
  const pauses = [];
  let stopAt = null;
  for (const ev of a.events) {
    if (ev.event !== 'timer' || !ev.timestamp) continue;
    if ((ev.event_type === 'stop_all' || ev.event_type === 'stop') && !stopAt) {
      stopAt = ev.timestamp;
    } else if (ev.event_type === 'start' && stopAt) {
      pauses.push([stopAt, ev.timestamp]);
      stopAt = null;
    }
  }
  return pauses;
}
