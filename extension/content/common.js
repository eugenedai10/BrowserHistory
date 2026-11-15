// Runs on every http(s) page (top frame). Reports link clicks, scroll depth, title, word count, referrer.
// Also exposes window.__CH (shared by youtube.js / discord.js, same isolated world).
(() => {
  if (window.__CH) return;

  const pad = (n, w = 2) => String(n).padStart(w, '0');
  function isoLocal(d = new Date()) {
    const off = -d.getTimezoneOffset();
    const a = Math.abs(off);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}${off >= 0 ? '+' : '-'}${pad(Math.floor(a / 60))}:${pad(a % 60)}`;
  }
  function send(msg) {
    try {
      if (!chrome.runtime || !chrome.runtime.id) return; // extension reloaded; orphaned script
      chrome.runtime.sendMessage(msg).catch(() => {});
    } catch (e) { /* context invalidated */ }
  }

  const CH = { isoLocal, send, clickHook: null };
  window.__CH = CH;

  const urlKey = () => location.href.split('#')[0];

  // ----- clicks -----
  function findAnchor(e) {
    const path = e.composedPath ? e.composedPath() : [];
    for (const el of path) {
      if (el && el.tagName === 'A' && el.getAttribute && el.getAttribute('href')) return el;
    }
    const t = e.target;
    return t && t.closest ? t.closest('a[href]') : null;
  }
  function onClick(e) {
    if (!e.isTrusted) return;
    if (e.type === 'auxclick' && e.button !== 1) return;
    const a = findAnchor(e);
    if (!a) return;
    let href;
    try { href = new URL(a.getAttribute('href'), location.href).href; } catch { return; }
    if (href.startsWith('javascript:')) return;
    const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\s+/g, ' ').slice(0, 200);
    let external = false;
    try { external = new URL(href).host !== location.host; } catch {}
    const ev = {
      type: 'click', page_url: location.href, href, text,
      new_tab: e.button === 1 || e.ctrlKey || e.metaKey || e.shiftKey || a.target === '_blank',
      external,
    };
    if (CH.clickHook) {
      try { Object.assign(ev, CH.clickHook(a, href) || {}); } catch (err) { /* site hook must never break logging */ }
    }
    send({ k: 'click', ev });
  }
  document.addEventListener('click', onClick, true);
  document.addEventListener('auxclick', onClick, true);

  // ----- scroll depth -----
  let maxScroll = 0;
  let lastSent = 0;
  function scrollPct() {
    const el = document.documentElement;
    const total = Math.max(el.scrollHeight, (document.body && document.body.scrollHeight) || 0);
    const view = window.innerHeight;
    if (total <= view + 4) return 100;
    return Math.min(100, Math.round(((window.scrollY + view) / total) * 100));
  }
  function reportScroll(force) {
    const p = scrollPct();
    if (p > maxScroll) maxScroll = p;
    if (maxScroll > lastSent && (force || maxScroll - lastSent >= 5 || maxScroll === 100)) {
      lastSent = maxScroll;
      send({ k: 'scroll', url: urlKey(), pct: maxScroll });
    }
  }
  let scrollTimer = null;
  window.addEventListener('scroll', () => {
    if (scrollTimer) return;
    scrollTimer = setTimeout(() => { scrollTimer = null; reportScroll(false); }, 1000);
  }, { passive: true });
  window.addEventListener('pagehide', () => reportScroll(true));
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') reportScroll(true); });

  // ----- page info (title, words, referrer), incl. SPA navigations -----
  const CJK = /[぀-ヿ一-鿿가-힯]/g;
  function countWords() {
    const body = document.body;
    if (!body) return 0;
    const text = (body.innerText || '').slice(0, 500000);
    const cjk = (text.match(CJK) || []).length;
    const rest = (text.replace(CJK, ' ').match(/\S+/g) || []).length;
    return cjk + rest;
  }
  let prevUrl = '';
  let first = true;
  function sendPage() {
    const referrer = first ? document.referrer : prevUrl;
    first = false;
    send({ k: 'page', url: urlKey(), title: document.title, referrer, words: countWords() });
    reportScroll(false);
  }
  let curKey = urlKey();
  function onUrlChange() {
    prevUrl = curKey;
    curKey = urlKey();
    maxScroll = 0;
    lastSent = 0;
    setTimeout(sendPage, 1500); // let the SPA update title/content
  }
  setInterval(() => { if (urlKey() !== curKey) onUrlChange(); }, 1000);

  function initialPage() { setTimeout(sendPage, 1500); }
  if (document.readyState === 'complete') initialPage();
  else window.addEventListener('load', initialPage, { once: true });
})();
