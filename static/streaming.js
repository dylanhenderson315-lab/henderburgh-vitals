/* /streaming — TVs (state first), a remote for the picked TV, and every service.
   textContent only; polling only while visible; control errors are shown, never
   hidden behind an optimistic "done". */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SERVICES = (window.HBStreaming || []).filter((s) => s.key !== 'applemusic' && s.key !== 'ytmusic');
  const BY_PACKAGE = Object.fromEntries((window.HBStreaming || []).filter((s) => s.tv).map((s) => [s.tv, s.name]));
  // The TVs' own launchers / live-TV apps (seen on the Hisense + TCL).
  Object.assign(BY_PACKAGE, { 'com.vt.tv.tvcenter': 'Home screen', 'com.tcl.tv': 'Home screen',
    'com.google.android.apps.tv.launcherx': 'Home screen', 'com.google.android.tvlauncher': 'Home screen' });

  let tvs = [];
  let selected = null;
  let canControl = false;
  try { selected = localStorage.getItem('hb-watch-tv'); } catch (e) {}

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function appName(app) {
    if (!app) return null;
    return BY_PACKAGE[app] || app.split('.').pop().replace(/^\w/, (c) => c.toUpperCase());
  }
  function setStatus(text, kind) {
    const s = $('remote-status');
    s.className = 'status' + (kind ? ' ' + kind : '');
    s.textContent = text;
  }

  function renderTvs() {
    const wrap = $('tvs');
    wrap.replaceChildren();
    if (!tvs.length) {
      const box = el('div', 'empty');
      box.append(
        el('h2', null, 'Your TVs aren’t connected yet'),
        el('p', 'muted', 'The Living Room, Theater and Bedroom Google TVs need pairing with Home Assistant once. Until then, every service still opens here on the iPad.'),
      );
      wrap.appendChild(box);
      $('remote').hidden = true;
      return;
    }
    if (!tvs.some((t) => t.entity_id === selected)) selected = (tvs.find((t) => t.on) || tvs[0]).entity_id;
    tvs.forEach((t) => {
      const b = el('button', 'tv' + (t.on ? '' : ' off'));
      b.type = 'button';
      b.setAttribute('aria-pressed', String(t.entity_id === selected));
      const name = el('span', 'tv-name');
      name.append(el('span', 'hb-dot ' + (t.on ? 'hb-dot-live' : 'hb-dot-off')), document.createTextNode(t.name));
      const what = !t.available ? 'Unreachable' : !t.on ? 'Off' : [appName(t.app), t.title].filter(Boolean).join(' — ') || 'On';
      b.append(name, el('span', 'tv-what', what));
      b.addEventListener('click', () => {
        selected = t.entity_id;
        try { localStorage.setItem('hb-watch-tv', selected); } catch (e) {}
        renderTvs(); renderServices();
      });
      wrap.appendChild(b);
    });
    $('remote').hidden = !canControl;
    const on = tvs.filter((t) => t.on);
    $('headline').textContent = on.length ? `On now: ${on.map((t) => t.name).join(', ')}` : "What's on tonight";
  }

  function selectedTv() { return tvs.find((t) => t.entity_id === selected) || null; }

  function renderServices() {
    const wrap = $('services');
    wrap.replaceChildren();
    const tv = selectedTv();
    SERVICES.forEach((s) => {
      const card = el('article', 'svc');
      card.style.setProperty('--tint', s.tint);
      const open = el('a', 'svc-open', s.name);
      open.href = s.open;
      const actions = el('div', 'svc-actions');
      const here = el('a', null, 'Open here');
      here.href = s.open;
      const onTv = el('button', null, tv ? `On ${tv.name}` : 'On TV');
      onTv.type = 'button';
      onTv.disabled = !tv || !canControl || !s.tv;
      onTv.addEventListener('click', () => launch(s));
      actions.append(here, onTv);
      card.append(open, actions);
      wrap.appendChild(card);
    });
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) {}
      throw new Error(res.status === 403 ? 'Controls are locked on this device' : detail || `Failed (${res.status})`);
    }
  }

  async function launch(s) {
    const tv = selectedTv();
    if (!tv) return;
    setStatus(`Opening ${s.name} on ${tv.name}…`);
    try {
      await post('/api/media/launch', { tv: tv.entity_id, app: s.tv });
      setStatus(`${s.name} is opening on ${tv.name}`, 'ok');
      setTimeout(loadTvs, 2500);
    } catch (e) {
      setStatus(e.message, 'err');
    }
    $('remote').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  document.querySelectorAll('#remote [data-cmd], #remote [data-power]').forEach((b) => {
    b.addEventListener('click', async () => {
      const tv = selectedTv();
      if (!tv) return;
      const body = b.dataset.power ? { tv: tv.entity_id, power: b.dataset.power === 'on' } : { tv: tv.entity_id, command: b.dataset.cmd };
      try {
        await post('/api/media/command', body);
        setStatus('');
        if (b.dataset.power) setTimeout(loadTvs, 2000);
      } catch (e) {
        setStatus(e.message, 'err');
      }
    });
  });

  async function loadTvs() {
    try {
      const d = await (await fetch('/api/media/tvs', { credentials: 'same-origin' })).json();
      tvs = d.tvs || [];
      $('fresh-dot').className = 'hb-dot ' + (d.ha_ok ? 'hb-dot-live' : 'hb-dot-stale');
    } catch (e) {
      $('fresh-dot').className = 'hb-dot hb-dot-stale';
    }
    renderTvs(); renderServices();
  }
  async function loadAuth() {
    try { canControl = !!(await (await fetch('/api/auth/status', { credentials: 'same-origin' })).json()).can_control; }
    catch (e) { canControl = false; }
    renderTvs(); renderServices();
  }

  let timer = null;
  function start() { stop(); loadTvs(); timer = setInterval(loadTvs, 10000); }
  function stop() { if (timer) clearInterval(timer); timer = null; }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { loadAuth(); start(); } else stop();
  });

  renderServices();
  loadAuth();
  if (document.visibilityState === 'visible') start();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
