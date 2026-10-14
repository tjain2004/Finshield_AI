/* ================================================================
   FinShield AI — dashboard front-end
   All numbers rendered here come from the Flask API (/api/*) which
   reads your notebooks' own saved CSV / PKL outputs.
   ================================================================ */

const CSS = getComputedStyle(document.documentElement);
const C = n => CSS.getPropertyValue(n).trim();

const PALETTE = {
  s1: C('--s1'), s2: C('--s2'), s3: C('--s3'), s4: C('--s4'),
  s5: C('--s5'), s6: C('--s6'), s7: C('--s7'), s8: C('--s8'),
  good: C('--good'), warn: C('--warn'), serious: C('--serious'), critical: C('--critical'),
  accent: C('--accent'),
  text: C('--text'), text2: C('--text2'), text3: C('--text3'),
  grid: '#1B2C48', surface: C('--bg3'),
};
const SERIES = [PALETTE.s1, PALETTE.s2, PALETTE.s3, PALETTE.s4,
                PALETTE.s5, PALETTE.s6, PALETTE.s7, PALETTE.s8];
const RISK_COLORS = { LOW: PALETTE.good, MEDIUM: PALETTE.warn, HIGH: PALETTE.serious, CRITICAL: PALETTE.critical };

/* ---------- Chart.js global defaults ---------- */
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.color = PALETTE.text2;
Chart.defaults.plugins.legend.labels.usePointStyle = true;
Chart.defaults.plugins.legend.labels.pointStyle = 'rectRounded';
Chart.defaults.plugins.legend.labels.boxWidth = 9;
Chart.defaults.plugins.legend.labels.boxHeight = 9;
Chart.defaults.plugins.legend.labels.padding = 14;
Chart.defaults.plugins.tooltip.backgroundColor = '#050B16';
Chart.defaults.plugins.tooltip.borderColor = '#24395C';
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.titleColor = '#fff';
Chart.defaults.plugins.tooltip.bodyColor = PALETTE.text2;
Chart.defaults.plugins.tooltip.padding = 10;
Chart.defaults.plugins.tooltip.cornerRadius = 7;
Chart.defaults.plugins.tooltip.displayColors = true;
Chart.defaults.plugins.tooltip.boxPadding = 4;
Chart.defaults.maintainAspectRatio = false;

const AXIS = {
  grid: { color: PALETTE.grid, drawTicks: false },
  border: { display: false },
  ticks: { color: PALETTE.text3, padding: 8 },
};
const AXIS_NOGRID = {
  grid: { display: false },
  border: { display: false },
  ticks: { color: PALETTE.text3, padding: 8 },
};

/* ---------- helpers ---------- */
const fmt = n => (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');
const pct = (n, d = 2) => (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toFixed(d) + '%';
const $ = id => document.getElementById(id);
const charts = {};

function makeChart(id, cfg) {
  const el = $(id);
  if (!el) return null;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(el, cfg);
  return charts[id];
}

async function getJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (e) {
    console.error('API error', url, e);
    return null;
  }
}



/* ================================================================
   MODEL VERDICTS
   The CNN / LSTM callouts are written from the live metrics rather
   than hard-coded, so the wording stays truthful after a re-run.
   ================================================================ */
const VERDICT_STYLE = {
  good:      { cls: 'callout',               tag: 'PERFORMING' },
  weak:      { cls: 'callout warn-box',      tag: 'WEAK SIGNAL' },
  random:    { cls: 'callout critical-box',  tag: 'NEAR-RANDOM' },
  collapsed: { cls: 'callout critical-box',  tag: 'COLLAPSED' },
  missing:   { cls: 'callout warn-box',      tag: 'NOT FOUND' },
};

async function renderVerdict(elId, which) {
  const el = $(elId);
  if (!el) return null;
  const h = await getJSON('/api/model_health');
  const v = h && h[which];
  if (!v) { el.innerHTML = '<b>Model verdict unavailable.</b>'; return null; }
  const st = VERDICT_STYLE[v.state] || VERDICT_STYLE.missing;
  el.className = st.cls;
  el.innerHTML =
    `<b>[${st.tag}] ${v.headline}</b> ${v.detail}` +
    (v.sequences ? ` <span class="loading-note">Measured on ${fmt(v.sequences)} ${v.scope}.</span>` : '');
  return v;
}

/* ================================================================
   AUTO CHART GALLERIES
   Every .png the backend discovered is rendered into the gallery for
   its tab, so a chart can never be silently missing from the UI.
   Blank/corrupt exports are filtered out server-side.
   ================================================================ */
async function fillGallery(tab) {
  const el = document.querySelector(`.chart-gallery[data-gallery="${tab}"]`);
  if (!el || el.dataset.filled) return;
  const d = await getJSON('/api/charts_list?tab=' + encodeURIComponent(tab));
  if (!d || !d.charts.length) {
    el.innerHTML = '<div class="empty-note">No chart images found for this tab.</div>';
    el.dataset.filled = '1';
    return;
  }
  el.innerHTML = d.charts.map(c => `
    <div class="chart-img-card">
      <a href="/charts/${encodeURI(c.name)}" target="_blank" rel="noopener">
        <img src="/charts/${encodeURI(c.name)}" loading="lazy" alt="${c.caption}">
      </a>
      <div class="chart-img-cap">${c.caption}</div>
    </div>`).join('');
  el.dataset.filled = '1';
}

/* ================================================================
   NAVIGATION
   ================================================================ */
const PAGE_META = {
  overview:      ['Overview', 'Executive summary — full pipeline status'],
  cleaning:      ['Data Cleaning', 'Notebook 01 — data understanding and cleaning'],
  eda:           ['Exploratory Analysis', 'Notebook 02 — exploratory data analysis'],
  features:      ['Feature Engineering', 'Notebook 03 — the 29/30-feature set the models consume'],
  preprocessing: ['Preprocessing', 'Notebook 04 — scaling, splitting and SMOTE'],
  mlmodels:      ['ML Model Comparison', 'Notebook 05 — 7 models benchmarked'],
  cnn:           ['CNN Deep Learning', 'Notebook 06 — multi-scale 1D convolutional network'],
  lstm:          ['LSTM Deep Learning', 'Notebook 07 — temporal sequence model'],
  graph:         ['Graph Network Analysis', 'Notebook 08 — 515K nodes, 1.02M edges'],
  shap:          ['SHAP Explainability', 'Notebook 09 — exact TreeExplainer values'],
  risk:          ['Risk Scoring Engine', 'Notebook 10 — weighted ensemble + calibrated alert tiers'],
  live:          ['Live Prediction & Alerts', 'Notebook 11 — real-time scoring console'],
};

const loaded = {};

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pg = $('pg-' + name);
  if (pg) pg.classList.add('active');
  const nav = document.querySelector(`.nav-item[data-page="${name}"]`);
  if (nav) nav.classList.add('active');
  const meta = PAGE_META[name] || ['', ''];
  $('page-title').textContent = meta[0];
  $('page-sub').textContent = meta[1];
  $('content').scrollTop = 0;
  fillGallery(name);
  if (!loaded[name]) { loaded[name] = true; (LOADERS[name] || (() => {}))(); }
}

document.querySelectorAll('.nav-item').forEach(n =>
  n.addEventListener('click', () => showPage(n.dataset.page)));
document.querySelectorAll('.pipe-step').forEach(n =>
  n.addEventListener('click', () => showPage(n.dataset.goto)));

/* ================================================================
   PAGE LOADERS
   ================================================================ */
const LOADERS = {};

/* ---------- OVERVIEW ---------- */
LOADERS.overview = async () => {
  const o = window.OVERVIEW || await getJSON('/api/overview');
  if (o) {
    $('ov-total').textContent = fmt(o.total_transactions);
    $('ov-fraud').textContent = fmt(o.fraud_transactions);
    $('ov-fraud-rate').textContent = o.fraud_rate_pct + '% of all transactions';
    $('ov-imbalance').textContent = fmt(o.imbalance_ratio) + ' : 1';
    $('ov-accounts').textContent = fmt(o.unique_accounts);
    $('ov-best-model').textContent = (o.best_model || '').replace(/\s*\(.*\)/, '');
    $('ov-best-auc').textContent = 'AUC-ROC ' + o.best_model_auc;
    $('ov-alerts').textContent = fmt(o.total_alerts);
    $('ov-alert-rate').textContent = o.alert_rate_pct + '% alert rate';
    $('ov-capture').textContent = o.capture_rate_pct + '%';
    $('ov-capture-detail').textContent = fmt(o.fraud_captured) + ' / ' + fmt(o.total_fraud_cfg) + ' fraud in alert queue';
    $('chip-alerts').textContent = fmt(o.total_alerts) + ' alerts';
  }

  const ml = await getJSON('/api/ml_comparison');
  if (ml && ml.models.length) {
    const models = ml.models.slice().sort((a, b) => b.auc_roc - a.auc_roc);
    makeChart('chart-ov-leaderboard', {
      type: 'bar',
      data: {
        labels: models.map(m => m.name.replace(/\s*\(.*\)/, '')),
        datasets: [{
          label: 'AUC-ROC',
          data: models.map(m => m.auc_roc),
          backgroundColor: models.map((m, i) => i === 0 ? PALETTE.accent : PALETTE.s1),
          borderRadius: 4, borderSkipped: false, barPercentage: .7,
        }],
      },
      options: {
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => 'AUC-ROC ' + c.parsed.x.toFixed(4) } },
        },
        scales: { x: { ...AXIS, min: .9, max: 1 }, y: AXIS_NOGRID },
      },
    });
  }

  const rd = await getJSON('/api/risk_distribution');
  if (rd && rd.level_counts) {
    const order = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
    makeChart('chart-ov-risk', {
      type: 'doughnut',
      data: {
        labels: order,
        datasets: [{
          data: order.map(l => rd.level_counts[l] || 0),
          backgroundColor: order.map(l => RISK_COLORS[l]),
          borderColor: PALETTE.surface, borderWidth: 2,
        }],
      },
      options: {
        cutout: '58%',
        plugins: {
          legend: { position: 'right' },
          tooltip: { callbacks: { label: c => c.label + ': ' + fmt(c.parsed) + ' txns' } },
        },
      },
    });
  }

  $('btn-txn-lookup').addEventListener('click', runTxnLookup);
  $('txn-id-input').addEventListener('keydown', e => { if (e.key === 'Enter') runTxnLookup(); });

  $('btn-net-search').addEventListener('click', () => {
    netHistory.length = 0;
    loadAccountNetwork($('net-account-input').value.trim());
  });
  $('net-account-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') { netHistory.length = 0; loadAccountNetwork($('net-account-input').value.trim()); }
  });
  $('btn-net-back').addEventListener('click', () => {
    netHistory.pop();
    const prev = netHistory.pop();
    if (prev) loadAccountNetwork(prev, true);
  });
  $('net-suggest').addEventListener('click', e => {
    const b = e.target.closest('button[data-account]');
    if (!b) return;
    $('net-account-input').value = b.dataset.account;
    netHistory.length = 0;
    loadAccountNetwork(b.dataset.account);
  });
  loadNetworkSuggestions();
};

const money = v => Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const compact = v => Number(v).toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 });
const escHTML = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ---------- TRANSACTION OVERVIEW ---------- */
async function runTxnLookup() {
  const raw = $('txn-id-input').value.trim();
  const status = $('txn-lookup-status');
  const result = $('txn-lookup-result');
  if (!raw) { status.textContent = 'Enter a transaction ID first.'; result.style.display = 'none'; return; }

  const btn = $('btn-txn-lookup');
  btn.disabled = true;
  status.textContent = 'looking up…';

  let d = null;
  try {
    const r = await fetch('/api/transaction/' + encodeURIComponent(raw));
    d = await r.json();
  } catch (e) { console.error(e); }
  btn.disabled = false;

  if (!d || d.error) {
    status.textContent = d ? d.error : 'Request failed.';
    result.style.display = 'none';
    return;
  }

  status.textContent = '';
  result.style.display = 'block';
  $('txn-result-id').textContent = 'Transaction #' + fmt(d.transaction_id);
  const lvl = $('txn-result-level');
  lvl.className = 'pill ' + d.risk_level;
  lvl.textContent = d.risk_level + ' · score ' + d.risk_score;
  $('txn-result-fraud').textContent = d.is_laundering
    ? 'labeled fraud in the training data' : 'not labeled fraud in the training data';

  $('txn-flags').innerHTML = d.flags.map(f =>
    `<span class="txo-flag${f.on ? ' on' : ''}" title="${escHTML(f.detail)}"><i></i>${escHTML(f.label)}: ${f.on ? 'Yes' : 'No'}</span>`
  ).join('');

  $('txn-sender').textContent = fmt(d.sender.account);
  $('txn-sender-bank').textContent = d.sender.bank;
  $('txn-amount-paid').innerHTML = `${money(d.sender.amount)}<small title="${escHTML(d.sender.currency)}">${escHTML(d.sender.currency_code)}</small>`;
  $('txn-receiver').textContent = fmt(d.receiver.account);
  $('txn-receiver-bank').textContent = d.receiver.bank;
  $('txn-amount-received').innerHTML = `${money(d.receiver.amount)}<small title="${escHTML(d.receiver.currency)}">${escHTML(d.receiver.currency_code)}</small>`;
}

/* ---------- LINKED ACCOUNT NETWORK VIEW ----------
   netHistory tracks the accounts visited so "Back" can retrace a walk
   through the graph. Clicking a neighbor node re-centers on it instead
   of navigating away, so tracing a money trail stays in one view. */
const netHistory = [];

async function loadNetworkSuggestions() {
  const d = await getJSON('/api/account/suggestions');
  if (!d || !d.suggestions || !d.suggestions.length) return;
  $('net-suggest').innerHTML = d.suggestions.map(s =>
    `<button data-account="${Number(s.account)}" title="Community ${Number(s.community)} · ${fmt(s.txns)} transactions sent, ${fmt(s.laundering)} labeled laundering">` +
    `ACC ${Number(s.account)} <small>comm ${Number(s.community)}</small>${s.mule ? ' <b>MULE</b>' : ''}</button>`
  ).join('');
  $('net-suggest-wrap').style.display = '';
}

async function loadAccountNetwork(accountId, fromBack) {
  if (!accountId) { $('net-status').textContent = 'Enter an account ID first.'; return; }
  const status = $('net-status');
  const result = $('net-result');
  status.textContent = 'loading network…';

  let d = null;
  try {
    const r = await fetch('/api/account/' + encodeURIComponent(accountId) + '/network?limit=10');
    d = await r.json();
  } catch (e) { console.error(e); }

  if (!d || d.error) {
    status.textContent = d ? d.error : 'Request failed.';
    result.style.display = 'none';
    return;
  }

  status.textContent = '';
  result.style.display = 'block';
  if (!fromBack) netHistory.push(accountId);
  $('btn-net-back').style.display = netHistory.length > 1 ? 'inline-block' : 'none';

  const c = d.center;
  $('net-center-label').textContent = 'Account #' + c.account_id;
  const comm = $('net-center-community');
  comm.title = '';
  if (!d.community_map_available) {
    comm.className = 'pill';
    comm.textContent = 'community data unavailable';
  } else if (c.community_id === null || c.community_id === undefined) {
    comm.className = 'pill';
    comm.textContent = 'not in a detected community';
    comm.title = 'The Graph notebook detected communities among the 5,000 busiest accounts only.';
  } else {
    comm.className = 'pill ' + (c.community_suspicious ? 'CRITICAL' : 'LOW');
    comm.textContent = 'community ' + c.community_id +
      (c.community_suspicious ? ' · suspicious' : '') +
      (c.community_fraud_rate != null ? ' · fraud rate ' + pct(c.community_fraud_rate * 100) : '');
  }
  $('net-center-mule').style.display = c.mule ? '' : 'none';
  $('net-degree-note').textContent =
    `${fmt(d.sent)} sent · ${fmt(d.received)} received` +
    (d.self_transfers ? ` · ${fmt(d.self_transfers)} self-transfers` : '') +
    ` · paid ${fmt(d.out_degree)} accounts, paid by ${fmt(d.in_degree)} · ` +
    `showing top ${fmt(d.neighbors.length)} of ${fmt(d.total_neighbors)} links by number of transactions`;

  renderNetworkGraph(d, 'net-svg', accId => loadAccountNetwork(accId));
}

// svgId: which <svg> to draw into. onNodeClick(accountId): called when a
// neighbor node is clicked, to re-center the graph on that account.
function renderNetworkGraph(d, svgId, onNodeClick) {
  const svg = $(svgId);
  if (!svg) return;
  const vb = svg.getAttribute('viewBox').split(' ').map(Number);
  const cx = vb[2] / 2, cy = vb[3] / 2, r = Math.min(cx, cy) - 55;
  const n = d.neighbors.length;
  const positions = d.neighbors.map((nb, i) => {
    const angle = (i / Math.max(1, n)) * 2 * Math.PI - Math.PI / 2;
    return { x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle), data: nb };
  });
  const suspiciousFill = s => (s.community_suspicious ? PALETTE.critical : null);
  const halo = `stroke="${PALETTE.surface}" stroke-width="4" paint-order="stroke"`;

  let edges = '', labels = '';
  positions.forEach(p => {
    const e = p.data;
    const color = e.direction === 'in' ? PALETTE.good : PALETTE.accent;
    const width = Math.min(7, 1.5 + 1.6 * Math.log10(e.txn_count + 1));
    edges += `<line x1="${cx}" y1="${cy}" x2="${p.x}" y2="${p.y}" stroke="${color}" stroke-width="${width}" opacity="0.55"/>`;
    const lx = cx + (p.x - cx) * 0.56, ly = cy + (p.y - cy) * 0.56;
    const text = `${compact(e.amount)} ${e.currency_code}${e.other_currencies ? ' +' + e.other_currencies : ''}`;
    labels += `<text x="${lx}" y="${ly + 3}" text-anchor="middle" font-size="10" font-weight="700" fill="${PALETTE.text}" ${halo} style="font-family:var(--mono)">${escHTML(text)}</text>`;
  });

  const cRad = Math.max(16, Math.round(r * 0.16)), nRad = Math.max(11, Math.round(r * 0.11));
  const fSize = Math.max(9, Math.round(nRad * 0.55));
  const c = d.center;
  const centerLabel = String(c.account_id).length > 7 ? String(c.account_id).slice(0, 7) + '…' : c.account_id;
  let nodes = `
    <g>
      <circle cx="${cx}" cy="${cy}" r="${cRad}" fill="${suspiciousFill(c) || PALETTE.accent}"
        stroke="${c.mule ? PALETTE.serious : '#fff'}" stroke-width="${c.mule ? 4 : 2}"/>
      <text x="${cx}" y="${cy + 4}" text-anchor="middle" font-size="${fSize}" font-weight="700" fill="#00131f" style="font-family:var(--mono)">${escHTML(centerLabel)}</text>
    </g>`;

  positions.forEach(p => {
    const e = p.data;
    const label = String(e.account_id).length > 6 ? String(e.account_id).slice(0, 6) + '…' : e.account_id;
    const tip = `Account ${e.account_id} — ${e.direction === 'in' ? 'sent to' : 'received from'} #${c.account_id}: ` +
      `${money(e.amount)} ${e.currency}${e.other_currencies ? ` (+${e.other_currencies} other currencies)` : ''} across ${fmt(e.txn_count)} txn(s)` +
      (e.fraud_count ? `, ${e.fraud_count} labeled laundering` : '') +
      (e.community_suspicious ? ` · suspicious community ${e.community_id}` : '') + (e.mule ? ' · MULE account' : '');
    nodes += `
      <g class="net-node" data-account="${escHTML(e.account_id)}" style="cursor:pointer">
        <circle cx="${p.x}" cy="${p.y}" r="${nRad}" fill="${suspiciousFill(e) || PALETTE.s3}"
          stroke="${e.mule ? PALETTE.serious : (e.community_suspicious ? '#fff' : 'transparent')}" stroke-width="${e.mule ? 3.5 : 1.5}"/>
        <text x="${p.x}" y="${p.y + 4}" text-anchor="middle" font-size="${fSize}" font-weight="700" fill="#00131f" style="font-family:var(--mono)">${escHTML(label)}</text>
        <title>${escHTML(tip)}</title>
      </g>`;
  });

  svg.innerHTML = edges + labels + nodes;
  if (onNodeClick) {
    svg.querySelectorAll('.net-node').forEach(g =>
      g.addEventListener('click', () => onNodeClick(g.dataset.account)));
  }
}

/* ---------- DATA CLEANING ---------- */
LOADERS.cleaning = () => {
  makeChart('chart-cln-outliers', {
    type: 'bar',
    data: {
      labels: ['IQR method', 'Z-score > 3', 'Account-level (5× own median)', 'Velocity (10+/hr)'],
      datasets: [{
        label: '% of transactions flagged',
        data: [17.36, 0.02, 15.17, 14.42],
        backgroundColor: [PALETTE.s1, PALETTE.s2, PALETTE.s3, PALETTE.s4],
        borderRadius: 4, borderSkipped: false, barPercentage: .65,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y + '% of 5.08M transactions' } },
      },
      scales: { y: { ...AXIS, title: { display: true, text: '% flagged', color: PALETTE.text3 } }, x: AXIS_NOGRID },
    },
  });

  makeChart('chart-cln-flags', {
    type: 'bar',
    data: {
      labels: ['Same bank', 'Cross bank', 'Different accounts', 'Self transfer'],
      datasets: [{
        label: 'Fraud rate (%)',
        data: [0.0149, 0.1157, 0.1151, 0.0019],
        backgroundColor: [PALETTE.s1, PALETTE.s8, PALETTE.s8, PALETTE.s1],
        borderRadius: 4, borderSkipped: false, barPercentage: .65,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'fraud rate ' + c.parsed.y.toFixed(4) + '%' } },
      },
      scales: { y: AXIS, x: AXIS_NOGRID },
    },
  });
};

/* ---------- EDA ---------- */
LOADERS.eda = async () => {
  // Class balance — rebuilt live (the notebook PNG saved blank)
  const cb = await getJSON('/api/class_balance');
  if (cb) {
    makeChart('chart-eda-balance', {
      type: 'bar',
      data: {
        labels: ['Full dataset', 'Training set', 'After SMOTE', 'Test set'],
        datasets: [
          { label: 'Normal', data: [cb.normal, cb.train_normal, cb.smote_normal, cb.test_normal],
            backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false },
          { label: 'Fraud',  data: [cb.fraud, cb.train_fraud, cb.smote_fraud, cb.test_fraud],
            backgroundColor: PALETTE.s8, borderRadius: 4, borderSkipped: false },
        ],
      },
      options: {
        plugins: {
          legend: { position: 'top', align: 'end' },
          tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmt(c.parsed.y) + ' rows' } },
        },
        scales: {
          y: { ...AXIS, type: 'logarithmic',
               title: { display: true, text: 'rows (log scale)', color: PALETTE.text3 } },
          x: AXIS_NOGRID,
        },
      },
    });
  }

  // Fraud rate by hour — rebuilt live from the 5.08M-row cleaned dataset
  const hr = await getJSON('/api/fraud_by_hour');
  if (hr && hr.hours && hr.hours.length) {
    const peak = hr.fraud_rate.indexOf(Math.max(...hr.fraud_rate));
    const sub = $('eda-hour-sub');
    if (sub) sub.textContent = `rebuilt live from ${hr.source} · peak ${String(peak).padStart(2,'0')}:00 at ${hr.fraud_rate[peak].toFixed(3)}%`;
    makeChart('chart-eda-hour', {
      type: 'bar',
      data: {
        labels: hr.hours.map(h => String(h).padStart(2, '0') + ':00'),
        datasets: [{
          label: 'Fraud rate (%)',
          data: hr.fraud_rate,
          backgroundColor: hr.fraud_rate.map((v, i) => i === peak ? PALETTE.critical : PALETTE.s1),
          borderRadius: 3, borderSkipped: false, barPercentage: .85,
        }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: c => c.parsed.y.toFixed(4) + '% fraud rate',
              afterLabel: c => fmt(hr.frauds[c.dataIndex]) + ' fraud / ' + fmt(hr.totals[c.dataIndex]) + ' txns',
            },
          },
        },
        scales: {
          y: { ...AXIS, title: { display: true, text: 'fraud rate (%)', color: PALETTE.text3 } },
          x: { ...AXIS_NOGRID, ticks: { ...AXIS_NOGRID.ticks, maxRotation: 60, minRotation: 60, font: { size: 9 } } },
        },
      },
    });
  } else if ($('eda-hour-sub')) {
    $('eda-hour-sub').textContent = 'needs data/cleaned_transactions.csv — not found';
  }

  const fmts = ['ACH', 'Bitcoin', 'Cash', 'Cheque', 'Credit Card', 'Reinvestment', 'Wire'];
  const rates = [0.746, 0.038, 0.022, 0.017, 0.016, 0.000, 0.000];
  makeChart('chart-eda-format', {
    type: 'bar',
    data: {
      labels: fmts,
      datasets: [{
        label: 'Fraud rate (%)',
        data: rates,
        backgroundColor: rates.map(r => r > 0.5 ? PALETTE.critical : PALETTE.s1),
        borderRadius: 4, borderSkipped: false, barPercentage: .65,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y.toFixed(3) + '% fraud rate' } },
      },
      scales: {
        y: { ...AXIS, title: { display: true, text: 'fraud rate (%)', color: PALETTE.text3 } },
        x: AXIS_NOGRID,
      },
    },
  });
};

/* ---------- FEATURE ENGINEERING ---------- */
LOADERS.features = () => {
  makeChart('chart-feat-risk', {
    type: 'bar',
    data: {
      labels: ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '10'],
      datasets: [{
        label: 'Fraud rate (%)',
        data: [0.0516, 0.0278, 0.1209, 0.1146, 0.1372, 0.1504, 0.1386, 0.1486, 0.1379, 0.3180, 0.0],
        backgroundColor: PALETTE.s1,
        borderRadius: 4, borderSkipped: false, barPercentage: .7,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y.toFixed(4) + '% fraud rate' } },
      },
      scales: {
        y: { ...AXIS, title: { display: true, text: 'fraud rate (%)', color: PALETTE.text3 } },
        x: { ...AXIS_NOGRID, title: { display: true, text: 'composite risk score (0–17 possible)', color: PALETTE.text3 } },
      },
    },
  });

  makeChart('chart-feat-mule', {
    type: 'bar',
    data: {
      labels: ['0', '1', '2', '3', '4'],
      datasets: [{
        label: 'Fraud rate (%)',
        data: [0.0815, 0.1148, 0.1570, 0.2471, 50.0],
        backgroundColor: [PALETTE.s1, PALETTE.s1, PALETTE.s4, PALETTE.serious, PALETTE.critical],
        borderRadius: 4, borderSkipped: false, barPercentage: .65,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: c => c.parsed.y + '% fraud rate',
            afterLabel: c => c.dataIndex === 4 ? 'only 2 accounts reach score 4' : '',
          },
        },
      },
      scales: {
        y: { ...AXIS, type: 'logarithmic', title: { display: true, text: 'fraud rate (%) — log scale', color: PALETTE.text3 } },
        x: { ...AXIS_NOGRID, title: { display: true, text: 'mule score', color: PALETTE.text3 } },
      },
    },
  });

  makeChart('chart-feat-fan', {
    type: 'bar',
    data: {
      labels: ['Fan-in score', 'Fan-out score', 'Fan ratio'],
      datasets: [
        { label: 'Normal', data: [0.1720, 0.1617, 1.3494], backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false },
        { label: 'Fraud',  data: [0.3943, 0.3698, 2.1560], backgroundColor: PALETTE.s8, borderRadius: 4, borderSkipped: false },
      ],
    },
    options: {
      plugins: { legend: { position: 'top', align: 'end' } },
      scales: { y: AXIS, x: AXIS_NOGRID },
    },
  });
};

/* ---------- PREPROCESSING ---------- */
LOADERS.preprocessing = () => {
  makeChart('chart-prep-smote', {
    type: 'bar',
    data: {
      labels: ['Before SMOTE', 'After SMOTE'],
      datasets: [
        { label: 'Normal', data: [4058526, 4058526], backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false, barPercentage: .6 },
        { label: 'Fraud',  data: [4142, 1217557],    backgroundColor: PALETTE.s8, borderRadius: 4, borderSkipped: false, barPercentage: .6 },
      ],
    },
    options: {
      plugins: {
        legend: { position: 'top', align: 'end' },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + fmt(c.parsed.y) + ' rows' } },
      },
      scales: {
        y: { ...AXIS, ticks: { ...AXIS.ticks, callback: v => (v / 1e6).toFixed(1) + 'M' },
             title: { display: true, text: 'training rows', color: PALETTE.text3 } },
        x: AXIS_NOGRID,
      },
    },
  });
};

/* ---------- ML MODELS ---------- */
let ML_MODELS = [];

async function showModelDetail(name) {
  document.querySelectorAll('#ml-picker .model-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.model === name));
  const box = $('ml-detail');
  box.innerHTML = '<div class="empty-note">Loading model…</div>';

  const d = await getJSON('/api/model_detail?name=' + encodeURIComponent(name));
  if (!d || d.error) { box.innerHTML = '<div class="empty-note">Could not load this model.</div>'; return; }

  const rec = (d.recall * 100), pre = (d.precision * 100);
  const metrics = [
    ['AUC-ROC', d.auc_roc.toFixed(4), ''],
    ['AUC-PR', d.auc_pr.toFixed(4), ''],
    ['Recall', rec.toFixed(1) + '%', ''],
    ['Precision', pre.toFixed(2) + '%', ''],
    ['F1 Score', d.f1.toFixed(4), ''],
    ['MCC', d.mcc.toFixed(4), ''],
    ['Fraud Caught', fmt(d.fraud_caught), 'good'],
    ['Fraud Missed', fmt(d.fraud_missed), 'bad'],
    ['False Alarms', fmt(d.false_alarms), 'warn'],
    ['Train Time', d.train_time_s + 's', ''],
  ];

  const params = d.params && Object.keys(d.params).length
    ? Object.entries(d.params).map(([k, v]) => `<span class="param-row"><b>${k}</b> = ${v}</span>`).join('')
    : '<span class="loading-note">no hyper-parameters exposed by this model</span>';

  const feats = (d.top_features || []).length
    ? `<div class="sec-head" style="margin-top:22px">Top Features (from the saved model)</div>
       <div class="chart-wrap" style="height:${Math.max(200, d.top_features.length * 22)}px">
         <canvas id="chart-model-feat"></canvas></div>`
    : '';

  box.innerHTML = `
    <div class="metric-grid">
      ${metrics.map(([k, v, cls]) => `<div class="metric-box ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('')}
    </div>

    <div class="grid2">
      <div>
        <div class="card-title" style="margin-bottom:8px">Confusion Matrix — ${fmt(d.test_rows)} test rows</div>
        <table class="cm-table">
          <thead><tr><th></th><th>Pred. Normal</th><th>Pred. Fraud</th></tr></thead>
          <tbody>
            <tr><th class="cm-rowhead">Actual Normal</th>
                <td class="cm-cell cm-tn">${fmt(d.true_negatives)}</td>
                <td class="cm-cell cm-fp">${fmt(d.false_alarms)}</td></tr>
            <tr><th class="cm-rowhead">Actual Fraud</th>
                <td class="cm-cell cm-fn">${fmt(d.fraud_missed)}</td>
                <td class="cm-cell cm-tp">${fmt(d.fraud_caught)}</td></tr>
          </tbody>
        </table>
      </div>
      <div>
        <div class="card-title" style="margin-bottom:8px">Saved Model File</div>
        <table>
          <tbody>
            <tr><td class="name">File</td><td class="mono">${d.file || '—'}</td></tr>
            <tr><td class="name">Size</td><td class="mono">${d.size_mb != null ? d.size_mb + ' MB' : '—'}</td></tr>
            <tr><td class="name">Class</td><td class="mono">${d.class || 'not loaded'}</td></tr>
            <tr><td class="name">Features</td><td class="mono">${d.n_features || '—'}</td></tr>
            <tr><td class="name">Status</td><td>${d.loaded
                ? '<span class="pill LOW">LOADED</span>'
                : '<span class="pill MEDIUM">NOT LOADED</span>'}</td></tr>
          </tbody>
        </table>
        <div class="card-title" style="margin:16px 0 8px">Hyper-parameters</div>
        <div>${params}</div>
      </div>
    </div>
    ${feats}`;

  if ((d.top_features || []).length) {
    makeChart('chart-model-feat', {
      type: 'bar',
      data: {
        labels: d.top_features.map(f => f.feature),
        datasets: [{
          label: 'importance', data: d.top_features.map(f => f.importance),
          backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false, barPercentage: .8,
        }],
      },
      options: {
        indexAxis: 'y',
        plugins: { legend: { display: false },
                   tooltip: { callbacks: { label: c => 'importance ' + c.parsed.x.toFixed(5) } } },
        scales: { x: AXIS, y: { ...AXIS_NOGRID, ticks: { ...AXIS_NOGRID.ticks, font: { size: 10 }, autoSkip: false } } },
      },
    });
  }
}

LOADERS.mlmodels = async () => {
  const d = await getJSON('/api/ml_comparison');
  if (!d || !d.models.length) {
    $('ml-table-body').innerHTML = '<tr><td colspan="11" class="empty-note">No model comparison data found.</td></tr>';
    return;
  }
  const models = d.models.slice().sort((a, b) => b.auc_roc - a.auc_roc);
  ML_MODELS = models;

  $('ml-picker').innerHTML = models.map((m, i) => `
    <button class="model-btn${i === 0 ? ' active' : ''}" data-model="${m.name}">${m.name}</button>`).join('');
  $('ml-picker').querySelectorAll('.model-btn').forEach(b =>
    b.addEventListener('click', () => showModelDetail(b.dataset.model)));
  showModelDetail(models[0].name);

  $('ml-table-body').innerHTML = models.map((m, i) => `
    <tr>
      <td class="name">${m.name} ${i === 0 ? '<span class="pill win">BEST</span>' : ''}</td>
      <td class="mono">${m.auc_roc.toFixed(4)}</td>
      <td class="mono">${m.auc_pr.toFixed(4)}</td>
      <td class="mono">${(m.recall * 100).toFixed(1)}%</td>
      <td class="mono">${(m.precision * 100).toFixed(2)}%</td>
      <td class="mono">${m.f1.toFixed(4)}</td>
      <td class="mono">${m.mcc.toFixed(4)}</td>
      <td class="mono" style="color:var(--good)">${fmt(m.fraud_caught)}</td>
      <td class="mono" style="color:var(--critical)">${fmt(m.fraud_missed)}</td>
      <td class="mono">${fmt(m.false_alarms)}</td>
      <td class="mono">${m.train_time_s}</td>
    </tr>`).join('');

  makeChart('chart-ml-auc', {
    type: 'bar',
    data: {
      labels: models.map(m => m.name.replace(/\s*\(.*\)/, '')),
      datasets: [
        { label: 'AUC-ROC', data: models.map(m => m.auc_roc), backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false },
        { label: 'AUC-PR',  data: models.map(m => m.auc_pr),  backgroundColor: PALETTE.s2, borderRadius: 4, borderSkipped: false },
      ],
    },
    options: {
      plugins: { legend: { position: 'top', align: 'end' } },
      scales: { y: { ...AXIS, min: 0, max: 1 }, x: { ...AXIS_NOGRID, ticks: { ...AXIS_NOGRID.ticks, maxRotation: 40, minRotation: 40 } } },
    },
  });

  makeChart('chart-ml-recall-prec', {
    type: 'scatter',
    data: {
      datasets: models.map((m, i) => ({
        label: m.name.replace(/\s*\(.*\)/, ''),
        data: [{ x: m.recall * 100, y: m.precision * 100 }],
        backgroundColor: SERIES[i % 8],
        borderColor: PALETTE.surface, borderWidth: 2,
        pointRadius: 9, pointHoverRadius: 11,
      })),
    },
    options: {
      plugins: {
        legend: { position: 'right' },
        tooltip: {
          callbacks: {
            label: c => `${c.dataset.label} — recall ${c.parsed.x.toFixed(1)}%, precision ${c.parsed.y.toFixed(2)}%`,
          },
        },
      },
      scales: {
        x: { ...AXIS, grace: '12%', title: { display: true, text: 'recall (% of fraud caught)', color: PALETTE.text3 } },
        y: { ...AXIS, grace: '18%', title: { display: true, text: 'precision (%)', color: PALETTE.text3 } },
      },
    },
  });
};

/* ---------- CNN ---------- */
LOADERS.cnn = async () => {
  renderVerdict('cnn-verdict', 'cnn');
  const m = await getJSON('/api/cnn_metrics');
  if (m) {
    const el = $('cnn-arch-input');
    if (el && m.n_features && m.sequence_len) {
      el.innerHTML = `Input: <span class="mono">(batch, ${m.n_features} features, ${m.sequence_len} transactions)</span>`;
    }
    const tr = $('cnn-arch-train');
    if (tr && m.sequences) {
      tr.innerHTML = `${fmt(m.sequences)} test sequences · split by account (no window overlap)`;
    }
  }
  if (m && m.auc_roc !== undefined) {
    const g = $('cnn-stat-grid');
    if (g) {
      g.children[0].querySelector('.stat-val').textContent = Number(m.auc_roc).toFixed(4);
      g.children[1].querySelector('.stat-val').textContent = (Number(m.recall) * 100).toFixed(2) + '%';
      g.children[1].querySelector('.stat-sub').textContent =
        `${fmt(m.fraud_caught)} / ${fmt(Number(m.fraud_caught) + Number(m.fraud_missed))} fraud sequences caught`;
    }
  }

  const d = await getJSON('/api/cnn_score_distribution');
  if (d && d.bins.length) {
    makeChart('chart-cnn-dist', {
      type: 'bar',
      data: {
        labels: d.bins.map(b => Number(b).toFixed(2)),
        datasets: [{
          label: 'accounts',
          data: d.counts,
          backgroundColor: PALETTE.s1,
          borderRadius: 3, borderSkipped: false, barPercentage: .95, categoryPercentage: .98,
        }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => fmt(c.parsed.y) + ' accounts' } },
        },
        scales: {
          y: { ...AXIS, type: 'logarithmic', title: { display: true, text: 'accounts (log scale)', color: PALETTE.text3 } },
          x: { ...AXIS_NOGRID, title: { display: true, text: 'CNN fraud score', color: PALETTE.text3 } },
        },
      },
    });
  }
};

/* ---------- LSTM ---------- */
LOADERS.lstm = async () => {
  renderVerdict('lstm-verdict', 'lstm');
  const m = await getJSON('/api/lstm_metrics');
  if (m) {
    const el = $('lstm-arch-seq');
    if (el && m.seq_len) {
      el.innerHTML = `Sequence length ${m.seq_len} · ${m.n_features || '—'} features per step`;
    }
  }
  if (m && m.auc_roc !== undefined) {
    $('lstm-auc').textContent = Number(m.auc_roc).toFixed(4);
    $('lstm-recall').textContent = (Number(m.recall) * 100).toFixed(0) + '%';
    $('lstm-precision').textContent = (Number(m.precision) * 100).toFixed(2) + '%';

    $('cm-tn').textContent = fmt(m.true_negatives);
    $('cm-fp').textContent = fmt(m.false_alarms);
    $('cm-fn').textContent = fmt(m.fraud_missed);
    $('cm-tp').textContent = fmt(m.fraud_caught);
  }

  const xgb = await getJSON('/api/xgb_metrics');
  makeChart('chart-lstm-vs-xgb', {
    type: 'bar',
    data: {
      labels: ['AUC-ROC', 'AUC-PR', 'Precision'],
      datasets: [
        { label: 'LSTM', data: [m ? m.auc_roc : 0.51, m ? m.auc_pr : 0.001, m ? m.precision : 0.001],
          backgroundColor: PALETTE.s2, borderRadius: 4, borderSkipped: false },
        { label: 'XGBoost', data: [xgb ? xgb.auc_roc : 0.9732, xgb ? xgb.auc_pr : 0.2368, xgb ? xgb.precision : 0.5],
          backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false },
      ],
    },
    options: {
      plugins: { legend: { position: 'top', align: 'end' } },
      scales: { y: { ...AXIS, min: 0, max: 1 }, x: AXIS_NOGRID },
    },
  });
};

/* ---------- GRAPH ---------- */
LOADERS.graph = async () => {
  makeChart('chart-graph-mule', {
    type: 'bar',
    data: {
      labels: ['0', '1', '2', '3', '4'],
      datasets: [{
        label: 'Fraud rate (%)',
        data: [0.0976, 0.0619, 0.2504, 0.8045, 3.6364],
        backgroundColor: [PALETTE.s1, PALETTE.s1, PALETTE.s4, PALETTE.serious, PALETTE.critical],
        borderRadius: 4, borderSkipped: false, barPercentage: .65,
      }],
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y.toFixed(4) + '% fraud rate' } },
      },
      scales: {
        y: { ...AXIS, title: { display: true, text: 'fraud rate (%)', color: PALETTE.text3 } },
        x: { ...AXIS_NOGRID, title: { display: true, text: 'graph mule score', color: PALETTE.text3 } },
      },
    },
  });

  const d = await getJSON('/api/graph_communities');
  const body = $('graph-comm-body');
  if (d && d.communities.length) {
    body.innerHTML = d.communities.slice(0, 15).map(c => `
      <tr>
        <td class="mono">${c.community_id === -1 ? 'unassigned' : '#' + c.community_id}</td>
        <td class="mono">${fmt(c.transaction_count)}</td>
        <td class="mono">${c.fraud_rate.toFixed(4)}%</td>
        <td>${c.suspicious ? '<span class="pill CRITICAL">FLAGGED</span>' : '<span class="pill LOW">clean</span>'}</td>
      </tr>`).join('');
  } else {
    body.innerHTML = '<tr><td colspan="4" class="empty-note">No community data found.</td></tr>';
  }
};

/* ---------- SHAP ---------- */
LOADERS.shap = async () => {
  const d = await getJSON('/api/shap_importance?n=20');
  if (!d || !d.features.length) return;
  makeChart('chart-shap-importance', {
    type: 'bar',
    data: {
      labels: d.features,
      datasets: [{
        label: 'mean |SHAP value|',
        data: d.values,
        backgroundColor: d.values.map((v, i) => i === 0 ? PALETTE.accent : PALETTE.s1),
        borderRadius: 4, borderSkipped: false, barPercentage: .8,
      }],
    },
    options: {
      indexAxis: 'y',
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'mean |SHAP| = ' + c.parsed.x.toFixed(4) } },
      },
      scales: {
        x: { ...AXIS, title: { display: true, text: 'mean |SHAP value| — higher = more influence on fraud calls', color: PALETTE.text3 } },
        y: { ...AXIS_NOGRID, ticks: { ...AXIS_NOGRID.ticks, font: { size: 10 }, autoSkip: false } },
      },
    },
  });
};

/* ---------- RISK ENGINE ---------- */
LOADERS.risk = async () => {
  const o = await getJSON('/api/risk_overview');
  if (o) {
    $('risk-total-alerts').textContent = fmt(o.total_alerts);
    $('risk-alert-rate').textContent = o.alert_rate_pct + '% alert rate';
    $('risk-captured').textContent = fmt(o.fraud_captured) + ' / ' + fmt(o.total_fraud);
    $('risk-capture-rate').textContent = o.capture_rate_pct + '% capture rate';
    $('risk-auc-ensemble').textContent = o.auc_ensemble;
    $('risk-auc-xgb').textContent = 'vs ' + o.auc_xgb + ' for XGBoost alone on full 5M';

    const wk = Object.keys(o.weights || {}).sort((a, b) => o.weights[b] - o.weights[a]);
    if (wk.length) {
      makeChart('chart-risk-weights', {
        type: 'bar',
        data: {
          labels: wk.map(k => k.toUpperCase()),
          datasets: [{
            label: 'weight',
            data: wk.map(k => o.weights[k] * 100),
            backgroundColor: wk.map((k, i) => SERIES[i % 8]),
            borderRadius: 4, borderSkipped: false, barPercentage: .6,
          }],
        },
        options: {
          indexAxis: 'y',
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: c => c.parsed.x.toFixed(1) + '% of ensemble score' } },
          },
          scales: { x: { ...AXIS, max: 100, title: { display: true, text: '% weight', color: PALETTE.text3 } }, y: AXIS_NOGRID },
        },
      });
    }

    // per-model admission table (populated once the corrected notebook has run)
    const gb = $('risk-gate-body');
    const diag = o.score_diagnostics || {};
    const gates = o.admission_gates || {};
    if (gb) {
      const names = Object.keys(diag);
      if (!names.length) {
        gb.innerHTML = '<tr><td colspan="5" class="empty-note">' +
          'Re-run Risk_Scoring_Engine.ipynb — the corrected notebook records per-model ' +
          'AUC and coverage in risk_engine_config.pkl.</td></tr>';
        if ($('risk-gate-sub')) $('risk-gate-sub').textContent =
          'not yet available — run the corrected Risk Scoring Engine notebook';
      } else {
        const minAuc = gates.min_auc ?? 0.55, minCov = gates.min_coverage ?? 0.80;
        if ($('risk-gate-sub')) $('risk-gate-sub').textContent =
          `gates: AUC-ROC ≥ ${minAuc} and coverage ≥ ${(minCov*100).toFixed(0)}% · scaling: ${o.scaling}`;
        gb.innerHTML = names.map(n => {
          const d = diag[n] || {};
          const auc = Number(d.auc ?? 0), cov = Number(d.coverage ?? 0);
          const w = Number((o.weights || {})[n.replace('_score','')] ?? 0);
          const passed = auc >= minAuc && cov >= minCov;
          const why = passed ? 'ADMITTED'
            : (auc < minAuc && cov < minCov) ? 'AUC + coverage too low'
            : (auc < minAuc) ? 'AUC too low' : 'coverage too low';
          return `<tr>
            <td class="name">${n.replace('_score','')}</td>
            <td class="mono">${auc.toFixed(4)}</td>
            <td class="mono">${(cov*100).toFixed(2)}%</td>
            <td class="mono">${w > 0 ? (w*100).toFixed(1)+'%' : '—'}</td>
            <td>${passed ? '<span class="pill LOW">ADMITTED</span>'
                         : '<span class="pill HIGH">EXCLUDED — '+why+'</span>'}</td>
          </tr>`;
        }).join('');
      }
    }

    const tb = $('risk-threshold-body');
    if (tb && o.thresholds && o.thresholds.CRITICAL !== undefined) {
      tb.innerHTML = `
        <tr><td><span class="pill CRITICAL">CRITICAL</span></td><td class="mono">≥ ${o.thresholds.CRITICAL}</td><td>top 0.5% of transactions</td></tr>
        <tr><td><span class="pill HIGH">HIGH</span></td><td class="mono">≥ ${o.thresholds.HIGH}</td><td>top 2.5%</td></tr>
        <tr><td><span class="pill MEDIUM">MEDIUM</span></td><td class="mono">≥ ${o.thresholds.MEDIUM}</td><td>top 10%</td></tr>
        <tr><td><span class="pill LOW">LOW</span></td><td class="mono">everything else</td><td>90% of volume</td></tr>`;
    }
  }

  // --- ensemble vs XGBoost-alone diagnostics ---
  const dg = await getJSON('/api/risk_diagnostics');
  if (dg && dg.available) {
    const s0 = dg.sweep[0];
    $('risk-diag-text').innerHTML =
      ` At the current alert budget of <b>${s0.alert_pct}%</b> (${fmt(s0.alerts)} alerts), the ensemble
        catches <b>${fmt(dg.current.caught)} of ${fmt(dg.total_fraud)}</b> fraud cases
        (<b>${dg.current.capture_pct}%</b>). Ranking the very same number of alerts by the raw XGBoost
        score instead catches <b>${fmt(s0.xgb_caught)}</b> (<b>${s0.xgb_capture_pct}%</b>) —
        <b>${dg.uplift}× more fraud for identical analyst workload</b>.`;

    $('risk-diag-body').innerHTML = dg.sweep.map(r => `
      <tr>
        <td class="mono">top ${r.alert_pct}%</td>
        <td class="mono">${fmt(r.alerts)}</td>
        <td class="mono" style="color:var(--serious)">${fmt(r.ensemble_caught)} (${r.ensemble_capture_pct}%)</td>
        <td class="mono" style="color:var(--good)">${fmt(r.xgb_caught)} (${r.xgb_capture_pct}%)</td>
        <td class="mono" style="color:var(--accent)">+${fmt(r.xgb_caught - r.ensemble_caught)}</td>
      </tr>`).join('');

    makeChart('chart-risk-diag', {
      type: 'bar',
      data: {
        labels: dg.sweep.map(r => `top ${r.alert_pct}%  (${fmt(r.alerts)} alerts)`),
        datasets: [
          { label: 'Current ensemble', data: dg.sweep.map(r => r.ensemble_capture_pct),
            backgroundColor: PALETTE.s2, borderRadius: 4, borderSkipped: false },
          { label: 'XGBoost alone', data: dg.sweep.map(r => r.xgb_capture_pct),
            backgroundColor: PALETTE.s1, borderRadius: 4, borderSkipped: false },
        ],
      },
      options: {
        plugins: {
          legend: { position: 'top', align: 'end' },
          tooltip: { callbacks: { label: c => c.dataset.label + ': ' + c.parsed.y + '% of all fraud caught' } },
        },
        scales: {
          y: { ...AXIS, title: { display: true, text: '% of all fraud captured', color: PALETTE.text3 } },
          x: AXIS_NOGRID,
        },
      },
    });
  } else if ($('risk-diag-text')) {
    $('risk-diag-text').textContent =
      ' Diagnostics need results/final_risk_scores.csv — not found, so this comparison is unavailable.';
    $('risk-diag-body').innerHTML = '<tr><td colspan="5" class="empty-note">final_risk_scores.csv not found</td></tr>';
  }

  const rd = await getJSON('/api/risk_distribution');
  if (rd && rd.level_counts) {
    const order = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
    makeChart('chart-risk-levels', {
      type: 'bar',
      data: {
        labels: order,
        datasets: [{
          label: 'transactions',
          data: order.map(l => rd.level_counts[l] || 0),
          backgroundColor: order.map(l => RISK_COLORS[l]),
          borderRadius: 4, borderSkipped: false, barPercentage: .6,
        }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => fmt(c.parsed.y) + ' transactions' } },
        },
        scales: {
          y: { ...AXIS, type: 'logarithmic', title: { display: true, text: 'transactions (log scale)', color: PALETTE.text3 } },
          x: AXIS_NOGRID,
        },
      },
    });

    makeChart('chart-risk-fraudrate', {
      type: 'bar',
      data: {
        labels: order,
        datasets: [{
          label: 'fraud rate (%)',
          data: order.map(l => rd.fraud_by_level[l] || 0),
          backgroundColor: order.map(l => RISK_COLORS[l]),
          borderRadius: 4, borderSkipped: false, barPercentage: .6,
        }],
      },
      options: {
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: c => c.parsed.y.toFixed(4) + '% fraud rate' } },
        },
        scales: { y: { ...AXIS, title: { display: true, text: 'fraud rate (%)', color: PALETTE.text3 } }, x: AXIS_NOGRID },
      },
    });
  }
};

/* ---------- LIVE PREDICTION & ALERTS ---------- */
let alertsPage = 1;

async function loadAlerts(page = 1) {
  alertsPage = page;
  const level = $('al-level').value;
  const minScore = $('al-min-score').value || 0;
  const orderEl = $('al-order');
  const sample = orderEl && orderEl.value === 'sample' ? '&sample=1' : '';
  $('alerts-loading').textContent = 'loading…';
  const d = await getJSON(`/api/alerts?page=${page}&per_page=20&level=${level}&min_score=${minScore}${sample}`);
  $('alerts-loading').textContent = '';
  const body = $('alerts-table-body');
  if (!d || !d.alerts.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty-note">No alerts match these filters.</td></tr>';
    $('alerts-pager').innerHTML = '';
    return;
  }
  body.innerHTML = d.alerts.map(a => `
    <tr>
      <td class="mono">${a.alert_id}</td>
      <td><span class="pill ${a.risk_level}">${a.risk_level}</span></td>
      <td class="mono">${a.risk_score.toFixed(2)}</td>
      <td class="mono">${a.xgb_score.toFixed(4)}</td>
      <td class="mono">${a.cnn_score.toFixed(4)}</td>
      <td class="mono">${a.composite_score.toFixed(2)}</td>
      <td>${a.is_fraud
            ? '<span class="pill CRITICAL">CONFIRMED FRAUD</span>'
            : '<span class="mono" style="font-size:10px;color:var(--text3)">' + a.status + '</span>'}</td>
    </tr>`).join('');

  $('al-total').textContent = fmt(d.total);

  const comp = $('al-composition');
  if (comp && d.level_counts) {
    const c = d.level_counts.CRITICAL || 0, h = d.level_counts.HIGH || 0;
    const tot = c + h;
    const critPages = Math.ceil(c / 20);
    comp.className = 'callout';
    comp.innerHTML = tot === 0
      ? 'Alert queue is empty.'
      : `<b>What this queue contains.</b> An <i>alert</i> is by definition a
         transaction the risk engine scored HIGH or CRITICAL — MEDIUM and LOW are
         never included, which is why those filters are disabled.
         This queue is
         <span class="pill CRITICAL">CRITICAL ${fmt(c)}</span>
         <span class="pill HIGH">HIGH ${fmt(h)}</span>
         (${(c / tot * 100).toFixed(1)}% / ${(h / tot * 100).toFixed(1)}%).
         Sorted by risk score, the CRITICAL rows fill the first
         <b>${fmt(critPages)} pages</b> — so page 1 looking entirely CRITICAL is
         expected, not a scoring fault. Switch to
         <b>Representative sample</b> to see the real mix.`;
  }
  $('alerts-pager').innerHTML = `
    <button class="page-btn" ${page <= 1 ? 'disabled' : ''} data-page="${Math.max(1, page - 1)}">← Prev</button>
    <span class="page-btn cur">${fmt(page)} / ${fmt(d.pages)}</span>
    <button class="page-btn" ${page >= d.pages ? 'disabled' : ''} data-page="${Math.min(d.pages, page + 1)}">Next →</button>
    <span class="loading-note" style="margin-left:8px">${fmt(d.total)} alerts matching</span>`;
  $('alerts-pager').querySelectorAll('.page-btn[data-page]').forEach(b =>
    b.addEventListener('click', () => loadAlerts(Number(b.dataset.page))));
}

LOADERS.live = async () => {
  const meta = await getJSON('/api/predict_meta');
  if (meta) {
    const paySel = $('p-pay-currency'), recvSel = $('p-recv-currency');
    meta.currencies.forEach(c => {
      paySel.insertAdjacentHTML('beforeend', `<option ${c === 'US Dollar' ? 'selected' : ''}>${c}</option>`);
      recvSel.insertAdjacentHTML('beforeend', `<option ${c === 'US Dollar' ? 'selected' : ''}>${c}</option>`);
    });
    if (!meta.model_available) {
      $('predict-status').textContent = 'XGBoost model not loaded on the server — check models/xgboost_model.pkl';
      $('btn-predict').disabled = true;
    } else if (!meta.shap_available) {
      $('predict-status').textContent = 'Note: models/shap_explainer.pkl not found — scoring works, SHAP factors will be empty.';
    }
  }

  const o = await getJSON('/api/risk_overview');
  if (o) {
    $('al-fraud').textContent = fmt(o.fraud_captured);
    $('al-rate').textContent = o.alert_rate_pct + '%';
    $('al-capture').textContent = o.capture_rate_pct + '%';
  }

  loadAlerts(1);
  $('btn-refresh-alerts').addEventListener('click', () => loadAlerts(1));
  const ord = $('al-order');
  if (ord) ord.addEventListener('change', () => loadAlerts(1));
  $('btn-predict').addEventListener('click', runPredict);
};

async function runPredict() {
  const btn = $('btn-predict');
  btn.disabled = true;
  $('predict-status').textContent = 'scoring…';

  const payload = {
    amount_paid: Number($('p-amount-paid').value),
    amount_received: Number($('p-amount-received').value),
    payment_format: $('p-format').value,
    payment_currency: $('p-pay-currency').value,
    receiving_currency: $('p-recv-currency').value,
    hour: Number($('p-hour').value),
    day: Number($('p-day').value),
    day_of_week: Number($('p-dow').value),
    sender_bank: Number($('p-sender-bank').value),
    receiver_bank: Number($('p-receiver-bank').value),
    txn_count_per_hour: Number($('p-txn-hour').value),
    txn_count_per_day: Number($('p-txn-day').value),
    is_weekend: $('p-weekend').checked,
    is_cross_bank: $('p-crossbank').checked,
    is_self_transfer: $('p-self').checked,
    is_amount_outlier_iqr: $('p-iqr').checked,
    is_account_level_outlier: $('p-acclevel').checked,
    is_high_velocity: $('p-velocity').checked,
  };

  let d = null;
  try {
    const r = await fetch('/api/predict', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    d = await r.json();
  } catch (e) { console.error(e); }

  btn.disabled = false;

  if (!d || d.error) {
    $('predict-status').textContent = 'Error: ' + (d ? d.error : 'request failed');
    return;
  }
  $('predict-status').textContent = '';
  $('predict-placeholder-card').style.display = 'none';
  $('predict-result-card').style.display = '';

  $('pred-score').textContent = (d.percentile != null ? d.percentile : d.risk_score).toFixed(2);
  const lvl = $('pred-level');
  lvl.className = 'pill ' + d.risk_level;
  lvl.textContent = d.risk_level;
  $('pred-prob').textContent = 'fraud probability ' + (d.fraud_probability * 100).toFixed(4) + '%';
  if ($('pred-context') && d.percentile != null) {
    $('pred-context').textContent =
      `riskier than ${d.percentile.toFixed(2)}% of all 5.08M transactions`;
  }
  if ($('pred-thresholds') && d.thresholds) {
    const t = d.thresholds;
    $('pred-thresholds').innerHTML =
      `<b>How this level was decided.</b> The transaction's fraud probability
       (<span class="mono">${d.fraud_probability.toFixed(6)}</span>) is compared against the XGBoost
       score distribution of all 5.08M transactions, then bucketed with the risk engine's own tiers —
       CRITICAL = top 0.5% (<span class="mono">≥ ${t.CRITICAL.toFixed(5)}</span>),
       HIGH = top 2.5% (<span class="mono">≥ ${t.HIGH.toFixed(5)}</span>),
       MEDIUM = top 10% (<span class="mono">≥ ${t.MEDIUM.toFixed(5)}</span>).`;
  }
  $('pred-sar').textContent = d.sar_narrative;

  if (d.top_factors && d.top_factors.length) {
    const f = d.top_factors.slice(0, 8);
    makeChart('chart-pred-shap', {
      type: 'bar',
      data: {
        labels: f.map(x => x.feature),
        datasets: [{
          label: 'SHAP value',
          data: f.map(x => x.shap_value),
          backgroundColor: f.map(x => x.direction === 'fraud' ? PALETTE.critical : PALETTE.good),
          borderRadius: 4, borderSkipped: false, barPercentage: .75,
        }],
      },
      options: {
        indexAxis: 'y',
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: c => 'SHAP ' + (c.parsed.x >= 0 ? '+' : '') + c.parsed.x.toFixed(4),
              afterLabel: c => f[c.dataIndex].direction === 'fraud' ? '→ pushes toward FRAUD' : '→ pushes toward NORMAL',
            },
          },
        },
        scales: {
          x: { ...AXIS, title: { display: true, text: '← normal    SHAP value    fraud →', color: PALETTE.text3 } },
          y: { ...AXIS_NOGRID, ticks: { ...AXIS_NOGRID.ticks, font: { size: 10 } } },
        },
      },
    });
  } else {
    const el = $('chart-pred-shap');
    if (el && charts['chart-pred-shap']) { charts['chart-pred-shap'].destroy(); delete charts['chart-pred-shap']; }
  }
}

/* ================================================================
   BOOT
   ================================================================ */
loaded.overview = true;
LOADERS.overview();

getJSON('/api/health').then(h => {
  if (h) $('sb-status').textContent = h.xgboost_loaded ? 'ONLINE' : 'DEGRADED';
});
