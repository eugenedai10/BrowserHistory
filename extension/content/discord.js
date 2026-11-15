// Discord web app (discord.com/channels/...): which channels you read and what you post.
// Class names on Discord are obfuscated, so only stable attributes are used:
//   ol[data-list-id="chat-messages"], li[id^="chat-messages-"], div[role="textbox"][aria-label^="Message"]
(() => {
  const CH = window.__CH;
  if (!CH || window.__CH_DISCORD) return;
  window.__CH_DISCORD = true;

  const SEL = {
    list: 'ol[data-list-id="chat-messages"]',
    message: 'li[id^="chat-messages-"]',
    textbox: '[role="textbox"]',
  };
  const DISCORD_EPOCH = 1420070400000n;

  let cur = null;
  let lastStateSent = 0;
  let observedList = null;
  let mutObs = null;
  let intObs = null;
  let lastNewMessageAt = 0;

  const rnd1 = (n) => Math.round(n * 10) / 10;
  const isActive = () => document.visibilityState === 'visible' && document.hasFocus();

  function parseLocation() {
    const m = location.pathname.match(/^\/channels\/(@me|\d+)\/(\d+)/);
    return m ? { guild: m[1], channel: m[2], dm: m[1] === '@me' } : null;
  }

  // Title formats: "#general | Server - Discord", "Discord | #general | Server", "@user - Discord"
  function parseTitle() {
    let t = document.title.replace(/^\(\d+\)\s*/, '').replace(/^[•●]\s*/, '');
    t = t.replace(/\s*-\s*Discord$/, '');
    const parts = t.split(/\s*\|\s*/).filter((p) => p && p !== 'Discord');
    let channel = '', guild = '';
    for (const p of parts) {
      if (!channel && (p.startsWith('#') || p.startsWith('@'))) channel = p;
      else if (!guild) guild = p;
    }
    return { channel, guild };
  }

  function snowflakeIso(id) {
    try { return CH.isoLocal(new Date(Number((BigInt(id) >> 22n) + DISCORD_EPOCH))); } catch { return null; }
  }

  function messageId(li) {
    const m = li.id.match(/^chat-messages-(?:\d+-)?(\d+)$/);
    return m ? m[1] : null;
  }

  // ---- channel sessions ----
  function startChannel(loc) {
    cur = {
      guild_id: loc.guild, channel_id: loc.channel, dm: loc.dm,
      enter: CH.isoLocal(), activeMs: 0, lastSample: Date.now(),
      seen: new Set(), channel_name: '', guild_name: '', nameSetAt: 0,
    };
  }

  function snapshot() {
    let first = null, last = null;
    for (const id of cur.seen) {
      if (first === null || BigInt(id) < BigInt(first)) first = id;
      if (last === null || BigInt(id) > BigInt(last)) last = id;
    }
    return {
      ts: CH.isoLocal(), type: 'discord_channel',
      guild_id: cur.guild_id, channel_id: cur.channel_id, dm: cur.dm,
      guild_name: cur.guild_name, channel_name: cur.channel_name,
      enter: cur.enter, leave: CH.isoLocal(),
      dur_active_s: rnd1(cur.activeMs / 1000),
      messages_seen: cur.seen.size,
      oldest_seen_msg_ts: first ? snowflakeIso(first) : null,
      newest_seen_msg_ts: last ? snowflakeIso(last) : null,
    };
  }

  function endChannel(reason) {
    if (!cur) return;
    sampleTime();
    if (cur.activeMs >= 1000 || cur.seen.size > 0) {
      const ev = snapshot();
      ev.reason = reason;
      CH.send({ k: 'event', ev, clears: 'discord' });
    }
    cur = null;
  }

  function sampleTime() {
    if (!cur) return;
    const now = Date.now();
    const dt = now - cur.lastSample;
    cur.lastSample = now;
    if (isActive() && dt < 5000) cur.activeMs += dt;
  }

  function refreshNames() {
    if (!cur) return;
    const n = parseTitle();
    if (n.channel) cur.channel_name = n.channel;
    if (n.guild && !cur.dm) cur.guild_name = n.guild;
  }

  // ---- messages seen (counted when they scroll into view while the tab is active) ----
  function ensureObservers() {
    const list = document.querySelector(SEL.list);
    if (list === observedList) return;
    if (mutObs) mutObs.disconnect();
    if (intObs) intObs.disconnect();
    observedList = list;
    if (!list) return;
    intObs = new IntersectionObserver((entries) => {
      if (!cur || !isActive()) return;
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const id = messageId(e.target);
        const m = e.target.id.match(/^chat-messages-(\d+)-/);
        if (id && (!m || m[1] === cur.channel_id)) cur.seen.add(id);
      }
    }, { threshold: 0.5 });
    const watch = (li) => { if (li.matches && li.matches(SEL.message)) intObs.observe(li); };
    list.querySelectorAll(SEL.message).forEach(watch);
    mutObs = new MutationObserver((muts) => {
      for (const mu of muts) {
        mu.addedNodes.forEach((n) => { if (n.nodeType === 1) { watch(n); lastNewMessageAt = Date.now(); } });
      }
    });
    mutObs.observe(list, { childList: true });
  }

  // ---- posting ----
  const blank = (s) => s.replace(/[\s​﻿]/g, '') === '';
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing || !e.isTrusted) return;
    const tb = e.target && e.target.closest ? e.target.closest(SEL.textbox) : null;
    if (!tb || !(tb.getAttribute('aria-label') || '').startsWith('Message')) return;
    if (tb.closest(SEL.message)) return; // inline edit of an existing message
    const text = (tb.innerText || '').trim();
    if (blank(text) || !cur) return;
    const at = Date.now();
    const snap = { guild_id: cur.guild_id, channel_id: cur.channel_id, dm: cur.dm, name: cur.channel_name, guild_name: cur.guild_name };
    // Enter may only have picked an autocomplete entry; it was a real post if the box emptied.
    setTimeout(() => {
      if (!blank(tb.innerText || '')) return;
      CH.send({
        k: 'event',
        ev: {
          ts: CH.isoLocal(new Date(at)), type: 'discord_post',
          guild_id: snap.guild_id, channel_id: snap.channel_id, dm: snap.dm,
          guild_name: snap.guild_name, channel_name: snap.name,
          length: text.length, text, confirmed: lastNewMessageAt >= at,
        },
      });
    }, 2500);
  }, true);

  // ---- loop ----
  function tick() {
    sampleTime();
    const loc = parseLocation();
    if (cur && (!loc || loc.channel !== cur.channel_id)) endChannel('switch');
    if (!cur && loc) startChannel(loc);
    if (cur) {
      const now = Date.now();
      if (!cur.channel_name || now - cur.nameSetAt > 5000) { cur.nameSetAt = now; refreshNames(); }
      ensureObservers();
      if (now - lastStateSent > 20000 && (cur.activeMs > 0 || cur.seen.size)) {
        lastStateSent = now;
        CH.send({ k: 'state', name: 'discord', ev: snapshot() });
      }
    }
  }
  setInterval(tick, 1000);
  window.addEventListener('pagehide', () => endChannel('pagehide'));
  tick();
})();
