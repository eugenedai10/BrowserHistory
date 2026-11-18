// YouTube video + Shorts tracking. Sampling state machine: media events act as hints, a 500ms
// sampler is the source of truth, so it survives <video> swaps (Shorts), ads and SPA navigation.
(() => {
  const CH = window.__CH;
  if (!CH || window.__CH_YT) return;
  window.__CH_YT = true;

  // ---- selectors (isolated here: YouTube's DOM changes) ----
  const SEL = {
    adShowing: '.html5-video-player.ad-showing, .html5-video-player.ad-interrupting',
    watchTitle: ['ytd-watch-metadata h1 yt-formatted-string', 'h1.ytd-watch-metadata', '#title h1'],
    watchChannel: ['ytd-watch-metadata #owner ytd-channel-name a', '#owner #channel-name a', 'ytd-video-owner-renderer a'],
    shortsTitle: ['ytd-reel-video-renderer[is-active] yt-shorts-video-title-view-model h2', 'ytd-reel-video-renderer[is-active] h2', 'ytd-reel-video-renderer[is-active] .title'],
    shortsChannel: ['ytd-reel-video-renderer[is-active] ytd-channel-name a', 'ytd-reel-video-renderer[is-active] a[href^="/@"]'],
    embedTitle: ['.ytp-title-link', '.ytp-title-text a', '.ytp-title-text'],
    liked: 'ytd-watch-metadata like-button-view-model button[aria-pressed="true"], ytd-watch-metadata #segmented-like-button button[aria-pressed="true"]',
    likeBtn: 'ytd-watch-metadata like-button-view-model button, ytd-watch-metadata #segmented-like-button button',
    subscribed: 'ytd-watch-metadata ytd-subscribe-button-renderer[subscribed]',
    subscribeBtn: 'ytd-watch-metadata ytd-subscribe-button-renderer',
    captionsOn: '.ytp-subtitles-button[aria-pressed="true"]',
    miniplayer: 'ytd-miniplayer[active]',
  };
  const SAMPLE_MS = 500;
  const META_TIMEOUT_MS = 4000;

  const qsa = (sels) => { for (const s of sels) { const el = document.querySelector(s); if (el && el.textContent.trim()) return el; } return null; };
  const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
  const inAd = () => !!document.querySelector(SEL.adShowing);
  const rnd1 = (n) => Math.round(n * 10) / 10;

  // Shorts keeps several <video> elements (preloaded neighbours), so pick by what is on screen / playing.
  function videoScore(v) {
    const r = v.getBoundingClientRect();
    const visH = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
    const visW = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
    let score = visH * visW;
    if (!v.paused && !v.ended) score += 1e9;             // a playing video beats a merely visible one
    if (v.closest && v.closest('ytd-reel-video-renderer[is-active]')) score += 1e6;
    return score;
  }
  function getVideo() {
    const vids = Array.from(document.querySelectorAll('video'));
    if (vids.length <= 1) return vids[0] || null;
    let best = null, bestScore = -1;
    for (const v of vids) {
      const s = videoScore(v);
      if (s > bestScore) { best = v; bestScore = s; }
    }
    return best;
  }

  function parseLocation() {
    const u = new URL(location.href);
    if (u.pathname === '/watch' && u.searchParams.get('v')) return { id: u.searchParams.get('v'), kind: 'video' };
    let m = u.pathname.match(/^\/shorts\/([\w-]{5,})/);
    if (m) return { id: m[1], kind: 'short' };
    m = u.pathname.match(/^\/live\/([\w-]{5,})/);
    if (m) return { id: m[1], kind: 'video' };
    // Embedded player (an iframe on another site). 'videoseries' is a playlist embed: the video id is not in the URL.
    m = u.pathname.match(/^\/embed\/([\w-]{5,})/);
    if (m && m[1] !== 'videoseries') return { id: m[1], kind: 'embed' };
    return { id: null, kind: null };
  }

  // ---- click source tracking (where did the user find this video?) ----
  let lastClick = null; // { id, source, at }
  function classifySource(a) {
    if (a.closest('ytd-watch-next-secondary-results-renderer, #related, ytd-compact-video-renderer')) return 'sidebar';
    if (a.closest('ytd-playlist-panel-renderer')) return 'playlist';
    if (a.closest('ytd-end-screen, .ytp-endscreen-content, .ytp-ce-element, .ytp-videowall-still')) return 'endscreen';
    const browse = a.closest('ytd-browse');
    if (browse && browse.getAttribute('page-subtype')) return browse.getAttribute('page-subtype'); // home, subscriptions, channel, history...
    if (location.pathname === '/results') return 'search';
    if (location.pathname === '/') return 'home';
    if (location.pathname.startsWith('/feed/')) return 'feed';
    return 'other';
  }
  CH.clickHook = (a, href) => {
    let u;
    try { u = new URL(href); } catch { return null; }
    if (!/(^|\.)youtube\.com$/.test(u.hostname)) return null;
    let id = null;
    if (u.pathname === '/watch') id = u.searchParams.get('v');
    else { const m = u.pathname.match(/^\/shorts\/([\w-]+)/); if (m) id = m[1]; }
    if (!id) return null;
    const source = classifySource(a);
    lastClick = { id, source, at: Date.now() };
    return { yt_source: source, yt_video_id: id };
  };

  // ---- session ----
  let cur = null;
  let prev = null; // summary of previous session, for autoplay / shorts-swipe inference
  let lastStateSent = 0;
  let lastLiveSent = 0;

  // oEmbed gives title + channel by video id, independent of YouTube's (changing) page layout.
  const oembedCache = new Map(); // id -> {title, author} | null (failed)
  // Embeds can live on youtube-nocookie.com, where a page fetch to youtube.com is cross-origin: ask the worker instead.
  function fetchOembed(id, viaWorker) {
    if (oembedCache.has(id)) return;
    oembedCache.set(id, undefined); // in flight
    const url = 'https://www.youtube.com/oembed?format=json&url=' + encodeURIComponent('https://www.youtube.com/watch?v=' + id);
    (viaWorker
      ? chrome.runtime.sendMessage({ k: 'yt_oembed', id }).then((r) => (r && r.ok ? r : Promise.reject(new Error('oembed worker'))))
      : fetch(url, { credentials: 'omit' }).then((r) => (r.ok ? r.json() : Promise.reject(new Error('oembed ' + r.status)))))
      .then((j) => oembedCache.set(id, { title: clean(j.title), author: clean(j.author_name) }))
      .catch(() => oembedCache.set(id, null));
    if (oembedCache.size > 200) oembedCache.delete(oembedCache.keys().next().value);
  }

  function readMeta() {
    if (!cur) return;
    const oe = oembedCache.get(cur.id); // undefined = pending, null = failed
    if (oe) {
      cur.title = oe.title || cur.title;
      cur.channel = oe.author || cur.channel;
      return !!cur.title;
    }
    if (cur.kind !== 'video' && oe === undefined && Date.now() - cur.created < 3000) return false; // wait for oEmbed
    let title = '', channel = '';
    if (cur.kind === 'short') {
      title = clean((qsa(SEL.shortsTitle) || {}).textContent);
      channel = clean((qsa(SEL.shortsChannel) || {}).textContent);
    } else if (cur.kind === 'embed') {
      title = clean((qsa(SEL.embedTitle) || {}).textContent);
    } else {
      const flexy = document.querySelector('ytd-watch-flexy');
      const fresh = !flexy || flexy.getAttribute('video-id') === cur.id; // avoid stale DOM from the previous video
      if (fresh) {
        title = clean((qsa(SEL.watchTitle) || {}).textContent);
        channel = clean((qsa(SEL.watchChannel) || {}).textContent);
      }
    }
    if (!title) {
      const t = clean(document.title).replace(/^\(\d+\)\s*/, '').replace(/\s*-\s*YouTube( Music)?$/, '');
      if (t && t !== 'YouTube' && !/^YouTube video player$/i.test(t)) title = t; // the embed's generic title says nothing
    }
    if (title) cur.title = title;
    if (channel) cur.channel = channel;
    return !!(cur.title && (cur.channel || Date.now() - cur.created > 2500));
  }

  function base(v) {
    const len = v && isFinite(v.duration) ? Math.round(v.duration) : cur.length;
    if (len) cur.length = len;
    return { video_id: cur.id, kind: cur.kind, title: cur.title, channel: cur.channel, length_s: cur.length || null };
  }

  function emitYt(type, extra, v) {
    if (!cur) return;
    const ev = { ts: CH.isoLocal(), type, ...base(v), ...extra };
    if (!cur.metaReady) { cur.buf.push(ev); return; }
    CH.send({ k: 'event', ev });
  }
  function releaseBuffer() {
    cur.metaReady = true;
    for (const ev of cur.buf) {
      ev.title = cur.title; ev.channel = cur.channel; ev.length_s = cur.length || ev.length_s;
      CH.send({ k: 'event', ev });
    }
    cur.buf = [];
  }

  // ---- watch-time ticks for the reminder (the worker keeps today's totals) ----
  const IS_MUSIC = location.hostname === 'music.youtube.com';
  const tickMs = { video: 0, short: 0 };
  let lastTickSent = Date.now();
  function flushTicks(force) {
    const now = Date.now();
    if (!force && now - lastTickSent < 5000) return;
    lastTickSent = now;
    for (const kind of ['video', 'short']) {
      if (tickMs[kind] <= 0) continue;
      CH.send({ k: 'yt_tick', kind, ms: Math.round(tickMs[kind]), music: IS_MUSIC });
      tickMs[kind] = 0;
    }
  }

  // ---- reminder popup (Shadow DOM so YouTube's CSS can't touch it) ----
  function showReminder(message) {
    const old = document.getElementById('__ch_yt_reminder');
    if (old) old.remove();
    const host = document.createElement('div');
    host.id = '__ch_yt_reminder';
    host.style.cssText = 'all:initial;position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:2147483647;';
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = `
      <style>
        .box { font: 15px/1.4 -apple-system, system-ui, sans-serif; color: #fff; background: #d63031;
               padding: 14px 18px; border-radius: 12px; box-shadow: 0 6px 24px rgba(0,0,0,.4);
               display: flex; align-items: center; gap: 16px; max-width: 90vw; }
        b { display: block; font-size: 13px; opacity: .85; }
        button { font: inherit; font-weight: 600; cursor: pointer; border: 0; border-radius: 8px;
                 padding: 6px 14px; background: #fff; color: #d63031; }
      </style>
      <div class="box" role="alert"><div><b>YouTube reminder</b><span class="msg"></span></div><button>Dismiss</button></div>`;
    root.querySelector('.msg').textContent = message;
    root.querySelector('button').addEventListener('click', () => host.remove());
    (document.fullscreenElement || document.documentElement).appendChild(host);
  }
  chrome.runtime.onMessage.addListener((msg) => {
    if (window !== window.top) return; // embed iframes must not show the banner
    if (msg && msg.k === 'yt_reminder') showReminder(msg.message || 'Time to take a break.');
  });

  function newSession(loc) {
    let source;
    if (loc.kind === 'embed') source = 'embed';
    else if (lastClick && lastClick.id === loc.id && Date.now() - lastClick.at < 15000) source = lastClick.source;
    else if (prev && prev.kind === 'short' && loc.kind === 'short') source = 'shorts_swipe';
    else if (prev && prev.finished) source = 'autoplay';
    else source = document.referrer && !document.referrer.includes('youtube.com') ? 'external' : 'direct';
    const now = Date.now();
    cur = {
      sid: Math.random().toString(36).slice(2, 10), id: loc.id, kind: loc.kind, source,
      title: '', channel: '', length: 0, created: now,
      metaReady: false, buf: [],
      started: false, startTs: null, startPos: 0, playing: false, lastSample: now,
      watchedMs: 0, speedMs: 0, maxPos: 0, lastPos: 0, pauses: 0, seeks: 0, loops: 0, ads: 0,
      finished: false, wasAd: false, seekFrom: null,
    };
    fetchOembed(loc.id, loc.kind === 'embed');
  }

  function snapshot(v) {
    const len = cur.length || (v && isFinite(v.duration) ? Math.round(v.duration) : 0);
    const watched = cur.watchedMs / 1000;
    const f = cur.flags || {};
    return {
      ts: CH.isoLocal(), type: 'yt_session', sid: cur.sid,
      video_id: cur.id, kind: cur.kind, title: cur.title, channel: cur.channel, length_s: len || null,
      start: cur.startTs, end: CH.isoLocal(),
      start_position_s: cur.startPos,
      watched_s: rnd1(watched), max_position_s: rnd1(cur.maxPos),
      pct_watched: len ? Math.min(100, Math.round((cur.maxPos / len) * 100)) : null,
      finished: cur.finished || (len ? cur.maxPos >= len * 0.95 : false),
      pauses: cur.pauses, seeks: cur.seeks, loops: cur.loops, ads: cur.ads,
      avg_speed: cur.watchedMs ? rnd1(cur.speedMs / cur.watchedMs * 100) / 100 : 1,
      quality: f.quality || null, captions: !!f.captions,
      liked: f.liked === undefined ? null : f.liked,
      subscribed: f.subscribed === undefined ? null : f.subscribed,
      source: cur.source,
      music: IS_MUSIC, // music.youtube.com: logged, but excluded from the video stats
    };
  }

  // UI state (quality, captions, like, subscribe) is cached while the video is on screen,
  // because by the time a session ends the DOM may already show the next video.
  function refreshFlags(v) {
    const likeBtn = document.querySelector(SEL.likeBtn);
    const subBtn = document.querySelector(SEL.subscribeBtn);
    cur.flags = {
      quality: v && v.videoHeight ? v.videoHeight + 'p' : null,
      captions: !!document.querySelector(SEL.captionsOn),
      liked: cur.kind === 'video' && likeBtn ? !!document.querySelector(SEL.liked) : undefined,
      subscribed: cur.kind === 'video' && subBtn ? !!document.querySelector(SEL.subscribed) : undefined,
    };
  }

  function endSession(reason) {
    if (!cur) return;
    const v = getVideo();
    if (reason === 'pagehide') { sample(true); refreshFlags(v); } // after SPA navigation the DOM already shows the next video
    flushTicks(true);
    if (reason === 'pagehide' || !cur.title) readMeta();
    if (cur.started) {
      const ev = snapshot(v);
      ev.reason = reason;
      CH.send({ k: 'yt_live', ev: { ...ev } }); // final totals for the popup until the host log has the session
      if (!cur.metaReady) releaseBuffer();
      CH.send({ k: 'event', ev, clears: 'yt' });
    } else if (Date.now() - cur.created > 3000 && cur.kind !== 'embed') { // muted embeds are expected never to start
      // Diagnostic: a session lived >3s but never saw playback. Helps tune selectors for YouTube changes.
      CH.send({ k: 'event', ev: {
        ts: CH.isoLocal(), type: 'yt_unstarted', video_id: cur.id, kind: cur.kind, reason,
        age_s: rnd1((Date.now() - cur.created) / 1000), ad: inAd(),
        videos: Array.from(document.querySelectorAll('video')).slice(0, 6).map((x) => {
          const r = x.getBoundingClientRect();
          return { paused: x.paused, ended: x.ended, ready: x.readyState, t: rnd1(x.currentTime || 0),
                   dur: isFinite(x.duration) ? rnd1(x.duration) : null, w: Math.round(r.width), h: Math.round(r.height),
                   top: Math.round(r.top), in_active_reel: !!(x.closest && x.closest('ytd-reel-video-renderer[is-active]')) };
        }),
      } });
    }
    prev = { kind: cur.kind, finished: cur.finished || (cur.length && cur.maxPos >= cur.length * 0.95) };
    cur = null;
  }

  // ---- sampler ----
  function sample(force) {
    if (!cur) return;
    const v = getVideo();
    const now = Date.now();
    const dt = now - cur.lastSample;
    cur.lastSample = now;
    if (!v) return;

    // Right after an SPA navigation the <video> may still hold the previous video's state.
    const loc = parseLocation();
    if (loc.id && loc.id !== cur.id) return;
    if (cur.kind === 'video' && !cur.started) {
      const flexy = document.querySelector('ytd-watch-flexy');
      if (flexy && flexy.getAttribute('video-id') && flexy.getAttribute('video-id') !== cur.id) return;
    }

    if (!cur.metaReady) {
      if (readMeta() || now - cur.created > META_TIMEOUT_MS) releaseBuffer();
    }

    const ad = inAd();
    if (ad) {
      if (!cur.wasAd) cur.ads++;
      cur.wasAd = true;
      return; // ads: no accounting, no transitions
    }
    cur.wasAd = false;

    // An embed only counts while you can hear it: muted autoplay (news sites, hero videos) is not watching.
    const audible = !v.muted && v.volume > 0;
    const playing = !v.paused && !v.ended && (cur.kind !== 'embed' || audible);
    const pos = v.currentTime || 0;

    // accumulate watch time for the interval that just ended
    if (cur.playing && dt > 0 && dt < 5000) {
      cur.watchedMs += dt;
      cur.speedMs += dt * (v.playbackRate || 1);
      if (cur.kind in tickMs) tickMs[cur.kind] += dt; // reminders cover videos and Shorts only, not embeds
    }
    // loop detection (Shorts loop instead of ending)
    const len = isFinite(v.duration) ? v.duration : cur.length;
    if (len && cur.playing && cur.lastPos > len * 0.8 && pos < cur.lastPos - len * 0.5) {
      cur.loops++;
      if (cur.kind === 'short') cur.finished = true;
    }
    if (pos > cur.maxPos && !v.seeking) cur.maxPos = pos;
    cur.lastPos = pos;

    if (!cur.playing && playing) {
      if (!cur.started) {
        cur.started = true;
        cur.startTs = CH.isoLocal();
        cur.startPos = rnd1(pos);
        emitYt('yt_start', { position_s: rnd1(pos), speed: v.playbackRate || 1, source: cur.source }, v);
      } else {
        emitYt('yt_resume', { position_s: rnd1(pos) }, v);
      }
      cur.playing = true;
    } else if (cur.playing && !playing) {
      cur.playing = false;
      if (v.ended || (len && pos >= len - 0.3)) {
        cur.finished = true;
        emitYt('yt_end', { position_s: rnd1(pos), watched_s: rnd1(cur.watchedMs / 1000) }, v);
      } else {
        cur.pauses++;
        emitYt('yt_pause', { position_s: rnd1(pos), watched_s: rnd1(cur.watchedMs / 1000) }, v);
      }
    }
    if (cur.started && (!cur.flagsAt || now - cur.flagsAt > 5000)) { cur.flagsAt = now; refreshFlags(v); }
    // live snapshot for the toolbar popup (in-memory in the worker, not logged); only when watch time moved
    if (cur.started && cur.watchedMs !== cur.liveSentMs && now - lastLiveSent >= 1000) {
      lastLiveSent = now;
      cur.liveSentMs = cur.watchedMs;
      CH.send({ k: 'yt_live', ev: snapshot(v) });
    }
    if (cur.started && now - lastStateSent > 20000) {
      lastStateSent = now;
      CH.send({ k: 'state', name: 'yt', ev: snapshot(v) });
    }
  }

  // seeks come from events (positions jump)
  function onSeeking() {
    if (cur && cur.seekFrom === null && !inAd()) cur.seekFrom = cur.lastPos;
  }
  function onSeeked() {
    if (!cur || inAd()) { if (cur) cur.seekFrom = null; return; }
    const v = getVideo();
    if (!v) return;
    const from = cur.seekFrom;
    cur.seekFrom = null;
    if (from === null) return;
    const to = v.currentTime || 0;
    const len = isFinite(v.duration) ? v.duration : cur.length;
    const isLoopWrap = len && from > len - 3 && to < 3;
    if (Math.abs(to - from) > 1.5 && !isLoopWrap && cur.started) {
      cur.seeks++;
      emitYt('yt_seek', { from_s: rnd1(from), to_s: rnd1(to) }, v);
    }
    cur.lastPos = to;
    if (to > cur.maxPos) cur.maxPos = to;
  }

  // media events do not bubble: capture on document (also covers replaced <video> elements)
  ['play', 'playing', 'pause', 'ended', 'waiting', 'ratechange', 'emptied', 'volumechange'].forEach((t) =>
    document.addEventListener(t, () => sample(true), true));
  document.addEventListener('seeking', onSeeking, true);
  document.addEventListener('seeked', () => { onSeeked(); sample(true); }, true);

  // ---- navigation detection ----
  function checkUrl() {
    const loc = parseLocation();
    if (cur && loc.id === cur.id) return;
    if (cur && !loc.id) {
      // navigated away from the watch page: keep the session alive while the miniplayer still plays
      const v = getVideo();
      if (document.querySelector(SEL.miniplayer) && v && !v.paused) return;
    }
    if (cur) endSession('navigate');
    if (loc.id) newSession(loc);
  }

  setInterval(() => { checkUrl(); sample(false); flushTicks(false); }, SAMPLE_MS);
  document.addEventListener('yt-navigate-finish', checkUrl, true);
  window.addEventListener('pagehide', () => endSession('pagehide'));
  checkUrl();
})();
