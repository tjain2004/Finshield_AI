/* ================================================================
   FinShield AI — new.js
   Loaded by new.py after the existing dashboard script. Two sidebar tabs:
     • User Portal
         Screen a transaction  composer + live risk verdict  /api/portal/*
         Link Security         phishing-link check           /api/check-url
     • Investigation Workbench
         alert queue + live counts, case detail, transaction / account
         lookup, why flagged (exact SHAP), linked accounts (network),
         account history, SAR report, analyst workflow + audit trail,
         docked simulator                                   /api/wb/*
   Registered through the dashboard's own PAGE_META / LOADERS (bottom of
   this file). Reuses the dashboard's design tokens; does not change any
   existing behaviour.
   ================================================================ */

/* ---------- own helpers (independent of dashboard script version) ---------- */
const nx$ = id => document.getElementById(id);

function nxEsc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
  }[ch]));
}

async function nxGet(url) {
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
   USER PORTAL — split-window transaction screening
   Feature 1: Transaction Composer (scenarios + plain-language form)
   Feature 2: Live Risk Verdict (decision, percentile meter, reasons)
   Backend: /api/portal/meta, /api/portal/score (app.py)
   ================================================================ */

const PORTAL = { meta: null, seq: 0, timer: null, scored: false, inited: null, home: null };

const PORTAL_ICONS = {
  LOW: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m5 12 5 5L20 7"/></svg>',
  MEDIUM: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  HIGH: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  CRITICAL: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg>',
};

/* percentile -> position on the stretched tier meter (widths 3:2:2:1.4) */
function portalMeterPos(p) {
  const stops = [[0, 0], [90, 3], [97.5, 5], [99.5, 7], [100, 8.4]];
  for (let i = 1; i < stops.length; i++) {
    const [p0, x0] = stops[i - 1], [p1, x1] = stops[i];
    if (p <= p1) return ((x0 + (x1 - x0) * (p - p0) / (p1 - p0)) / 8.4) * 100;
  }
  return 100;
}

function portalFillSelect(id, items, labelFn) {
  const el = nx$(id);
  if (!el) return;
  el.innerHTML = items.map((v, i) =>
    `<option value="${nxEsc(typeof v === 'object' ? v.value : v)}">${nxEsc(labelFn ? labelFn(v, i) : v)}</option>`).join('');
}

function portalValues() {
  const num = id => Number(nx$(id).value);
  return {
    amount: num('pt-amount'),
    payment_format: nx$('pt-format').value,
    send_currency: nx$('pt-send-cur').value,
    receive_currency: nx$('pt-recv-cur').value,
    sender_bank: num('pt-sender-bank'),
    receiver_bank: num('pt-receiver-bank'),
    self_transfer: nx$('pt-self').checked,
    hour: num('pt-hour'),
    weekday: num('pt-weekday'),
    usual_amount: num('pt-usual'),
    txn_hour: num('pt-txn-hour'),
    txn_day: num('pt-txn-day'),
  };
}

function portalApply(v) {
  const set = (id, val) => { if (val !== undefined && nx$(id)) nx$(id).value = val; };
  set('pt-amount', v.amount); set('pt-format', v.payment_format);
  set('pt-send-cur', v.send_currency); set('pt-recv-cur', v.receive_currency);
  set('pt-sender-bank', v.sender_bank); set('pt-receiver-bank', v.receiver_bank);
  set('pt-hour', v.hour); set('pt-weekday', v.weekday); set('pt-usual', v.usual_amount);
  set('pt-txn-hour', v.txn_hour); set('pt-txn-day', v.txn_day);
  nx$('pt-self').checked = !!v.self_transfer;
  portalHints();
}

function portalHints() {
  const v = portalValues();
  const route = v.self_transfer ? 'Self-transfer (same account)'
    : (v.sender_bank === v.receiver_bank ? 'Same bank' : 'Cross-bank transfer');
  nx$('pt-route-hint').textContent = route;
  nx$('pt-receiver-bank').disabled = v.self_transfer;
  const ratio = v.usual_amount > 0 ? v.amount / v.usual_amount : 1;
  nx$('pt-ratio-hint').textContent =
    `This payment is ${ratio.toFixed(1)}× their usual amount` + (ratio > 5 ? ' — personal outlier' : '');
}

function portalValidate(v) {
  if (!(v.amount > 0)) return 'Enter an amount greater than 0.';
  if (!(v.txn_hour >= 1)) return 'Transfers this hour must be at least 1.';
  if (v.txn_day < v.txn_hour) return 'Transfers today can\'t be fewer than transfers this hour.';
  return '';
}

async function portalScore() {
  const v = portalValues();
  const problem = portalValidate(v);
  nx$('pt-status').textContent = problem;
  if (problem) return;

  const seq = ++PORTAL.seq;
  const btn = nx$('pt-screen');
  btn.disabled = true;
  nx$('pt-verdict-card').classList.add('portal-busy');
  let d = null;
  try {
    const r = await fetch('/api/portal/score', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(v),
    });
    d = await r.json();
  } catch (e) { console.error(e); }
  if (seq !== PORTAL.seq) return;            // a newer request has taken over
  btn.disabled = false;
  nx$('pt-verdict-card').classList.remove('portal-busy');

  if (!d || d.error) {
    nx$('pt-status').textContent = 'Error: ' + (d ? d.error : 'request failed — is the server running?');
    return;
  }
  PORTAL.scored = true;
  portalRender(d);
}

function portalRender(d) {
  nx$('pt-empty').style.display = 'none';
  nx$('pt-result').style.display = '';

  const lvl = d.risk_level;
  const pill = nx$('pt-level');
  pill.style.display = '';
  pill.className = 'pill ' + lvl;
  pill.innerHTML = '<span class="dot"></span>' + lvl;

  nx$('pt-decision').className = 'decision ' + lvl;
  nx$('pt-decision-icon').innerHTML = PORTAL_ICONS[lvl] || '';
  nx$('pt-decision-word').textContent = d.decision;
  nx$('pt-decision-action').textContent = d.action;

  nx$('pt-pct').textContent = Number(d.percentile).toFixed(1);
  nx$('pt-headline').textContent = d.headline;
  nx$('pt-prob').textContent = (d.fraud_probability * 100).toFixed(2) + '%';
  const order = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'];
  document.querySelectorAll('#pt-meter .tier-seg').forEach((s, i) =>
    s.classList.toggle('on', i <= order.indexOf(lvl)));
  nx$('pt-needle').style.left = portalMeterPos(d.percentile) + '%';

  const why = nx$('pt-why');
  const reasons = (d.reasons || []).filter(r => Math.abs(r.impact) >= 0.005);
  if (!d.shap_available || !reasons.length) {
    why.innerHTML = '<div class="empty-note">Explanation unavailable for this transaction.</div>';
    nx$('pt-why-note').textContent = '';
  } else {
    const max = Math.max(...reasons.map(r => Math.abs(r.impact)), 1e-9);
    why.innerHTML = reasons.map(r => {
      const w = (Math.abs(r.impact) / max) * 50;
      const tip = `${r.title}: ${r.impact > 0 ? 'raises' : 'lowers'} the risk by ${Math.abs(r.impact).toFixed(2)} (log-odds)`;
      return `<div class="why-row" title="${nxEsc(tip)}">
        <div><div class="why-title">${nxEsc(r.title)}</div><div class="why-detail">${nxEsc(r.detail)}</div></div>
        <div class="why-bar"><div class="why-fill ${r.direction}" style="width:${w.toFixed(1)}%"></div></div>
        <div class="why-val">${r.impact > 0 ? '+' : '−'}${Math.abs(r.impact).toFixed(2)}</div>
      </div>`;
    }).join('');
    nx$('pt-why-note').textContent =
      'Bars show each factor\'s exact SHAP contribution in log-odds, grouped into factors a person can act on. Hover a row for detail.';
  }

  nx$('pt-derived').innerHTML = (d.derived || []).map(x =>
    `<div class="derived${x.alert ? ' alert' : ''}"><div class="k">${nxEsc(x.label)}</div><div class="v">${nxEsc(x.value)}</div></div>`
  ).join('');
  nx$('pt-note').innerHTML = '<b>Note:</b> ' + nxEsc((d.notes || []).join(' '));
  nx$('pt-status').textContent = '';
}

function portalQueue() {
  portalHints();
  document.querySelectorAll('.preset').forEach(p => p.classList.remove('active'));
  if (!nx$('pt-live').checked || !PORTAL.scored) return;
  clearTimeout(PORTAL.timer);
  PORTAL.timer = setTimeout(portalScore, 350);
}

function portalSplitter() {
  const split = nx$('portal-split'), bar = nx$('portal-splitter');
  if (!split || !bar) return;
  try {
    const saved = localStorage.getItem('finshield.portal.left');
    if (saved) split.style.setProperty('--portal-left', saved);
  } catch (e) { /* storage unavailable — default width */ }
  const setPct = pct => {
    pct = Math.min(70, Math.max(30, pct));
    split.style.setProperty('--portal-left', pct + '%');
    try { localStorage.setItem('finshield.portal.left', pct + '%'); } catch (e) {}
  };
  bar.addEventListener('pointerdown', e => {
    e.preventDefault();
    bar.classList.add('drag');
    bar.setPointerCapture(e.pointerId);
    const rect = split.getBoundingClientRect();
    const move = ev => setPct(((ev.clientX - rect.left) / rect.width) * 100);
    const up = () => {
      bar.classList.remove('drag');
      bar.removeEventListener('pointermove', move);
      bar.removeEventListener('pointerup', up);
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', up);
  });
  bar.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const cur = parseFloat(getComputedStyle(split).getPropertyValue('--portal-left')) || 46;
    setPct(cur + (e.key === 'ArrowRight' ? 3 : -3));
  });
}

/* Idempotent: used by the User Portal tab and by the docked simulator. */
function portalInit() {
  if (!PORTAL.inited) {
    PORTAL.inited = portalSetup().then(ok => { if (!ok) PORTAL.inited = null; return ok; });
  }
  return PORTAL.inited;
}

async function portalSetup() {
  const m = await nxGet('/api/portal/meta');
  if (!m) {
    nx$('portal-presets').innerHTML = '<div class="empty-note">Could not reach /api/portal/meta — start the dashboard with python new.py.</div>';
    return false;
  }
  PORTAL.meta = m;
  portalFillSelect('pt-format', m.formats);
  portalFillSelect('pt-send-cur', m.currencies);
  portalFillSelect('pt-recv-cur', m.currencies);
  portalFillSelect('pt-hour', [...Array(24).keys()], h => String(h).padStart(2, '0') + ':00');
  portalFillSelect('pt-weekday', [...Array(7).keys()], (d, i) => m.weekdays[i]);
  portalApply({ payment_format: 'ACH', send_currency: 'US Dollar', receive_currency: 'US Dollar', hour: 12, weekday: 2 });

  nx$('portal-presets').innerHTML = m.presets.map(p => `
    <button class="preset" data-id="${nxEsc(p.id)}">
      <div class="preset-name"><span class="tone-dot ${nxEsc(p.tone)}"></span>${nxEsc(p.label)}</div>
      <div class="preset-desc">${nxEsc(p.desc)}</div>
    </button>`).join('');
  document.querySelectorAll('.preset').forEach(btn => btn.addEventListener('click', () => {
    const p = m.presets.find(x => x.id === btn.dataset.id);
    if (!p) return;
    portalApply(p.values);
    document.querySelectorAll('.preset').forEach(b => b.classList.toggle('active', b === btn));
    portalScore();
  }));

  document.querySelectorAll('#portal-split input, #portal-split select').forEach(el => {
    if (el.id === 'pt-live') return;
    el.addEventListener('input', portalQueue);
    el.addEventListener('change', portalQueue);
  });
  nx$('pt-screen').addEventListener('click', portalScore);
  portalSplitter();
  portalHints();
  return true;
}


/* ================================================================
   INVESTIGATION WORKBENCH
   1. Alert Investigation Workbench — queue (existing /api/alerts) + details
   2. Explainable Risk Card        — exact TreeSHAP "Why flagged?"
   3. Linked Account Network       — Cytoscape.js, real counterparties only
   4. Analyst Workflow             — Confirm / Dismiss / Escalate + audit trail
   5. Live Transaction Simulator   — the User Portal tool, docked here
   + Transaction Lookup, Account Search and account history
   Backend: /api/wb/* in new.py
   ================================================================ */

const WB = {
  inited: false, view: 'queue', page: 1, per: 20, queueToken: 0,
  ctx: null, tab: 'overview', loaded: {}, index: null, pollTimer: null,
  cy: null, netAccount: null, histAccount: null, histRole: 'all', histPage: 1,
  pendingReload: false,
};

const WB_STATUS_LABEL = {
  PENDING_REVIEW: 'Pending review', CONFIRMED_FRAUD: 'Confirmed fraud',
  DISMISSED: 'Dismissed', ESCALATED: 'Escalated', SAR_FILED: 'SAR filed',
};
const WB_ACTION_LABEL = {
  CONFIRM_FRAUD: 'Confirmed fraud', DISMISS: 'Dismissed', ESCALATE: 'Escalated',
  FILE_SAR: 'SAR filed', REOPEN: 'Reopened',
};
const WB_STATUS_ORDER = ['PENDING_REVIEW', 'ESCALATED', 'CONFIRMED_FRAUD', 'SAR_FILED', 'DISMISSED'];
const WB_CYTO_SRC = [
  '/static/cytoscape.min.js',
  'https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js',
];

/* ---------- small helpers ---------- */
function wbNum(v, d = 2) {
  return (v === null || v === undefined || isNaN(v)) ? '—'
    : Number(v).toLocaleString('en-US', { maximumFractionDigits: d });
}
function wbMoney(v, cur) { return `${wbNum(v, 2)} ${cur || ''}`.trim(); }
function wbTime(t) {
  if (!t) return '—';
  const hh = String(t.hour).padStart(2, '0') + ':00';
  return `Day ${t.day} · ${hh}` + (t.weekday ? ` · ${t.weekday}` : '');
}
function wbPill(level) {
  return level ? `<span class="pill ${nxEsc(level)}"><span class="dot"></span>${nxEsc(level)}</span>` : '<span class="pill">—</span>';
}
function wbStatusBadge(st) {
  return st ? `<span class="wb-status ${nxEsc(st)}">${nxEsc(WB_STATUS_LABEL[st] || st)}</span>` : '';
}
function wbLabelBadge(label) {
  return label === 1
    ? '<span class="wb-status CONFIRMED_FRAUD" title="Ground-truth label in the IBM dataset">Dataset label: laundering</span>'
    : '<span class="wb-status" title="Ground-truth label in the IBM dataset">Dataset label: normal</span>';
}
function wbAccBtn(acct) {
  return `<button class="wb-link" data-acc="${Number(acct)}">ACC-${Number(acct)}</button>`;
}
function wbTxBtn(row, txId) {
  return `<button class="wb-link" data-tx="${Number(row)}">${nxEsc(txId)}</button>`;
}
async function wbFetch(url, opts) {
  try {
    const r = await fetch(url, opts);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { ok: r.ok, status: r.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: { error: 'Server not reachable — is python new.py running?' } };
  }
}
function wbSetMsg(id, text, kind) {
  const el = nx$(id);
  if (!el) return;
  el.textContent = text || '';
  el.className = 'wb-msg' + (kind ? ' ' + kind : '');
}
function wbAnalyst() {
  try { return localStorage.getItem('finshield.analyst') || ''; } catch (e) { return ''; }
}
function wbSaveAnalyst(name) {
  try { localStorage.setItem('finshield.analyst', name); } catch (e) { /* storage unavailable */ }
}

/* ---------- index status ---------- */
async function wbPollIndex() {
  clearTimeout(WB.pollTimer);
  const r = await wbFetch('/api/wb/status');
  const st = r.data || { state: 'error', message: 'status unavailable' };
  WB.index = st;
  const box = nx$('wb-index'), txt = nx$('wb-index-text');
  box.className = 'wb-index ' + st.state;
  if (st.state === 'ready') {
    const al = st.alerts || {};
    txt.innerHTML = `Transaction index ready · ${wbNum(st.rows, 0)} transactions` +
      (st.risk_available ? ' · Risk Engine scores attached' : ' · risk scores unavailable') +
      (al.total ? ` · ${wbNum(al.mapped, 0)} / ${wbNum(al.total, 0)} alerts linked to their transaction` : '');
    box.title = (st.risk_note || '') + (al.ambiguous ? ` ${al.ambiguous} alerts share identical stored values with another alert.` : '');
    const bar = box.querySelector('.wb-progress');
    if (bar) bar.remove();
    if (WB.pendingReload && WB.ctx) { WB.pendingReload = false; wbReloadCtx(); }
  } else if (st.state === 'building' || st.state === 'idle') {
    txt.textContent = `Building transaction index — ${st.stage || 'starting'} ${Math.round((st.progress || 0) * 100)}%` +
      ' (first run reads data/cleaned_transactions.csv; later runs load a cache)';
    let bar = box.querySelector('.wb-progress');
    if (!bar) { bar = document.createElement('span'); bar.className = 'wb-progress'; bar.innerHTML = '<i></i>'; box.appendChild(bar); }
    bar.firstChild.style.width = Math.round((st.progress || 0) * 100) + '%';
    WB.pollTimer = setTimeout(wbPollIndex, 2000);
  } else {
    txt.textContent = 'Transaction index unavailable — ' + (st.message || st.state) +
      ' The alert queue and workflow still work.';
  }
}

/* ---------- queue ---------- */
async function wbLoadQueue() {
  const token = ++WB.queueToken;
  const list = nx$('wb-queue-list');
  list.innerHTML = '<div class="empty-note">Loading…</div>';
  nx$('wb-queue-pager').innerHTML = '';
  if (WB.view === 'cases') {
    const st = nx$('wb-case-status').value;
    const r = await wbFetch('/api/wb/cases' + (st ? '?status=' + encodeURIComponent(st) : ''));
    if (token !== WB.queueToken) return;
    const rows = (r.data && r.data.cases) || [];
    nx$('wb-queue-sub').textContent = `${wbNum(rows.length, 0)} alerts with an analyst decision`;
    list.innerHTML = rows.length ? rows.map(c => wbQueueItem({
      alert_id: c.alert_id, risk_level: c.risk_level, risk_score: c.risk_score,
      status: c.status, meta: `${WB_ACTION_LABEL[c.last_action.action] || c.last_action.action} by ${c.last_action.analyst}`,
    })).join('') : '<div class="empty-note">No decisions recorded yet.</div>';
    wbMarkActive();
    return;
  }
  const params = new URLSearchParams({ page: WB.page, per_page: WB.per });
  const lvl = nx$('wb-level').value;
  if (lvl) params.set('level', lvl);
  if (nx$('wb-order').value === 'sample') params.set('sample', '1');
  const r = await wbFetch('/api/alerts?' + params.toString());
  if (token !== WB.queueToken) return;
  if (!r.ok || !r.data) { list.innerHTML = '<div class="empty-note">Could not load /api/alerts.</div>'; return; }
  const d = r.data;
  const ids = (d.alerts || []).map(a => a.alert_id);
  const meta = ids.length ? ((await wbFetch('/api/wb/queue_meta?ids=' + encodeURIComponent(ids.join(',')))).data || {}) : {};
  if (token !== WB.queueToken) return;
  nx$('wb-queue-sub').textContent = `${wbNum(d.total, 0)} alerts` + (lvl ? ` · ${lvl}` : '') +
    (nx$('wb-order').value === 'sample' ? ' · representative sample' : ' · highest risk first');
  list.innerHTML = (d.alerts || []).length ? d.alerts.map(a => wbQueueItem({
    alert_id: a.alert_id, risk_level: a.risk_level, risk_score: a.risk_score,
    status: (meta[a.alert_id] || {}).status || a.status,
    meta: (meta[a.alert_id] || {}).tx_id || `XGB ${Number(a.xgb_score).toFixed(4)}`,
  })).join('') : '<div class="empty-note">No alerts match.</div>';
  wbMarkActive();
  const pages = d.pages || 1;
  nx$('wb-queue-pager').innerHTML =
    `<button class="page-btn" data-qp="${WB.page - 1}" ${WB.page <= 1 ? 'disabled' : ''}>← Prev</button>` +
    `<span class="loading-note">${wbNum(WB.page, 0)} / ${wbNum(pages, 0)}</span>` +
    `<button class="page-btn" data-qp="${WB.page + 1}" ${WB.page >= pages ? 'disabled' : ''}>Next →</button>`;
}

function wbQueueItem(a) {
  return `<button class="wb-q-item" data-alert="${nxEsc(a.alert_id)}">
    <span class="wb-q-id">${nxEsc(a.alert_id)}</span>
    <span class="wb-q-score">${a.risk_score !== undefined && a.risk_score !== null ? Number(a.risk_score).toFixed(2) : '—'}</span>
    <span>${wbPill(a.risk_level)} ${wbStatusBadge(a.status)}</span>
    <span class="wb-q-meta">${nxEsc(a.meta || '')}</span>
  </button>`;
}

function wbMarkActive() {
  const id = WB.ctx && WB.ctx.alertId;
  document.querySelectorAll('#wb-queue-list .wb-q-item').forEach(b =>
    b.classList.toggle('active', !!id && b.dataset.alert === id));
}

function wbUpdateQueueStatus(alertId, status) {
  document.querySelectorAll('#wb-queue-list .wb-q-item').forEach(b => {
    if (b.dataset.alert !== alertId) return;
    const old = b.querySelector('.wb-status');
    if (old) old.outerHTML = wbStatusBadge(status);
  });
}

/* ---------- context loading ---------- */
function wbResetPanes() {
  WB.loaded = {};
  WB.simFor = null;
  WB.netAccount = null;
  WB.histAccount = null;
  WB.histPage = 1;
  WB.histRole = 'all';
  nx$('wb-pane-why').innerHTML = '';
  nx$('wb-pane-history').innerHTML = '';
  nx$('wb-pane-sar').innerHTML = '';
  WB.sar = null;
  nx$('wb-net-info').textContent = 'Click an account or a link for details. Double-click an account to re-centre the map on it.';
  if (WB.cy) { WB.cy.destroy(); WB.cy = null; }
  nx$('wb-cy-msg').style.display = '';
  nx$('wb-cy-msg').textContent = 'Loading…';
}

function wbSetHeader(kind, title, sub, level) {
  nx$('wb-ctx-kind').textContent = kind;
  nx$('wb-ctx-title').textContent = title;
  nx$('wb-ctx-sub').textContent = sub || '';
  const pill = nx$('wb-ctx-pill');
  if (level) { pill.style.display = ''; pill.className = 'pill ' + level; pill.innerHTML = '<span class="dot"></span>' + nxEsc(level); }
  else pill.style.display = 'none';
}

function wbEnableTabs() {
  const c = WB.ctx || {};
  const hasTxn = !!c.txn, hasAcct = !!(c.txn || c.account !== undefined && c.account !== null);
  const rule = { overview: true, why: hasTxn, network: hasAcct, history: hasAcct, sar: !!c.alert, sim: true };
  document.querySelectorAll('#wb-tabs .model-btn').forEach(b => { b.disabled = !rule[b.dataset.tab]; });
  if (!rule[WB.tab]) wbShowTab('overview');
}

async function wbOpenAlert(alertId) {
  alertId = String(alertId).trim().toUpperCase();
  wbResetPanes();
  WB.ctx = { kind: 'alert', alertId, txn: null, alert: null, audit: [] };
  wbSetHeader('Alert', alertId, 'Loading…', null);
  nx$('wb-pane-overview').innerHTML = '<div class="empty-note">Loading alert…</div>';
  wbMarkActive();
  const r = await wbFetch('/api/wb/alert/' + encodeURIComponent(alertId));
  if (!WB.ctx || WB.ctx.alertId !== alertId) return;
  if (!r.ok) {
    nx$('wb-pane-overview').innerHTML = `<div class="callout critical-box">${nxEsc((r.data && r.data.error) || 'Alert not found.')}</div>`;
    wbSetHeader('Alert', alertId, '', null);
    wbEnableTabs();
    return;
  }
  const d = r.data;
  Object.assign(WB.ctx, { alert: d.alert, txn: d.txn, audit: d.audit || [], txnNote: d.txn_note });
  if (!d.txn && d.index && d.index.state === 'building') WB.pendingReload = true;
  wbSetHeader('Alert', alertId,
    d.txn ? `${d.txn.tx_id} · ${wbTime(d.txn)} · ${d.txn.payment_format}` : (d.txn_note || ''),
    d.alert.risk_level);
  wbRenderOverview();
  wbEnableTabs();
  wbShowTab(WB.tab);
}

async function wbOpenTxn(ref) {
  const r = await wbFetch('/api/wb/txn/' + encodeURIComponent(ref));
  if (!r.ok) { wbSetMsg('wb-search-status', (r.data && r.data.error) || 'Lookup failed.', 'err'); return; }
  wbSetMsg('wb-search-status', '');
  const t = r.data.txn;
  if (t.alert && t.alert.alert_id) { await wbOpenAlert(t.alert.alert_id); return; }
  wbResetPanes();
  WB.ctx = { kind: 'txn', alertId: null, txn: t, alert: null, audit: [] };
  wbMarkActive();
  wbSetHeader('Transaction', t.tx_id, `${wbTime(t)} · ${t.payment_format} · ${wbMoney(t.amount_paid, t.pay_currency)}`,
    t.risk && t.risk.level);
  wbRenderOverview();
  wbEnableTabs();
  wbShowTab(WB.tab === 'sim' ? 'sim' : 'overview');
}

async function wbOpenAccount(acct) {
  acct = Number(acct);
  wbResetPanes();
  WB.ctx = { kind: 'account', alertId: null, txn: null, account: acct };
  wbMarkActive();
  wbSetHeader('Account', 'ACC-' + acct, 'Account search · transaction history and linked accounts', null);
  nx$('wb-pane-overview').innerHTML = '<div class="empty-note">Loading account…</div>';
  wbEnableTabs();
  const r = await wbFetch(`/api/wb/account/${acct}?per_page=5`);
  if (!WB.ctx || WB.ctx.account !== acct) return;
  if (!r.ok) {
    nx$('wb-pane-overview').innerHTML = `<div class="callout critical-box">${nxEsc((r.data && r.data.error) || 'Account lookup failed.')}</div>`;
    return;
  }
  WB.ctx.summary = r.data.summary;
  wbSetHeader('Account', 'ACC-' + acct,
    `${wbNum(r.data.summary.total, 0)} transactions · ${wbNum(r.data.summary.counterparties, 0)} counterparties`,
    r.data.summary.max_level);
  nx$('wb-pane-overview').innerHTML = wbAccountSummaryHTML(r.data.summary) +
    `<div class="wb-actions-row">
       <button class="btn" data-go="history">Open transaction history</button>
       <button class="btn-ghost" data-go="network">Map linked accounts</button>
     </div>`;
  wbShowTab(WB.tab === 'overview' || WB.tab === 'why' ? 'history' : WB.tab);
}

function wbReloadCtx() {
  const c = WB.ctx;
  if (!c) return;
  if (c.kind === 'alert') wbOpenAlert(c.alertId);
  else if (c.kind === 'txn') wbOpenTxn(String(c.txn.row));
  else if (c.kind === 'account') wbOpenAccount(c.account);
}

/* ---------- overview ---------- */
function wbKV(k, v, s) {
  return `<div class="wb-kv"><div class="k">${nxEsc(k)}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;
}

function wbRenderOverview() {
  const c = WB.ctx, t = c.txn, al = c.alert;
  let h = '';
  if (al) {
    h += `<div class="wb-strip">${wbPill(al.risk_level)}
      <span class="wb-status">Risk score ${Number(al.risk_score).toFixed(2)} / 100</span>
      ${wbStatusBadge(al.status)}
      ${al.label !== null && al.label !== undefined ? wbLabelBadge(al.label) : ''}</div>`;
  } else if (t) {
    h += `<div class="wb-strip">${wbPill(t.risk.level)}
      <span class="wb-status">${t.risk.score !== null ? 'Risk score ' + Number(t.risk.score).toFixed(2) + ' / 100' : 'No Risk Engine score'}</span>
      ${wbLabelBadge(t.label)}</div>`;
  }
  if (t) {
    const f = t.flags;
    h += `<div class="sec-head" style="margin-top:6px">Transaction</div><div class="wb-kv-grid">
      ${wbKV('Transaction ID', nxEsc(t.tx_id), 'row ' + wbNum(t.row, 0) + ' of data/cleaned_transactions.csv')}
      ${wbKV('Time', nxEsc(wbTime(t)), t.is_weekend ? 'weekend' : 'weekday')}
      ${wbKV('Payment method', nxEsc(t.payment_format))}
      ${wbKV('Amount paid', nxEsc(wbMoney(t.amount_paid, t.pay_currency)))}
      ${wbKV('Amount received', nxEsc(wbMoney(t.amount_received, t.recv_currency)), t.currency_mismatch ? 'currency converted' : 'same currency')}
      ${wbKV('Risk Engine', t.risk.score !== null ? `${Number(t.risk.score).toFixed(2)} · ${nxEsc(t.risk.level || '—')}` : '—', nxEsc(t.risk.available ? 'results/final_risk_scores.csv' : t.risk.source || ''))}
    </div>
    <div class="sec-head">Parties</div><div class="wb-kv-grid">
      ${wbKV('Sender', wbAccBtn(t.sender.account), 'bank ' + nxEsc(t.sender.bank))}
      ${wbKV('Receiver', wbAccBtn(t.receiver.account), 'bank ' + nxEsc(t.receiver.bank))}
      ${wbKV('Route', t.self_transfer ? 'Self-transfer' : (t.cross_bank ? 'Cross-bank' : 'Same bank'))}
    </div>
    <div class="sec-head">Sender behaviour</div><div class="wb-kv-grid">
      ${wbKV('vs sender\'s usual', wbNum(t.ratio_vs_usual, 2) + '×', t.usual_amount !== null ? 'usual ≈ ' + nxEsc(wbMoney(t.usual_amount, t.pay_currency)) : '')}
      ${wbKV('Velocity', `${wbNum(t.txn_hour, 0)} / hour · ${wbNum(t.txn_day, 0)} / day`, 'sender\'s transfers in the same hour / day')}
      ${wbKV('Outlier checks', `${t.outlier_score} / 4`)}
    </div>
    <div class="wb-flags">
      <span class="wb-flag ${f.large_amount ? 'on' : ''}">Large amount (IQR)</span>
      <span class="wb-flag ${f.extreme_amount ? 'on' : ''}">Extreme amount (z-score)</span>
      <span class="wb-flag ${f.personal_outlier ? 'on' : ''}">5×+ sender's usual</span>
      <span class="wb-flag ${f.high_velocity ? 'on' : ''}">High velocity (10+/hr)</span>
    </div>
    <div class="wb-actions-row">
      <button class="btn" data-go="why">Why flagged?</button>
      <button class="btn-ghost" data-go="network">Linked accounts</button>
      <button class="btn-ghost" data-go="history">Sender history</button>
      ${al ? '<button class="btn-ghost" data-go="sar">Draft SAR report</button>' : ''}
      <button class="btn-ghost" data-go="sim">Simulate this transaction</button>
    </div>`;
  } else if (al) {
    h += `<div class="callout warn-box">${nxEsc(c.txnNote || 'Transaction details unavailable.')}</div>
      <div class="wb-kv-grid">
        ${wbKV('XGB score (stored)', Number(al.xgb_score).toFixed(6))}
        ${wbKV('CNN score (stored)', Number(al.cnn_score).toFixed(6))}
        ${wbKV('Composite', Number(al.composite).toFixed(4))}
      </div>`;
  }
  if (al) h += wbWorkflowHTML();
  else if (t) h += `<div class="callout" style="margin-top:14px;margin-bottom:0">This transaction is not in the alert queue
    (Risk Engine level ${nxEsc(t.risk.level || 'unavailable')}). Analyst decisions apply to alerts.</div>`;
  nx$('wb-pane-overview').innerHTML = h;
  if (al) wbRenderAudit();
}

function wbWorkflowHTML() {
  const al = WB.ctx.alert;
  const pending = al.status === 'PENDING_REVIEW';
  return `<div class="sec-head">Analyst workflow</div>
  <div class="wb-workflow">
    <div class="wb-strip" style="margin-bottom:10px">Current status ${wbStatusBadge(al.status)}
      ${WB.ctx.txn && WB.ctx.txn.alert && WB.ctx.txn.alert.ambiguous ? '<span class="wb-status" title="Another alert has identical stored values, so the match to this transaction row is one of two equivalent rows">identical twin alert</span>' : ''}</div>
    <div class="wb-wf-grid">
      <div class="field"><label class="field-label" for="wb-analyst">Analyst</label>
        <input type="text" id="wb-analyst" maxlength="60" value="${nxEsc(wbAnalyst())}" placeholder="Your name"></div>
      <div class="field"><label class="field-label" for="wb-note">Decision note (required)</label>
        <textarea id="wb-note" maxlength="1000" placeholder="What you checked and why you decided this"></textarea></div>
    </div>
    <div class="wb-wf-buttons">
      <button class="wb-btn fraud" data-act="CONFIRM_FRAUD">Confirm fraud</button>
      <button class="wb-btn escalate" data-act="ESCALATE">Escalate</button>
      <button class="wb-btn dismiss" data-act="DISMISS">Dismiss</button>
      ${pending ? '' : '<button class="wb-btn" data-act="REOPEN">Reopen</button>'}
      ${['CONFIRMED_FRAUD', 'ESCALATED'].includes(al.status) ? '<button class="wb-btn sar" data-go="sar">File SAR…</button>' : ''}
    </div>
    <div class="wb-msg" id="wb-wf-msg"></div>
    <div class="sec-head" style="margin-top:16px">Audit trail</div>
    <ul class="wb-audit" id="wb-audit"></ul>
  </div>`;
}

function wbRenderAudit() {
  const el = nx$('wb-audit');
  if (!el) return;
  const items = WB.ctx.audit || [];
  el.innerHTML = items.length ? items.map(e => {
    const when = new Date(e.ts);
    return `<li><i class="${nxEsc(e.status_after)}"></i><div>
      <div class="t"><b>${nxEsc(WB_ACTION_LABEL[e.action] || e.action)}</b> by ${nxEsc(e.analyst)}</div>
      <div class="m">${nxEsc(isNaN(when) ? e.ts : when.toLocaleString())} · ${nxEsc(WB_STATUS_LABEL[e.status_before] || e.status_before)} → ${nxEsc(WB_STATUS_LABEL[e.status_after] || e.status_after)}${e.tx_id ? ' · ' + nxEsc(e.tx_id) : ''}</div>
      <div class="n">${nxEsc(e.note)}</div></div></li>`;
  }).join('') : '<li><i></i><div class="m">No analyst actions yet. The alert is as the Risk Engine created it.</div></li>';
}

/* One place every analyst decision goes through — the Overview workflow
   buttons and the SAR tab's "File SAR" both call this, so the audit log,
   the queue badge and the live counts always stay in step. */
const WB_FORM_OVERVIEW = { analyst: 'wb-analyst', note: 'wb-note', msg: 'wb-wf-msg', scope: '#wb-pane-overview' };
const WB_FORM_SAR = { analyst: 'wb-sar-analyst', note: 'wb-sar-note', msg: 'wb-sar-msg', scope: '#wb-pane-sar' };

async function wbAct(action, form = WB_FORM_OVERVIEW) {
  const c = WB.ctx;
  if (!c || !c.alert) return;
  const analyst = (nx$(form.analyst).value || '').trim();
  const note = (nx$(form.note).value || '').trim();
  if (analyst.length < 2) { wbSetMsg(form.msg, 'Enter your name as the analyst.', 'err'); return; }
  if (note.length < 5) { wbSetMsg(form.msg, 'Add a short note explaining the decision.', 'err'); return; }
  wbSaveAnalyst(analyst);
  const buttons = document.querySelectorAll(`${form.scope} .wb-btn`);
  buttons.forEach(b => { b.disabled = true; });
  const r = await wbFetch('/api/wb/action', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ alert_id: c.alert.alert_id, action, analyst, note }),
  });
  buttons.forEach(b => { b.disabled = false; });
  if (!r.ok) { wbSetMsg(form.msg, (r.data && r.data.error) || 'Could not save the decision.', 'err'); return; }
  c.alert.status = r.data.status;
  c.audit = r.data.audit || [];
  WB.loaded.sar = false;
  wbRenderOverview();
  wbSetMsg(WB_FORM_OVERVIEW.msg, `Saved: ${WB_STATUS_LABEL[r.data.status] || r.data.status}. Recorded in results/analyst_audit_log.jsonl.`, 'ok');
  wbUpdateQueueStatus(c.alert.alert_id, r.data.status);
  wbLoadStats();
  if (WB.view === 'cases') wbLoadQueue();
  if (WB.tab === 'sar') await wbLoadSar(`Saved: ${WB_STATUS_LABEL[r.data.status] || r.data.status}.`);
}

/* ---------- account summary ---------- */
function wbCurList(items) {
  if (!items || !items.length) return '<span class="loading-note">none</span>';
  return items.slice(0, 4).map(x => `<b>${wbNum(x.total, 2)}</b> ${nxEsc(x.currency)} <span class="loading-note">(${wbNum(x.count, 0)})</span>`).join('<br>') +
    (items.length > 4 ? `<br><span class="loading-note">+${items.length - 4} more currencies</span>` : '');
}

function wbAccountSummaryHTML(s) {
  return `<div class="wb-kv-grid">
    ${wbKV('Transactions', wbNum(s.total, 0), `${wbNum(s.sent, 0)} sent · ${wbNum(s.received, 0)} received`)}
    ${wbKV('Counterparties', wbNum(s.counterparties, 0))}
    ${wbKV('Active', `Day ${s.first_seen.day} → Day ${s.last_seen.day}`, `${String(s.first_seen.hour).padStart(2, '0')}:00 → ${String(s.last_seen.hour).padStart(2, '0')}:00`)}
    ${wbKV('Alert-level transactions', s.alert_txns === null ? '—' : wbNum(s.alert_txns, 0), s.max_level ? 'highest level ' + nxEsc(s.max_level) : '')}
    ${wbKV('Labelled laundering', wbNum(s.labelled_laundering, 0), 'IBM ground-truth label')}
    ${wbKV('Account ID', nxEsc(s.account_id), 'encoded ID from the cleaned dataset')}
  </div>
  <div class="wb-two" style="margin-top:10px">
    <div class="wb-kv"><div class="k">Sent (as paid)</div><div class="wb-cur-list" style="margin-top:4px">${wbCurList(s.sent_by_currency)}</div></div>
    <div class="wb-kv"><div class="k">Received (as received)</div><div class="wb-cur-list" style="margin-top:4px">${wbCurList(s.received_by_currency)}</div></div>
  </div>`;
}

/* ---------- tabs ---------- */
function wbShowTab(tab) {
  WB.tab = tab;
  document.querySelectorAll('#wb-tabs .model-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  ['overview', 'why', 'network', 'history', 'sar', 'sim'].forEach(p => { nx$('wb-pane-' + p).hidden = p !== tab; });
  if (tab === 'sar' && WB.ctx && WB.ctx.alert && !WB.loaded.sar) wbLoadSar();
  const c = WB.ctx;
  if (tab === 'why' && c && c.txn && !WB.loaded.why) wbLoadWhy();
  if (tab === 'network' && c && !WB.loaded.network) wbInitNetwork();
  if (tab === 'history' && c && !WB.loaded.history) {
    WB.histAccount = c.txn ? c.txn.sender.account : c.account;
    wbLoadHistory();
  }
  if (tab === 'sim') wbDockSimulator();
  if (tab === 'network' && WB.cy) setTimeout(() => { WB.cy.resize(); WB.cy.fit(undefined, 30); }, 30);
}

/* ---------- 2. Explainable Risk Card ---------- */
async function wbLoadWhy() {
  const t = WB.ctx.txn;
  WB.loaded.why = true;
  const pane = nx$('wb-pane-why');
  pane.innerHTML = '<div class="empty-note">Computing SHAP explanation…</div>';
  const r = await wbFetch('/api/wb/explain/' + t.row);
  if (!WB.ctx || !WB.ctx.txn || WB.ctx.txn.row !== t.row) return;
  if (!r.ok) {
    WB.loaded.why = false;
    pane.innerHTML = `<div class="callout critical-box">${nxEsc((r.data && r.data.error) || 'Explanation failed.')}</div>`;
    return;
  }
  const d = r.data, st = d.stored || {};
  const reasons = (d.reasons || []).filter(x => Math.abs(x.impact) >= 0.005);
  const max = Math.max(...reasons.map(x => Math.abs(x.impact)), 1e-9);
  pane.innerHTML = `
    <div class="wb-two">
      <div class="wb-score-box"><div class="k">Risk Engine (stored)</div>
        <div class="big">${st.score !== null && st.score !== undefined ? Number(st.score).toFixed(2) : '—'}</div>
        <div class="s">${wbPill(st.level)} &nbsp;${nxEsc(st.available ? 'results/final_risk_scores.csv · weighted ensemble' : (st.source || 'not available'))}</div></div>
      <div class="wb-score-box"><div class="k">Production XGBoost (re-scored)</div>
        <div class="big">${Number(d.model.percentile).toFixed(1)}<span style="font-size:13px;color:var(--text2)"> percentile</span></div>
        <div class="s">${wbPill(d.model.level)} &nbsp;fraud probability ${(d.model.probability * 100).toFixed(2)}% · decision ${nxEsc(d.model.decision)}</div></div>
    </div>
    <div class="sec-head" style="margin-top:4px">Why flagged?</div>
    <div class="why-legend"><span><i style="background:var(--s1)"></i>lowers risk</span><span>raises risk<i style="background:var(--s8)"></i></span></div>
    ${reasons.map(x => {
      const w = (Math.abs(x.impact) / max) * 50;
      return `<div class="why-row" title="${nxEsc(`${x.title}: ${x.impact > 0 ? 'raises' : 'lowers'} the log-odds by ${Math.abs(x.impact).toFixed(3)}`)}">
        <div><div class="why-title">${nxEsc(x.title)}</div><div class="why-detail">${nxEsc(x.detail)}</div></div>
        <div class="why-bar"><div class="why-fill ${x.direction}" style="width:${w.toFixed(1)}%"></div></div>
        <div class="why-val">${x.impact > 0 ? '+' : '−'}${Math.abs(x.impact).toFixed(2)}</div></div>`;
    }).join('')}
    <div class="sec-head">Top model features</div>
    <div class="table-scroll"><table class="wb-feat-table">
      <thead><tr><th>Feature</th><th>Value</th><th>SHAP (log-odds)</th></tr></thead>
      <tbody>${d.features.map(f => `<tr><td class="mono">${nxEsc(f.feature)}</td><td class="mono">${wbNum(f.value, 4)}</td>
        <td class="mono" style="color:${f.shap > 0 ? 'var(--s8)' : 'var(--s1)'}">${f.shap > 0 ? '+' : '−'}${Math.abs(f.shap).toFixed(4)}</td></tr>`).join('')}</tbody>
    </table></div>
    <ul class="finding-list" style="margin-top:10px">${(d.notes || []).map(n => `<li>${nxEsc(n)}</li>`).join('')}</ul>`;
}

/* ---------- 3. Linked Account Network ---------- */
function wbLoadCytoscape() {
  if (window.cytoscape) return Promise.resolve(true);
  if (WB.cytoPromise) return WB.cytoPromise;
  WB.cytoPromise = new Promise(resolve => {
    const tryNext = i => {
      if (i >= WB_CYTO_SRC.length) { resolve(false); return; }
      const s = document.createElement('script');
      s.src = WB_CYTO_SRC[i];
      s.onload = () => (window.cytoscape ? resolve(true) : tryNext(i + 1));
      s.onerror = () => { s.remove(); tryNext(i + 1); };
      document.head.appendChild(s);
    };
    tryNext(0);
  });
  return WB.cytoPromise;
}

function wbInitNetwork() {
  const c = WB.ctx;
  const sel = nx$('wb-net-account');
  const opts = [];
  if (c.txn) {
    opts.push([c.txn.sender.account, `Sender · ACC-${c.txn.sender.account}`]);
    if (c.txn.receiver.account !== c.txn.sender.account) opts.push([c.txn.receiver.account, `Receiver · ACC-${c.txn.receiver.account}`]);
  } else if (c.account !== undefined && c.account !== null) {
    opts.push([c.account, `ACC-${c.account}`]);
  }
  sel.innerHTML = opts.map(([v, l]) => `<option value="${Number(v)}">${nxEsc(l)}</option>`).join('');
  WB.netAccount = opts.length ? Number(opts[0][0]) : null;
  WB.loaded.network = true;
  if (WB.netAccount !== null) wbLoadNetwork(WB.netAccount);
}

function wbLevelColor(level) {
  const css = getComputedStyle(document.documentElement);
  const map = { LOW: '--good', MEDIUM: '--warn', HIGH: '--serious', CRITICAL: '--critical' };
  return css.getPropertyValue(map[level] || '--text3').trim() || '#59708F';
}

async function wbLoadNetwork(acct) {
  acct = Number(acct);
  WB.netAccount = acct;
  const sel = nx$('wb-net-account');
  if (![...sel.options].some(o => Number(o.value) === acct)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${acct}">ACC-${acct}</option>`);
  }
  sel.value = String(acct);
  const msg = nx$('wb-cy-msg');
  msg.style.display = '';
  msg.textContent = 'Loading linked accounts…';
  const [ok, r] = await Promise.all([wbLoadCytoscape(),
    wbFetch(`/api/wb/network/${acct}?depth=${nx$('wb-net-depth').value}&limit=${nx$('wb-net-limit').value}`)]);
  if (WB.netAccount !== acct) return;
  if (!r.ok) { msg.textContent = (r.data && r.data.error) || 'Could not load the network.'; return; }
  if (!ok) {
    msg.textContent = 'Cytoscape.js could not be loaded (no internet?). Put cytoscape.min.js in the static/ folder to use the map offline.';
    return;
  }
  const d = r.data;
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue('--accent').trim(), text = css.getPropertyValue('--text').trim(),
    text3 = css.getPropertyValue('--text3').trim(), border2 = css.getPropertyValue('--border2').trim(),
    critical = css.getPropertyValue('--critical').trim(), bg = css.getPropertyValue('--bg2').trim();
  const maxCount = Math.max(...d.edges.map(e => e.count), 1);
  const elements = [
    ...d.nodes.map(n => ({ data: { ...n, label: n.id, color: n.hop === 0 ? accent : wbLevelColor(n.max_level),
      size: n.hop === 0 ? 34 : (n.hop === 1 ? 22 : 15) } })),
    ...d.edges.map(e => ({ data: { ...e, width: 1.5 + 5 * Math.log1p(e.count) / Math.log1p(maxCount),
      color: e.laundering > 0 ? critical : border2 } })),
  ];
  if (WB.cy) WB.cy.destroy();
  msg.style.display = 'none';
  WB.cy = cytoscape({
    container: nx$('wb-cy'),
    elements,
    wheelSensitivity: 0.2,
    style: [
      { selector: 'node', style: { 'background-color': 'data(color)', width: 'data(size)', height: 'data(size)',
        label: 'data(label)', color: text, 'font-size': 10, 'font-family': 'JetBrains Mono, monospace',
        'text-valign': 'bottom', 'text-margin-y': 4, 'border-width': 2, 'border-color': bg } },
      { selector: 'node[hop = 0]', style: { 'border-color': '#fff', 'border-width': 3, 'font-size': 11, 'font-weight': 700 } },
      { selector: 'edge', style: { width: 'data(width)', 'line-color': 'data(color)', 'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'arrow-scale': 0.8, opacity: 0.85 } },
      { selector: ':selected', style: { 'overlay-color': accent, 'overlay-opacity': 0.18, 'overlay-padding': 5 } },
    ],
    layout: d.depth === 2
      ? { name: 'cose', animate: false, padding: 30, nodeRepulsion: () => 9000, idealEdgeLength: () => 90,
          edgeElasticity: () => 80, gravity: 0.6, numIter: 1500 }
      : { name: 'concentric', concentric: n => 3 - n.data('hop'), levelWidth: () => 1, minNodeSpacing: 40,
          spacingFactor: 1.1, avoidOverlap: true, animate: false, padding: 30 },
  });
  WB.cy.on('tap', 'node', ev => wbNetNodeInfo(ev.target.data()));
  WB.cy.on('tap', 'edge', ev => wbNetEdgeInfo(ev.target.data()));
  WB.cy.on('dbltap', 'node', ev => wbLoadNetwork(ev.target.data('account')));
  nx$('wb-net-legend').innerHTML = `
    <span><i style="background:${accent}"></i>centre account</span>
    <span><i style="background:${wbLevelColor('LOW')}"></i>LOW</span>
    <span><i style="background:${wbLevelColor('MEDIUM')}"></i>MEDIUM</span>
    <span><i style="background:${wbLevelColor('HIGH')}"></i>HIGH</span>
    <span><i style="background:${wbLevelColor('CRITICAL')}"></i>CRITICAL</span>
    <span><i class="edge" style="background:${critical}"></i>link with labelled laundering</span>
    <span><i class="edge" style="background:${border2}"></i>other link (width = no. of transactions)</span>`;
  nx$('wb-net-note').textContent =
    `Node colour = highest Risk Engine level among the account's transactions. Showing ${d.counterparties_shown} of ${d.counterparties_total} direct counterparties` +
    (d.depth === 2 ? ` and up to 5 onward links for the top 8 (of ${d.second_hop_total}).` : '.') + ' ' + d.source + '.';
  nx$('wb-net-info').innerHTML = `<b>ACC-${acct}</b> — click an account or link for details; double-click an account to re-centre.`;
}

function wbNetNodeInfo(n) {
  nx$('wb-net-info').innerHTML = `<b>${nxEsc(n.id)}</b> ${n.hop === 0 ? '(centre)' : `· ${n.hop} hop${n.hop > 1 ? 's' : ''} away`}<br>
    ${wbNum(n.txns, 0)} transactions · ${n.alert_txns === null ? '—' : wbNum(n.alert_txns, 0)} at alert level · ${wbNum(n.laundering, 0)} labelled laundering<br>
    Highest Risk Engine level: ${n.max_level ? wbPill(n.max_level) : '—'}<br>
    <button class="wb-link" data-acc="${Number(n.account)}">Open account history</button> ·
    <button class="wb-link" data-net="${Number(n.account)}">Centre map here</button>`;
}

function wbNetEdgeInfo(e) {
  nx$('wb-net-info').innerHTML = `<b>${nxEsc(e.source)} → ${nxEsc(e.target)}</b><br>
    ${wbNum(e.count, 0)} transactions · ${wbNum(e.laundering, 0)} labelled laundering · highest level ${e.max_level ? wbPill(e.max_level) : '—'}<br>
    ${(e.by_currency || []).map(x => `${wbNum(x.total, 2)} ${nxEsc(x.currency)}`).join(' · ')}`;
}

/* ---------- account history ---------- */
async function wbLoadHistory() {
  const acct = WB.histAccount;
  WB.loaded.history = true;
  const pane = nx$('wb-pane-history');
  const c = WB.ctx;
  let switcher = '';
  if (c.txn) {
    const s = c.txn.sender.account, r = c.txn.receiver.account;
    switcher = `<div class="wb-seg">
      <button class="${acct === s ? 'on' : ''}" data-hacc="${s}">Sender ACC-${s}</button>
      ${r !== s ? `<button class="${acct === r ? 'on' : ''}" data-hacc="${r}">Receiver ACC-${r}</button>` : ''}</div>`;
  }
  pane.innerHTML = `<div class="wb-hist-head">${switcher || `<b class="mono">ACC-${acct}</b>`}
    <div class="wb-seg">
      <button class="${WB.histRole === 'all' ? 'on' : ''}" data-hrole="all">All</button>
      <button class="${WB.histRole === 'sent' ? 'on' : ''}" data-hrole="sent">Sent</button>
      <button class="${WB.histRole === 'received' ? 'on' : ''}" data-hrole="received">Received</button></div></div>
    <div id="wb-hist-body"><div class="empty-note">Loading history…</div></div>`;
  const r = await wbFetch(`/api/wb/account/${acct}?role=${WB.histRole}&page=${WB.histPage}&per_page=20`);
  if (WB.histAccount !== acct) return;
  const body = nx$('wb-hist-body');
  if (!r.ok) {
    WB.loaded.history = false;
    body.innerHTML = `<div class="callout critical-box">${nxEsc((r.data && r.data.error) || 'History unavailable.')}</div>`;
    return;
  }
  const d = r.data;
  const curTx = c.txn ? c.txn.row : -1;
  body.innerHTML = wbAccountSummaryHTML(d.summary) + `
    <div class="table-scroll"><table>
      <thead><tr><th>Transaction</th><th>Time</th><th>Direction</th><th>Counterparty</th><th>Amount</th><th>Method</th><th>Risk</th><th>Label</th><th>Alert</th></tr></thead>
      <tbody>${d.rows.length ? d.rows.map(t => {
        const cp = t.direction === 'out' ? t.receiver.account : t.sender.account;
        const amt = t.direction === 'in' ? wbMoney(t.amount_received, t.recv_currency) : wbMoney(t.amount_paid, t.pay_currency);
        return `<tr${t.row === curTx ? ' style="background:var(--accent-dim)"' : ''}>
          <td>${wbTxBtn(t.row, t.tx_id)}</td><td class="mono">${nxEsc(wbTime(t))}</td>
          <td><span class="wb-dir ${t.direction}">${t.direction === 'out' ? 'SENT' : t.direction === 'in' ? 'RECEIVED' : 'SELF'}</span></td>
          <td>${t.direction === 'self' ? '—' : wbAccBtn(cp)}</td><td class="mono">${nxEsc(amt)}</td><td>${nxEsc(t.payment_format)}</td>
          <td>${t.risk.level ? wbPill(t.risk.level) + ' <span class="mono">' + Number(t.risk.score).toFixed(1) + '</span>' : '—'}</td>
          <td>${t.label === 1 ? '<span class="wb-status CONFIRMED_FRAUD">laundering</span>' : '<span class="loading-note">normal</span>'}</td>
          <td>${t.alert ? `<button class="wb-link" data-alert-open="${nxEsc(t.alert.alert_id)}">${nxEsc(t.alert.alert_id)}</button>` : '—'}</td></tr>`;
      }).join('') : '<tr><td colspan="9" class="empty-note">No transactions.</td></tr>'}</tbody>
    </table></div>
    <div class="pager">
      <button class="page-btn" data-hp="${d.page - 1}" ${d.page <= 1 ? 'disabled' : ''}>← Newer</button>
      <span class="loading-note">page ${wbNum(d.page, 0)} / ${wbNum(d.pages, 0)} · ${wbNum(d.total, 0)} transactions · newest first</span>
      <button class="page-btn" data-hp="${d.page + 1}" ${d.page >= d.pages ? 'disabled' : ''}>Older →</button>
    </div>`;
}

/* ---------- live alert counts (queue header) ---------- */
async function wbLoadStats() {
  const el = nx$('wb-stats');
  const r = await wbFetch('/api/wb/queue_stats');
  if (!el || !r.ok || !r.data) return;
  const counts = r.data.counts || {};
  el.innerHTML = WB_STATUS_ORDER.map(s =>
    `<button class="wb-stat ${s}" data-stat="${s}" title="Show ${nxEsc(WB_STATUS_LABEL[s].toLowerCase())} alerts">` +
    `<span class="n">${wbNum(counts[s] || 0, 0)}</span><span class="l">${nxEsc(WB_STATUS_LABEL[s])}</span></button>`).join('');
}

function wbFilterByStatus(status) {
  const view = nx$('wb-view');
  if (status === 'PENDING_REVIEW') {
    view.value = 'queue';
  } else {
    view.value = 'cases';
    nx$('wb-case-status').value = status;
  }
  view.dispatchEvent(new Event('change'));
}

/* ---------- SAR report (auto-drafted Suspicious Activity Report) ---------- */
async function wbLoadSar(okMsg) {
  const c = WB.ctx;
  if (!c || !c.alert) return;
  const alertId = c.alert.alert_id;
  WB.loaded.sar = true;
  const pane = nx$('wb-pane-sar');
  pane.innerHTML = '<div class="empty-note">Drafting the SAR from the alert, its transaction, the SHAP explanation, account activity and the audit trail…</div>';
  const r = await wbFetch('/api/wb/sar/' + encodeURIComponent(alertId));
  if (!WB.ctx || !WB.ctx.alert || WB.ctx.alert.alert_id !== alertId) return;
  if (!r.ok) {
    WB.loaded.sar = false;
    pane.innerHTML = `<div class="callout critical-box">${nxEsc((r.data && r.data.error) || 'Could not draft the SAR.')}</div>`;
    return;
  }
  const d = WB.sar = r.data;
  const rows = s => (s.rows && s.rows.length
    ? `<dl class="sar-rows">${s.rows.map(x => `<dt>${nxEsc(x.label)}</dt><dd>${nxEsc(x.value)}</dd>`).join('')}</dl>` : '');
  const factors = s => (!s.factors ? '' : `
    <div class="sec-head" style="margin:12px 0 6px">Top factors (exact SHAP, log-odds)</div>
    <ul class="sar-list">${[...s.factors.raises.map(f => ['up', f]), ...s.factors.lowers.map(f => ['down', f])].map(([k, f]) =>
      `<li><span class="${k}">${f.impact > 0 ? '+' : '−'}${Math.abs(f.impact).toFixed(2)}</span> <b>${nxEsc(f.title)}</b> — ${nxEsc(f.detail)}</li>`
    ).join('') || '<li>Explanation unavailable.</li>'}</ul>`);
  const audit = s => (!('audit' in s) ? '' : (s.audit.length
    ? `<ul class="sar-list">${s.audit.map(e => `<li><b>${nxEsc(WB_ACTION_LABEL[e.action] || e.action)}</b> by ${nxEsc(e.analyst)}
        <span class="loading-note">${nxEsc(new Date(e.ts).toLocaleString())}</span><br>${nxEsc(e.note)}</li>`).join('')}</ul>`
    : '<div class="loading-note">No analyst actions recorded yet.</div>'));
  const fileBox = d.filed
    ? '<div class="callout" style="margin:4px 0 0">This SAR has been filed — the filing is recorded in the audit trail above.</div>'
    : d.can_file
      ? `<div class="sar-file"><div class="sec-head" style="margin:0 0 8px">File this SAR</div>
          <div class="wb-wf-grid">
            <div class="field"><label class="field-label" for="wb-sar-analyst">Analyst</label>
              <input type="text" id="wb-sar-analyst" maxlength="60" value="${nxEsc(wbAnalyst())}" placeholder="Your name"></div>
            <div class="field"><label class="field-label" for="wb-sar-note">Filing note (required)</label>
              <textarea id="wb-sar-note" maxlength="1000" placeholder="e.g. Reviewed the narrative and evidence; filed with the FIU"></textarea></div>
          </div>
          <div class="wb-wf-buttons"><button class="wb-btn sar" data-sar="file">File SAR</button></div>
          <div class="wb-msg" id="wb-sar-msg"></div></div>`
      : `<div class="callout warn-box" style="margin:4px 0 0">To file this SAR, first <b>Confirm fraud</b> or <b>Escalate</b> the alert in the
          <button class="wb-link" data-go="overview">Overview</button> tab. You can still copy or download the draft.</div>`;
  pane.innerHTML = `
    <div class="sar-head">
      <div><div class="sar-title">Suspicious Activity Report — ${d.filed ? 'filed' : 'draft'}</div>
        <div class="sar-meta">${nxEsc(d.alert_id)} · ${nxEsc(d.tx_id)} · generated ${nxEsc(new Date(d.generated_at).toLocaleString())}</div></div>
      <div class="sar-tools">
        <button class="btn-ghost" data-sar="copy">Copy text</button>
        <button class="btn-ghost" data-sar="download">Download .txt</button>
        <button class="btn-ghost" data-sar="refresh">Regenerate</button>
      </div>
    </div>
    <div class="wb-msg" id="wb-sar-tool-msg"></div>
    ${d.sections.map(s => `<div class="sar-sec"><h4>${nxEsc(s.title)}</h4>${rows(s)}${factors(s)}${audit(s)}</div>`).join('')}
    <div class="sar-sec"><h4>7. Narrative</h4><div class="sar-narr">${nxEsc(d.narrative)}</div></div>
    ${fileBox}
    <div class="sar-disclaimer">${nxEsc(d.disclaimer)}</div>`;
  if (okMsg) wbSetMsg('wb-sar-tool-msg', okMsg, 'ok');
}

async function wbSarTool(kind) {
  if (kind === 'refresh') { WB.loaded.sar = false; await wbLoadSar('Regenerated.'); return; }
  if (kind === 'file') { await wbAct('FILE_SAR', WB_FORM_SAR); return; }
  const d = WB.sar;
  if (!d) return;
  if (kind === 'copy') {
    try {
      await navigator.clipboard.writeText(d.text);
      wbSetMsg('wb-sar-tool-msg', 'Copied to the clipboard.', 'ok');
    } catch (e) {
      wbSetMsg('wb-sar-tool-msg', 'The clipboard is not available here — use Download instead.', 'err');
    }
  } else if (kind === 'download') {
    const url = URL.createObjectURL(new Blob([d.text], { type: 'text/plain;charset=utf-8' }));
    const a = Object.assign(document.createElement('a'), { href: url, download: `SAR_${d.alert_id}_${d.tx_id}.txt` });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

/* ---------- 5. Live Transaction Simulator (docked User Portal) ---------- */
async function wbDockSimulator() {
  const ok = await portalInit();
  const split = nx$('portal-split'), dock = nx$('wb-sim-dock');
  if (!ok || !split || !dock) {
    dock.innerHTML = '<div class="callout critical-box">The simulator could not start (User Portal unavailable).</div>';
    return;
  }
  if (split.parentElement !== dock) {
    PORTAL.home = PORTAL.home || split.parentElement;
    dock.appendChild(split);
    const note = nx$('portal-docked-note');
    if (note) note.style.display = '';
  }
  const t = WB.ctx && WB.ctx.txn;
  if (t && WB.simFor !== t.row) {
    WB.simFor = t.row;
    const txnDay = Math.max(t.txn_day, t.txn_hour);
    portalApply({
      amount: t.amount_paid, payment_format: t.payment_format,
      send_currency: t.pay_currency, receive_currency: t.recv_currency,
      sender_bank: t.sender.bank, receiver_bank: t.receiver.bank, self_transfer: t.self_transfer,
      hour: t.hour, weekday: t.day_of_week >= 0 ? t.day_of_week : 0,
      usual_amount: t.usual_amount !== null ? t.usual_amount : t.amount_paid,
      txn_hour: Math.max(1, t.txn_hour), txn_day: Math.max(1, txnDay),
    });
    document.querySelectorAll('.preset').forEach(b => b.classList.remove('active'));
    nx$('wb-sim-note').innerHTML = `<b>Live Transaction Simulator</b> — pre-filled from <b>${nxEsc(t.tx_id)}</b>'s real values
      (amount, method, currencies, banks, time, sender's usual amount and velocity). Edit anything to test a what-if; the verdict re-scores as you type.` +
      (t.usual_amount === null ? ' The sender\'s usual amount is not recorded for this row, so it is set equal to this payment.' : '') +
      (txnDay !== t.txn_day ? ' Transfers-today was raised to match transfers-this-hour.' : '');
    portalScore();
  }
}

function wbUndockSimulator() {
  const split = nx$('portal-split');
  if (split && PORTAL.home && split.parentElement !== PORTAL.home) {
    PORTAL.home.appendChild(split);
  }
  const note = nx$('portal-docked-note');
  if (note) note.style.display = 'none';
}

/* ---------- search ---------- */
function wbSearch() {
  const mode = nx$('wb-search-mode').value;
  const raw = (nx$('wb-search-input').value || '').trim().toUpperCase();
  if (!raw) { wbSetMsg('wb-search-status', 'Enter an ID to look up.', 'err'); return; }
  wbSetMsg('wb-search-status', 'Looking up…');
  const kind = raw.startsWith('AML-') ? 'alert' : raw.startsWith('ACC-') ? 'account' : raw.startsWith('TX-') ? 'txn' : mode;
  const digits = raw.replace(/^(AML|ACC|TX)-/, '');
  if (!/^\d+$/.test(digits)) { wbSetMsg('wb-search-status', 'IDs are numbers, optionally prefixed TX-, AML- or ACC-.', 'err'); return; }
  if (kind === 'alert') {
    wbSetMsg('wb-search-status', '');
    wbOpenAlert('AML-' + digits.padStart(6, '0'));
  } else if (kind === 'account') {
    wbSetMsg('wb-search-status', '');
    wbOpenAccount(Number(digits));
  } else {
    wbOpenTxn(String(Number(digits)));
  }
}

/* ---------- init ---------- */
function wbInit() {
  if (WB.inited) return;
  WB.inited = true;
  const placeholders = { txn: 'TX-0001234', alert: 'AML-000001', account: 'ACC-743' };
  nx$('wb-search-mode').addEventListener('change', e => { nx$('wb-search-input').placeholder = placeholders[e.target.value]; });
  nx$('wb-search-btn').addEventListener('click', wbSearch);
  nx$('wb-search-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); wbSearch(); } });
  nx$('wb-view').addEventListener('change', e => {
    WB.view = e.target.value; WB.page = 1;
    const cases = WB.view === 'cases';
    nx$('wb-level').style.display = cases ? 'none' : '';
    nx$('wb-order').style.display = cases ? 'none' : '';
    nx$('wb-case-status').style.display = cases ? '' : 'none';
    wbLoadQueue();
  });
  ['wb-level', 'wb-order', 'wb-case-status'].forEach(id =>
    nx$(id).addEventListener('change', () => { WB.page = 1; wbLoadQueue(); }));
  nx$('wb-queue-pager').addEventListener('click', e => {
    const b = e.target.closest('[data-qp]');
    if (b && !b.disabled) { WB.page = Number(b.dataset.qp); wbLoadQueue(); nx$('wb-queue-list').scrollTop = 0; }
  });
  nx$('wb-queue-list').addEventListener('click', e => {
    const b = e.target.closest('.wb-q-item');
    if (b) wbOpenAlert(b.dataset.alert);
  });
  nx$('wb-tabs').addEventListener('click', e => {
    const b = e.target.closest('.model-btn');
    if (b && !b.disabled) wbShowTab(b.dataset.tab);
  });
  nx$('wb-detail').addEventListener('click', e => {
    const t = e.target;
    const go = t.closest('[data-go]');
    if (go) { wbShowTab(go.dataset.go); return; }
    const acc = t.closest('[data-acc]');
    if (acc) { wbOpenAccount(acc.dataset.acc); return; }
    const tx = t.closest('[data-tx]');
    if (tx) { wbOpenTxn(tx.dataset.tx); return; }
    const alertOpen = t.closest('[data-alert-open]');
    if (alertOpen) { wbOpenAlert(alertOpen.dataset.alertOpen); return; }
    const sar = t.closest('[data-sar]');
    if (sar) { wbSarTool(sar.dataset.sar); return; }
    const act = t.closest('[data-act]');
    if (act) { wbAct(act.dataset.act); return; }
    const net = t.closest('[data-net]');
    if (net) { wbLoadNetwork(net.dataset.net); return; }
    const hacc = t.closest('[data-hacc]');
    if (hacc) { WB.histAccount = Number(hacc.dataset.hacc); WB.histPage = 1; wbLoadHistory(); return; }
    const hrole = t.closest('[data-hrole]');
    if (hrole) { WB.histRole = hrole.dataset.hrole; WB.histPage = 1; wbLoadHistory(); return; }
    const hp = t.closest('[data-hp]');
    if (hp && !hp.disabled) { WB.histPage = Number(hp.dataset.hp); wbLoadHistory(); }
  });
  nx$('wb-net-account').addEventListener('change', e => wbLoadNetwork(e.target.value));
  nx$('wb-net-depth').addEventListener('change', () => { if (WB.netAccount !== null) wbLoadNetwork(WB.netAccount); });
  nx$('wb-net-limit').addEventListener('change', () => { if (WB.netAccount !== null) wbLoadNetwork(WB.netAccount); });
  nx$('wb-net-fit').addEventListener('click', () => { if (WB.cy) WB.cy.fit(undefined, 30); });
  nx$('wb-stats').addEventListener('click', e => {
    const b = e.target.closest('[data-stat]');
    if (b) wbFilterByStatus(b.dataset.stat);
  });
  wbEnableTabs();
  wbPollIndex();
  wbLoadQueue();
  wbLoadStats();
}

/* ================================================================
   LINK SECURITY — static phishing-link check
   Backend: POST /api/check-url (new.py). The server only reads the
   link text; it never opens the website.
   ================================================================ */
const LS = { inited: false, seq: 0 };

function lsRender(d) {
  nx$('ls-empty').style.display = 'none';
  nx$('ls-result').style.display = '';
  const pill = nx$('ls-level');
  pill.style.display = '';
  pill.className = 'pill ' + d.risk_level;
  pill.innerHTML = '<span class="dot"></span>' + nxEsc(d.risk_level);
  nx$('ls-score').textContent = d.risk_score;
  nx$('ls-advice').textContent = d.recommendation;
  const meter = nx$('ls-meter');
  meter.className = d.risk_level;
  meter.style.width = Math.max(2, d.risk_score) + '%';
  nx$('ls-host').textContent = d.host;
  nx$('ls-full').textContent = d.url;
  const inds = d.indicators || [];
  nx$('ls-indicators').innerHTML = inds.length
    ? inds.map(i => `<div class="ls-ind"><span>${nxEsc(i.reason)}</span><b>+${Number(i.points)}</b></div>`).join('')
    : '<div class="empty-note">No red flags found in this link.</div>';
  nx$('ls-notes').textContent = (d.notes || []).join(' ');
}

async function lsCheck() {
  const url = nx$('ls-url').value.trim();
  if (!url) { wbSetMsg('ls-status', 'Enter a link to check.', 'err'); return; }
  const seq = ++LS.seq;
  const btn = nx$('ls-check');
  btn.disabled = true;
  wbSetMsg('ls-status', 'Checking…');
  const r = await wbFetch('/api/check-url', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }),
  });
  if (seq !== LS.seq) return;
  btn.disabled = false;
  if (!r.ok || !r.data || !r.data.success) {
    wbSetMsg('ls-status', (r.data && r.data.error) || 'Link check failed.', 'err');
    return;
  }
  wbSetMsg('ls-status', '');
  lsRender(r.data);
}

function lsInit() {
  if (LS.inited || !nx$('ls-check')) return;
  LS.inited = true;
  nx$('ls-check').addEventListener('click', lsCheck);
  nx$('ls-url').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); lsCheck(); } });
  nx$('ls-url').addEventListener('input', () => wbSetMsg('ls-status', ''));
  nx$('ls-examples').addEventListener('click', e => {
    const b = e.target.closest('button[data-url]');
    if (!b) return;
    nx$('ls-url').value = b.dataset.url;
    lsCheck();
  });
}

/* ================================================================
   USER PORTAL PAGE — two sub-tabs: Screen a transaction · Link Security
   ================================================================ */
const UP = { inited: false };

function upShowView(view) {
  document.querySelectorAll('#up-tabs button').forEach(b => b.classList.toggle('on', b.dataset.view === view));
  document.querySelectorAll('#pg-portal .up-view').forEach(v => v.classList.toggle('on', v.id === 'up-view-' + view));
  if (view === 'link') lsInit();
  else { wbUndockSimulator(); portalInit(); }
}

function upInit() {
  if (!UP.inited) {
    UP.inited = true;
    nx$('up-tabs').addEventListener('click', e => {
      const b = e.target.closest('button[data-view]');
      if (b) upShowView(b.dataset.view);
    });
  }
  const active = document.querySelector('#up-tabs button.on');
  upShowView(active ? active.dataset.view : 'screen');
}

/* ================================================================
   REGISTER WITH THE DASHBOARD
   new.html's NEW:NAV adds two sidebar items and NEW:PAGES adds their
   .page divs; the dashboard's own router (showPage / PAGE_META /
   LOADERS in static/dashboard.js) shows them and calls the loaders.
   ================================================================ */
if (typeof PAGE_META !== 'undefined') {
  PAGE_META.portal = ['User Portal', 'Screen a transaction · check a suspicious link'];
  PAGE_META.workbench = ['Investigation Workbench', 'Alert queue · case detail · why flagged · linked accounts · SAR report · analyst workflow'];
}
if (typeof LOADERS !== 'undefined') {
  LOADERS.portal = upInit;
  LOADERS.workbench = wbInit;
}
/* The loaders run once per page; re-entering the User Portal must still
   bring a simulator that was docked in the Workbench back home. */
document.querySelectorAll('.nav-item[data-page="portal"]').forEach(n =>
  n.addEventListener('click', () => { if (UP.inited) upInit(); }));
if (nx$('portal-undock')) nx$('portal-undock').addEventListener('click', wbUndockSimulator);
if (!nx$('pg-portal') || !nx$('pg-workbench')) {
  console.error('FinShield new.js: the User Portal / Investigation Workbench pages are missing from the page. ' +
    'Check that new.html still has its <!-- NEW:NAV --> and <!-- NEW:PAGES --> marker sections.');
}
