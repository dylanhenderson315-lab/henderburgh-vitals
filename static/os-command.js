/* Henderburgh OS — universal command bar (⌘K / Ctrl+K).
   One truthful power layer for the whole site: instant navigation, real light
   control (via the existing admin-gated HA endpoints — honest errors when locked),
   and the lamp poke. Zero dependencies, injected on every page. */
(function () {
  'use strict';

  const NAV = [
    { label: 'Home', icon: 'fa-home', href: '/' },
    { label: 'Vitals', icon: 'fa-heart-pulse', href: '/vitals' },
    { label: 'Lighting', icon: 'fa-lightbulb', href: '/home-assistant' },
    { label: '3D Model', icon: 'fa-cube', href: '/model' },
    { label: 'Golf', icon: 'fa-golf-ball-tee', href: '/golf' },
    { label: 'Clips', icon: 'fa-video', href: '/clips' },
    { label: 'Xbox', icon: 'fa-gamepad', href: '/xbox' },
    { label: 'Blog', icon: 'fa-book', href: '/blog' },
  ];

  let overlay = null, input = null, list = null, items = [], selected = 0, roomsCache = null;

  function haCall(domain, service, entityIds) {
    return fetch(`/api/ha/service/${domain}/${service}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: entityIds }),
    });
  }

  function toast(msg, bad) {
    const t = document.createElement('div');
    t.textContent = msg;
    t.style.cssText = `position:fixed;bottom:84px;left:50%;transform:translateX(-50%);z-index:400;
      background:${bad ? '#3f1d1d' : '#122b22'};color:${bad ? '#fca5a5' : '#6ee7b7'};
      border:1px solid ${bad ? '#7f1d1d' : '#065f46'};padding:8px 16px;border-radius:16px;
      font:500 12px Inter,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.5)`;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2200);
  }

  async function lightActions() {
    // Rooms from the public bootstrap (persisted structure; no HA roundtrip)
    if (!roomsCache) {
      try {
        const r = await fetch('/api/ha/bootstrap');
        if (r.ok) roomsCache = (await r.json()).rooms || [];
      } catch (e) { roomsCache = []; }
    }
    const acts = [];
    const allIds = (roomsCache || []).flatMap(r => r.light_ids || []);
    if (allIds.length) {
      acts.push({ label: 'All lights off', icon: 'fa-moon', run: () => doLights('turn_off', allIds, 'All lights off') });
      acts.push({ label: 'All lights on', icon: 'fa-sun', run: () => doLights('turn_on', allIds, 'All lights on') });
    }
    (roomsCache || []).forEach(r => {
      if (!(r.light_ids || []).length) return;
      acts.push({ label: `${r.name}: lights on`, icon: 'fa-toggle-on', run: () => doLights('turn_on', r.light_ids, `${r.name} on`) });
      acts.push({ label: `${r.name}: lights off`, icon: 'fa-toggle-off', run: () => doLights('turn_off', r.light_ids, `${r.name} off`) });
    });
    acts.push({ label: 'Poke the office lamp', icon: 'fa-bell', run: async () => {
      const res = await fetch('/api/ha/poke', { method: 'POST' });
      toast(res.ok ? 'Lamp poked' : 'Poke rate-limited — wait a few seconds', !res.ok);
    }});
    return acts;
  }

  async function doLights(service, ids, label) {
    const res = await haCall('light', service, ids);
    if (res.status === 403) toast('Locked — unlock on the Lighting page to control lights', true);
    else toast(res.ok ? label : 'Light call failed', !res.ok);
  }

  function build() {
    overlay = document.createElement('div');
    overlay.id = 'os-cmd';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:300;display:none;background:rgba(0,0,0,.55);backdrop-filter:blur(6px)';
    overlay.innerHTML = `
      <div style="max-width:560px;margin:12vh auto 0;padding:0 16px;">
        <div style="background:#121316;border:1px solid #3f3f46;border-radius:20px;overflow:hidden;box-shadow:0 24px 80px rgba(0,0,0,.6)">
          <div style="display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid #27272a">
            <i class="fa-solid fa-terminal" style="color:#34d399;font-size:13px"></i>
            <input id="os-cmd-input" placeholder="Go to a page, control lights…" autocomplete="off" spellcheck="false"
                   style="flex:1;background:transparent;border:none;outline:none;color:#f4f4f5;font:500 15px Inter,system-ui,sans-serif">
            <span style="font:500 10px Inter;color:#52525b;border:1px solid #3f3f46;border-radius:6px;padding:2px 6px">esc</span>
          </div>
          <div id="os-cmd-list" style="max-height:44vh;overflow:auto;padding:6px"></div>
        </div>
      </div>`;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    input = overlay.querySelector('#os-cmd-input');
    list = overlay.querySelector('#os-cmd-list');
    input.addEventListener('input', render);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); selected = Math.min(selected + 1, items.length - 1); paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); selected = Math.max(selected - 1, 0); paint(); }
      else if (e.key === 'Enter') { e.preventDefault(); if (items[selected]) exec(items[selected]); }
      else if (e.key === 'Escape') close();
    });
  }

  let allActions = [];
  async function open() {
    if (!overlay) build();
    overlay.style.display = 'block';
    input.value = ''; selected = 0;
    allActions = NAV.map(n => ({ label: n.label, icon: n.icon, run: () => { location.href = n.href; } }));
    render();
    input.focus();
    // lights load async and appear when ready
    lightActions().then(acts => { allActions = allActions.concat(acts); render(); });
  }
  function close() { if (overlay) overlay.style.display = 'none'; }

  function render() {
    const q = (input.value || '').toLowerCase().trim();
    items = allActions.filter(a => !q || a.label.toLowerCase().includes(q)).slice(0, 12);
    selected = Math.min(selected, Math.max(0, items.length - 1));
    paint();
  }

  function paint() {
    list.innerHTML = items.length ? items.map((a, i) => `
      <div class="os-cmd-item" data-i="${i}" style="display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:12px;cursor:pointer;
        font:500 14px Inter,system-ui,sans-serif;color:${i === selected ? '#f4f4f5' : '#a1a1aa'};
        background:${i === selected ? 'rgba(52,211,153,.10)' : 'transparent'}">
        <i class="fa-solid ${a.icon}" style="width:16px;text-align:center;color:${i === selected ? '#34d399' : '#52525b'};font-size:12px"></i>
        ${a.label}
      </div>`).join('')
      : '<div style="padding:18px;text-align:center;color:#52525b;font:500 13px Inter">No matches</div>';
    list.querySelectorAll('.os-cmd-item').forEach(el => {
      el.addEventListener('click', () => exec(items[parseInt(el.dataset.i, 10)]));
      el.addEventListener('mousemove', () => { const i = parseInt(el.dataset.i, 10); if (i !== selected) { selected = i; paint(); } });
    });
  }

  function exec(a) { close(); a.run(); }

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      (overlay && overlay.style.display === 'block') ? close() : open();
    }
  });
})();
