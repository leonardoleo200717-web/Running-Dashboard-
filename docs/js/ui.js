// RunLens PWA UI — hash-routed, vanilla ES modules, Chart.js (vendored).
import * as db from './db.js';
import * as M from './metrics.js';
import { importFit, recompute, clusterEntries, repEffortsOf } from './pipeline.js';
import { snapDistance } from './intervals.js';

const $ = (sel) => document.querySelector(sel);
const app = () => $('#app');
const TZ = 'Europe/Amsterdam';
let charts = [];

const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const localdt = (iso) => iso ? new Date(iso).toLocaleString('en-GB',
  { timeZone: TZ, weekday: 'short', day: '2-digit', month: 'short',
    year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–';
const km = (m) => `${((m ?? 0) / 1000).toFixed(1)} km`;
const hms = M.fmtHms, pace = M.fmtPace;

function vizColors() {
  const s = getComputedStyle(document.documentElement);
  const g = (n) => s.getPropertyValue(n).trim();
  return { pace: g('--series-pace'), hr: g('--series-hr'), vol: g('--series-vol'),
    cats: [1, 2, 3, 4, 5, 6, 7, 8].map(i => g(`--cat-${i}`)),
    muted: g('--muted'), grid: g('--grid'), baseline: g('--baseline') };
}
function baseOptions(c) {
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.color = c.muted;
  return { responsive: true, maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { display: false } },
    scales: { x: { grid: { display: false }, border: { color: c.baseline },
                   ticks: { maxTicksLimit: 8 } },
              y: { grid: { color: c.grid }, border: { display: false } } } };
}
function mkChart(id, cfg) {
  const el = document.getElementById(id);
  if (!el) return null;
  const ch = new Chart(el, cfg);
  charts.push(ch);
  return ch;
}
function clearCharts() { charts.forEach(c => c.destroy()); charts = []; }

const badge = (conf) => `<span class="badge confidence-${conf}">${conf}</span>`;
const verdictCard = (v, extra = '') => {
  if (!v) return '';
  const style = { improving: 'var(--status-good)', declining: 'var(--status-crit)',
    similar: 'var(--status-warn)' }[v.verdict] || 'var(--baseline)';
  const head = { improving: '✔ IMPROVING', declining: '✘ NOT IMPROVING',
    similar: '≈ NO MEANINGFUL CHANGE' }[v.verdict] || '– NOT COMPARABLE';
  return `<div class="card" style="border-left:6px solid ${style}">
    <div style="font-size:1.15rem;font-weight:700;margin-bottom:.4rem">${head}
      ${v.delta_pct !== null && v.delta_pct !== undefined
        ? `<span class="muted" style="font-weight:400"> early-session efficiency ${v.delta_pct > 0 ? '+' : ''}${v.delta_pct}%</span>` : ''}
      ${extra}</div>
    <ul style="margin:0;padding-left:1.2rem">${v.insights.map(i => `<li>${esc(i)}</li>`).join('')}</ul>
  </div>`;
};

// ------------------------------------------------------------- import

async function handleFiles(files) {
  const status = $('#import-status');
  let ok = 0, failed = [];
  for (const f of files) {
    try {
      await importFit(await f.arrayBuffer());
      ok++;
      if (status) status.textContent = `Imported ${ok}/${files.length}…`;
    } catch (e) {
      console.error(e);
      failed.push(f.name);
    }
  }
  await route();  // re-render first, then report into the fresh element
  const fresh = $('#import-status');
  if (fresh) fresh.textContent =
    `Imported ${ok} file(s).` + (failed.length ? ` Failed: ${failed.join(', ')}` : '');
}

function wireGlobalDrop() {
  let depth = 0;
  document.addEventListener('dragenter', e => {
    e.preventDefault();
    if (++depth === 1) document.body.classList.add('dragging');
  });
  document.addEventListener('dragleave', () => {
    if (--depth === 0) document.body.classList.remove('dragging');
  });
  document.addEventListener('dragover', e => e.preventDefault());
  document.addEventListener('drop', async e => {
    e.preventDefault();
    depth = 0;
    document.body.classList.remove('dragging');
    const files = [...e.dataTransfer.files].filter(f => /\.fit$/i.test(f.name));
    if (files.length) await handleFiles(files);
  });
}

// ------------------------------------------------------------- sessions

async function viewSessions(params) {
  const mode = params.get('group') || 'week';
  const sessions = await db.listSessions();
  const label = (iso) => {
    const d = new Date(iso);
    if (mode === 'week') return `${M.isoWeek(d).replace('-W', ' · Week ')}`;
    if (mode === 'month') return d.toLocaleString('en-GB', { timeZone: TZ, month: 'long', year: 'numeric' });
    return d.toLocaleString('en-GB', { timeZone: TZ, year: 'numeric' });
  };
  const groups = [];
  for (const s of sessions) {
    const l = label(s.start_time_utc);
    if (!groups.length || groups[groups.length - 1].label !== l) {
      groups.push({ label: l, sessions: [], kmv: 0, timer: 0 });
    }
    const g = groups[groups.length - 1];
    g.sessions.push(s);
    g.kmv += (s.total_distance_m ?? 0) / 1000;
    g.timer += s.total_timer_s ?? 0;
  }
  app().innerHTML = `
    <h1>Sessions</h1>
    <div class="card">
      <input type="file" id="fits" accept=".fit,.FIT" multiple>
      <button class="primary" id="importBtn">Import FIT files</button>
      <span class="muted" id="import-status">…or drag &amp; drop .FIT files anywhere.
        Re-import is idempotent.</span>
    </div>
    ${sessions.length ? `<p><span class="muted">Group by:</span> ${
      ['week', 'month', 'year'].map(m => m === mode ? `<strong>${m}</strong>`
        : `<a href="#/?group=${m}">${m}</a>`).join(' · ')}</p>` : ''}
    ${groups.map(g => `
      <h2>${esc(g.label)} <span class="muted" style="font-weight:400">— ${g.sessions.length}
        session${g.sessions.length !== 1 ? 's' : ''}, ${g.kmv.toFixed(1)} km, ${hms(g.timer)}</span></h2>
      <div class="card"><table>
        <thead><tr><th>Date</th><th>Type</th><th>Structure</th><th>Dist</th>
          <th>Time</th><th>HR</th><th>Confidence</th></tr></thead>
        <tbody>${g.sessions.map(s => `
          <tr>
            <td><a class="rowlink" href="#/session/${encodeURIComponent(s.dedup_key)}">${localdt(s.start_time_utc)}</a></td>
            <td>${esc(s.session_type)}${s.overridden ? ' <span class="muted">✎</span>' : ''}</td>
            <td>${esc(s.structure ?? s.wkt_name ?? '—')}</td>
            <td>${km(s.total_distance_m)}</td>
            <td>${hms(s.total_timer_s)}</td>
            <td>${s.avg_hr ? Math.round(s.avg_hr) : '–'}</td>
            <td>${badge(s.confidence)}${s.needs_manual_tag ? ' <span class="flag">tag manually</span>' : ''}</td>
          </tr>`).join('')}</tbody>
      </table></div>`).join('') ||
      '<p class="muted">No sessions yet — import your Garmin FIT files above.</p>'}`;
  $('#importBtn').onclick = () => {
    const files = [...$('#fits').files];
    if (files.length) handleFiles(files);
  };
}

// ------------------------------------------------------------- session

function phaseSummary(segs) {
  const phases = {};
  for (const [key, kinds] of [['warmup', ['warmup']], ['work', ['rep']],
                              ['recovery', ['recovery']], ['cooldown', ['cooldown']]]) {
    const sel = segs.filter(s => kinds.includes(s.kind) && !s.outlier);
    if (!sel.length) continue;
    const dist = sel.reduce((a, s) => a + (s.distance_m ?? 0), 0);
    const timer = sel.reduce((a, s) => a + (s.timer_s ?? 0), 0);
    const hrW = sel.filter(s => s.avg_hr).map(s => [s.avg_hr, s.timer_s ?? 0]);
    const hrT = hrW.reduce((a, [, t]) => a + t, 0);
    phases[key] = { n: sel.length, dist, timer,
      pace: dist && timer ? 1000 / (dist / timer) : null,
      hr: hrT ? Math.round(hrW.reduce((a, [h, t]) => a + h * t, 0) / hrT) : null };
  }
  return phases;
}

async function similarOf(key) {
  const clusters = await clusterEntries();
  for (let ci = 0; ci < clusters.length; ci++) {
    const ids = clusters[ci].members.map(m => m.session.id);
    if (ids.includes(key) && ids.length > 1) {
      return { ci, others: clusters[ci].members.map(m => m.session)
        .filter(s => s.id !== key) };
    }
  }
  return { ci: null, others: [] };
}

async function viewSession(key) {
  const session = await db.get('sessions', key);
  if (!session) { app().innerHTML = '<p>Not found.</p>'; return; }
  const ov = await db.getOverrides(key);
  if (ov.session_type) session.session_type = ov.session_type;
  if (ov.structure) session.structure = ov.structure;
  const segs = await db.getSegments(key);
  const recRow = await db.get('records', key);
  const records = db.unpackRecords(recRow ? recRow.records : []);
  const phases = phaseSummary(segs);
  const { ci, others } = await similarOf(key);
  const settings = await db.getSettings();
  const repEffs = await repEffortsOf(key);
  const efficiency = M.sessionEfficiency(repEffs);
  const regression = M.gatedRegression(repEffs, settings.ref_hr,
    efficiency.median_pace_s_km);

  let verdict = null;
  const older = others.filter(s => s.start_time_utc < session.start_time_utc)
    .sort((a, b) => b.start_time_utc < a.start_time_utc ? -1 : 1);
  for (const cand of older) {
    const ce = M.sessionEfficiency(await repEffortsOf(cand.id));
    if (ce.domain === efficiency.domain || cand === older[older.length - 1]) {
      verdict = M.compareSessions(efficiency, ce,
        { date: session.start_time_utc.slice(0, 10), hot: session.hot },
        { date: cand.start_time_utc.slice(0, 10), hot: cand.hot });
      break;
    }
  }

  const step = Math.max(1, Math.floor(records.length / 600));
  const series = records.filter((_, i) => i % step === 0).map(r => ({
    t: r.timestamp.toLocaleTimeString('en-GB', { timeZone: TZ }),
    pace: r.enhanced_speed > 1.1 ? Math.round(10000 / r.enhanced_speed) / 10 : null,
    hr: r.heart_rate }));
  const reps = (session.kpis?.rep_table) ?? [];
  const d = session.kpis?.drift ?? {};
  const rec = session.kpis?.recovery ?? {};
  const kinds = { rep: 'Active', recovery: 'Recovery', warmup: 'WU', cooldown: 'CD' };

  app().innerHTML = `
    <h1>${localdt(session.start_time_utc)} ${badge(session.confidence)}</h1>
    <p class="muted">${esc(session.origin)} · ${esc(session.detection_source)}
      ${session.wkt_name ? `· workout “${esc(session.wkt_name)}”` : ''}</p>
    <div class="stat-row">
      ${[['Type', session.session_type], ['Structure', session.structure ?? '—'],
         ['Distance', km(session.total_distance_m)], ['Timer', hms(session.total_timer_s)],
         ['Avg HR', session.avg_hr ? Math.round(session.avg_hr) : '–']]
        .map(([l, v]) => `<div class="stat"><div class="label">${l}</div>
          <div class="value">${esc(v)}</div></div>`).join('')}
    </div>
    ${Object.keys(phases).length ? `<h2>Phases</h2><div class="card"><table>
      <thead><tr><th>Phase</th><th>Distance</th><th>Duration</th><th>Pace</th><th>HR</th></tr></thead>
      <tbody>${[['warmup', 'Warmup'], ['work', 'Work (reps)'], ['recovery', 'Recoveries'],
                ['cooldown', 'Cooldown']].filter(([k]) => phases[k]).map(([k, l]) => {
        const p = phases[k];
        return `<tr><td><strong>${l}</strong>${k === 'work' || k === 'recovery'
          ? ` <span class="muted">×${p.n}</span>` : ''}</td>
          <td>${km(p.dist)}</td><td>${hms(p.timer)}</td>
          <td>${pace(p.pace)}</td><td>${p.hr ?? '–'}</td></tr>`;
      }).join('')}</tbody></table></div>` : ''}
    ${reps.length ? `<h2>Rep table</h2><div class="card"><table>
      <thead><tr><th>#</th><th>Distance</th><th>Duration</th><th>Pace</th>
        <th>Avg HR</th><th>Max HR</th></tr></thead>
      <tbody>${reps.map(r => `<tr${r.outlier ? ' class="muted"' : ''}>
        <td>${r.rep_index ?? '⚑'}</td>
        <td>${r.distance_m ? Math.round(r.distance_m) + ' m' : '–'}</td>
        <td>${hms(r.timer_s)}</td><td>${r.pace}</td>
        <td>${r.avg_hr ? Math.round(r.avg_hr) : '–'}</td>
        <td>${r.max_hr ? Math.round(r.max_hr) : '–'}</td></tr>`).join('')}
      </tbody></table></div>
    <div class="stat-row">
      <div class="stat"><div class="label">HR drift</div><div class="value">
        ${d.hr_drift_pct !== null && d.hr_drift_pct !== undefined ? (d.hr_drift_pct > 0 ? '+' : '') + d.hr_drift_pct + '%' : '–'}</div></div>
      <div class="stat"><div class="label">Pace drift</div><div class="value">
        ${d.pace_drift_pct !== null && d.pace_drift_pct !== undefined ? (d.pace_drift_pct > 0 ? '+' : '') + d.pace_drift_pct + '%' : '–'}</div></div>
      <div class="stat"><div class="label">Recovery HR drop</div><div class="value">
        ${rec.avg_hr_drop !== null && rec.avg_hr_drop !== undefined ? Math.round(rec.avg_hr_drop) + ' bpm' : '–'}</div></div>
    </div>` : ''}
    ${efficiency.n_reps ? `<h2>Physiological efficiency</h2>
      ${verdictCard(verdict)}
      <div class="stat-row">
        <div class="stat"><div class="label">Early REI</div><div class="value">
          ${efficiency.rei_early ? (efficiency.rei_early * 1000).toFixed(2) : '–'}</div></div>
        <div class="stat"><div class="label">Full REI</div><div class="value">
          ${efficiency.rei_full ? (efficiency.rei_full * 1000).toFixed(2) : '–'}</div></div>
        <div class="stat"><div class="label">Domain</div><div class="value">
          ${esc(efficiency.domain ?? '–')}</div></div>
        <div class="stat"><div class="label">Stabilized HR reps</div><div class="value">
          ${efficiency.n_hr_valid}/${efficiency.n_reps}</div></div>
        <div class="stat"><div class="label">Heat flag</div><div class="value">
          <button id="hotBtn">${session.hot ? '🔥 hot' : 'normal'}</button></div></div>
      </div>
      <div class="card">${regression.valid
        ? `<p><strong>HR ↔ pace regression</strong> (R² ${regression.r2}, n=${regression.n}):
           pace cost <strong>${regression.pace_cost} bpm per 10 s/km</strong>
           ${regression.expected_pace_at_ref_hr ? `· predicted pace @ ${Math.round(settings.ref_hr)} bpm:
             <strong>${pace(regression.expected_pace_at_ref_hr)}</strong>` : ''}</p>`
        : `<p class="muted">Normalized performance not computable — ${esc(regression.reason)}.</p>`}
      </div>` : ''}
    ${series.length ? `<h2>Pace</h2><div class="card"><div class="chart-box"><canvas id="paceChart"></canvas></div></div>
      <h2>Heart rate</h2><div class="card"><div class="chart-box"><canvas id="hrChart"></canvas></div></div>` : ''}
    ${segs.length ? `<h2>Segments <span class="muted" style="font-weight:400">— reclassify; recomputes</span></h2>
      <div class="card"><table><thead><tr><th>#</th><th>Kind</th><th>Distance</th>
        <th>Duration</th><th>Pace</th><th>HR</th></tr></thead>
      <tbody>${segs.map((s, i) => `<tr>
        <td class="muted">${i + 1}${s.rep_index ? ` <strong>· rep ${s.rep_index}</strong>` : ''}</td>
        <td><select data-seg="${esc(s.start_time.toISOString())}">
          ${Object.entries(kinds).map(([v, l]) =>
            `<option value="${v}"${s.kind === v ? ' selected' : ''}>${l}</option>`).join('')}
          ${!kinds[s.kind] ? `<option value="${esc(s.kind)}" selected>${esc(s.kind)}</option>` : ''}
        </select>${s.overridden_kind ? ' <span class="muted">✎</span>' : ''}</td>
        <td>${s.distance_m ? Math.round(s.distance_m) + ' m' : '–'}</td>
        <td>${hms(s.timer_s)}</td>
        <td>${s.avg_speed_ms ? pace(1000 / s.avg_speed_ms) : '–'}</td>
        <td>${s.avg_hr ? Math.round(s.avg_hr) : '–'}</td></tr>`).join('')}
      </tbody></table></div>` : ''}
    ${others.length ? `<h2>Similar sessions</h2><div class="card">
      <p class="muted"><a href="#/cluster/${ci}">Compare them overlapped →</a></p>
      <table><thead><tr><th>Date</th><th>Structure</th><th>Distance</th><th>HR</th></tr></thead>
      <tbody>${others.map(s => `<tr>
        <td><a class="rowlink" href="#/session/${encodeURIComponent(s.id)}">${localdt(s.start_time_utc)}</a></td>
        <td>${esc(s.structure ?? s.session_type)}</td><td>${km(s.total_distance_m)}</td>
        <td>${s.avg_hr ? Math.round(s.avg_hr) : '–'}</td></tr>`).join('')}</tbody></table></div>` : ''}
    <h2>Manual override</h2><div class="card">
      <label>Type <select id="ovType"><option value="">(keep: ${esc(session.session_type)})</option>
        ${['easy', 'long', 'intervals', 'fartlek', 'race', 'unknown']
          .map(t => `<option>${t}</option>`).join('')}</select></label>
      <label>Structure <input id="ovStruct" type="text" placeholder="e.g. 8x400m R200m"></label>
      <button id="ovSave">Save override</button>
      <span class="muted">Overrides always win and survive re-import.</span>
    </div>`;

  const c = vizColors();
  if (series.length) {
    const labels = series.map(p => p.t);
    const po = baseOptions(c);
    po.scales.y.reverse = true;
    po.scales.y.ticks = { callback: v => pace(v) };
    mkChart('paceChart', { type: 'line', data: { labels, datasets: [{
      data: series.map(p => p.pace), borderColor: c.pace, borderWidth: 2,
      pointRadius: 0, spanGaps: true }] }, options: po });
    mkChart('hrChart', { type: 'line', data: { labels, datasets: [{
      data: series.map(p => p.hr), borderColor: c.hr, borderWidth: 2,
      pointRadius: 0, spanGaps: true }] }, options: baseOptions(c) });
  }
  $('#hotBtn')?.addEventListener('click', async () => {
    session.hot = session.hot ? 0 : 1;
    await db.put('sessions', session);
    route();
  });
  document.querySelectorAll('select[data-seg]').forEach(sel => {
    sel.addEventListener('change', async () => {
      await db.setOverride(key, { segkinds: { [sel.dataset.seg]: sel.value } });
      await recompute(key);
      route();
    });
  });
  $('#ovSave').onclick = async () => {
    const patch = {};
    if ($('#ovType').value) patch.session_type = $('#ovType').value;
    if ($('#ovStruct').value.trim()) patch.structure = $('#ovStruct').value.trim();
    await db.setOverride(key, patch);
    route();
  };
}

// ------------------------------------------------------------- clusters

async function viewClusters() {
  const clusters = await clusterEntries();
  const multi = clusters.map((c, i) => [c, i]).filter(([c]) => c.members.length > 1);
  app().innerHTML = `<h1>Similar sessions</h1>
    <p class="muted">Same rep distances regardless of count; time intervals ±15%
    work; runs ±10% distance.</p>
    ${multi.map(([c, i]) => `
      <h2>${esc(c.label)} <span class="muted" style="font-weight:400">
        — ${c.members.length} sessions · <a href="#/cluster/${i}">compare overlapped</a></span></h2>
      <div class="card"><table><thead><tr><th>Date</th><th>Type</th><th>Structure</th>
        <th>Distance</th><th>HR</th></tr></thead>
      <tbody>${c.members.map(m => `<tr>
        <td><a class="rowlink" href="#/session/${encodeURIComponent(m.session.id)}">${localdt(m.session.start_time_utc)}</a></td>
        <td>${esc(m.session.session_type)}</td><td>${esc(m.session.structure ?? '—')}</td>
        <td>${km(m.session.total_distance_m)}</td>
        <td>${m.session.avg_hr ? Math.round(m.session.avg_hr) : '–'}</td></tr>`).join('')}
      </tbody></table></div>`).join('') ||
      '<p class="muted">No groups with more than one session yet.</p>'}`;
}

async function viewCluster(idx) {
  const clusters = await clusterEntries();
  const cluster = clusters[idx];
  if (!cluster) { app().innerHTML = '<p>Not found.</p>'; return; }
  const members = cluster.members.slice(-8);
  const overlay = [], trendPoints = [], effs = {};
  for (const m of members) {
    const sid = m.session.id;
    const sel = cluster.kind !== 'run'
      ? m.segments.filter(s => s.kind === 'rep' && !s.outlier)
      : m.segments.filter(s => (s.timer_s ?? 0) > 60).slice(0, 30);
    const paces = sel.map(s => s.avg_speed_ms ? Math.round(10000 / s.avg_speed_ms) / 10 : null);
    const hrs = sel.map(s => s.avg_hr ? Math.round(s.avg_hr) : null);
    let cum = 0;
    const cumMin = sel.map(s => Math.round((cum += (s.timer_s ?? 0) / 60) * 10) / 10);
    const eff = M.sessionEfficiency(await repEffortsOf(sid));
    effs[sid] = eff;
    const date = m.session.start_time_utc.slice(0, 10);
    overlay.push({ sid, date, structure: m.session.structure,
      domain: eff.domain, paces, hrs, cumMin });
    trendPoints.push({ date, sid, rei: eff.rei_early ? Math.round(eff.rei_early * 100000) / 100 : null,
      pace: eff.median_pace_s_km, n: eff.n_hr_valid, domain: eff.domain });
  }
  let verdict = null, compared = null, pairVerdict = null, pairCompared = null;
  if (members.length >= 2) {
    const cur = members[members.length - 1];
    const curDom = effs[cur.session.id].domain;
    let prev = members[members.length - 2];
    for (let i = members.length - 2; i >= 0; i--) {
      if (effs[members[i].session.id].domain === curDom) { prev = members[i]; break; }
    }
    verdict = M.compareSessions(effs[cur.session.id], effs[prev.session.id],
      { date: cur.session.start_time_utc.slice(0, 10), hot: cur.session.hot },
      { date: prev.session.start_time_utc.slice(0, 10), hot: prev.session.hot });
    compared = { cur: cur.session.start_time_utc.slice(0, 10),
                 prev: prev.session.start_time_utc.slice(0, 10) };
    if (verdict.verdict === 'not_comparable') {
      outer: for (let i = members.length - 1; i > 0; i--) {
        for (let j = i - 1; j >= 0; j--) {
          if (effs[members[i].session.id].domain === effs[members[j].session.id].domain) {
            const pv = M.compareSessions(effs[members[i].session.id], effs[members[j].session.id],
              { date: members[i].session.start_time_utc.slice(0, 10), hot: members[i].session.hot },
              { date: members[j].session.start_time_utc.slice(0, 10), hot: members[j].session.hot });
            if (pv.verdict !== 'not_comparable') {
              pairVerdict = pv;
              pairCompared = { cur: members[i].session.start_time_utc.slice(0, 10),
                               prev: members[j].session.start_time_utc.slice(0, 10) };
              break outer;
            }
          }
        }
      }
    }
  }
  const xMax = Math.max(0, ...overlay.map(o => o.paces.length));
  const xLabel = cluster.kind !== 'run' ? 'Rep' : 'Segment';

  app().innerHTML = `<h1>Compare: ${esc(cluster.label)}</h1>
    <p class="muted">${cluster.members.length} similar sessions.
      <a href="#/clusters">← all groups</a></p>
    ${verdictCard(verdict, compared
      ? `<span class="muted" style="font-weight:400;font-size:.9rem"> — ${compared.cur} vs ${compared.prev}</span>` : '')}
    ${pairVerdict ? `<div class="card"><strong>Last comparable pair</strong>
      <span class="muted">(${pairCompared.cur} vs ${pairCompared.prev})</span>:
      ${pairVerdict.verdict === 'improving' ? '<span style="color:var(--good-text);font-weight:700">✔ IMPROVING</span>'
        : pairVerdict.verdict === 'declining' ? '<span style="color:var(--status-crit);font-weight:700">✘ NOT IMPROVING</span>'
        : '≈ no meaningful change'}
      ${pairVerdict.delta_pct !== null ? ` (${pairVerdict.delta_pct > 0 ? '+' : ''}${pairVerdict.delta_pct}%)` : ''}
      <ul style="margin:.3rem 0 0;padding-left:1.2rem">
        ${pairVerdict.insights.map(i => `<li>${esc(i)}</li>`).join('')}</ul></div>` : ''}
    <h2>Efficiency across these sessions
      <span class="muted" style="font-weight:400">— early REI ×1000, colored by domain
      (only same-colored points compare)</span></h2>
    <div class="card"><div class="chart-box"><canvas id="effTrend"></canvas></div></div>
    <h2>Rep-by-rep overlay</h2>
    <div class="card">
      <div id="chips" style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.8rem">
        <span class="muted" style="font-size:.85rem">Toggle · dbl-click solo:</span>
        <button id="allBtn">All</button><button id="noneBtn">None</button>
        <label class="muted" style="font-size:.85rem">X:
          <select id="xMode"><option value="rep">${xLabel} #</option>
          <option value="time">work time (min)</option></select></label>
      </div>
      <h2 style="margin-top:0">Pace (min/km)</h2>
      <div class="chart-box"><canvas id="ovPace"></canvas></div>
      <h2>Heart rate (bpm)</h2>
      <div class="chart-box"><canvas id="ovHr"></canvas></div>
    </div>
    <div class="card"><table><thead><tr><th></th><th>Date</th><th>Structure</th>
      <th>Domain</th><th>Avg rep pace</th><th>REI</th></tr></thead>
    <tbody>${overlay.map((o, i) => {
      const valid = o.paces.filter(p => p !== null);
      const avg = valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null;
      const t = trendPoints[i];
      return `<tr><td><span class="swatch" data-slot="${i}"></span></td>
        <td><a class="rowlink" href="#/session/${encodeURIComponent(o.sid)}">${o.date}</a></td>
        <td>${esc(o.structure ?? '—')}</td><td class="muted">${esc(o.domain ?? '–')}</td>
        <td>${pace(avg)}</td><td>${t.rei ?? '–'} <span class="muted" style="font-size:.8rem">(n=${t.n})</span></td></tr>`;
    }).join('')}</tbody></table></div>`;

  const c = vizColors();
  document.querySelectorAll('.swatch').forEach(el => {
    el.style.background = c.cats[+el.dataset.slot % 8];
  });
  const domainColor = { easy: c.cats[1], steady: c.cats[0],
    threshold: c.cats[2], hard: c.cats[5] };
  {
    const hasRei = trendPoints.some(p => p.rei !== null);
    const o = baseOptions(c);
    o.plugins.tooltip = { callbacks: { label: (ctx) => {
      const p = trendPoints[ctx.dataIndex];
      return p.rei !== null ? ` ${p.domain}: REI ${p.rei} · ${pace(p.pace)} (n=${p.n})`
        : ` ${pace(p.pace)} (no stabilized HR)`; } } };
    if (!hasRei) { o.scales.y.reverse = true; o.scales.y.ticks = { callback: v => pace(v) }; }
    mkChart('effTrend', { type: 'line', data: {
      labels: trendPoints.map(p => p.date),
      datasets: [{ data: trendPoints.map(p => hasRei ? p.rei : p.pace),
        borderColor: c.baseline, borderDash: [4, 4], borderWidth: 1.5,
        pointRadius: 6, pointHoverRadius: 8, spanGaps: true,
        pointBackgroundColor: trendPoints.map(p => domainColor[p.domain] ?? c.muted),
        pointBorderColor: trendPoints.map(p => domainColor[p.domain] ?? c.muted) }] },
      options: o });
  }
  let xMode = 'rep', paceChart = null, hrChart = null;
  const hidden = new Set();
  const datasetsFor = (metric) => overlay.map((s, i) => ({
    label: s.date,
    data: xMode === 'rep' ? s[metric]
      : s[metric].map((v, k) => ({ x: s.cumMin[k], y: v })),
    borderColor: c.cats[i % 8], backgroundColor: c.cats[i % 8],
    borderWidth: 2, pointRadius: 4, pointHoverRadius: 6, spanGaps: true,
    hidden: hidden.has(i) }));
  const xScale = () => xMode === 'rep'
    ? { grid: { display: false }, border: { color: c.baseline },
        title: { display: true, text: `${xLabel} #`, color: c.muted } }
    : { type: 'linear', grid: { display: false }, border: { color: c.baseline },
        title: { display: true, text: 'work time (min)', color: c.muted } };
  const build = () => {
    paceChart?.destroy(); hrChart?.destroy();
    charts = charts.filter(ch => ch !== paceChart && ch !== hrChart);
    const labels = Array.from({ length: xMax }, (_, i) => i + 1);
    const op = baseOptions(c);
    op.scales.x = xScale();
    op.scales.y.reverse = true;
    op.scales.y.ticks = { callback: v => pace(v) };
    op.plugins.tooltip = { callbacks: { label: ctx => ` ${ctx.dataset.label} ${pace(ctx.parsed.y)}` } };
    paceChart = mkChart('ovPace', { type: 'line',
      data: { labels, datasets: datasetsFor('paces') }, options: op });
    const oh = baseOptions(c);
    oh.scales.x = xScale();
    oh.plugins.tooltip = { callbacks: { label: ctx => ` ${ctx.dataset.label} ${ctx.parsed.y} bpm` } };
    hrChart = mkChart('ovHr', { type: 'line',
      data: { labels, datasets: datasetsFor('hrs') }, options: oh });
  };
  const chipBox = $('#chips'), allBtn = $('#allBtn');
  overlay.forEach((s, i) => {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `<span class="dot" style="background:${c.cats[i % 8]}"></span>${s.date}`;
    chip.onclick = () => {
      hidden.has(i) ? hidden.delete(i) : hidden.add(i);
      chip.classList.toggle('off', hidden.has(i));
      sync();
    };
    chip.ondblclick = () => {
      hidden.clear();
      overlay.forEach((_, j) => { if (j !== i) hidden.add(j); });
      refresh(); sync();
    };
    chipBox.insertBefore(chip, allBtn);
  });
  const refresh = () => document.querySelectorAll('.chip').forEach((el, i) =>
    el.classList.toggle('off', hidden.has(i)));
  const sync = () => [paceChart, hrChart].forEach(ch => {
    ch.data.datasets.forEach((d, i) => { d.hidden = hidden.has(i); });
    ch.update();
  });
  allBtn.onclick = () => { hidden.clear(); refresh(); sync(); };
  $('#noneBtn').onclick = () => { overlay.forEach((_, i) => hidden.add(i)); refresh(); sync(); };
  $('#xMode').onchange = (e) => { xMode = e.target.value; build(); };
  build();
}

// ------------------------------------------------------------- trends

async function viewTrends() {
  const sessions = await db.listSessions();
  const pointsHr = [], pointsPace = [];
  for (const s of sessions) {
    const date = s.start_time_utc.slice(0, 10);
    if (s.kpis?.pace_at_hr) pointsHr.push({ date, value: Math.round(s.kpis.pace_at_hr * 10) / 10 });
    if (s.kpis?.hr_at_pace) pointsPace.push({ date, value: s.kpis.hr_at_pace });
  }
  pointsHr.sort((a, b) => a.date < b.date ? -1 : 1);
  pointsPace.sort((a, b) => a.date < b.date ? -1 : 1);
  const weekly = M.weeklyVolume(sessions);
  const stats = M.improvementStats(pointsHr, pointsPace, weekly);

  const byDomain = {};
  for (const s of sessions) {
    const effs = db.unpackEfforts((await db.get('efforts', s.dedup_key))?.efforts ?? []);
    const grouped = {};
    for (const e of effs.filter(e => e.rei)) (grouped[e.domain] ??= []).push(e);
    for (const [dom, list] of Object.entries(grouped)) {
      const reps = list.filter(e => e.kind === 'rep')
        .sort((a, b) => (a.rep_index ?? 0) - (b.rep_index ?? 0));
      const pool = reps.length ? reps.slice(0, Math.ceil(reps.length / 3)) : list;
      const value = pool.reduce((a, e) => a + e.rei, 0) / pool.length;
      (byDomain[dom] ??= []).push({ date: s.start_time_utc.slice(0, 10),
        value: Math.round(value * 100000) / 100, hot: !!s.hot });
    }
  }
  for (const dom of Object.keys(byDomain)) byDomain[dom].sort((a, b) => a.date < b.date ? -1 : 1);

  const yn = (v) => v === null ? '<span class="muted">–</span>'
    : v ? '<span style="color:var(--good-text)">✔ YES</span>'
        : '<span style="color:var(--status-crit)">✘ NO</span>';
  app().innerHTML = `<h1>Trends</h1>
    <h2>Am I improving?</h2>
    <div class="stat-row">
      <div class="stat"><div class="label">Overall</div><div class="value">${yn(stats.improving)}</div></div>
      <div class="stat"><div class="label">Pace @ HR 165–175</div><div class="value">
        ${stats.pace_at_hr_slope_30d !== null ? `${stats.pace_at_hr_slope_30d > 0 ? '+' : ''}${stats.pace_at_hr_slope_30d} s/km <span class="muted" style="font-size:.8rem">/30d</span>` : '–'}</div></div>
      <div class="stat"><div class="label">Volume 4wk</div><div class="value">
        ${stats.volume_change_pct !== null ? `${stats.volume_change_pct > 0 ? '+' : ''}${stats.volume_change_pct}%` : '–'}</div></div>
    </div>
    <h2>Efficiency by domain <span class="muted" style="font-weight:400">— early REI ×1000; never mixed; 🔥 heat</span></h2>
    ${Object.keys(byDomain).length ? ['easy', 'steady', 'threshold', 'hard']
      .filter(d => byDomain[d]).map(d => `
      <div class="card"><strong style="text-transform:capitalize">${d}</strong>
        <span class="muted">(n=${byDomain[d].length})</span>
        ${byDomain[d].length >= 2
          ? `<div class="chart-box" style="height:200px"><canvas id="dom-${d}"></canvas></div>`
          : '<p class="muted">Needs more sessions for a trend.</p>'}</div>`).join('')
      : '<div class="card"><p class="muted">No efforts with stabilized HR yet.</p></div>'}
    <h2>Weekly volume</h2>
    <div class="card"><div class="chart-box"><canvas id="weekly"></canvas></div></div>`;

  const c = vizColors();
  for (const [dom, pts] of Object.entries(byDomain)) {
    if (pts.length < 2) continue;
    const trend = M.trendSeries(pts);
    const o = baseOptions(c);
    o.plugins.tooltip = { callbacks: { label: ctx =>
      ` REI ${ctx.parsed.y}${pts[ctx.dataIndex].hot ? ' 🔥' : ''}` } };
    const ds = [{ data: pts.map(p => p.value), borderColor: c.pace,
      backgroundColor: c.pace, borderWidth: 2, pointRadius: 4,
      pointStyle: pts.map(p => p.hot ? 'triangle' : 'circle') }];
    if (trend.fit) ds.push({ data: trend.fit, borderColor: c.muted,
      borderDash: [6, 4], borderWidth: 2, pointRadius: 0 });
    mkChart(`dom-${dom}`, { type: 'line',
      data: { labels: pts.map(p => p.date), datasets: ds }, options: o });
  }
  if (weekly.length) {
    const o = baseOptions(c);
    o.plugins.tooltip = { callbacks: { label: ctx => ` ${ctx.parsed.y} km` } };
    mkChart('weekly', { type: 'bar', data: { labels: weekly.map(w => w.week),
      datasets: [{ data: weekly.map(w => w.km), backgroundColor: c.vol,
        borderRadius: 4, maxBarThickness: 26 }] }, options: o });
  }
}

// ------------------------------------------------------------- progression

async function viewProgression() {
  const points = await db.getAll('progression');
  const confirmed = points.filter(p => p.status === 'confirmed')
    .sort((a, b) => a.date < b.date ? -1 : 1);
  const proposed = points.filter(p => p.status === 'proposed')
    .sort((a, b) => a.date < b.date ? -1 : 1);
  app().innerHTML = `<h1>Aerobic progression tracker</h1>
    <p class="muted">Steady blocks ≥10', CV ≤5%, no pauses. Proposed points
    need one-click confirmation.</p>
    <div class="card">${confirmed.length
      ? '<div class="chart-box tall"><canvas id="tracker"></canvas></div>'
      : '<p class="muted">No confirmed points yet.</p>'}</div>
    ${proposed.length ? `<h2>Proposed</h2><div class="card"><table>
      <thead><tr><th>Date</th><th>Block</th><th>Pace</th><th>HR</th><th></th></tr></thead>
      <tbody>${proposed.map(p => `<tr><td>${p.date}</td><td>${esc(p.label)}</td>
        <td>${pace(p.pace_s_km)}</td><td>${Math.round(p.avg_hr)}</td>
        <td><button class="primary" data-confirm="${p.id}">Confirm</button>
            <button data-reject="${p.id}">Reject</button></td></tr>`).join('')}
      </tbody></table></div>` : ''}
    <h2>Confirmed</h2><div class="card"><table>
      <thead><tr><th>Date</th><th>Label</th><th>Pace</th><th>HR</th><th>Source</th></tr></thead>
      <tbody>${confirmed.map(p => `<tr><td>${p.date}</td><td>${esc(p.label)}</td>
        <td>${pace(p.pace_s_km)}</td><td>${Math.round(p.avg_hr)}</td>
        <td class="muted">${esc(p.source)}</td></tr>`).join('')}</tbody></table></div>`;
  if (confirmed.length) {
    const c = vizColors();
    const o = baseOptions(c);
    o.plugins.legend = { display: true, labels: { boxWidth: 12, boxHeight: 12 } };
    o.scales.y = { position: 'left', reverse: true, grid: { color: c.grid },
      border: { display: false }, title: { display: true, text: 'Pace', color: c.muted },
      ticks: { callback: v => pace(v) } };
    o.scales.y2 = { position: 'right', grid: { drawOnChartArea: false },
      border: { display: false }, title: { display: true, text: 'Avg HR', color: c.muted } };
    mkChart('tracker', { type: 'line', data: { labels: confirmed.map(p => p.date),
      datasets: [
        { label: 'Pace', yAxisID: 'y', data: confirmed.map(p => p.pace_s_km),
          borderColor: c.pace, backgroundColor: c.pace, borderWidth: 2, pointRadius: 4 },
        { label: 'Avg HR', yAxisID: 'y2', data: confirmed.map(p => p.avg_hr),
          borderColor: c.hr, backgroundColor: c.hr, borderWidth: 2, pointRadius: 4 }] },
      options: o });
  }
  document.querySelectorAll('[data-confirm]').forEach(b => b.onclick = async () => {
    const p = await db.get('progression', Number(b.dataset.confirm));
    p.status = 'confirmed';
    await db.put('progression', p);
    route();
  });
  document.querySelectorAll('[data-reject]').forEach(b => b.onclick = async () => {
    const p = await db.get('progression', Number(b.dataset.reject));
    p.status = 'rejected';
    await db.put('progression', p);
    route();
  });
}

// ------------------------------------------------------------- predictor

async function viewPredictor() {
  const races = (await db.getAll('races')).sort((a, b) => a.date < b.date ? 1 : -1);
  const sessions = await db.listSessions();
  const weekly = M.weeklyVolume(sessions);
  const last4arr = weekly.slice(-4);
  const last4 = last4arr.length
    ? Math.round(last4arr.reduce((a, w) => a + w.km, 0) / last4arr.length * 10) / 10 : 0;
  const SUPPORT = 65;
  const anchor = races[0] ?? null;
  const predictions = anchor ? M.racePredictions(anchor.time_s, anchor.distance_m) : [];
  const vdot = anchor ? Math.round(M.vdotFrom(anchor.time_s, anchor.distance_m) * 10) / 10 : null;
  const band = anchor && anchor.distance_m < 42195 && last4 < SUPPORT
    ? { low: Math.round(anchor.time_s * (42195 / anchor.distance_m) ** 1.06),
        high: Math.round(anchor.time_s * (42195 / anchor.distance_m) ** 1.15) } : null;
  const csEfforts = races.filter(r => r.time_s >= 120 && r.time_s <= 1500);

  const bests = {};
  for (const s of sessions) {
    const latest = sessions[0] ? new Date(sessions[0].start_time_utc) : null;
    if (latest && (latest - new Date(s.start_time_utc)) / 86400000 > 120) continue;
    for (const [d, t] of Object.entries(s.kpis?.best_efforts ?? {})) {
      if (!bests[d] || t < bests[d].t) bests[d] = { t, sid: s.dedup_key, date: s.start_time_utc.slice(0, 10) };
    }
  }
  app().innerHTML = `<h1>Race predictor</h1>
    <p class="muted">Anchored on <strong>confirmed races only</strong> — never rolling
    windows over interval sessions.</p>
    ${anchor ? `<div class="stat-row">
      <div class="stat"><div class="label">Tier 1 anchor</div><div class="value">${esc(anchor.label)}</div></div>
      <div class="stat"><div class="label">Performance</div><div class="value">
        ${(anchor.distance_m / 1000).toFixed(1)} km in ${hms(anchor.time_s)}</div></div>
      <div class="stat"><div class="label">Date</div><div class="value">${anchor.date}</div></div>
      <div class="stat"><div class="label">VDOT</div><div class="value">${vdot}</div></div>
      <div class="stat"><div class="label">4-wk volume</div><div class="value">${last4} km/wk</div></div>
    </div>
    <h2>Predictions</h2><div class="card"><table>
      <thead><tr><th>Distance</th><th>Riegel</th><th>VDOT</th><th>Cameron</th>
        <th>Consensus</th><th>Pace</th></tr></thead>
      <tbody>${predictions.map(p => `<tr>
        <td><strong>${p.label}</strong>${p.label === 'Marathon'
          ? (last4 >= SUPPORT ? ' <span style="color:var(--good-text);font-size:.8rem">volume ✔</span>'
             : ` <span class="flag">volume ${last4} &lt; ~${SUPPORT} km/wk</span>`) : ''}</td>
        <td>${hms(p.riegel_s)}</td><td>${hms(p.vdot_s)}</td><td>${hms(p.cameron_s)}</td>
        <td><strong>${hms(p.mean_s)}</strong></td>
        <td>${pace(p.mean_s / (p.distance_m / 1000))}</td></tr>`).join('')}</tbody></table>
      ${band ? `<p class="muted">Marathon with low-volume correction (exp 1.06→1.15):
        <strong>${hms(band.low)} – ${hms(band.high)}</strong>.</p>` : ''}
      <p class="muted" style="font-size:.85rem">Anchor from ${anchor.date} — the older,
      the less current. Add a recent race/test below.</p></div>`
    : '<p class="muted">No confirmed races yet — add one below.</p>'}
    <h2>Tier 2 — Critical Speed</h2><div class="card"><p class="muted">
      ${csEfforts.length >= 3 ? 'Enough confirmed efforts — CS fit coming next release.'
        : `Needs ≥3 confirmed maximal efforts of 2–15' (has ${csEfforts.length}) —
           a 12–15' max test unlocks it. Training reps are excluded by design.`}</p></div>
    <h2>Confirmed races &amp; tests</h2><div class="card">
      ${races.length ? `<table><thead><tr><th>Date</th><th>Label</th><th>Distance</th>
        <th>Time</th><th>Pace</th></tr></thead><tbody>
        ${races.map(r => `<tr><td>${r.date}</td><td>${esc(r.label)}</td>
          <td>${(r.distance_m / 1000).toFixed(2)} km</td><td>${hms(r.time_s)}</td>
          <td>${pace(r.time_s / (r.distance_m / 1000))}</td></tr>`).join('')}</tbody></table>` : ''}
      <p style="margin-top:.8rem">
        <input id="rDate" placeholder="2026-07-15" size="10">
        <input id="rKm" placeholder="21.1" size="5">
        <input id="rTime" placeholder="1:25:00" size="8">
        <input id="rLabel" placeholder="HM test" size="12">
        <button class="primary" id="rAdd">Add race / test</button></p></div>
    <h2>Best rolling efforts <span class="muted" style="font-weight:400">(info only, never anchors)</span></h2>
    <div class="card">${Object.keys(bests).length ? `<table>
      <thead><tr><th>Distance</th><th>Time</th><th>Pace</th><th>Date</th></tr></thead>
      <tbody>${Object.entries(bests).sort((a, b) => a[0] - b[0]).map(([d, b]) => `
        <tr><td>${d} m</td><td>${hms(b.t)}</td><td>${pace(b.t / (d / 1000))}</td>
        <td><a class="rowlink" href="#/session/${encodeURIComponent(b.sid)}">${b.date}</a></td></tr>`).join('')}
      </tbody></table>` : '<p class="muted">No qualifying efforts.</p>'}</div>`;
  $('#rAdd').onclick = async () => {
    try {
      const parts = $('#rTime').value.split(':').map(Number);
      const timeS = parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2]
        : parts[0] * 60 + parts[1];
      const distM = parseFloat($('#rKm').value) * 1000;
      if (!$('#rDate').value || !distM || !timeS) throw new Error('bad input');
      await db.put('races', { date: $('#rDate').value, distance_m: distM,
        time_s: timeS, label: $('#rLabel').value || 'race' });
      route();
    } catch { alert('Need date, distance (km) and time (h:mm:ss).'); }
  };
}

// ------------------------------------------------------------- settings

async function viewSettings() {
  const s = await db.getSettings();
  app().innerHTML = `<h1>Settings</h1><div class="card">
    ${[['lthr', 'LTHR (bpm)'], ['max_hr', 'Max HR (bpm)'],
       ['z2_ceiling', 'Z2 ceiling (bpm)'], ['ref_hr', 'Reference HR (bpm)'],
       ['lthr_pace_s_km', 'Threshold pace (s/km)']].map(([k, l]) => `
      <p><label>${l}: <input data-set="${k}" type="number" value="${s[k]}" step="0.5"></label></p>`).join('')}
    <button class="primary" id="saveSettings">Save</button>
    <span class="muted">Domain boundaries derive from LTHR; re-import files to
    re-tag efforts after changing.</span></div>`;
  $('#saveSettings').onclick = async () => {
    for (const el of document.querySelectorAll('[data-set]')) {
      await db.setSetting(el.dataset.set, parseFloat(el.value));
    }
    alert('Saved. Re-import FIT files to re-tag efforts with new boundaries.');
  };
}

// ------------------------------------------------------------- router

async function route() {
  clearCharts();
  const hash = location.hash.slice(1) || '/';
  const [path, query] = hash.split('?');
  const params = new URLSearchParams(query || '');
  document.querySelectorAll('nav a').forEach(a =>
    a.classList.toggle('active', a.getAttribute('href') === `#${path}`));
  try {
    if (path === '/' || path === '') await viewSessions(params);
    else if (path.startsWith('/session/')) await viewSession(decodeURIComponent(path.slice(9)));
    else if (path === '/trends') await viewTrends();
    else if (path === '/progression') await viewProgression();
    else if (path === '/clusters') await viewClusters();
    else if (path.startsWith('/cluster/')) await viewCluster(Number(path.slice(9)));
    else if (path === '/predictor') await viewPredictor();
    else if (path === '/settings') await viewSettings();
    else app().innerHTML = '<p>Not found.</p>';
  } catch (e) {
    console.error(e);
    app().innerHTML = `<div class="card"><p class="flag">Error: ${esc(e.message)}</p></div>`;
  }
}

export async function start() {
  await db.seedOnce();
  wireGlobalDrop();
  window.addEventListener('hashchange', route);
  await route();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  }
}
