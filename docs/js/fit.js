// FIT binary parser — minimal, purpose-built for RunLens.
// Parses only the messages the pipeline needs (Running.MD §3); skips
// everything else without crashing. Cross-validated against the Python
// fitdecode output on the 13-file real-session ledger.

const FIT_EPOCH_MS = Date.UTC(1989, 11, 31); // 1989-12-31T00:00:00Z

const BASE_TYPES = {
  0x00: { size: 1, invalid: 0xff, read: (v, o) => v.getUint8(o) },          // enum
  0x01: { size: 1, invalid: 0x7f, read: (v, o) => v.getInt8(o) },           // sint8
  0x02: { size: 1, invalid: 0xff, read: (v, o) => v.getUint8(o) },          // uint8
  0x83: { size: 2, invalid: 0x7fff, read: (v, o, le) => v.getInt16(o, le) },
  0x84: { size: 2, invalid: 0xffff, read: (v, o, le) => v.getUint16(o, le) },
  0x85: { size: 4, invalid: 0x7fffffff, read: (v, o, le) => v.getInt32(o, le) },
  0x86: { size: 4, invalid: 0xffffffff, read: (v, o, le) => v.getUint32(o, le) },
  0x07: { size: 1, invalid: null, read: (v, o) => v.getUint8(o) },          // string (byte-wise)
  0x88: { size: 4, invalid: null, read: (v, o, le) => v.getFloat32(o, le) },
  0x89: { size: 8, invalid: null, read: (v, o, le) => v.getFloat64(o, le) },
  0x0a: { size: 1, invalid: 0, read: (v, o) => v.getUint8(o) },             // uint8z
  0x8b: { size: 2, invalid: 0, read: (v, o, le) => v.getUint16(o, le) },    // uint16z
  0x8c: { size: 4, invalid: 0, read: (v, o, le) => v.getUint32(o, le) },    // uint32z
  0x0d: { size: 1, invalid: 0xff, read: (v, o) => v.getUint8(o) },          // byte
  0x8e: { size: 8, invalid: null, read: (v, o, le) => Number(v.getBigInt64(o, le)) },
  0x8f: { size: 8, invalid: null, read: (v, o, le) => Number(v.getBigUint64(o, le)) },
  0x90: { size: 8, invalid: 0, read: (v, o, le) => Number(v.getBigUint64(o, le)) },
};

// Field profiles: {fieldNum: [name, scale, isDate]} — subset we consume.
const PROFILES = {
  0: { name: 'file_id', fields: {
    0: ['type'], 1: ['manufacturer'], 2: ['product'],
    3: ['serial_number'], 4: ['time_created', 1, true] } },
  18: { name: 'session', fields: {
    253: ['timestamp', 1, true], 2: ['start_time', 1, true],
    5: ['sport'], 6: ['sub_sport'],
    7: ['total_elapsed_time', 1000], 8: ['total_timer_time', 1000],
    9: ['total_distance', 100], 14: ['avg_speed', 1000],
    16: ['avg_heart_rate'], 17: ['max_heart_rate'],
    22: ['total_ascent'], 23: ['total_descent'], 26: ['num_laps'],
    57: ['avg_temperature'], 124: ['enhanced_avg_speed', 1000] } },
  19: { name: 'lap', fields: {
    253: ['timestamp', 1, true], 2: ['start_time', 1, true],
    7: ['total_elapsed_time', 1000], 8: ['total_timer_time', 1000],
    9: ['total_distance', 100], 13: ['avg_speed', 1000],
    15: ['avg_heart_rate'], 16: ['max_heart_rate'],
    23: ['intensity'], 24: ['lap_trigger'], 71: ['wkt_step_index'],
    110: ['enhanced_avg_speed', 1000], 254: ['message_index'] } },
  20: { name: 'record', fields: {
    253: ['timestamp', 1, true], 3: ['heart_rate'], 4: ['cadence'],
    5: ['distance', 100], 6: ['speed', 1000], 7: ['power'],
    13: ['temperature'], 73: ['enhanced_speed', 1000] } },
  21: { name: 'event', fields: {
    253: ['timestamp', 1, true], 0: ['event'], 1: ['event_type'],
    3: ['data'] } },
  26: { name: 'workout', fields: { 6: ['num_valid_steps'], 8: ['wkt_name'] } },
  27: { name: 'workout_step', fields: {
    254: ['message_index'], 0: ['wkt_step_name'], 1: ['duration_type'],
    2: ['duration_value'], 3: ['target_type'], 4: ['target_value'],
    5: ['custom_target_value_low'], 6: ['custom_target_value_high'],
    7: ['intensity'] } },
  312: { name: 'split', fields: {
    0: ['split_type'], 1: ['total_elapsed_time', 1000],
    2: ['total_timer_time', 1000], 3: ['total_distance', 100],
    4: ['avg_speed', 1000], 9: ['start_time', 1, true],
    27: ['end_time', 1, true] } },
};

// Enum name maps (only the values the engine reads by name).
export const ENUMS = {
  event: { 0: 'timer' },
  event_type: { 0: 'start', 1: 'stop', 4: 'stop_all' },
  lap_trigger: { 0: 'manual', 1: 'time', 2: 'distance', 7: 'session_end' },
  intensity: { 0: 'active', 1: 'rest', 2: 'warmup', 3: 'cooldown',
               4: 'recovery', 5: 'interval', 6: 'other' },
  duration_type: { 0: 'time', 1: 'distance', 5: 'open',
                   6: 'repeat_until_steps_cmplt', 7: 'repeat_until_time',
                   8: 'repeat_until_distance' },
  target_type: { 0: 'speed', 1: 'heart_rate', 2: 'open', 3: 'cadence',
                 4: 'power', 5: 'grade', 6: 'resistance' },
  sport: { 1: 'running' },
  sub_sport: { 0: 'generic', 1: 'treadmill', 2: 'street', 3: 'trail',
               4: 'track' },
  manufacturer: { 1: 'garmin' },
  split_type: { 1: 'ascent_split', 2: 'descent_split', 3: 'interval_active',
                4: 'interval_rest', 5: 'interval_warmup',
                6: 'interval_cooldown', 7: 'interval_recovery',
                8: 'interval_other', 9: 'climb_active', 10: 'climb_rest',
                11: 'surf_active', 12: 'run_active', 13: 'run_rest',
                14: 'workout_round', 17: 'rwd_run', 18: 'rwd_walk',
                20: 'transition', 22: 'rwd_stand' },
};

function enumName(kind, value) {
  if (value === null || value === undefined) return null;
  const m = ENUMS[kind];
  return (m && m[value] !== undefined) ? m[value] : value;
}

/** Parse a FIT ArrayBuffer into {messageName: [msg, ...]} raw messages. */
export function parseFit(buffer) {
  const view = new DataView(buffer);
  const headerSize = view.getUint8(0);
  const dataSize = view.getUint32(4, true);
  const out = {};
  const defs = {};            // local msg type -> definition
  let lastTimestamp = null;   // for compressed timestamp headers (FIT s)
  let pos = headerSize;
  const end = Math.min(headerSize + dataSize, buffer.byteLength - 2);

  while (pos < end) {
    const header = view.getUint8(pos); pos += 1;

    if (header & 0x80) {
      // Compressed timestamp data message.
      const local = (header >> 5) & 0x03;
      const offset = header & 0x1f;
      const def = defs[local];
      if (!def) break; // corrupt: bail out of the loop, keep what we have
      if (lastTimestamp !== null) {
        lastTimestamp = (lastTimestamp & ~0x1f) +
          offset + (offset < (lastTimestamp & 0x1f) ? 0x20 : 0);
      }
      pos = readData(view, pos, def, out, lastTimestamp,
                     (t) => { lastTimestamp = t; });
      continue;
    }

    const local = header & 0x0f;
    if (header & 0x40) {
      // Definition message.
      pos += 1; // reserved
      const arch = view.getUint8(pos); pos += 1;
      const le = arch === 0;
      const globalNum = le ? view.getUint16(pos, true) : view.getUint16(pos, false);
      pos += 2;
      const nFields = view.getUint8(pos); pos += 1;
      const fields = [];
      for (let i = 0; i < nFields; i++) {
        fields.push({ num: view.getUint8(pos), size: view.getUint8(pos + 1),
                      baseType: view.getUint8(pos + 2) });
        pos += 3;
      }
      let devBytes = 0;
      if (header & 0x20) {
        const nDev = view.getUint8(pos); pos += 1;
        const devFields = [];
        for (let i = 0; i < nDev; i++) {
          devFields.push(view.getUint8(pos + 1)); // size
          pos += 3;
        }
        devBytes = devFields.reduce((a, b) => a + b, 0);
      }
      defs[local] = { globalNum, le, fields, devBytes };
    } else {
      const def = defs[local];
      if (!def) break;
      pos = readData(view, pos, def, out, lastTimestamp,
                     (t) => { lastTimestamp = t; });
    }
  }
  return out;
}

function readData(view, pos, def, out, lastTs, setTs) {
  const profile = PROFILES[def.globalNum];
  const msg = profile ? {} : null;
  for (const f of def.fields) {
    const bt = BASE_TYPES[f.baseType];
    let value = null;
    if (bt) {
      if (f.baseType === 0x07) {
        // string: read bytes to null
        let s = '';
        for (let i = 0; i < f.size; i++) {
          const ch = view.getUint8(pos + i);
          if (ch === 0) break;
          s += String.fromCharCode(ch);
        }
        value = s || null;
      } else if (f.size === bt.size) {
        const raw = bt.read(view, pos, def.le);
        value = (bt.invalid !== null && raw === bt.invalid) ? null : raw;
      } else if (f.size % bt.size === 0) {
        // array field: engine needs none of these — take first element
        const raw = bt.read(view, pos, def.le);
        value = (bt.invalid !== null && raw === bt.invalid) ? null : raw;
      }
    }
    if (f.num === 253 && value !== null) setTs(value);
    if (msg && profile.fields[f.num]) {
      const [name, scale, isDate] = profile.fields[f.num];
      if (value !== null) {
        if (isDate) value = new Date(FIT_EPOCH_MS + value * 1000);
        else if (scale && scale !== 1) value = value / scale;
      }
      msg[name] = value;
    }
    pos += f.size;
  }
  pos += def.devBytes || 0;
  if (msg && profile) {
    if (msg.timestamp === undefined && lastTs !== null && profile.fields[253]) {
      msg.timestamp = new Date(FIT_EPOCH_MS + lastTs * 1000);
    }
    (out[profile.name] = out[profile.name] || []).push(msg);
  }
  return pos;
}

/** Decode enum fields in-place to their names (post-parse pass). */
export function decodeEnums(messages) {
  const apply = (list, mapping) => (list || []).forEach(m => {
    for (const [field, kind] of Object.entries(mapping)) {
      if (m[field] !== undefined) m[field] = enumName(kind, m[field]);
    }
  });
  apply(messages.file_id, { manufacturer: 'manufacturer' });
  apply(messages.session, { sport: 'sport', sub_sport: 'sub_sport' });
  apply(messages.lap, { lap_trigger: 'lap_trigger', intensity: 'intensity' });
  apply(messages.event, { event: 'event', event_type: 'event_type' });
  apply(messages.split, { split_type: 'split_type' });
  apply(messages.workout_step, { duration_type: 'duration_type',
                                 target_type: 'target_type',
                                 intensity: 'intensity' });
  return messages;
}
