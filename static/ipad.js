/* /ipad — iPad home, portrait scroll + landscape console (layout is pure CSS).
   Rules this file follows (HENDERBURGH_BRIEF.md): content is visible without JS
   animation, every interpolated string goes through textContent, polling only
   runs while the tab is visible, and optimistic light toggles revert on failure
   and reconcile with the next real poll. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
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
  // The sky half of the Now panel's light: gold dawn, clear blue day, ember dusk, violet night.
  const SKY = { morning: '255,200,120', afternoon: '96,165,250', evening: '255,140,84', night: '139,127,255' };

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
      root.style.setProperty('--sky-rgb', SKY[band]);
      currentBand = band;
      renderModes();
    }
  }

  // Pin the current band near the top on first load. In landscape the Now panel
  // is beside the bands, so morning still starts at the top there too.
  function pinNow() {
    if (location.hash) return;
    const el = document.querySelector('.band.is-now');
    if (el && el.dataset.band !== 'morning') el.scrollIntoView({ block: 'start', behavior: 'instant' });
  }

  function setFresh(ok) {
    const dot = $('fresh-dot');
    dot.className = 'hb-dot ' + (ok ? 'hb-dot-live' : 'hb-dot-stale');
    dot.title = ok ? 'Live' : 'Home Assistant unreachable — showing last known';
  }

  // ── Colour of the house ───────────────────────────────────────────────────
  // Average of every lit bulb's real rgb_color, so the Now panel and each room
  // card glow the colour the house actually is right now.
  function avgRgb(lights) {
    const lit = lights.filter((l) => l.state === 'on');
    if (!lit.length) return null;
    const sum = [0, 0, 0];
    lit.forEach((l) => {
      const c = Array.isArray(l.rgb_color) && l.rgb_color.length >= 3 ? l.rgb_color : [255, 180, 84];
      for (let i = 0; i < 3; i++) sum[i] += c[i];
    });
    return sum.map((v) => Math.round(v / lit.length)).join(',');
  }

  // ── Lights ────────────────────────────────────────────────────────────────
  let rooms = [];          // [{name, ids:[...], on, total, rgb}]
  let lightsData = {};     // last /api/ha/lights-data payload (modes need the full list)
  let canControl = false;
  const pending = new Map(); // room name -> optimistic on_count until the call settles

  // Same definition as /home-assistant's allLightIds(), so a mode means the
  // same lights on both pages.
  function allLightIds() {
    return [...Object.values(lightsData.lights_by_room || {}).flat(), ...(lightsData.unassigned_lights || [])]
      .filter((l) => !l.is_sync).map((l) => l.entity_id);
  }
  const MODES = window.HBHouseModes ? window.HBHouseModes.build({ allLightIds }) : {};
  const MODE_TINT = {
    'desk-on': 'rgba(139,127,255,0.30)',   // Party — violet
    'meeting': 'rgba(255,180,84,0.18)',    // Candlelight — dim amber
    'evening': 'rgba(255,180,84,0.30)',    // Warm white
    'bedtime': 'rgba(248,113,113,0.22)',   // Fireplace ember
    'lights-out': 'rgba(255,255,255,0.03)',
  };
  function suggestedMode(h) {
    if (h >= 17 && h < 21) return 'evening';
    if (h >= 21 || h < 5) return 'bedtime';
    return null;
  }

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
        rgb: avgRgb(lights),
      };
    }).filter((r) => r.total > 0);
  }

  function renderLights() {
    const wrap = $('rooms');
    wrap.replaceChildren();
    let on = 0, total = 0;
    const litNames = [];
    rooms.forEach((r) => {
      const shownOn = pending.has(r.name) ? pending.get(r.name) : r.on;
      on += shownOn; total += r.total;
      if (shownOn > 0) litNames.push(r.name);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'room' + (shownOn > 0 ? ' on' : '') + (pending.has(r.name) ? ' pending' : '');
      if (r.rgb) b.style.setProperty('--room-rgb', r.rgb);
      b.disabled = !canControl;
      b.setAttribute('aria-pressed', String(shownOn > 0));
      const name = document.createElement('span');
      name.className = 'room-name';
      const dot = document.createElement('span');
      dot.className = 'hb-dot ' + (shownOn > 0 ? '' : 'hb-dot-off');
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
    $('lock-note').hidden = canControl;

    const house = avgRgb(Object.values(lightsData.lights_by_room || {}).flat().filter((l) => !l.is_sync));
    root.style.setProperty('--house-rgb', house || 'var(--accent-rgb)');
    root.style.setProperty('--house-a', String(house ? Math.min(0.30, 0.10 + on * 0.015) : 0.08));
    $('house-line').textContent = on
      ? `${on} light${on === 1 ? '' : 's'} on — ${litNames.join(', ')}`
      : 'Every light is off.';
    renderModes();
  }

  let modeBusy = null;
  function renderModes() {
    const wrap = $('modes');
    if (!wrap) return;
    const hint = suggestedMode(new Date().getHours());
    const modeButton = (key, m) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'mode' + (key === hint ? ' suggested' : '') + (modeBusy === key ? ' busy' : '');
      b.disabled = !canControl || !!modeBusy;
      b.style.setProperty('--mode-a', MODE_TINT[key] || 'var(--surface-2)');
      const n = document.createElement('span'); n.className = 'mode-name'; n.textContent = m.name;
      const d = document.createElement('span'); d.className = 'mode-desc'; d.textContent = m.desc;
      b.append(n, d);
      b.addEventListener('click', () => runMode(key));
      return b;
    };
    wrap.replaceChildren(...Object.entries(MODES).map(([key, m]) => modeButton(key, m)));
    // Landscape Now panel: the one routine that fits this hour, a thumb away.
    const sw = $('now-suggest-wrap');
    if (sw) {
      sw.hidden = !(hint && MODES[hint]);
      $('now-suggest').replaceChildren(...(hint && MODES[hint] ? [modeButton(hint, MODES[hint])] : []));
    }
  }

  async function callService(service, ids, extra) {
    const res = await fetch(`/api/ha/service/light/${service}`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: ids, ...(extra || {}) }),
    });
    if (!res.ok) throw new Error(res.status === 403 ? 'Controls are locked' : `Couldn't reach the lights (${res.status})`);
  }

  async function runMode(key) {
    const m = MODES[key];
    if (!m || !canControl || modeBusy) return;
    const steps = m.run().filter(([ids]) => ids && ids.length);
    if (!steps.length) return;
    modeBusy = key;
    const msg = $('lights-err');
    msg.className = 'err'; msg.textContent = '';
    renderModes();
    const results = await Promise.allSettled(steps.map(([ids, payload, svc]) =>
      svc === 'turn_off' ? callService('turn_off', ids) : callService('turn_on', ids, { transition: 0.4, ...payload })));
    modeBusy = null;
    if (results.every((r) => r.status === 'fulfilled')) {
      msg.className = 'err ok'; msg.textContent = `${m.name} set`;
      setTimeout(() => { if (msg.textContent === `${m.name} set`) msg.textContent = ''; }, 4000);
    } else {
      msg.textContent = `${m.name}: some lights didn't respond`;
    }
    setTimeout(loadLights, 600);
    renderModes();
  }

  async function toggleRoom(r) {
    if (!canControl || pending.has(r.name)) return;
    const turnOff = r.on > 0;
    pending.set(r.name, turnOff ? 0 : r.total);
    const msg = $('lights-err');
    msg.className = 'err'; msg.textContent = '';
    renderLights();
    try {
      await callService(turnOff ? 'turn_off' : 'turn_on', r.ids);
    } catch (e) {
      msg.textContent = `${r.name}: ${e.message}`;
    }
    // Hold the optimistic value briefly so the next poll sees HA's settled state.
    setTimeout(() => { pending.delete(r.name); loadLights(); }, 1200);
  }

  async function loadLights() {
    try {
      const res = await fetch('/api/ha/lights-data', { credentials: 'same-origin' });
      const data = await res.json();
      lightsData = data;
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

  // ── Small DOM helpers for the list tiles ──────────────────────────────────
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function note(cls, text) { return el('p', cls, text); }
  function cap(s) { return s ? s.replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '—'; }

  function whenLabel(ev) {
    if (!ev.start) return '';
    const d = new Date(ev.all_day ? ev.start + 'T00:00:00' : ev.start);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    const dayLabel = diff === 0 ? 'Today' : diff === 1 ? 'Tomorrow' : d.toLocaleDateString(undefined, { weekday: 'short' });
    if (ev.all_day) return `${dayLabel} · all day`;
    return `${dayLabel} ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
  }
  function ago(uts) {
    if (!uts) return '';
    const m = Math.round((Date.now() / 1000 - uts) / 60);
    if (m < 60) return `${m}m ago`;
    if (m < 1440) return `${Math.round(m / 60)}h ago`;
    return `${Math.round(m / 1440)}d ago`;
  }

  function renderCalendar(cal) {
    const body = $('cal-body');
    body.replaceChildren();
    if (!cal || !cal.configured) {
      body.appendChild(note('placeholder muted', 'No calendar in Home Assistant yet — add one and your next three events land here.'));
      $('next-line').textContent = 'No calendar connected yet.';
      return;
    }
    if (!cal.events.length) {
      body.appendChild(note('muted', 'Nothing on the calendar this week.'));
      $('next-line').textContent = 'A clear week.';
      return;
    }
    const ul = el('ul', 'rows');
    cal.events.forEach((ev) => {
      const li = el('li');
      const main = el('div', 'row-main');
      main.append(el('div', 'row-title', ev.title), el('div', 'row-sub', ev.location || ev.calendar || ''));
      li.append(el('span', 'row-when', whenLabel(ev)), main);
      ul.appendChild(li);
    });
    body.appendChild(ul);
    const first = cal.events[0];
    $('next-line').textContent = `${first.title} — ${whenLabel(first)}`;
  }

  function renderMusic(music) {
    const body = $('music-body');
    body.replaceChildren();
    if (!music || !music.configured) {
      body.appendChild(note('placeholder muted', "Last.fm isn't connected yet — now playing and recent scrobbles will show here."));
      return;
    }
    if (music.ok === false) {
      body.appendChild(note('muted', "Couldn't reach Last.fm just now."));
      return;
    }
    const np = music.now_playing || music.recent[0];
    if (np) {
      const row = el('div', 'np');
      const art = el('div', 'np-art');
      if (np.art && /^https:\/\//.test(np.art)) art.style.backgroundImage = `url("${np.art.replace(/"/g, '')}")`;
      const txt = el('div', 'row-main');
      const label = el('span', 'hb-label');
      if (music.now_playing) {
        const eq = el('span', 'eq'); eq.append(el('i'), el('i'), el('i'));
        label.append(eq, document.createTextNode('Now playing'));
      } else {
        label.textContent = `Last played · ${ago(np.uts)}`;
      }
      txt.append(label, el('div', 'np-track', np.track), el('div', 'np-artist', np.artist));
      row.append(art, txt);
      body.appendChild(row);
    }
    const rest = music.now_playing ? music.recent : music.recent.slice(1);
    if (rest.length) {
      const ul = el('ul', 'rows');
      rest.slice(0, 4).forEach((t) => {
        const li = el('li');
        const main = el('div', 'row-main');
        main.append(el('div', 'row-title', t.track), el('div', 'row-sub', t.artist));
        li.append(main, el('span', 'row-when', ago(t.uts)));
        ul.appendChild(li);
      });
      body.appendChild(ul);
    }
  }

  function renderMedia() {
    const row = $('media-row');
    if (!row || !window.HBStreaming) return;
    row.replaceChildren();
    window.HBStreaming.filter((s) => s.key !== 'applemusic' && s.key !== 'ytmusic').forEach((s) => {
      const a = el('a', 'media-card', s.name);
      a.href = s.open;
      a.style.background = `linear-gradient(150deg, rgba(${s.tint},0.45), var(--surface-2) 70%)`;
      row.appendChild(a);
    });
  }

  // ── Home + arcade + calendar + music ──────────────────────────────────────
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
    const sensors = (d && d.sensors) || [];
    if (sensors.length) {
      sensors.forEach((s) => {
        const box = el('div', 'stat');
        box.append(el('span', 'hb-label', s.name), el('span', 'stat-val', `${s.value}${s.unit || ''}`));
        stats.appendChild(box);
      });
    } else {
      const box = el('div', 'stat');
      box.append(el('span', 'hb-label', 'Lights on'), el('span', 'stat-val', $('lights-on').textContent));
      const box2 = el('div', 'stat');
      box2.append(el('span', 'hb-label', 'Home Assistant'), el('span', 'stat-val', d && d.ha_ok ? 'Online' : 'Offline'));
      stats.append(box, box2);
      const n = note('faint', 'Add an indoor temperature/humidity sensor to Home Assistant and it appears here.');
      n.style.gridColumn = '1 / -1';
      stats.appendChild(n);
    }

    const a = d && d.arcade;
    $('arcade-mode').textContent = a ? cap(a.mode) : '—';
    $('arcade-on').textContent = a ? (a.on === false ? 'Off' : a.paused ? 'Paused' : 'On') : '—';
    $('arcade-note').textContent = a ? '' : 'Panel status is only visible from the home network.';

    renderCalendar(d && d.calendar);
    renderMusic(d && d.music);
    setLaunch('ln-arcade', a && a.mode ? `Showing ${cap(a.mode)}` : null);
    loadLauncher();
  }

  // ── Launcher live lines ───────────────────────────────────────────────────
  // Only cheap, public, already-privacy-filtered feeds. A missing value keeps the
  // card's static description rather than inventing one.
  function setLaunch(id, live) {
    const n = $(id);
    if (!n) return;
    if (!n.dataset.base) n.dataset.base = n.textContent;
    n.textContent = live || n.dataset.base;
    n.classList.toggle('live', !!live);
  }
  async function loadLauncher() {
    const onCount = Number($('lights-on').textContent) || 0;
    setLaunch('ln-lights', onCount ? `${onCount} on right now` : null);
    try {
      const x = await (await fetch('/api/xbox/status')).json();
      setLaunch('ln-xbox', x.playing_now && x.game ? `Playing ${x.game}` : null);
    } catch (e) {}
    try {
      const v = await (await fetch('/api/os-state')).json();
      setLaunch('ln-vitals', v.readiness != null ? `Readiness ${v.readiness}${v.sleep != null ? ` · sleep ${v.sleep}` : ''}` : null);
    } catch (e) {}
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

  $('work-toggle').addEventListener('click', () => setWork(!document.body.classList.contains('work-mode')));
  let work = false;
  try { work = localStorage.getItem('hb-ipad-work') === '1'; } catch (e) {}
  setWork(work);

  tickClock();
  renderMedia();
  pinNow();
  loadAuth();
  if (document.visibilityState === 'visible') start();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();
