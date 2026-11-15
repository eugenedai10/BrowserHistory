const $ = (id) => document.getElementById(id);

async function refresh() {
  const s = await chrome.runtime.sendMessage({ k: 'status' });
  const ok = s.host && s.host.ok;
  $('host').textContent = ok ? 'connected' : 'not connected';
  $('host').className = ok ? 'ok' : 'bad';
  $('today').textContent = ok ? s.host.events_today : '–';
  $('queue').textContent = s.queue;
  $('tabs').textContent = s.tracked_tabs;
  const inc = s.incognito_allowed;
  $('incog').textContent = inc ? 'logged' : inc === false ? 'NOT logged (enable in extension details)' : '–';
  $('incog').className = inc ? 'ok' : 'bad';
  $('file').textContent = ok ? s.host.file : '';
  $('err').textContent = ok ? '' : (s.host && s.host.error) || 'Run native-host/install.sh, then reload the extension.';
}

refresh();
