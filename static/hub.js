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

    const house = avgRgb(Object.values(lightsData.lights_by_room || {}).flat().filter((l) => !l.is_sync));
    root.style.setProperty('--house-rgb', house || 'var(--accent-rgb)');
    root.style.setProperty('--house-a', String(house ? Math.min(0.32, 0.12 + on * 0.015) : 0.08));
    renderModes();
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
    $('card-suggest').hidden = !has;
    $('now-suggest').replaceChildren(...(has ? [modeButton(hint, MODES[hint], hint)] : []));
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
    setLaunch('ln-clips', d.clips && d.clips[0] && d.clips[0].title ? d.clips[0].title : null);

    document.body.classList.toggle('work', !!d.work_hours);
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

  // ═══════════════════════ Day (the front door's story) ═══════════════════════
  // The logical day starts at 4 AM ET, matching the front door's explorer.
  function logicalTodayISO() {
    const d = new Date(Date.now() - 4 * 3600 * 1000);
    try {
      const p = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
      return p;
    } catch (e) { return d.toISOString().slice(0, 10); }
  }
  function shiftISO(iso, days) {
    const [y, m, dd] = iso.split('-').map(Number);
    const t = new Date(Date.UTC(y, m - 1, dd + days));
    return t.toISOString().slice(0, 10);
  }
  const TODAY = logicalTodayISO();
  let dayIso = TODAY;
  let dayFellBack = false;
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
    const days = Math.round((Date.UTC(...TODAY.split('-').map((v, i) => i === 1 ? v - 1 : +v)) - Date.UTC(y, m - 1, d)) / 86400000);
    return `${days} days ago`;
  }

  const SVGNS = 'http://www.w3.org/2000/svg';
  function drawChart(d) {
    const svg = $('day-svg');
    svg.replaceChildren();
    const H = 160, lo = 40, hi = 170;
    const y = (bpm) => H - ((Math.min(hi, Math.max(lo, bpm)) - lo) / (hi - lo)) * (H - 12) - 6;
    (d.bands || []).forEach((b) => {
      const x0 = Math.max(0, Math.min(1440, b.x0)), x1 = Math.max(0, Math.min(1440, b.x1));
      if (x1 - x0 < 1) return;
      const r = document.createElementNS(SVGNS, 'rect');
      r.setAttribute('x', x0); r.setAttribute('y', 0); r.setAttribute('width', x1 - x0); r.setAttribute('height', H);
      r.setAttribute('fill', b.kind === 'media' ? 'rgba(96,165,250,0.14)' : 'rgba(139,127,255,0.16)');
      svg.appendChild(r);
    });
    const pts = (d.hr_series || []).filter((p) => p && p.x >= 0 && p.x <= 1440 && p.y);
    if (pts.length > 1) {
      const path = document.createElementNS(SVGNS, 'polyline');
      path.setAttribute('points', pts.map((p) => `${p.x.toFixed(1)},${y(p.y).toFixed(1)}`).join(' '));
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', '#4ade80');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('vector-effect', 'non-scaling-stroke');
      path.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(path);
    }
  }

  function paintDay(iso, d) {
    $('day-rel').textContent = relLabel(iso) + (dayFellBack && iso !== TODAY ? ' · today is still quiet' : '');
    $('day-label').textContent = d.day || iso;
    $('day-next').disabled = iso >= TODAY;
    const tl = $('day-timeline');
    tl.replaceChildren();
    const stats = $('day-stats');
    stats.replaceChildren();
    if (!d.has_data) {
      $('day-narration').textContent = 'Nothing recorded for this day.';
      drawChart({});
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
    drawChart(d);
    (d.timeline || []).forEach((e) => {
      const li = el('li');
      const body = el('div');
      body.append(el('div', 'tl-title', e.title), el('div', 'tl-detail', e.detail || ''));
      li.append(el('span', 'tl-clk', e.clk || ''), el('span', 'tl-dot ' + (e.kind || '')), body);
      tl.appendChild(li);
    });
  }

  let daySeq = 0;
  async function loadDay(iso, allowFallback) {
    const seq = ++daySeq;
    dayIso = iso;
    let d = DAY_CACHE[iso];
    if (!d) {
      try { d = await (await fetch('/api/day-replay?day=' + encodeURIComponent(iso), { credentials: 'same-origin' })).json(); }
      catch (e) { d = { has_data: false }; }
      if (d && d.has_data) DAY_CACHE[iso] = d;
    }
    if (seq !== daySeq) return;
    // Early in the day there's often nothing yet — show yesterday's full story instead.
    if (allowFallback && !d.has_data && iso === TODAY) { dayFellBack = true; return loadDay(shiftISO(TODAY, -1), false); }
    paintDay(iso, d);
  }
  $('day-prev').addEventListener('click', () => loadDay(shiftISO(dayIso, -1)));
  $('day-next').addEventListener('click', () => { if (dayIso < TODAY) loadDay(shiftISO(dayIso, 1)); });

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
  }
  function stop() { timers.forEach(clearInterval); timers = []; }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { loadAuth(); start(); wake(); } else stop();
  });

  tickClock();
  renderModes();
  loadAuth();
  loadDay(TODAY, true);
  wake();
  if (document.visibilityState === 'visible') start();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
