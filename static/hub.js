/* HENDERBURGH HUB — /hub
   Spaces (Now · Day · Lights · Watch · Everything), the live house colour,
   lights + modes, the front door's day story, context-aware opening, and an
   ambient idle state for a home-screen install. Watch is driven by
   streaming.js (same element ids); house modes come from house-modes.js.

   Rules (HENDERBURGH_BRIEF.md): content is visible without animation; all
   interpolated text goes through textContent; polling runs only while the
   page is visible; optimistic toggles revert and reconcile with real state. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const LIGHTS_POLL_MS = 5000;
  const STATE_POLL_MS = 30000;
  const IDLE_MS = 75000;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function cap(s) { return s ? String(s).replace(/[-_]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '—'; }
  function ago(uts) {
    if (!uts) return '';
    const m = Math.round((Date.now() / 1000 - uts) / 60);
    if (m < 60) return `${m}m ago`;
    if (m < 1440) return `${Math.round(m / 60)}h ago`;
    return `${Math.round(m / 1440)}d ago`;
  }
  const RAW = String((window.HB_HUB && window.HB_HUB.name) || 'Dylan');
  const NAME = RAW.charAt(0).toUpperCase() + RAW.slice(1).toLowerCase();

  // ═══════════════════════ Spaces ═══════════════════════
  const track = $('spaces');
  const spaces = [...document.querySelectorAll('.space')];
  const tabs = [...document.querySelectorAll('#tabs button')];
  const indexOf = (key) => Math.max(0, spaces.findIndex((s) => s.dataset.space === key));
  let userMoved = false;

  function goTo(key, instant) {
    const i = indexOf(key);
    track.scrollTo({ left: i * track.clientWidth, behavior: instant ? 'instant' : 'smooth' });
    markTab(key);
  }
  function markTab(key) {
    tabs.forEach((t) => t.setAttribute('aria-current', String(t.dataset.space === key)));
    try { sessionStorage.setItem('hb-hub-space', JSON.stringify({ key, at: Date.now() })); } catch (e) {}
  }
  tabs.forEach((t) => t.addEventListener('click', () => { userMoved = true; goTo(t.dataset.space); }));
  document.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => { userMoved = true; goTo(b.dataset.go); }));
  let scrollT = null;
  track.addEventListener('scroll', () => {
    clearTimeout(scrollT);
    scrollT = setTimeout(() => {
      const i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
      if (spaces[i]) markTab(spaces[i].dataset.space);
    }, 80);
  }, { passive: true });
  track.addEventListener('pointerdown', () => { userMoved = true; }, { passive: true });
  // Rotation changes the track width: keep the same space in view.
  window.addEventListener('resize', () => {
    const cur = tabs.find((t) => t.getAttribute('aria-current') === 'true');
    if (cur) goTo(cur.dataset.space, true);
  });

  // Open where you were if it was recent; otherwise Now (context may move it
  // once live state arrives — see openForContext).
  (function restore() {
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem('hb-hub-space') || 'null'); } catch (e) {}
    const hash = location.hash.replace('#', '');
    if (hash && spaces.some((s) => s.dataset.space === hash)) { userMoved = true; goTo(hash, true); return; }
    if (saved && Date.now() - saved.at < 10 * 60 * 1000) { userMoved = true; goTo(saved.key, true); return; }
    goTo('now', true);
  })();

  // ═══════════════════════ Time ═══════════════════════
  const SKY = { morning: '255,200,120', afternoon: '96,165,250', evening: '255,140,84', night: '139,127,255' };
  function bandFor(h) { return h >= 5 && h < 12 ? 'morning' : h >= 12 && h < 17 ? 'afternoon' : h >= 17 && h < 21 ? 'evening' : 'night'; }
  function greetingFor(h) { return h >= 5 && h < 12 ? 'Good morning' : h >= 12 && h < 17 ? 'Good afternoon' : h >= 17 && h < 22 ? 'Good evening' : 'Good night'; }
  function tickClock() {
    const now = new Date();
    const h = now.getHours();
    const h12 = ((h + 11) % 12) + 1;
    const mm = String(now.getMinutes()).padStart(2, '0');
    const ap = h < 12 ? 'AM' : 'PM';
    const clock = $('clock');
    clock.textContent = `${h12}:${mm}`;
    clock.appendChild(el('span', 'ampm', ap));
    $('bar-time').textContent = `${h12}:${mm} ${ap}`;
    $('ambient-clock').textContent = `${h12}:${mm}`;
    $('date').textContent = now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }).toUpperCase();
    $('hero-eyebrow').textContent = `${greetingFor(h)}, ${NAME} · Personal OS · Live`;
    root.style.setProperty('--sky-rgb', SKY[bandFor(h)]);
  }

  // ═══════════════════════ Lights ═══════════════════════
  let lightsData = {};
  let rooms = [];
  let canControl = false;
  const pending = new Map();

  function allLightIds() {
    return [...Object.values(lightsData.lights_by_room || {}).flat(), ...(lightsData.unassigned_lights || [])]
      .filter((l) => !l.is_sync).map((l) => l.entity_id);
  }
  const MODES = window.HBHouseModes ? window.HBHouseModes.build({ allLightIds }) : {};
  const MODE_TINT = {
    'desk-on': 'rgba(139,127,255,0.30)', 'meeting': 'rgba(255,180,84,0.18)', 'evening': 'rgba(255,180,84,0.30)',
    'bedtime': 'rgba(248,113,113,0.22)', 'lights-out': 'rgba(255,255,255,0.03)',
  };
  function suggestedMode(h) { return h >= 17 && h < 21 ? 'evening' : (h >= 21 || h < 5) ? 'bedtime' : null; }

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
  function roomsFrom(data) {
    const byRoom = data.lights_by_room || {};
    return (data.rooms || []).map((r) => {
      const lights = (byRoom[r.name] || []).filter((l) => !l.is_sync);
      return {
        name: r.name,
        ids: lights.map((l) => l.entity_id),
        on: lights.filter((l) => l.state === 'on').length,
        total: lights.length,
        rgb: avgRgb(lights),
      };
    }).filter((r) => r.total > 0);
  }

  function renderLights() {
    const wrap = $('rooms');
    wrap.replaceChildren();
    let on = 0, total = 0;
    const lit = [];
    rooms.forEach((r) => {
      const shown = pending.has(r.name) ? pending.get(r.name) : r.on;
      on += shown; total += r.total;
      if (shown > 0) lit.push(r.name);
      const b = el('button', 'room' + (shown > 0 ? ' on' : '') + (pending.has(r.name) ? ' pending' : ''));
      b.type = 'button';
      if (r.rgb) b.style.setProperty('--room-rgb', r.rgb);
      b.disabled = !canControl;
      b.setAttribute('aria-pressed', String(shown > 0));
      const name = el('span', 'room-name');
      name.append(el('span', 'hb-dot ' + (shown > 0 ? '' : 'hb-dot-off')), document.createTextNode(r.name));
      b.append(name, el('span', 'room-count', shown > 0 ? `${shown} of ${r.total} on` : 'off'));
      b.addEventListener('click', () => toggleRoom(r));
      wrap.appendChild(b);
    });
    $('lights-on').textContent = String(on);
    $('lights-of').textContent = `of ${total} lights on`;
    $('house-on').textContent = String(on);
    $('house-of').textContent = `of ${total} on`;
    $('house-rooms').textContent = on ? lit.join(' · ') : 'Every light is off.';
    $('card-house').classList.toggle('dark', on === 0);
    $('lock-note').hidden = canControl;
    $('ln-lights').textContent = on ? `${on} on right now` : 'Rooms, modes, effects';
    $('ln-lights').classList.toggle('live', on > 0);

    const all = Object.values(lightsData.lights_by_room || {}).flat().filter((l) => !l.is_sync);
    const house = avgRgb(all);
    root.style.setProperty('--house-rgb', house || 'var(--accent-rgb)');
    root.style.setProperty('--house-a', String(house ? Math.min(0.32, 0.12 + on * 0.015) : 0.08));
    paintAurora(all);
    renderModes();
  }

  // The front door's light strip + aurora: the distinct colours the lit bulbs
  // are showing, in room order. Dark house = a dim emerald idle.
  function litColours(lights) {
    const seen = new Map();
    lights.filter((l) => l.state === 'on').forEach((l) => {
      const c = Array.isArray(l.rgb_color) && l.rgb_color.length >= 3 ? l.rgb_color : [255, 196, 120];
      const key = c.map((v) => Math.round(v / 24)).join('-');
      if (!seen.has(key)) seen.set(key, c.slice(0, 3).join(','));
    });
    return [...seen.values()];
  }
  function paintAurora(lights) {
    const cols = litColours(lights);
    const strip = $('light-strip');
    if (!cols.length) {
      ['--aur-1', '--aur-2', '--aur-3'].forEach((k) => root.style.setProperty(k, 'var(--accent-rgb)'));
      root.style.setProperty('--aur-a', '0.07');
      strip.style.background = '';
      strip.style.opacity = '0.25';
      return;
    }
    root.style.setProperty('--aur-1', cols[0]);
    root.style.setProperty('--aur-2', cols[1] || cols[0]);
    root.style.setProperty('--aur-3', cols[2] || cols[0]);
    root.style.setProperty('--aur-a', String(Math.min(0.34, 0.18 + lights.filter((l) => l.state === 'on').length * 0.01)));
    strip.style.background = cols.length === 1
      ? `rgb(${cols[0]})`
      : `linear-gradient(90deg, ${cols.map((c, i) => `rgb(${c}) ${Math.round((i / (cols.length - 1)) * 100)}%`).join(', ')})`;
    strip.style.opacity = '0.7';
  }

  let modeBusy = null;
  function modeButton(key, m, hint) {
    const b = el('button', 'mode' + (key === hint ? ' suggested' : '') + (modeBusy === key ? ' busy' : ''));
    b.type = 'button';
    b.disabled = !canControl || !!modeBusy;
    b.style.setProperty('--mode-a', MODE_TINT[key] || 'var(--surface-2)');
    b.append(el('span', 'mode-name', m.name), el('span', 'mode-desc', m.desc));
    b.addEventListener('click', () => runMode(key));
    return b;
  }
  function renderModes() {
    const hint = suggestedMode(new Date().getHours());
    $('modes').replaceChildren(...Object.entries(MODES).map(([k, m]) => modeButton(k, m, hint)));
    const has = !!(hint && MODES[hint]);
    if (has) $('now-suggest').replaceChildren(modeButton(hint, MODES[hint], hint));
    else $('now-suggest').replaceChildren(...contextCard());
    $('card-suggest').hidden = !$('now-suggest').childElementCount;
  }
  // When no lighting routine fits the hour, "Right now" shows what the M-1
  // panel is doing. (Nothing work-related lives on this site — owner rule.)
  function contextCard() {
    const d = lastState || {};
    const link = (href, name, sub) => {
      const a = el('a', 'ctx');
      a.href = href;
      a.append(el('span', 'ctx-name', name), el('span', 'ctx-sub', sub));
      return [a];
    };
    if (d.arcade && d.arcade.mode) return link('/arcade', `Arcade · ${cap(d.arcade.mode)}`, 'The M-1 panel — change mode');
    return [];
  }

  function flash(text, kind) {
    const m = $('lights-msg');
    m.className = 'msg' + (kind ? ' ' + kind : '');
    m.textContent = text;
    if (kind === 'ok') setTimeout(() => { if (m.textContent === text) m.textContent = ''; }, 4000);
  }
  async function callService(service, ids, extra) {
    const res = await fetch(`/api/ha/service/light/${service}`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: ids, ...(extra || {}) }),
    });
    if (!res.ok) throw new Error(res.status === 403 ? 'Controls are locked' : `Couldn't reach the lights (${res.status})`);
  }
  async function runMode(key) {
    const m = MODES[key];
    if (!m || !canControl || modeBusy) return;
    const steps = m.run().filter(([ids]) => ids && ids.length);
    if (!steps.length) return;
    modeBusy = key; flash(''); renderModes();
    const results = await Promise.allSettled(steps.map(([ids, payload, svc]) =>
      svc === 'turn_off' ? callService('turn_off', ids) : callService('turn_on', ids, { transition: 0.4, ...payload })));
    modeBusy = null;
    if (results.every((r) => r.status === 'fulfilled')) flash(`${m.name} set`, 'ok');
    else flash(`${m.name}: some lights didn't respond`, 'err');
    setTimeout(loadLights, 600);
    renderModes();
  }
  async function toggleRoom(r) {
    if (!canControl || pending.has(r.name)) return;
    const off = r.on > 0;
    pending.set(r.name, off ? 0 : r.total);
    flash(''); renderLights();
    try { await callService(off ? 'turn_off' : 'turn_on', r.ids); }
    catch (e) { flash(`${r.name}: ${e.message}`, 'err'); }
    setTimeout(() => { pending.delete(r.name); loadLights(); }, 1200);
  }
  async function loadLights() {
    try {
      const data = await (await fetch('/api/ha/lights-data', { credentials: 'same-origin' })).json();
      lightsData = data;
      rooms = roomsFrom(data);
      $('fresh-dot').className = 'hb-dot ' + (data.ha_ok !== false ? 'hb-dot-live' : 'hb-dot-stale');
    } catch (e) {
      $('fresh-dot').className = 'hb-dot hb-dot-stale';
    }
    renderLights();
  }
  async function loadAuth() {
    try { canControl = !!(await (await fetch('/api/auth/status', { credentials: 'same-origin' })).json()).can_control; }
    catch (e) { canControl = false; }
    renderLights();
  }

  // ═══════════════════════ Hub state (Now + Everything) ═══════════════════════
  const APP_NAMES = Object.fromEntries((window.HBStreaming || []).filter((s) => s.pkg).map((s) => [s.pkg, s.name]));
  const HOME_APPS = /tvcenter|com\.tcl\.tv$|launcher|dreamx|screensaver/i;
  function tvApp(t) {
    if (!t.app) return null;
    if (HOME_APPS.test(t.app)) return 'Home screen';
    return APP_NAMES[t.app] || t.app.split('.').pop().replace(/^\w/, (c) => c.toUpperCase());
  }
  function whenLabel(ev) {
    if (!ev.start) return '';
    const d = new Date(ev.all_day ? ev.start + 'T00:00:00' : ev.start);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    const dl = diff === 0 ? 'Today' : diff === 1 ? 'Tomorrow' : d.toLocaleDateString(undefined, { weekday: 'short' });
    return ev.all_day ? `${dl} · all day` : `${dl} ${d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}`;
  }
  function setLaunch(id, live) {
    const n = $(id);
    if (!n) return;
    if (!n.dataset.base) n.dataset.base = n.textContent;
    n.textContent = live || n.dataset.base;
    n.classList.toggle('live', !!live);
  }

  // Camera uploads arrive titled like '843Dde65 B791 … 20260710 190932' —
  // show those as a dated clip instead of a hash.
  function clipName(title) {
    if (!title) return null;
    const hexish = (title.match(/\b[0-9a-f]{4,}\b/gi) || []).filter((t) => /\d/.test(t) && /[a-f]/i.test(t)).length;
    if (hexish < 2) return title;
    const m = title.match(/\b(20\d{2})(\d{2})(\d{2})\b/);
    if (!m) return 'Latest clip';
    const dt = new Date(+m[1], +m[2] - 1, +m[3]);
    return `Clip · ${dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
  }

  let lastState = null;
  function renderState(d) {
    lastState = d;
    // Headline — the front door's "Henderburgh is ___."
    const st = d.status || {};
    if (st.state) $('hero-state').textContent = st.state;
    if (st.sub) $('hero-sub').textContent = st.sub;
    $('hero-dot').classList.toggle('still', st.live === false);
    $('ambient-line').textContent = st.state ? `Henderburgh is ${st.state}.` : '';

    const w = d.weather;
    $('wx-temp').textContent = w && w.temperature != null ? `${Math.round(w.temperature)}${w.temperature_unit || '°'}` : '—';
    $('wx-cond').textContent = w ? cap(w.condition) : '—';
    $('wx-hum').textContent = w && w.humidity != null ? `${Math.round(w.humidity)}%` : '—';
    $('wx-wind').textContent = w && w.wind_speed != null ? `${Math.round(w.wind_speed)} ${w.wind_speed_unit || ''}`.trim() : '—';

    // On TV
    const rows = $('tv-rows');
    rows.replaceChildren();
    const tvs = d.tvs || [];
    if (!tvs.length) rows.appendChild(el('span', 'muted', 'No TVs connected yet.'));
    tvs.forEach((t) => {
      const r = el('span', 'row');
      r.append(el('span', 'hb-dot ' + (t.on ? 'hb-dot-live' : 'hb-dot-off')), el('b', null, t.name),
        el('span', null, !t.available ? 'unreachable' : t.on ? ([tvApp(t), t.title].filter(Boolean).join(' — ') || 'on') : 'off'));
      rows.appendChild(r);
    });
    const watching = tvs.filter((t) => t.on && tvApp(t) && tvApp(t) !== 'Home screen');
    setLaunch('ln-watch', watching.length ? `${watching[0].name}: ${tvApp(watching[0])}` : null);

    // Music
    const mb = $('music-body');
    mb.replaceChildren();
    const music = d.music || {};
    const np = music.now_playing || (music.recent || [])[0];
    if (!music.configured) mb.appendChild(el('p', 'muted', 'Last.fm isn’t connected.'));
    else if (!np) mb.appendChild(el('p', 'muted', music.ok === false ? 'Couldn’t reach Last.fm.' : 'Nothing scrobbled yet.'));
    else {
      $('music-label').replaceChildren();
      if (music.now_playing) {
        const eq = el('span', 'eq'); eq.append(el('i'), el('i'), el('i'));
        $('music-label').append(eq, document.createTextNode('Now playing'));
      } else {
        $('music-label').textContent = `Last played · ${ago(np.uts)}`;
      }
      const row = el('div', 'np');
      const art = el('div', 'np-art');
      if (np.art && /^https:\/\//.test(np.art)) art.style.backgroundImage = `url("${np.art.replace(/"/g, '')}")`;
      const txt = el('div');
      txt.style.minWidth = '0';
      txt.append(el('div', 'np-track', np.track), el('div', 'np-artist', np.artist));
      row.append(art, txt);
      mb.appendChild(row);
    }

    // Up next
    const cb = $('cal-body');
    cb.replaceChildren();
    const cal = d.calendar || {};
    if (!cal.configured) cb.appendChild(el('p', 'muted', 'No calendar connected yet.'));
    else if (!(cal.events || []).length) cb.appendChild(el('p', 'muted', 'Nothing on the calendar this week.'));
    else {
      const ul = el('ul', 'cal-list');
      cal.events.slice(0, 3).forEach((ev) => {
        const li = el('li');
        const main = el('div'); main.style.minWidth = '0';
        main.append(el('div', 'cal-title', ev.title));
        li.append(el('span', 'cal-when', whenLabel(ev)), main);
        ul.appendChild(li);
      });
      cb.appendChild(ul);
    }

    // Body
    const v = d.vitals || {};
    $('tick-bpm').textContent = v.hr != null ? String(v.hr) : (heartDay && seriesOf(heartDay).length ? String(seriesOf(heartDay).slice(-1)[0].y) : '—');
    $('tick-steps').textContent = v.steps != null ? Number(v.steps).toLocaleString() : '—';
    const vs = $('vitals-stats');
    vs.replaceChildren();
    [['Heart', v.hr != null ? `${v.hr}` : '—'], ['Steps', v.steps != null ? Number(v.steps).toLocaleString() : '—'],
     ['Ready', v.readiness != null ? `${v.readiness}` : '—']].forEach(([l, val]) => {
      const s = el('span', 'stat');
      s.append(el('span', 'hb-label', l), el('span', 'stat-val', val));
      vs.appendChild(s);
    });
    setLaunch('ln-vitals', v.readiness != null ? `Readiness ${v.readiness}${v.hr ? ` · heart ${v.hr}` : ''}` : null);

    // Everything: live lines
    const x = d.xbox || {};
    setLaunch('ln-xbox', x.playing_now && x.game && x.game !== '—' ? `${x.kind === 'media' ? 'Watching' : 'Playing'} ${x.game}` : null);
    setLaunch('ln-arcade', d.arcade && d.arcade.mode ? `Showing ${cap(d.arcade.mode)}` : null);
    const post = d.blog && d.blog.latest;
    setLaunch('ln-blog', post && post.text ? `“${post.text}”${post.name ? ` — ${post.name}` : ''}` : null);
    const g = d.golf;
    setLaunch('ln-golf', g && g.score ? `${g.score} at ${g.course || 'the course'}${g.date ? ` · ${g.date}` : ''}` : null);
    const strip = $('clip-strip');
    strip.replaceChildren();
    (d.clips || []).forEach((c) => {
      if (!c.url || !/^(https:\/\/|\/)/.test(c.url)) return;
      if (c.kind === 'image') { const i = el('img'); i.src = c.url; i.alt = ''; i.loading = 'lazy'; strip.appendChild(i); }
      else { const vid = el('video'); vid.src = c.url + '#t=0.1'; vid.preload = 'metadata'; vid.muted = true; vid.playsInline = true; strip.appendChild(vid); }
    });
    setLaunch('ln-clips', d.clips && d.clips[0] ? clipName(d.clips[0].title) : null);

    renderModes();
    paintHeart();
    openForContext(d);
  }

  // Context opening: if nobody has touched the hub yet, a TV actually playing
  // something in the evening pulls the hub to Watch. Otherwise Now.
  const loadedAt = Date.now();
  function openForContext(d) {
    if (userMoved || Date.now() - loadedAt > 4000) return;
    const h = new Date().getHours();
    const playing = (d.tvs || []).some((t) => t.on && tvApp(t) && tvApp(t) !== 'Home screen');
    if (playing && (h >= 17 || h < 3)) goTo('watch');
    userMoved = true;
  }

  async function loadState() {
    try { renderState(await (await fetch('/api/hub/state', { credentials: 'same-origin' })).json()); }
    catch (e) { /* keep last good state on screen; the fresh dot already reports HA */ }
  }

  // ═══════════════════════ Heart ═══════════════════════
  // Day-replay x values are minutes on the LOGICAL day: 240 (4 AM) through
  // 1680 (4 AM next day), the same window the front door's explorer uses.
  const D0 = 240, D1 = 1680;
  const GAP_MIN = 20;   // never draw a line across a ring gap longer than this
  const SVGNS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    return n;
  }
  function seriesOf(d) {
    return ((d && d.hr_series) || []).filter((p) => p && Number.isFinite(p.x) && p.y > 0).sort((a, b) => a.x - b.x);
  }
  function runs(series) {
    // split into continuous runs so gaps stay visibly empty
    const out = [];
    let cur = [];
    series.forEach((p, i) => {
      if (i && p.x - series[i - 1].x > GAP_MIN) { if (cur.length) out.push(cur); cur = []; }
      cur.push(p);
    });
    if (cur.length) out.push(cur);
    return out;
  }
  function bpmAt(series, m) {
    if (!series.length) return null;
    for (let i = 1; i < series.length; i++) {
      const a = series[i - 1], b = series[i];
      if (m >= a.x && m <= b.x) {
        if (b.x - a.x > GAP_MIN) return null;
        return Math.round(a.y + (b.y - a.y) * ((m - a.x) / Math.max(1e-6, b.x - a.x)));
      }
    }
    const first = series[0], last = series[series.length - 1];
    if (Math.abs(m - first.x) <= 3) return first.y;
    if (Math.abs(m - last.x) <= 3) return last.y;
    return null;
  }
  function clk(m) {
    const t = ((Math.round(m) % 1440) + 1440) % 1440;
    const h = Math.floor(t / 60), mm = String(t % 60).padStart(2, '0');
    return `${((h + 11) % 12) + 1}:${mm} ${h < 12 ? 'AM' : 'PM'}`;
  }
  function nowMinute() {
    // minutes since midnight ET, mapped into the logical-day window
    let h = new Date().getHours(), mi = new Date().getMinutes();
    try {
      const p = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: 'numeric', hour12: false }).formatToParts(new Date());
      h = +p.find((x) => x.type === 'hour').value % 24; mi = +p.find((x) => x.type === 'minute').value;
    } catch (e) {}
    const m = h * 60 + mi;
    return m < D0 ? m + 1440 : m;
  }
  // Draw a trace into an svg whose viewBox is "240 0 1440 H".
  function drawTrace(svg, d, H, opts) {
    svg.replaceChildren();
    const dom = (opts && opts.domain) || [D0, D1];
    svg.setAttribute('viewBox', `${dom[0]} 0 ${dom[1] - dom[0]} ${H}`);
    const series = seriesOf(d);
    if (!series.length) return { series, y: () => H / 2 };
    const ys = series.map((p) => p.y);
    const lo = Math.max(35, Math.min(...ys) - 6), hi = Math.max(lo + 30, Math.max(...ys) + 6);
    const y = (bpm) => H - 6 - ((bpm - lo) / (hi - lo)) * (H - 12);
    if (opts && opts.bands) (d.bands || []).forEach((b) => {
      const x0 = Math.max(dom[0], b.x0), x1 = Math.min(dom[1], b.x1);
      if (x1 - x0 < 1) return;
      svg.appendChild(svgEl('rect', { x: x0, y: 0, width: x1 - x0, height: H,
        fill: b.kind === 'media' ? 'rgba(96,165,250,0.13)' : 'rgba(139,127,255,0.15)' }));
    });
    const rs = runs(series);
    for (let i = 1; i < rs.length; i++) {   // a faint dotted bridge marks the gap honestly
      const a = rs[i - 1][rs[i - 1].length - 1], b = rs[i][0];
      svg.appendChild(svgEl('line', { x1: a.x, y1: y(a.y), x2: b.x, y2: y(b.y), class: 'trace-ghost' }));
    }
    rs.forEach((run) => {
      if (run.length === 1) run = [run[0], { x: run[0].x + 1, y: run[0].y }];
      svg.appendChild(svgEl('polyline', { points: run.map((p) => `${p.x.toFixed(1)},${y(p.y).toFixed(1)}`).join(' '), class: 'trace' }));
    });
    return { series, y, dom };
  }
  // The heartline zooms to the hours the ring actually recorded (at least two),
  // so a short sync reads as a heartbeat, not a speck in a 24-hour frame.
  function heartDomain(series) {
    if (!series.length) return [D0, D1];
    let a = series[0].x, b = series[series.length - 1].x;
    const pad = Math.max(6, (b - a) * 0.04);
    a -= pad; b += pad;
    if (b - a < 120) { const mid = (a + b) / 2; a = mid - 60; b = mid + 60; }
    return [Math.max(D0, a), Math.min(D1, b)];
  }
  function placeDot(dot, x, yPx, H, live, bpm, dom) {
    dom = dom || [D0, D1];
    dot.hidden = false;
    dot.style.left = `${((x - dom[0]) / (dom[1] - dom[0])) * 100}%`;
    dot.style.top = `${(yPx / H) * 100}%`;
    dot.classList.toggle('live', live);
    if (live && bpm) dot.style.setProperty('--beat', `${(60 / bpm).toFixed(2)}s`);
  }

  // The Now heartline + bar ticker + ambient heart, from the newest day with data.
  let heartDay = null, heartIso = null;
  function paintHeart() {
    const d = heartDay;
    const series = seriesOf(d);
    const last = series[series.length - 1];
    const isToday = heartIso === TODAY;
    const age = last && isToday ? nowMinute() - last.x : null;
    const fresh = age != null && age >= 0 && age <= 30;

    // The big number is always the NEWEST reading the house has: a live trace
    // from today wins; otherwise the vitals snapshot (often newer than the
    // last day's trace). The line stays labelled as the day it came from.
    const v = (lastState && lastState.vitals) || {};
    const vTs = v.hr_timestamp ? Date.parse(v.hr_timestamp) : NaN;
    const vAge = Number.isFinite(vTs) ? (Date.now() - vTs) / 60000 : (v.hr_age_minutes != null ? v.hr_age_minutes : null);
    const useVitals = v.hr != null && !(isToday && fresh);
    const bpm = useVitals ? v.hr : (last ? last.y : null);
    const bpmFresh = useVitals ? (vAge != null && vAge <= 30) : fresh;
    const when = useVitals
      ? (Number.isFinite(vTs) ? new Date(vTs).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }) : '')
      : (last ? clk(last.x) : '');

    $('hl-label').textContent = !d ? 'Heart' : isToday ? 'Heart · today' : `Heart · trace from ${relLabel(heartIso).toLowerCase()}`;
    $('hl-bpm').textContent = bpm != null ? String(bpm) : '—';
    $('hl-foot').textContent = bpm == null ? 'No ring data yet.' :
      bpmFresh ? `Live · ${bpm} bpm at ${when}` :
      `Latest ${bpm} bpm${when ? ` at ${when}` : ''}${d && !isToday ? ' · no trace synced today yet' : ''}`;

    const H = 100;
    const dom = heartDomain(series);
    const { y } = drawTrace($('hl-svg'), d || {}, H, { bands: true, domain: dom });
    const amb = drawTrace($('amb-svg'), d || {}, H, { domain: dom });
    if (last) {
      placeDot($('hl-dot'), last.x, y(last.y), H, fresh, last.y, dom);
      placeDot($('amb-dot'), last.x, amb.y(last.y), H, fresh, last.y, dom);
    } else { $('hl-dot').hidden = true; $('amb-dot').hidden = true; }
    const axis = $('hl-axis');
    axis.replaceChildren(...[0, 0.5, 1].map((f) => el('span', null, clk(dom[0] + (dom[1] - dom[0]) * f).replace(':00', '').replace(' ', '').toLowerCase())));
    $('ambient-bpm').textContent = bpm != null ? `${bpm} bpm${bpmFresh || !when ? '' : ` · ${when}`}` : '';

    // The bar's beat follows the newest reading; the trace dot only beats when
    // the trace itself is live (today + fresh), so an old line never pretends.
    const beat = $('tick-beat');
    beat.className = 'beat ' + (bpmFresh ? 'live' : bpm != null ? 'stale' : '');
    if (bpmFresh) beat.style.setProperty('--beat', `${(60 / bpm).toFixed(2)}s`);
  }

  // ═══════════════════════ Day (the front door's story) ═══════════════════════
  function logicalTodayISO() {
    const d = new Date(Date.now() - 4 * 3600 * 1000);
    try {
      return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
    } catch (e) { return d.toISOString().slice(0, 10); }
  }
  function shiftISO(iso, days) {
    const [y, m, dd] = iso.split('-').map(Number);
    return new Date(Date.UTC(y, m - 1, dd + days)).toISOString().slice(0, 10);
  }
  const TODAY = logicalTodayISO();
  let dayIso = TODAY;
  let dayData = null;
  const DAY_CACHE = {};

  function richText(node, text) {
    // **bold** markup from the server narration -> <b>, everything via textContent
    node.replaceChildren();
    String(text || '').split(/(\*\*[^*]+\*\*)/).forEach((part) => {
      if (/^\*\*[^*]+\*\*$/.test(part)) node.appendChild(el('b', null, part.slice(2, -2)));
      else if (part) node.appendChild(document.createTextNode(part));
    });
  }
  function relLabel(iso) {
    if (iso === TODAY) return 'Today';
    if (iso === shiftISO(TODAY, -1)) return 'Yesterday';
    const [y, m, d] = iso.split('-').map(Number);
    const [ty, tm, td] = TODAY.split('-').map(Number);
    return `${Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(y, m - 1, d)) / 86400000)} days ago`;
  }

  const DAY_INFLIGHT = {};   // the heartline and the Day space share one request per day
  function fetchDay(iso) {
    if (DAY_CACHE[iso]) return Promise.resolve(DAY_CACHE[iso]);
    if (DAY_INFLIGHT[iso]) return DAY_INFLIGHT[iso];
    DAY_INFLIGHT[iso] = (async () => {
      let d;
      try { d = await (await fetch('/api/day-replay?day=' + encodeURIComponent(iso), { credentials: 'same-origin' })).json(); }
      catch (e) { d = { has_data: false }; }
      if (d && d.has_data && iso !== TODAY) DAY_CACHE[iso] = d;   // today keeps filling in
      delete DAY_INFLIGHT[iso];
      return d || { has_data: false };
    })();
    return DAY_INFLIGHT[iso];
  }

  let rows = [];          // [{m, li}] for the playhead <-> timeline link
  let dayTrace = null;    // {series, y}
  const DAY_H = 160;

  function paintDay(iso, d) {
    dayData = d;
    $('day-rel').textContent = relLabel(iso);
    $('day-label').textContent = d.day || iso;
    $('day-next').disabled = iso >= TODAY;
    const tl = $('day-timeline');
    tl.replaceChildren();
    const stats = $('day-stats');
    stats.replaceChildren();
    rows = [];
    if (!d.has_data) {
      $('day-narration').textContent = iso === TODAY ? 'Nothing recorded yet today — the ring hasn’t synced.' : 'Nothing recorded for this day.';
      dayTrace = drawTrace($('day-svg'), {}, DAY_H, {});
      $('playhead').classList.remove('on');
      $('rd-time').textContent = '—'; $('rd-bpm').textContent = '—'; $('rd-what').textContent = '';
      $('tl-rail').replaceChildren();
      return;
    }
    richText($('day-narration'), d.narration || d.facts || '');
    const stat = (label, val, sub) => {
      const s = el('div', 'stat');
      s.append(el('span', 'hb-label', label), el('span', 'stat-val', val));
      if (sub) s.appendChild(el('span', 'faint', sub));
      stats.appendChild(s);
    };
    if (d.peak) stat('Peak', `${d.peak.bpm}`, d.peak.clk);
    if (d.low) stat('Calm', `${d.low.bpm}`, d.low.clk);
    if (d.current) stat(iso === TODAY ? 'Now' : 'Last', `${d.current.bpm}`, d.current.clk);
    stat('Games', `${d.games_count || 0}`);
    stat('Watching', `${d.media_count || 0}`);
    dayTrace = drawTrace($('day-svg'), d, DAY_H, { bands: true });
    (d.timeline || []).forEach((e) => {
      const li = el('li');
      li.dataset.kind = e.kind || '';
      const body = el('div');
      body.append(el('div', 'tl-title', e.title), el('div', 'tl-detail', e.detail || ''));
      li.append(el('span', 'tl-clk', e.clk || ''), body);
      li.addEventListener('click', () => setPlayhead(e.x, false));
      tl.appendChild(li);
      rows.push({ m: e.x, li, e });
    });
    requestAnimationFrame(drawRail);
    const start = d.current ? d.current.x : (rows[0] ? rows[0].m : (dayTrace.series[0] || {}).x);
    if (start != null) setPlayhead(start, true);
  }

  // The front door's vertical heart rail, drawn down the timeline: each HR
  // point is placed between the rows it falls between, so the line bends
  // through the events it was beating during.
  const KIND_COLOR = { game: '#8B7FFF', media: '#60a5fa', unlock: '#FFB454', peak: '#f87171', wake: '#4ade80', sleep: '#4ade80' };
  function drawRail() {
    const svg = $('tl-rail');
    svg.replaceChildren();
    const series = dayTrace ? dayTrace.series : [];
    if (!rows.length) return;
    const list = $('day-timeline');
    const H = list.offsetHeight;
    svg.setAttribute('viewBox', `0 0 44 ${H}`);
    svg.style.height = `${H}px`;
    const pts = rows.map((r) => ({ m: r.m, y: r.li.offsetTop + 18 })).sort((a, b) => a.m - b.m);
    const yOf = (m) => {
      if (m <= pts[0].m) return Math.max(4, pts[0].y - (pts[0].m - m) * 0.6);
      for (let i = 1; i < pts.length; i++) {
        if (m <= pts[i].m) {
          const a = pts[i - 1], b = pts[i];
          return a.y + (b.y - a.y) * ((m - a.m) / Math.max(1e-6, b.m - a.m));
        }
      }
      const z = pts[pts.length - 1];
      return Math.min(H - 4, z.y + (m - z.m) * 0.6);
    };
    const ys = series.map((p) => p.y);
    const lo = ys.length ? Math.min(...ys) : 60, hi = ys.length ? Math.max(...ys) : 100;
    const xOf = (bpm) => 8 + ((bpm - lo) / Math.max(1, hi - lo)) * 28;
    svg.appendChild(svgEl('line', { x1: 22, y1: 0, x2: 22, y2: H, class: 'rail-gap' }));
    runs(series).forEach((run) => {
      if (run.length < 2) return;
      svg.appendChild(svgEl('polyline', { points: run.map((p) => `${xOf(p.y).toFixed(1)},${yOf(p.x).toFixed(1)}`).join(' '), class: 'rail' }));
    });
    rows.forEach((r) => {
      const b = bpmAt(series, r.m);
      svg.appendChild(svgEl('circle', { cx: b != null ? xOf(b) : 22, cy: r.li.offsetTop + 18, r: 4.5,
        fill: KIND_COLOR[r.e.kind] || '#7a8494', stroke: '#0a0a0b', 'stroke-width': 2 }));
    });
  }

  // One playhead for the chart, the reading and the timeline.
  let activeRow = null;
  function setPlayhead(m, fromScroll) {
    if (!dayTrace) return;
    m = Math.max(D0, Math.min(D1, m));
    const ph = $('playhead');
    const bpm = bpmAt(dayTrace.series, m);
    ph.classList.add('on');
    ph.style.left = `${((m - D0) / (D1 - D0)) * 100}%`;
    ph.style.setProperty('--py', bpm != null ? `${(dayTrace.y(bpm) / DAY_H) * 100}%` : '-100%');
    $('rd-time').textContent = clk(m);
    $('rd-bpm').textContent = bpm != null ? String(bpm) : '—';
    let row = null;
    rows.forEach((r) => { if (r.m <= m + 2) row = r; });
    $('rd-what').textContent = row ? row.e.title : '';
    if (row !== activeRow) {
      if (activeRow) activeRow.li.classList.remove('active');
      if (row) row.li.classList.add('active');
      activeRow = row;
      if (row && !fromScroll) {
        const wrap = $('tl-wrap');
        const scroller = wrap.scrollHeight > wrap.clientHeight + 4 ? wrap : $('space-day');
        if (scroller === wrap) wrap.scrollTop = Math.max(0, row.li.offsetTop - 60);
      }
    }
  }

  // Scrub: drag across the chart (touch-action:none keeps the space swipe out of it).
  (function scrub() {
    const chart = $('day-chart');
    let dragging = false;
    const at = (ev) => {
      const r = chart.getBoundingClientRect();
      return D0 + ((ev.clientX - r.left) / Math.max(1, r.width)) * (D1 - D0);
    };
    chart.addEventListener('pointerdown', (ev) => { dragging = true; chart.setPointerCapture(ev.pointerId); setPlayhead(at(ev), false); });
    chart.addEventListener('pointermove', (ev) => { if (dragging) setPlayhead(at(ev), false); });
    const end = () => { dragging = false; };
    chart.addEventListener('pointerup', end);
    chart.addEventListener('pointercancel', end);
  })();

  // Scroll the timeline -> the playhead walks to the event at the top.
  (function follow() {
    let t = null;
    const onScroll = (ev) => {
      clearTimeout(t);
      t = setTimeout(() => {
        const sc = ev.target;
        const top = sc.getBoundingClientRect().top + 40;
        let hit = null;
        rows.forEach((r) => { if (r.li.getBoundingClientRect().top <= top) hit = r; });
        if (hit) setPlayhead(hit.m, true);
      }, 60);
    };
    $('tl-wrap').addEventListener('scroll', onScroll, { passive: true });
    $('space-day').addEventListener('scroll', onScroll, { passive: true });
  })();

  let daySeq = 0;
  async function loadDay(iso) {
    const seq = ++daySeq;
    dayIso = iso;
    const d = await fetchDay(iso);
    if (seq !== daySeq) return;
    paintDay(iso, d);
  }
  // Open on the newest day that actually has a story (today, else walk back
  // up to three days) — an empty Sunday shouldn't be the first thing you see.
  async function openNewestDay() {
    const seq = ++daySeq;
    const isos = [0, 1, 2, 3].map((i) => shiftISO(TODAY, -i));
    const days = await Promise.all(isos.map((iso) => fetchDay(iso)));
    if (seq !== daySeq) return;   // the user already picked a day
    const i = Math.max(0, days.findIndex((d) => d.has_data));
    dayIso = isos[i];
    paintDay(isos[i], days[i]);
  }
  // The heartline wants the newest day that has a trace: today, else walk back.
  async function loadHeart() {
    // Ask for the last four days at once; the newest one with a trace wins.
    const isos = [0, 1, 2, 3].map((i) => shiftISO(TODAY, -i));
    const days = await Promise.all(isos.map((iso) => fetchDay(iso)));
    const i = days.findIndex((d) => seriesOf(d).length);
    if (i >= 0) { heartDay = days[i]; heartIso = isos[i]; }
    paintHeart();
  }
  $('day-prev').addEventListener('click', () => loadDay(shiftISO(dayIso, -1)));
  $('day-next').addEventListener('click', () => { if (dayIso < TODAY) loadDay(shiftISO(dayIso, 1)); });
  // Swipe the date header to change day (the header opts out of the space swipe).
  (function daySwipe() {
    const nav = document.querySelector('.day-nav');
    let x0 = null, y0 = null;
    nav.addEventListener('pointerdown', (ev) => { x0 = ev.clientX; y0 = ev.clientY; });
    nav.addEventListener('pointerup', (ev) => {
      if (x0 == null) return;
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      x0 = null;
      if (Math.abs(dx) < 50 || Math.abs(dy) > Math.abs(dx)) return;
      if (dx > 0) loadDay(shiftISO(dayIso, -1)); else if (dayIso < TODAY) loadDay(shiftISO(dayIso, 1));
    });
  })();
  window.addEventListener('keydown', (ev) => {
    const cur = tabs.find((t) => t.getAttribute('aria-current') === 'true');
    if (!cur || cur.dataset.space !== 'day') return;
    if (ev.key === 'ArrowLeft') loadDay(shiftISO(dayIso, -1));
    if (ev.key === 'ArrowRight' && dayIso < TODAY) loadDay(shiftISO(dayIso, 1));
  });
  window.addEventListener('resize', () => requestAnimationFrame(drawRail));

  // ═══════════════════════ Idle (ambient) ═══════════════════════
  const idleAllowed = (window.matchMedia && matchMedia('(display-mode: standalone)').matches) ||
    window.navigator.standalone === true || /[?&]kiosk=1\b/.test(location.search);
  let idleT = null;
  function wake() {
    document.body.classList.remove('idle');
    clearTimeout(idleT);
    if (idleAllowed) idleT = setTimeout(() => document.body.classList.add('idle'), IDLE_MS);
  }
  ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach((ev) => window.addEventListener(ev, wake, { passive: true, capture: true }));

  // ═══════════════════════ Poke ═══════════════════════
  const poke = $('poke');
  if (poke) poke.addEventListener('click', async () => {
    poke.disabled = true;
    try {
      const r = await fetch('/api/ha/poke', { method: 'POST' });
      $('poke-msg').textContent = r.ok ? 'Blinked.' : r.status === 429 ? 'Give it a few seconds.' : 'The lamp didn’t answer.';
    } catch (e) { $('poke-msg').textContent = 'The lamp didn’t answer.'; }
    setTimeout(() => { poke.disabled = false; }, 3000);
  });

  // ═══════════════════════ Polling ═══════════════════════
  let timers = [];
  function start() {
    stop();
    tickClock(); loadLights(); loadState();
    timers.push(setInterval(tickClock, 15000));
    timers.push(setInterval(loadLights, LIGHTS_POLL_MS));
    timers.push(setInterval(loadState, STATE_POLL_MS));
    timers.push(setInterval(() => { delete DAY_CACHE[TODAY]; loadHeart(); }, 5 * 60 * 1000));
    timers.push(setInterval(paintHeart, 60 * 1000));   // keep 'n min ago' + the live/stale beat honest
  }
  function stop() { timers.forEach(clearInterval); timers = []; }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { loadAuth(); start(); wake(); } else stop();
  });

  tickClock();
  renderModes();
  loadAuth();
  openNewestDay();
  loadHeart();
  wake();
  if (document.visibilityState === 'visible') start();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
