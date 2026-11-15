const $ = (id) => document.getElementById(id);

async function refresh() {
  const s = await chrome.runtime.sendMessage({ k: 'status' });
  const ok = s.host && s.host.ok;
  $('host').textContent = ok ? 'connected' : 'not connected';
  $('host').className = ok ? 'ok' : 'bad';
  $('today').textContent = ok ? s.host.events_today : '–';
  $('queue').textContent = s.queue;
  $('tabs').textContent = s.tracked_tabs;
  $('state').textContent = s.paused ? 'paused' : 'on';
  $('state').className = s.paused ? 'bad' : 'ok';
  $('file').textContent = ok ? s.host.file : '';
  $('err').textContent = ok ? '' : (s.host && s.host.error) || 'Run native-host/install.sh, then reload the extension.';
  $('toggle').textContent = s.paused ? 'Resume logging' : 'Pause logging';
}

$('toggle').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ k: 'toggle' });
  refresh();
});

refresh();
