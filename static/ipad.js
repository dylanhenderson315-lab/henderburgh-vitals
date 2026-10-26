/* /ipad — portrait iPad home.
   Rules this file follows (HENDERBURGH_BRIEF.md): content is visible without JS
   animation, every interpolated string goes through textContent, polling only
   runs while the tab is visible, and optimistic light toggles revert on failure
   and reconcile with the next real poll. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const LIGHTS_POLL_MS = 5000;
  const HOME_POLL_MS = 60000;

  // ── Time of day ────────────────────────────────────────────────────────────
  function bandFor(h) {
    if (h >= 5 && h < 12) return 'morning';
    if (h >= 12 && h < 17) return 'afternoon';
    if (h >= 17 && h < 21) return 'evening';
    return 'night';
  }
  function greetingFor(h) {
    if (h >= 5 && h < 12) return 'Good morning';
    if (h >= 12 && h < 17) return 'Good afternoon';
    if (h >= 17 && h < 22) return 'Good evening';
    return 'Good night';
  }
  // DISPLAY_NAME is stored as the brand-styled "DYLAN"; a greeting reads as a sentence.
  const RAW_NAME = ($('greet').textContent.split(',')[1] || 'Dylan').trim();
  const NAME = RAW_NAME.charAt(0).toUpperCase() + RAW_NAME.slice(1).toLowerCase();

  let currentBand = null;
  function tickClock() {
    const now = new Date();
    const h = now.getHours();
    const h12 = ((h + 11) % 12) + 1;
    const mm = String(now.getMinutes()).padStart(2, '0');
    const clock = $('clock');
    clock.textContent = `${h12}:${mm}`;
    const ampm = document.createElement('span');
    ampm.className = 'ampm';
    ampm.textContent = h < 12 ? 'AM' : 'PM';
    clock.appendChild(ampm);
    $('hud-time').textContent = `${h12}:${mm} ${h < 12 ? 'AM' : 'PM'}`;
    $('date').textContent = now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }).toUpperCase();
    $('greet').textContent = `${greetingFor(h)}, ${NAME}`;

    const band = bandFor(h);
    if (band !== currentBand) {
      document.querySelectorAll('.band').forEach((el) => el.classList.toggle('is-now', el.dataset.band === band));
      currentBand = band;
    }
  }

  // Pin the current band near the top on first load (morning is already the top).
  function pinNow() {
    if (location.hash) return;
    const el = document.querySelector('.band.is-now');
    if (el && el.dataset.band !== 'morning') el.scrollIntoView({ block: 'start', behavior: 'instant' });
  }

  // ── Freshness dot ─────────────────────────────────────────────────────────
  function setFresh(ok) {
    const dot = $('fresh-dot');
    dot.className = 'hb-dot ' + (ok ? 'hb-dot-live' : 'hb-dot-stale');
    dot.title = ok ? 'Live' : 'Home Assistant unreachable — showing last known';
  }

  // ── Lights ────────────────────────────────────────────────────────────────
  let rooms = [];          // [{name, ids:[...], on, total}]
  let canControl = false;
  const pending = new Map(); // room name -> optimistic on_count until the call settles

  function roomsFrom(data) {
    const byRoom = data.lights_by_room || {};
    return (data.rooms || []).map((r) => {
      const lights = (byRoom[r.name] || []).filter((l) => !l.is_sync);
      const usable = lights.filter((l) => l.state !== 'unavailable');
      return {
        name: r.name,
        ids: lights.map((l) => l.entity_id),
        on: usable.filter((l) => l.state === 'on').length,
        total: lights.length,
      };
    }).filter((r) => r.total > 0);
  }

  function renderLights() {
    const wrap = $('rooms');
    wrap.replaceChildren();
    let on = 0, total = 0;
    rooms.forEach((r) => {
      const shownOn = pending.has(r.name) ? pending.get(r.name) : r.on;
      on += shownOn; total += r.total;
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'room' + (shownOn > 0 ? ' on' : '') + (pending.has(r.name) ? ' pending' : '');
      b.disabled = !canControl;
      b.setAttribute('aria-pressed', String(shownOn > 0));
      const name = document.createElement('span');
      name.className = 'room-name';
      const dot = document.createElement('span');
      dot.className = 'hb-dot ' + (shownOn > 0 ? 'hb-dot-live' : 'hb-dot-off');
      name.append(dot, document.createTextNode(r.name));
      const count = document.createElement('span');
      count.className = 'room-count';
      count.textContent = shownOn > 0 ? `${shownOn} of ${r.total} on` : 'off';
      b.append(name, count);
      b.addEventListener('click', () => toggleRoom(r));
      wrap.appendChild(b);
    });
    $('lights-on').textContent = String(on);
    $('lights-of').textContent = `of ${total} lights on`;
    $('all-off').disabled = !canControl || on === 0;
    $('lock-note').hidden = canControl;
  }

  async function callService(service, ids) {
    const res = await fetch(`/api/ha/service/light/${service}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: ids }),
    });
    if (!res.ok) throw new Error(res.status === 403 ? 'Controls are locked' : `Couldn't reach the lights (${res.status})`);
  }

  async function toggleRoom(r) {
    if (!canControl || pending.has(r.name)) return;
    const turnOff = r.on > 0;
    pending.set(r.name, turnOff ? 0 : r.total);
    $('lights-err').textContent = '';
    renderLights();
    try {
      await callService(turnOff ? 'turn_off' : 'turn_on', r.ids);
    } catch (e) {
      $('lights-err').textContent = `${r.name}: ${e.message}`;
    }
    // Hold the optimistic value briefly so the next poll sees HA's settled state.
    setTimeout(() => { pending.delete(r.name); loadLights(); }, 1200);
  }

  async function lightsOut() {
    if (!canControl) return;
    const lit = rooms.filter((r) => r.on > 0);
    lit.forEach((r) => pending.set(r.name, 0));
    $('lights-err').textContent = '';
    renderLights();
    try {
      // Room lights only — the M-1 matrix is unassigned on purpose and stays on.
      await callService('turn_off', lit.flatMap((r) => r.ids));
    } catch (e) {
      $('lights-err').textContent = e.message;
    }
    setTimeout(() => { lit.forEach((r) => pending.delete(r.name)); loadLights(); }, 1200);
  }

  async function loadLights() {
    try {
      const res = await fetch('/api/ha/lights-data', { credentials: 'same-origin' });
      const data = await res.json();
      rooms = roomsFrom(data);
      setFresh(data.ha_ok !== false);
    } catch (e) {
      setFresh(false);
    }
    renderLights();
  }

  async function loadAuth() {
    try {
      const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
      canControl = !!(await res.json()).can_control;
    } catch (e) {
      canControl = false;
    }
    renderLights();
  }

  // ── Home + arcade ─────────────────────────────────────────────────────────
  function cap(s) { return s ? s.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '—'; }

  async function loadHome() {
    let d = null;
    try { d = await (await fetch('/api/home')).json(); } catch (e) { d = null; }
    const w = d && d.weather;
    $('wx-temp').textContent = w && w.temperature != null ? `${Math.round(w.temperature)}${w.temperature_unit || '°'}` : '—';
    $('wx-cond').textContent = w ? cap(w.condition) : '—';
    $('wx-hum').textContent = w && w.humidity != null ? `${Math.round(w.humidity)}%` : '—';
    $('wx-wind').textContent = w && w.wind_speed != null ? `${Math.round(w.wind_speed)} ${w.wind_speed_unit || ''}`.trim() : '—';

    const stats = $('home-stats');
    stats.replaceChildren();
    const items = [];
    if (w && w.temperature != null) items.push(['Outside', `${Math.round(w.temperature)}${w.temperature_unit || '°'}`]);
    if (w && w.humidity != null) items.push(['Outdoor humidity', `${Math.round(w.humidity)}%`]);
    ((d && d.sensors) || []).forEach((s) => items.push([s.name, `${s.value}${s.unit || ''}`]));
    if (!items.length) {
      const p = document.createElement('p');
      p.className = 'muted';
      p.textContent = d && d.ha_ok === false ? 'Home Assistant is unreachable right now.' : 'No temperature or humidity sensors in Home Assistant yet.';
      stats.appendChild(p);
    } else {
      items.forEach(([label, val]) => {
        const s = document.createElement('div'); s.className = 'stat';
        const l = document.createElement('span'); l.className = 'hb-label'; l.textContent = label;
        const v = document.createElement('span'); v.className = 'stat-val'; v.textContent = val;
        s.append(l, v); stats.appendChild(s);
      });
      if (!((d && d.sensors) || []).length) {
        const note = document.createElement('p');
        note.className = 'faint';
        note.style.gridColumn = '1 / -1';
        note.textContent = 'Outdoor only — add an indoor temperature/humidity sensor to Home Assistant and it appears here.';
        stats.appendChild(note);
      }
    }

    const a = d && d.arcade;
    $('arcade-mode').textContent = a ? cap(a.mode) : '—';
    $('arcade-on').textContent = a ? (a.on === false ? 'Off' : a.paused ? 'Paused' : 'On') : '—';
    $('arcade-note').textContent = a ? '' : 'Panel status is only visible from the home network.';
  }

  // ── Work mode ─────────────────────────────────────────────────────────────
  function setWork(on) {
    document.body.classList.toggle('work-mode', on);
    $('work-toggle').setAttribute('aria-pressed', String(on));
    $('work-toggle').textContent = on ? 'Home mode' : 'Work mode';
    try { localStorage.setItem('hb-ipad-work', on ? '1' : '0'); } catch (e) {}
  }

  // ── Polling (visible-only) ────────────────────────────────────────────────
  let timers = [];
  function start() {
    stop();
    tickClock(); loadLights(); loadHome();
    timers.push(setInterval(tickClock, 15000));
    timers.push(setInterval(loadLights, LIGHTS_POLL_MS));
    timers.push(setInterval(loadHome, HOME_POLL_MS));
  }
  function stop() { timers.forEach(clearInterval); timers = []; }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { loadAuth(); start(); } else stop();
  });

  $('all-off').addEventListener('click', lightsOut);
  $('work-toggle').addEventListener('click', () => setWork(!document.body.classList.contains('work-mode')));
  let work = false;
  try { work = localStorage.getItem('hb-ipad-work') === '1'; } catch (e) {}
  setWork(work);

  tickClock();
  pinNow();
  loadAuth();
  if (document.visibilityState === 'visible') start();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();
