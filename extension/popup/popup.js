const $ = (id) => document.getElementById(id);

function fmt(sec) {
  sec = Math.floor(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  return m ? `${m}m${String(s).padStart(2, '0')}s` : `${s}s`;
}

function renderList(list, rows) {
  list.textContent = '';
  for (const e of rows) {
    const item = document.createElement('div');
    item.className = 'item';
    const t = document.createElement('div');
    t.className = 't';
    t.textContent = e.title || '(no title)';
    const m = document.createElement('div');
    m.className = 'm';
    const len = e.length_s ? ` of ${fmt(e.length_s)}` : '';
    m.textContent = `${fmt(e.watched_s)}${len} · ${e.channel || '?'}`;
    item.append(t, m);
    list.appendChild(item);
  }
}

function renderSection(kind, sessions) {
  const sec = $(kind);
  const rows = sessions.filter((e) => e.kind === kind);
  sec.querySelector('.time').textContent = fmt(rows.reduce((a, e) => a + (e.watched_s || 0), 0));
  sec.querySelector('.n').textContent = `(${rows.length})`;
  const list = sec.querySelector('.list');
  const btn = sec.querySelector('.toggle');
  renderList(list, rows);
  const label = () => (list.hidden ? `Show all (${rows.length})` : 'Hide');
  btn.disabled = !rows.length;
  btn.textContent = label();
  btn.onclick = () => { list.hidden = !list.hidden; btn.textContent = label(); };
}

async function refresh() {
  const r = await chrome.runtime.sendMessage({ k: 'yt_today' });
  if (!r || !r.ok) {
    $('err').textContent = (r && r.error) || 'Run native-host/install.sh, then reload the extension.';
    return;
  }
  const sessions = r.sessions || [];
  renderSection('video', sessions);
  renderSection('short', sessions);
}

refresh();
