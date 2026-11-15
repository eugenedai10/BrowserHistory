// Chrome History Logger: service worker.
// Tracks page visits (total + active time), receives events from content scripts,
// and delivers batches to the native host, which appends JSONL under ~/chrome-history.
importScripts('config.js');

const HOST = 'com.chrome_history.logger';
const MAX_QUEUE = 20000;
const NO_WINDOW = chrome.windows.WINDOW_ID_NONE;

let queue = [];                 // events waiting for the native host
let tabs = {};                  // tabId -> visit state (persisted; the worker may sleep at any time)
let focusedWindowId = NO_WINDOW;
let idleState = 'active';
let hostStatus = { ok: null, error: null, lastFlush: null };
let flushTimer = null;
let flushing = false;
let port = null;                // long-lived native-messaging port (keeps the service worker alive)
let pending = [];               // requests awaiting a host reply, FIFO: {kind:'events', n} | {kind:'ping', resolve}
let reconnectTimer = null;
let backoff = 1000;
let lastHeartbeat = 0;
let incognitoAllowed = null;
let startLogged = false;
let persistTimer = null;
let chain = Promise.resolve();  // serializes navigation handling

// ---------- helpers ----------
const pad = (n, w = 2) => String(n).padStart(w, '0');
function iso(ms = Date.now()) {
  const d = new Date(ms);
  const off = -d.getTimezoneOffset();
  const a = Math.abs(off);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}${off >= 0 ? '+' : '-'}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
}
const round1 = (n) => Math.round(n * 10) / 10;
const isHttp = (u) => /^https?:\/\//i.test(u || '');
const stripHash = (u) => (u || '').split('#')[0];
const serial = (fn) => (chain = chain.then(fn).catch((e) => console.warn('serial error', e)));

function hostOf(u) {
  try { return new URL(u).hostname; } catch { return ''; }
}
function blocked(u) {
  const h = hostOf(u);
  return !!h && CONFIG.blocklist.some((b) => h === b || h.endsWith('.' + b) || h.includes(b));
}
function cleanUrl(u) {
  if (!u || !CONFIG.stripQuery) return u;
  try { const x = new URL(u); return x.origin + x.pathname; } catch { return u; }
}

// ---------- persistence ----------
function persistSoon() {
  if (persistTimer) return;
  persistTimer = setTimeout(() => {
    persistTimer = null;
    chrome.storage.local.set({ queue, tabs }).catch(() => {});
  }, 250);
}

// ---------- event queue ----------
function emit(ev, tabInfo) {
  const url = ev.url || ev.page_url;
  if (url && blocked(url)) return;
  const { ts, type, ...rest } = ev;
  const out = { ts: ts || iso(), type, ...rest };
  if (tabInfo) { out.tab = tabInfo.id; out.incognito = !!tabInfo.incognito; }
  for (const k of ['url', 'page_url', 'href', 'referrer', 'opener_url']) {
    if (out[k]) out[k] = cleanUrl(out[k]);
  }
  if (out.type === 'discord_post' && !CONFIG.discord.logPostText) delete out.text;
  queue.push(out);
  if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);
  persistSoon();
  if (queue.length >= CONFIG.flushBatchSize) scheduleFlush(0); else scheduleFlush(CONFIG.flushIntervalMs);
}

function scheduleFlush(delay) {
  if (flushTimer && delay > 0) return;
  if (flushTimer) clearTimeout(flushTimer);
  flushTimer = setTimeout(() => { flushTimer = null; flush(); }, delay);
}

// ---------- native host connection ----------
function scheduleReconnect() {
  if (reconnectTimer || port) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

function connect() {
  if (port) return;
  let p;
  try {
    p = chrome.runtime.connectNative(HOST);
  } catch (e) {
    hostStatus = { ...hostStatus, ok: false, error: String((e && e.message) || e) };
    updateBadge();
    scheduleReconnect();
    return;
  }
  port = p;
  pending = [];
  flushing = false;
  p.onMessage.addListener(onHostMessage);
  p.onDisconnect.addListener(() => {
    const err = (chrome.runtime.lastError && chrome.runtime.lastError.message) || 'host disconnected';
    if (port === p) port = null;
    for (const w of pending) if (w.kind === 'ping') w.resolve({ ok: false, error: err });
    pending = [];
    flushing = false;
    hostStatus = { ...hostStatus, ok: false, error: err };
    updateBadge();
    scheduleReconnect();
  });
  // Verify the host is really there (a missing host shows up as an immediate disconnect), then drain.
  ping();
  if (queue.length) scheduleFlush(0);
}

function onHostMessage(msg) {
  const w = pending.shift();
  if (!w) return;
  if (w.kind === 'ping') { w.resolve(msg); if (msg && msg.ok) markHostOk(); return; }
  flushing = false;
  if (msg && msg.ok) {
    queue.splice(0, w.n);
    markHostOk();
    persistSoon();
    if (queue.length) scheduleFlush(0);
  } else {
    hostStatus = { ...hostStatus, ok: false, error: (msg && msg.error) || 'host error' };
    updateBadge();
  }
}

function markHostOk() {
  hostStatus = { ok: true, error: null, lastFlush: Date.now() };
  backoff = 1000;
  updateBadge();
}

function ping() {
  return new Promise((resolve) => {
    if (!port) connect();
    if (!port) { resolve({ ok: false, error: hostStatus.error || 'not connected' }); return; }
    pending.push({ kind: 'ping', resolve });
    try { port.postMessage({ cmd: 'ping' }); } catch (e) { pending.pop(); resolve({ ok: false, error: String(e) }); return; }
    setTimeout(() => resolve({ ok: false, error: 'host did not answer' }), 3000);
  });
}

async function flush() {
  await ready;
  if (!port) { connect(); return; } // the batch is sent once connect() succeeds
  if (flushing || !queue.length) return;
  const batch = queue.slice(0, 200);
  flushing = true;
  pending.push({ kind: 'events', n: batch.length });
  try {
    port.postMessage({ events: batch });
  } catch (e) {
    pending.pop();
    flushing = false;
    hostStatus = { ...hostStatus, ok: false, error: String((e && e.message) || e) };
    updateBadge();
  }
}

function updateBadge() {
  chrome.action.setBadgeText({ text: hostStatus.ok === false ? '!' : '' });
  chrome.action.setBadgeBackgroundColor({ color: '#d93025' });
}

// ---------- visit tracking ----------
function computeActive(s) {
  return !!(s.inWindow && s.windowId === focusedWindowId && (idleState === 'active' || s.audible));
}

// Accumulate active time with the OLD state, apply a mutation, then recompute activity.
function change(fn) {
  const now = Date.now();
  for (const s of Object.values(tabs)) {
    if (s.activeNow) s.activeMs += now - s.lastTick;
    s.lastTick = now;
    s.lastSeen = now;
  }
  if (fn) fn();
  for (const s of Object.values(tabs)) s.activeNow = computeActive(s);
  persistSoon();
}

function newState(tab, url) {
  const now = Date.now();
  const opener = tab.openerTabId != null && tabs[tab.openerTabId] ? tabs[tab.openerTabId].url : '';
  const s = {
    tabId: tab.id, windowId: tab.windowId, url, title: tab.title || '',
    start: now, lastTick: now, lastSeen: now, activeMs: 0, activeNow: false,
    inWindow: !!tab.active, audible: !!tab.audible, incognito: !!tab.incognito,
    scroll: 0, words: 0, referrer: '', opener_url: opener, pending: {},
  };
  s.activeNow = computeActive(s);
  return s;
}

function finalizeVisit(s, endMs, reason) {
  const total = Math.max(0, (endMs - s.start) / 1000);
  if (total < 1) return;
  const active = Math.min(total, s.activeMs / 1000);
  emit({
    ts: iso(endMs), type: 'visit', url: s.url, title: s.title,
    referrer: s.referrer, opener_url: s.opener_url,
    start: iso(s.start), end: iso(endMs),
    dur_total_s: round1(total), dur_active_s: round1(active),
    max_scroll_pct: s.scroll, words: s.words,
    est_read_min: s.words ? round1(s.words / 230) : 0, reason,
  }, { id: s.tabId, incognito: s.incognito });
}

// Content scripts keep a snapshot of in-flight sessions; if the page vanished without
// reporting (crash / forced close) we emit the last snapshot, marked partial.
function flushPending(s) {
  for (const ev of Object.values(s.pending || {})) {
    if (ev) emit({ ...ev, partial: true }, { id: s.tabId, incognito: s.incognito });
  }
  s.pending = {};
}

function endVisit(tabId, reason, withPending) {
  const s = tabs[tabId];
  if (!s) return;
  change();
  delete tabs[tabId];
  finalizeVisit(s, Date.now(), reason);
  if (withPending) flushPending(s);
  persistSoon();
}

function parseSearch(u) {
  let x;
  try { x = new URL(u); } catch { return null; }
  const h = x.hostname.replace(/^www\./, '');
  const p = x.searchParams;
  if (/^google\.[a-z.]+$/.test(h) && x.pathname === '/search') return { engine: 'google', query: p.get('q') };
  if (h === 'bing.com' && x.pathname === '/search') return { engine: 'bing', query: p.get('q') };
  if (h === 'duckduckgo.com' && p.get('q')) return { engine: 'duckduckgo', query: p.get('q') };
  if (h === 'search.brave.com' && x.pathname === '/search') return { engine: 'brave', query: p.get('q') };
  if (h === 'search.yahoo.com' && x.pathname === '/search') return { engine: 'yahoo', query: p.get('p') };
  if ((h === 'youtube.com' || h === 'm.youtube.com') && x.pathname === '/results') return { engine: 'youtube', query: p.get('search_query') };
  return null;
}

async function onNavigate(details, isHistory) {
  if (details.frameId !== 0) return;
  await ready;
  const url = details.url;
  const s = tabs[details.tabId];
  const reload = details.transitionType === 'reload';
  if (s && !reload && stripHash(s.url) === stripHash(url)) return;
  if (s) endVisit(details.tabId, isHistory ? 'spa' : 'navigate', false);
  if (!isHttp(url)) return;
  let tab;
  try { tab = await chrome.tabs.get(details.tabId); } catch { return; }
  change(() => { tabs[details.tabId] = newState(tab, url); });
  const q = parseSearch(url);
  if (q && q.query) emit({ type: 'search', engine: q.engine, query: q.query, url }, tab);
}

async function adoptOpenTabs() {
  const all = await chrome.tabs.query({});
  for (const t of all) {
    if (isHttp(t.url) && !tabs[t.id]) tabs[t.id] = newState(t, t.url);
  }
}

// ---------- init (runs on every worker start) ----------
async function init() {
  const [loc, ses] = await Promise.all([
    chrome.storage.local.get(['queue', 'tabs']),
    chrome.storage.session.get(['boot']),
  ]);
  queue = loc.queue || [];
  chrome.storage.local.remove('paused').catch(() => {}); // pause feature was removed; drop stale value
  const saved = loc.tabs || {};
  chrome.idle.setDetectionInterval(CONFIG.idleSeconds);
  try { idleState = await chrome.idle.queryState(CONFIG.idleSeconds); } catch {}
  try {
    const w = await chrome.windows.getLastFocused();
    focusedWindowId = w.focused ? w.id : NO_WINDOW;
  } catch {}
  if (!ses.boot) {
    // Fresh browser session (or extension reload): close out visits left by the previous run.
    for (const s of Object.values(saved)) finalizeVisit(s, s.lastSeen || Date.now(), 'orphan');
    for (const s of Object.values(saved)) flushPending(s);
    tabs = {};
    await chrome.storage.session.set({ boot: Date.now() });
    await adoptOpenTabs();
  } else {
    tabs = saved;
  }
  change();
  chrome.alarms.create('tick', { periodInMinutes: 0.5 });
  incognitoAllowed = await isIncognitoAllowed();
  connect();
  updateBadge();
  scheduleFlush(1000);
  lastHeartbeat = Date.now();
  if (ses.boot) logStart('worker_restart');
  else setTimeout(() => logStart('enabled'), 4000); // onStartup/onInstalled normally claim this first
}
const ready = init();

// ---------- always-on markers: let the log explain any silence ----------
async function isIncognitoAllowed() {
  try { return await chrome.extension.isAllowedIncognitoAccess(); } catch { return null; }
}
function logStart(cause) {
  if (startLogged && cause !== 'worker_restart') return;
  startLogged = true;
  emit({ type: 'logger_start', cause, version: chrome.runtime.getManifest().version, incognito_allowed: incognitoAllowed });
}
async function heartbeat() {
  incognitoAllowed = await isIncognitoAllowed();
  lastHeartbeat = Date.now();
  emit({
    type: 'heartbeat', tracked_tabs: Object.keys(tabs).length, queue: queue.length,
    idle: idleState, focused: focusedWindowId !== NO_WINDOW, host_ok: hostStatus.ok,
    incognito_allowed: incognitoAllowed,
  });
}
chrome.runtime.onStartup.addListener(async () => { await ready; logStart('browser_start'); });
chrome.runtime.onInstalled.addListener(async (d) => {
  await ready;
  logStart(d.reason === 'install' ? 'install' : d.reason === 'update' ? 'extension_reload' : d.reason);
});

// ---------- listeners (must be registered synchronously) ----------
chrome.webNavigation.onCommitted.addListener((d) => serial(() => onNavigate(d, false)));
chrome.webNavigation.onHistoryStateUpdated.addListener((d) => serial(() => onNavigate(d, true)));

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  await ready;
  const s = tabs[tabId];
  if (!s) return;
  if (info.title) s.title = info.title;
  if (info.audible !== undefined) change(() => { s.audible = !!info.audible; });
});
chrome.tabs.onActivated.addListener(async ({ tabId, windowId }) => {
  await ready;
  change(() => {
    for (const s of Object.values(tabs)) if (s.windowId === windowId) s.inWindow = s.tabId === tabId;
  });
});
chrome.tabs.onRemoved.addListener(async (tabId) => { await ready; endVisit(tabId, 'closed', true); });
chrome.tabs.onAttached.addListener(async (tabId, info) => {
  await ready;
  change(() => { if (tabs[tabId]) tabs[tabId].windowId = info.newWindowId; });
});
chrome.tabs.onReplaced.addListener(async (added, removed) => {
  await ready;
  if (tabs[removed]) { tabs[added] = { ...tabs[removed], tabId: added }; delete tabs[removed]; persistSoon(); }
});
chrome.windows.onFocusChanged.addListener(async (id) => { await ready; change(() => { focusedWindowId = id; }); });
chrome.idle.onStateChanged.addListener(async (st) => { await ready; change(() => { idleState = st; }); });

chrome.alarms.onAlarm.addListener(async (a) => {
  if (a.name !== 'tick') return;
  await ready;
  change();
  if (Date.now() - lastHeartbeat >= CONFIG.heartbeatSeconds * 1000 - 2000) await heartbeat(); // -2s: alarm jitter
  flush(); // also reconnects the native port if it dropped
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender).then(sendResponse).catch((e) => sendResponse({ error: String(e) }));
  return true;
});

async function handleMessage(msg, sender) {
  await ready;
  const tab = sender && sender.tab;
  const s = tab ? tabs[tab.id] : null;
  switch (msg.k) {
    case 'page':
      if (s && stripHash(s.url) === stripHash(msg.url)) {
        if (msg.title) s.title = msg.title;
        s.words = msg.words || 0;
        s.referrer = msg.referrer || '';
        persistSoon();
      }
      break;
    case 'scroll':
      if (s && stripHash(s.url) === stripHash(msg.url)) { s.scroll = Math.max(s.scroll, msg.pct | 0); persistSoon(); }
      break;
    case 'click':
      if (tab) emit(msg.ev, tab);
      break;
    case 'event':
      if (tab) {
        emit(msg.ev, tab);
        if (s && msg.clears) { s.pending[msg.clears] = null; persistSoon(); }
      }
      break;
    case 'state':
      if (s) { s.pending[msg.name] = msg.ev; persistSoon(); }
      break;
    case 'status': {
      const host = await ping();
      incognitoAllowed = await isIncognitoAllowed();
      return { queue: queue.length, tracked_tabs: Object.keys(tabs).length, host, lastFlush: hostStatus.lastFlush, incognito_allowed: incognitoAllowed };
    }
    case 'flush':
      await flush();
      break;
  }
  return { ok: true };
}
