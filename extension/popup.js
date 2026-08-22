const BASE = 'http://127.0.0.1:8787';
const $ = id => document.getElementById(id);
const show = (id, on) => $(id).classList.toggle('hide', !on);

let tabInfo = null;
let poller = null;

async function api(path, opts) {
  const r = await fetch(BASE + path, Object.assign({
    headers: { 'Content-Type': 'application/json' }
  }, opts || {}));
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

/** Read playback position straight out of the page's <video> element. */
function probePage() {
  const v = document.querySelector('video');
  const t = document.querySelector('h1.ytd-watch-metadata, h1.title');
  return {
    currentTime: v ? v.currentTime : 0,
    duration: v ? v.duration : 0,
    paused: v ? v.paused : true,
    title: (t && t.innerText.trim()) || document.title.replace(/ - YouTube$/, '')
  };
}

async function init() {
  // 1. is the daemon up?
  try { await api('/api/ping'); }
  catch { show('offline', true); $('dot').className = 'dot err'; return; }

  // 2. are we on a YouTube watch page?
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/youtube\.com\/watch|youtu\.be\//.test(tab.url || '')) {
    show('notyt', true);
    return;
  }

  let page = { currentTime: 0, title: tab.title || '' };
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, func: probePage
    });
    if (res && res.result) page = res.result;
  } catch { /* fall back to the tab title */ }

  tabInfo = { url: tab.url, ...page };
  $('vidtitle').textContent = page.title;
  show('main', true);

  const saved = await chrome.storage.local.get(['size', 'fps']);
  if (saved.size) $('size').value = saved.size;
  if (saved.fps) $('fps').value = saved.fps;

  startPolling();
}

$('play').onclick = async () => {
  show('err', false);
  const size = +$('size').value, fps = +$('fps').value;
  chrome.storage.local.set({ size, fps });
  const start = $('from').value === 'current' ? Math.floor(tabInfo.currentTime || 0) : 0;
  try {
    await api('/api/play', {
      method: 'POST',
      body: JSON.stringify({ url: tabInfo.url, size, fps, start })
    });
  } catch (e) { fail(e.message); }
};

$('stop').onclick = () =>
  api('/api/stop', { method: 'POST' }).catch(e => fail(e.message));

function fail(m) { $('err').textContent = m; show('err', true); }

function startPolling() {
  const tick = async () => {
    try {
      const s = await api('/api/status');
      const busy = s.state === 'playing' || s.state === 'resolving';
      $('dot').className = 'dot' + (busy ? ' on' : s.state === 'error' ? ' err' : '');
      $('play').disabled = busy;
      $('stop').disabled = !busy;
      show('stats', busy || s.batches > 0);
      $('s-state').textContent = s.state;
      $('s-num').textContent = s.batches
        ? `${s.frames}f  ${s.kbytes}KB  ${s.tx_kbps}KB/s` : '';
      if (s.error) fail(s.error);
    } catch { $('dot').className = 'dot err'; }
  };
  tick();
  poller = setInterval(tick, 900);
}

window.addEventListener('unload', () => poller && clearInterval(poller));
init();
