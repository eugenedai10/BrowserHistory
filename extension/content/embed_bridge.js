// Runs in YouTube embed iframes on other sites (all_frames) just before youtube.js. It exposes the small part of
// window.__CH that youtube.js needs. common.js is deliberately NOT loaded in frames: its click/scroll/page reports
// would be attributed to the host tab. Keep isoLocal/send in sync with common.js.
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
  window.__CH = { isoLocal, send, clickHook: null };
})();
