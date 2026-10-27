/* The hub's Watch space — the watching hub.
   Left: the TVs (what each is showing) and a real remote for the picked one.
   Right: what's on and where — one pick, "split the night" when two big
   events overlap and there are two TVs, live score bugs, tonight's timeline,
   and every app. Everything resolves to an app the owner actually has, and a
   launch only reports success once the TV itself says it switched.

   Rules: textContent only; polling only while visible; never invent a score,
   win %, or "it worked". */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const SVC = Object.fromEntries((window.HBStreaming || []).map((s) => [s.key, s]));
  const tvApp = (app) => (window.HBTvApp ? window.HBTvApp(app) : app);
  const OTA = /^(fox|nbc|cbs|abc|the cw|pbs)$/i;
  let showAllGames = false;

  let tvs = [];
  let selected = null;
  let canControl = false;
  let tonight = null;
  let channels = [];   // HTC quick-tune favourites, from /api/media/tvs
  try { selected = localStorage.getItem('hb-watch-tv'); } catch (e) {}

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const hex = (c) => (c && /^[0-9a-f]{6}$/i.test(c) ? `#${c}` : '#3a3a42');
  const timeOf = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  function hhmm(t) {   // "20:00" -> "8:00 PM"
    const [h, m] = t.split(':').map(Number);
    return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
  }
  function selectedTv() { return tvs.find((t) => t.entity_id === selected) || null; }
  function status(text, kind) {
    const s = $('remote-status');
    s.className = 'status' + (kind ? ' ' + kind : '');
    s.textContent = text;
  }
  function toast(text, kind) {
    // Launch feedback lives near whatever was tapped AND in the remote.
    status(text, kind);
    const p = $('pick-msg');
    if (p) { p.className = 'pick-msg' + (kind ? ' ' + kind : ''); p.textContent = text; }
  }

  async function post(url, body) {
    const res = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) {}
      throw new Error(res.status === 403 ? 'TV control is locked on this device' : detail || `Failed (${res.status})`);
    }
  }

  // ═══════════════ TVs ═══════════════
  function renderTvs() {
    const wrap = $('tvs');
    wrap.replaceChildren();
    if (!tvs.length) {
      const box = el('div', 'empty');
      box.append(el('h2', null, 'No TVs connected'), el('p', 'muted', 'Pair a Google TV with Home Assistant and it appears here.'));
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
      const what = !t.available ? 'Unreachable' : !t.on ? 'Off' : [tvApp(t.app), t.title].filter(Boolean).join(' — ') || 'On';
      b.append(name, el('span', 'tv-what', what));
      b.addEventListener('click', () => {
        selected = t.entity_id;
        try { localStorage.setItem('hb-watch-tv', selected); } catch (e) {}
        renderTvs(); renderGuide();
      });
      wrap.appendChild(b);
    });
    if (openScene) sceneTvs();
    const tv = selectedTv();
    $('remote').hidden = !canControl || !tv;
    $('watch-lock').hidden = canControl;
    if (tv) renderRemote(tv);
    placeFab();
  }

  // The remote's header says what it's driving and what that TV is doing.
  let deck = null;        // 'cable' | 'apps' | 'type' — remembered per session
  function renderRemote(tv) {
    // Screens: one chip per TV, the live dot says which are on.
    const sc = $('rm-screens');
    sc.replaceChildren();
    tvs.forEach((t) => {
      const c = el('button', 'rm-screen');
      c.type = 'button'; c.setAttribute('role', 'tab');
      c.setAttribute('aria-selected', String(t.entity_id === tv.entity_id));
      c.append(el('i', 'hb-dot ' + (t.on ? 'hb-dot-live' : 'hb-dot-off')), document.createTextNode(t.name.replace(/ TV$/, '')));
      c.addEventListener('click', () => {
        selected = t.entity_id;
        try { localStorage.setItem('hb-watch-tv', selected); } catch (e) {}
        renderTvs(); renderGuide();
      });
      sc.appendChild(c);
    });
    $('remote-for').textContent = !tv.available ? 'Unreachable' : !tv.on ? 'Off' : tv.on_box ? tv.box.label : tvApp(tv.app) || 'On';
    const live = tv.on_box && liveOnCable()[0];
    $('remote-what').textContent = !tv.on ? 'Tap power to turn it on' : live ? `${live.g.teams[0].abbr} ${live.g.teams[0].score ?? ''}–${live.g.teams[1].score ?? ''} ${live.g.teams[1].abbr} on ch ${live.c.ch}` :
      tv.on_box ? 'Live TV through the cable box' : tv.box ? 'Cable box ready' : tv.title || '';
    const pw = $('r-power');
    pw.setAttribute('aria-pressed', String(!!tv.on));
    pw.setAttribute('aria-label', tv.on ? `Turn ${tv.name} off` : `Turn ${tv.name} on`);
    $('remote').classList.toggle('is-off', !tv.on);
    const muted = tv.muted === true;
    $('r-mute').setAttribute('aria-pressed', String(muted));
    const v = tv.on && tv.volume != null ? tv.volume : null;
    $('r-vol').textContent = muted ? 'Muted' : v != null ? String(v) : '';
    $('rm-vol').hidden = v == null;
    $('rm-vol-fill').style.transform = `scaleX(${v != null ? Math.min(1, v / 100) : 0})`;
    $('remote').classList.toggle('muted', muted);
    // The deck: cable when this TV has a box, otherwise apps.
    $('tab-cable').hidden = !tv.box;
    const want = deck && (deck !== 'cable' || tv.box) ? deck : tv.box ? 'cable' : 'apps';
    showDeck(want, false);
    if (want === 'cable') renderCable(tv);
  }
  function showDeck(name, remember = true) {
    if (remember) deck = name;
    document.querySelectorAll('#remote .rm-tabs [data-deck]').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.deck === name)));
    document.querySelectorAll('#remote .rm-deck').forEach((d) => { d.hidden = d.dataset.deck !== name; });
    if (name === 'apps' && !$('rm-apps').childElementCount) renderRemoteApps();
  }
  document.querySelectorAll('#remote .rm-tabs [data-deck]').forEach((t) => t.addEventListener('click', () => {
    showDeck(t.dataset.deck);
    const tv = selectedTv();
    if (t.dataset.deck === 'cable' && tv) renderCable(tv);
    if (t.dataset.deck === 'type') { send({ command: 'SEARCH' }); $('r-text').focus(); }
  }));

  // Cable deck: what's live on HTC right now (one tap tunes), then the
  // favourite channels, each saying what's on it when we know.
  function liveOnCable() {
    const out = [];
    ((tonight && tonight.live) || []).forEach((g) => (g.cable || []).forEach((c) => out.push({ g, c })));
    return out;
  }
  function onChannel(ch) {
    const all = [...((tonight && tonight.live) || []), ...((tonight && tonight.upcoming) || [])];
    const g = all.find((x) => (x.cable || []).some((c) => c.ch === ch));
    if (g) return g.state === 'in' ? `${g.teams[0].abbr} ${g.teams[0].score ?? ''}–${g.teams[1].score ?? ''} ${g.teams[1].abbr}` : `${g.teams[0].abbr} @ ${g.teams[1].abbr} ${timeOf(g.start)}`;
    const show = ((tonight && tonight.guide) || []).find((i) => (i.cable || []).some((c) => c.ch === ch) && i.time && hhmmToMin(i.time) <= nowMin() && nowMin() < hhmmToMin(i.time) + (i.runtime || 30));
    return show ? show.title : '';
  }
  const nowMin = () => { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); };
  const hhmmToMin = (t) => { const [h, m] = String(t).split(':').map(Number); return h * 60 + (m || 0); };
  function tune(ch, from) {
    const tv = selectedTv();
    if (!tv || !tv.box) return;
    status(`Tuning ${tv.name} to ${ch}…`);
    from && from.classList.add('tuning');
    setTimeout(() => from && from.classList.remove('tuning'), 1400);
    queue.push({ tv: tv.entity_id, body: { channel: String(ch) }, key: from });
    pump();
    setTimeout(() => status(''), 1800);
  }
  function renderCable(tv) {
    const air = $('rm-onair');
    air.replaceChildren();
    liveOnCable().slice(0, 4).forEach(({ g, c }) => {
      const b = el('button', 'onair' + (g.close ? ' hot' : ''));
      b.type = 'button';
      b.style.setProperty('--ta', hex(g.teams[0].color)); b.style.setProperty('--tb', hex(g.teams[1].color));
      const teams = el('span', 'onair-teams');
      g.teams.forEach((t) => {
        const row = el('span', 'onair-team');
        if (t.logo && /^https:\/\//.test(t.logo)) { const i = el('img'); i.src = t.logo; i.alt = ''; i.loading = 'lazy'; row.appendChild(i); }
        row.append(el('b', null, t.abbr || ''), el('span', 'num', t.score ?? ''));
        teams.appendChild(row);
      });
      const meta = el('span', 'onair-meta');
      meta.append(el('b', 'num', String(c.ch)), el('span', null, c.name), el('span', 'faint', g.detail || ''));
      b.append(teams, meta);
      b.addEventListener('click', () => tune(c.ch, b));
      air.appendChild(b);
    });
    air.hidden = !air.childElementCount;
    const wrap = $('rm-chans');
    wrap.replaceChildren();
    channels.forEach((c) => {
      const b = el('button', 'chan');
      b.type = 'button';
      const what = onChannel(c.ch);
      b.append(el('b', 'num', String(c.ch)), el('span', 'chan-name', c.name));
      if (what) b.append(el('span', 'chan-what', what));
      b.addEventListener('click', () => tune(c.ch, b));
      wrap.appendChild(b);
    });
  }
  function renderRemoteApps() {
    const wrap = $('rm-apps');
    wrap.replaceChildren();
    (window.HBStreaming || []).filter((x) => x.tv).forEach((x) => {
      const b = el('button', 'rm-app');
      b.type = 'button';
      b.style.setProperty('--tint', x.tint);
      b.append(el('i'), el('span', null, x.name));
      b.addEventListener('click', () => launch(x.key, selectedTv()));
      wrap.appendChild(b);
    });
  }

  async function loadTvs() {
    try { const d = await (await fetch('/api/media/tvs', { credentials: 'same-origin' })).json(); tvs = d.tvs || []; channels = d.channels || channels; }
    catch (e) { /* keep last */ }
    renderTvs();
  }
  async function loadAuth() {
    try { canControl = !!(await (await fetch('/api/auth/status', { credentials: 'same-origin' })).json()).can_control; }
    catch (e) { canControl = false; }
    renderTvs(); renderGuide();
  }

  // Launch an app on a TV, then wait for the TV to report that app before
  // claiming it worked.
  async function launch(appKey, tv) {
    const s = SVC[appKey];
    tv = tv || selectedTv();
    if (!s || !s.tv || !tv) return;
    toast(`Opening ${s.name} on ${tv.name}…`);
    const before = (tvs.find((t) => t.entity_id === tv.entity_id) || {}).app;
    try { await post('/api/media/launch', { tv: tv.entity_id, app: s.tv }); }
    catch (e) { toast(e.message, 'err'); return; }
    for (let i = 0; i < 6; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      await loadTvs();
      const now = tvs.find((t) => t.entity_id === tv.entity_id);
      // Known package: it must match. Unknown (the free apps): the TV must
      // at least have left what it was showing.
      const landed = now && (s.pkg ? now.app === s.pkg : now.app && now.app !== before && !/tvcenter|com\.tcl\.tv$|launcher|dreamx/i.test(now.app));
      if (landed) { toast(`${s.name} is on ${tv.name}`, 'ok'); return; }
    }
    toast(`${tv.name} didn’t switch to ${s.name} — is it installed on that TV?`, 'err');
  }

  // ═══════════════ Remote ═══════════════
  // One press at a time, in order: every press waits its turn, and taps that
  // pile up behind a slow one merge into a single call with a repeat count
  // (five quick VOL+ taps = one request, five steps). The TV is a tunnel hop
  // away, so this is what keeps a burst of taps from landing out of order.
  const queue = [];
  let sending = false;
  let refreshSoon = null;
  // Watching cable, everything but volume and power drives the box.
  const TV_ONLY = /^(VOLUME_|power)/;
  function send(body, key) {
    const tv = selectedTv();
    if (!tv) return;
    if (tv.box && tv.on_box && !body.target && !('power' in body) && !TV_ONLY.test(body.command || '')) body = { ...body, target: 'box' };
    const last = queue[queue.length - 1];
    if (last && last.tv === tv.entity_id && body.command && last.body.command === body.command && last.body.target === body.target && !body.hold && !last.body.hold && (last.body.repeat || 1) < 15) {
      last.body.repeat = (last.body.repeat || 1) + 1;
    } else {
      queue.push({ tv: tv.entity_id, body: { ...body }, key });
    }
    pump();
  }
  async function pump() {
    if (sending) return;
    sending = true;
    while (queue.length) {
      const job = queue.shift();
      try {
        await post('/api/media/command', { tv: job.tv, ...job.body });
        status('');
      } catch (e) {
        status(e.message, 'err');
        queue.length = 0;   // the rest would fail the same way
        if (job.key) { job.key.classList.remove('nope'); void job.key.offsetWidth; job.key.classList.add('nope'); }
      }
      // Volume, mute and power change what the header shows; read it back.
      if (/^VOLUME_/.test(job.body.command || '') || 'power' in job.body) {
        clearTimeout(refreshSoon);
        refreshSoon = setTimeout(loadTvs, 'power' in job.body ? 2500 : 700);
      }
    }
    sending = false;
  }

  // Keys: tap sends once; data-hold="repeat" keeps sending while held
  // (volume, channel, scrubbing); data-hold="long" sends a long press.
  document.querySelectorAll('#remote [data-cmd]').forEach((b) => {
    let timer = null, held = false;
    const end = () => { clearTimeout(timer); clearInterval(timer); timer = null; };
    b.addEventListener('pointerdown', (ev) => {
      held = false;
      if (!b.dataset.hold) return;
      b.setPointerCapture(ev.pointerId);
      timer = setTimeout(() => {
        held = true;
        if (b.dataset.hold === 'long') { send({ command: b.dataset.cmd, hold: true }, b); return; }
        send({ command: b.dataset.cmd }, b);
        timer = setInterval(() => send({ command: b.dataset.cmd }, b), 160);
      }, 420);
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((t) => b.addEventListener(t, end));
    b.addEventListener('click', () => { if (held) { held = false; return; } send({ command: b.dataset.cmd }, b); });
    b.addEventListener('contextmenu', (ev) => ev.preventDefault());
  });
  document.querySelectorAll('#remote [data-box]').forEach((b) => b.addEventListener('click', () => send({ command: b.dataset.box, target: 'box' }, b)));
  $('r-num').addEventListener('click', () => {
    const pad = $('r-numpad');
    pad.hidden = !pad.hidden;
    $('r-num').setAttribute('aria-expanded', String(!pad.hidden));
  });
  // Number pad: digits build up on screen with the channel's name, Tune sends them.
  let npDigits = '';
  function npShow() {
    $('np-digits').textContent = npDigits || '—';
    const c = channels.find((x) => String(x.ch) === npDigits);
    $('np-name').textContent = npDigits ? (c ? c.name : '') + (onChannel(Number(npDigits)) ? ` · ${onChannel(Number(npDigits))}` : '') : 'Enter a channel';
  }
  document.querySelectorAll('#remote [data-np]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.np;
    if (k === 'clear') npDigits = '';
    else if (k === 'go') { if (npDigits) tune(npDigits, b); npDigits = ''; }
    else if (npDigits.length < 4) npDigits += k;
    npShow();
  }));
  npShow();
  $('r-power').addEventListener('click', () => {
    const tv = selectedTv();
    if (!tv) return;
    tv.on = !tv.on;   // optimistic; the read-back corrects it
    renderRemote(tv);
    send({ power: tv.on }, $('r-power'));
  });

  // Search + type: opens the TV's search, then letters go to its keyboard.
  $('r-typer').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const text = $('r-text').value.trim();
    if (!text) return;
    send({ text, enter: true });
    $('r-text').value = '';
  });

  // Swipe pad, Apple TV style: a swipe moves one step per ~70px travelled;
  // swipe and keep your finger down to keep moving; tap = OK; press and hold
  // without moving = a long OK (an app's options). Opts out of the space
  // swipe (touch-action:none).
  (function pad() {
    const p = $('pad');
    const ripple = $('pad-ripple');
    const STEP = 70;
    let x0 = null, y0 = null, t0 = 0, dir = null, sent = 0, rep = null, longT = null, longDone = false;
    const flash = (x, y, d) => {
      const r = p.getBoundingClientRect();
      ripple.style.left = `${x - r.left}px`; ripple.style.top = `${y - r.top}px`;
      ripple.dataset.dir = d;
      ripple.classList.remove('go'); void ripple.offsetWidth; ripple.classList.add('go');
    };
    const glow = $('pad-glow');
    // The light under the finger: 1:1 with the pointer, set straight on the
    // transform (no transition), so touch and light move together.
    const track = (ev) => {
      const r = p.getBoundingClientRect();
      glow.style.transform = `translate(${ev.clientX - r.left}px, ${ev.clientY - r.top}px)`;
    };
    const chev = (d) => {
      const c = p.querySelector(`.pad-chev.${d}`);
      if (!c) return;
      c.classList.remove('go'); void c.offsetWidth; c.classList.add('go');
    };
    const step = (d, x, y) => { flash(x, y, d.toLowerCase()); chev(d.toLowerCase()); send({ command: `DPAD_${d}` }); sent++; };
    const reset = () => { clearInterval(rep); clearTimeout(longT); rep = null; x0 = null; p.classList.remove('touching', 'holding'); };
    p.addEventListener('pointerdown', (ev) => {
      x0 = ev.clientX; y0 = ev.clientY; t0 = Date.now(); dir = null; sent = 0; longDone = false;
      p.setPointerCapture(ev.pointerId);
      track(ev);
      p.classList.add('touching');   // feedback on touch-down, not on release
      clearTimeout(longT);
      longT = setTimeout(() => {
        if (x0 == null || dir) return;
        longDone = true; p.classList.add('holding'); flash(ev.clientX, ev.clientY, 'ok'); send({ command: 'DPAD_CENTER', hold: true });
      }, 650);
    });
    p.addEventListener('pointermove', (ev) => {
      if (x0 == null) return;
      track(ev);
      if (longDone) return;
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      const dist = Math.hypot(dx, dy);
      if (dist < 18) return;
      const d = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'RIGHT' : 'LEFT') : (dy > 0 ? 'DOWN' : 'UP');
      if (dir && d !== dir) { x0 = ev.clientX; y0 = ev.clientY; dir = d; sent = 0; clearInterval(rep); rep = null; return; }
      dir = d;
      clearTimeout(longT);
      const want = 1 + Math.floor((dist - 18) / STEP);
      while (sent < Math.min(want, 8)) step(d, ev.clientX, ev.clientY);
      // Resting at the end of a swipe: keep going until the finger lifts.
      clearInterval(rep);
      rep = setInterval(() => step(d, ev.clientX, ev.clientY), 240);
    });
    p.addEventListener('pointerup', (ev) => {
      if (x0 == null) return;
      const quick = Date.now() - t0 < 600;
      const tapped = !dir && !longDone && quick;
      reset();
      if (tapped) { flash(ev.clientX, ev.clientY, 'ok'); send({ command: 'DPAD_CENTER' }); }
    });
    p.addEventListener('pointercancel', reset);
    p.addEventListener('contextmenu', (ev) => ev.preventDefault());
  })();

  // Rails: tap an end, or put a finger on the rail and slide — every 26px
  // is one step, so a long slide is a big volume change in one motion.
  document.querySelectorAll('#remote .rail').forEach((rail) => {
    const STEP = 26;
    let y0 = null, n = 0, moved = false;
    rail.addEventListener('pointerdown', (ev) => {
      if (ev.target.closest('.rail-end')) return;   // the ends are buttons
      y0 = ev.clientY; n = 0; moved = false;
      rail.setPointerCapture(ev.pointerId);
      rail.classList.add('dragging');
    });
    rail.addEventListener('pointermove', (ev) => {
      if (y0 == null) return;
      const want = Math.trunc((y0 - ev.clientY) / STEP);
      while (n < want) { send({ command: rail.dataset.up }); n++; moved = true; rail.style.setProperty('--nudge', '-3px'); }
      while (n > want) { send({ command: rail.dataset.down }); n--; moved = true; rail.style.setProperty('--nudge', '3px'); }
    });
    const end = () => { y0 = null; rail.classList.remove('dragging'); rail.style.setProperty('--nudge', '0px'); };
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((t) => rail.addEventListener(t, end));
  });

  // The remote from anywhere: a floating button on every space opens a sheet
  // and the one remote moves into it (and back on close).
  const rmHome = { parent: null, next: null };
  function openRemoteSheet() {
    const r = $('remote');
    if (!rmHome.parent) { rmHome.parent = r.parentNode; rmHome.next = r.nextSibling; }
    $('rm-slot').appendChild(r);
    const sh = $('rm-sheet');
    sh.hidden = false;
    sh.classList.add('opening');
    setTimeout(() => sh.classList.remove('opening'), 400);
    loadTvs();
  }
  function closeRemoteSheet() {
    $('rm-sheet').hidden = true;
    if (rmHome.parent) rmHome.parent.insertBefore($('remote'), rmHome.next);
  }
  $('rm-fab').addEventListener('click', openRemoteSheet);
  $('rm-close').addEventListener('click', closeRemoteSheet);
  $('rm-sheet').addEventListener('click', (ev) => { if (ev.target === $('rm-sheet')) closeRemoteSheet(); });
  window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !$('rm-sheet').hidden) closeRemoteSheet(); });
  // Shown when the remote can do something: controls unlocked, a TV exists,
  // and the Watch space (which has the remote inline) isn't the one on screen.
  function placeFab() {
    const onWatch = !!document.querySelector('[data-space="watch"][aria-current="true"]');
    $('rm-fab').hidden = !canControl || !tvs.length || onWatch || !$('rm-sheet').hidden;
  }
  new MutationObserver(placeFab).observe(document.querySelector('.tabs') || document.body, { attributes: true, subtree: true, attributeFilter: ['aria-current'] });
  setInterval(placeFab, 2000);

  // A hardware keyboard drives the TV while Watch is on screen: arrows,
  // Return = OK, Esc = Back, space = play/pause, +/− volume, m mute, h home.
  const KEYMAP = { ArrowUp: 'DPAD_UP', ArrowDown: 'DPAD_DOWN', ArrowLeft: 'DPAD_LEFT', ArrowRight: 'DPAD_RIGHT',
    Enter: 'DPAD_CENTER', Escape: 'BACK', ' ': 'MEDIA_PLAY_PAUSE', '=': 'VOLUME_UP', '+': 'VOLUME_UP',
    '-': 'VOLUME_DOWN', m: 'VOLUME_MUTE', h: 'HOME' };
  document.addEventListener('keydown', (ev) => {
    if (ev.metaKey || ev.ctrlKey || ev.altKey || !canControl || !selectedTv()) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test((ev.target && ev.target.tagName) || '')) return;
    const onWatch = document.querySelector('[data-space="watch"][aria-current="true"]') || openScene || !$('rm-sheet').hidden;
    const remote = $('remote');
    if (!onWatch || remote.hidden || !remote.offsetParent) return;
    if (/^[0-9]$/.test(ev.key) && selectedTv().box) { ev.preventDefault(); send({ command: ev.key, target: 'box' }); return; }
    const cmd = KEYMAP[ev.key];
    if (!cmd || (ev.key === 'Escape' && (openScene || !$('rm-sheet').hidden))) return;   // Esc closes the sheet first
    // Return/space on a focused button should still press that button.
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target && ev.target.closest && ev.target.closest('button, a') && ev.target !== $('pad')) return;
    ev.preventDefault(); ev.stopPropagation();
    send({ command: cmd });
  }, true);

  window.HBWhereRow = (apps, networks) => whereRow(apps, networks);

  // ═══════════════ What's on ═══════════════
  // Where something can be watched, in words + the one-tap action.
  // Network name -> HTC channel, learned from tonight's games and listings.
  const netCh = {};
  function learnChannels(t) {
    [...(t.live || []), ...(t.upcoming || []), ...(t.finals || []), ...(t.guide || [])].forEach((x) =>
      (x.cable || []).forEach((c) => { if (c.net) netCh[String(c.net).toLowerCase()] = c; }));
  }
  function cableFor(networks) {
    for (const n of networks || []) { const c = netCh[String(n || '').toLowerCase()]; if (c) return c; }
    return null;
  }
  function whereRow(apps, networks, { compact } = {}) {
    const row = el('div', 'where');
    const tv = selectedTv();
    // On HTC cable too: tune the box (the light kits follow the box's picture).
    const cab = cableFor(networks);
    if (cab && tv && tv.box && canControl) {
      const b = el('button', 'put cable');
      b.type = 'button';
      b.append(el('b', 'num', `Ch ${cab.ch}`), document.createTextNode(` ${cab.name} · ${tv.name.replace(/ TV$/, '')}`));
      b.addEventListener('click', () => { tune(cab.ch, b); });
      row.appendChild(b);
    }
    const playable = (apps || []).filter((k) => SVC[k]);
    if (playable.length) {
      playable.slice(0, compact ? 1 : 2).forEach((k) => {
        const s = SVC[k];
        const b = el('button', 'put');
        b.type = 'button';
        b.style.setProperty('--tint', s.tint);
        b.textContent = tv && canControl && s.tv ? `${s.name} on ${tv.name}` : `Open ${s.name}`;
        b.addEventListener('click', () => {
          if (tv && canControl && s.tv) launch(k, tv);
          else window.location.href = s.open;
        });
        row.appendChild(b);
      });
    } else if (networks && networks.length) {
      // Over-the-air networks are on the TVs' live channels; everything else
      // (regional sports nets, League Pass, FS1…) simply isn't in the owner's apps.
      const ota = networks.find((n) => OTA.test(n));
      if (cab && row.childElementCount) return row;   // the tune button already says where
      row.appendChild(el('span', 'where-none', cab ? `${cab.name} — HTC channel ${cab.ch}` : ota ? `${ota} — on the TV’s live channels` : `${networks[0]} — not in your apps`));
    }
    return row;
  }

  // A team's logo, or an empty slot of the same size so rows stay aligned.
  function logoImg(src, cls) {
    if (!src || !/^https:\/\//.test(src)) return el('span', cls + ' none');
    const i = el('img', cls); i.src = src; i.alt = ''; i.loading = 'lazy'; i.decoding = 'async';
    i.addEventListener('error', () => i.classList.add('none'));
    return i;
  }
  function teamLine(t, winPct) {
    const line = el('div', 'bug-team');
    line.style.setProperty('--tc', hex(t.color));
    const sw = el('i', 'bug-sw');
    const abbr = el('b', 'bug-abbr', t.abbr || '?');
    const rec = el('span', 'bug-rec', t.record || '');
    const score = el('span', 'bug-score num', t.score != null ? String(t.score) : '');
    line.append(sw, logoImg(t.logo, 'bug-logo'), abbr, rec, score);
    if (winPct != null) line.dataset.win = String(winPct);
    return line;
  }

  // The arcade's sports bug, on glass: team colours, score, clock, and win %
  // only when ESPN actually publishes it.
  function bug(g) {
    const card = el('article', 'bug' + (g.close ? ' hot' : ''));
    card.dataset.game = g.id;
    // The whole bug opens Game Center; its put-on-TV buttons still do their own thing.
    card.addEventListener('click', (ev) => { if (!ev.target.closest('button, a') && window.HBOpenGame) window.HBOpenGame(g.league, g.id, g); });
    const [away, home] = g.teams;
    const homeWin = g.win && g.win.home != null ? g.win.home : null;
    card.append(teamLine(away, homeWin != null ? 100 - homeWin : null), teamLine(home, homeWin));
    const meta = el('div', 'bug-meta');
    meta.append(el('span', 'bug-clock', g.detail || ''), el('span', 'bug-net', (g.networks || [])[0] || g.league));
    card.appendChild(meta);
    if (g.situation) card.appendChild(el('div', 'bug-sit', g.situation));
    if (homeWin != null) {
      const wp = el('div', 'bug-wp');
      const bar = el('i');
      bar.style.width = `${homeWin}%`;
      bar.style.background = hex(home.color);
      wp.style.background = hex(away.color);
      wp.appendChild(bar);
      const lead = homeWin >= 50 ? home : away;
      card.append(wp, el('div', 'bug-wpl', `${lead.abbr} ${Math.max(homeWin, 100 - homeWin)}% to win`));
    }
    if (g.close) card.prepend(el('span', 'bug-flag', g.close));
    if (g.fav) { card.classList.add('mine'); card.prepend(el('span', 'bug-flag mine', 'Your team')); }
    card.appendChild(whereRow(g.apps, g.networks, { compact: true }));
    return card;
  }

  function renderPick() {
    const box = $('pick');
    const p = tonight && tonight.pick;
    box.hidden = !p;
    box.replaceChildren();
    if (!p) return;
    const g = p.kind === 'game' ? [...tonight.live, ...tonight.upcoming].find((x) => x.id === p.game_id) : null;
    if (g && g.state === 'in') { box.dataset.game = g.id; box.onclick = (ev) => { if (!ev.target.closest('button, a') && window.HBOpenGame) window.HBOpenGame(g.league, g.id, g); }; }
    else { delete box.dataset.game; box.onclick = null; }
    if (g && g.teams.length === 2) box.style.setProperty('--pa', hex(g.teams[0].color)), box.style.setProperty('--pb', hex(g.teams[1].color));
    else box.style.removeProperty('--pa'), box.style.removeProperty('--pb');
    const when = p.state === 'in' ? 'Live now' : p.start ? `Tonight · ${timeOf(p.start)}` : 'Tonight';
    box.append(el('span', 'hb-label', `Tonight’s pick · ${when}`), el('h2', 'pick-title', p.title), el('p', 'pick-why', p.reason));
    if (g && g.teams.length === 2) {
      const vs = el('div', 'pick-vs');
      g.teams.forEach((t, i) => {
        const side = el('span', 'pick-team');
        if (t.logo && /^https:\/\//.test(t.logo)) { const img = el('img'); img.src = t.logo; img.alt = ''; img.loading = 'lazy'; side.appendChild(img); }
        side.append(el('b', null, t.name || t.abbr), el('small', null, [t.record, g.series && i === 1 ? g.series : null].filter(Boolean).join(' · ')));
        vs.appendChild(side);
        if (i === 0) vs.appendChild(el('span', 'pick-at', '@'));
      });
      box.appendChild(vs);
    }
    box.append(whereRow(p.apps, p.networks), el('p', 'pick-msg', ''));
    box.lastChild.id = 'pick-msg';
  }

  // Two big things at once and two TVs: put one on each.
  function renderSplit() {
    const box = $('split');
    box.replaceChildren();
    box.hidden = true;
    if (!tonight || tvs.filter((t) => t.available).length < 2) return;
    const big = [...tonight.live, ...tonight.upcoming].filter((g) => g.score >= 50).slice(0, 4);
    let pair = null;
    for (let i = 0; i < big.length && !pair; i++) for (let j = i + 1; j < big.length && !pair; j++) {
      const a = Date.parse(big[i].start), b = Date.parse(big[j].start);
      if (Math.abs(a - b) <= 90 * 60000) pair = [big[i], big[j]];
    }
    if (!pair) return;
    // The game an app can play goes to the TV that's already on; the other gets the second TV.
    pair.sort((x, y) => (y.apps.length ? 1 : 0) - (x.apps.length ? 1 : 0));
    const order = [...tvs].filter((t) => t.available).sort((x, y) => (y.on ? 1 : 0) - (x.on ? 1 : 0));
    box.hidden = false;
    box.appendChild(el('span', 'hb-label', 'Split the night'));
    box.appendChild(el('p', 'muted', 'Two big ones overlap — one on each TV.'));
    pair.forEach((g, i) => {
      const tv = order[i];
      const row = el('div', 'split-row');
      const label = g.note || `${g.teams[0].abbr} @ ${g.teams[1].abbr}`;
      row.append(el('b', null, label), el('span', 'faint', `${timeOf(g.start)} · ${(g.networks || [])[0] || ''} → ${tv.name}`));
      const s = SVC[(g.apps || [])[0]];
      if (s && s.tv && canControl) {
        const b = el('button', 'put');
        b.type = 'button'; b.style.setProperty('--tint', s.tint);
        b.textContent = `${s.name} on ${tv.name}`;
        b.addEventListener('click', () => launch(s.key, tv));
        row.appendChild(b);
      } else if (!s) {
        row.appendChild(el('span', 'where-none', 'live channels'));
      }
      box.appendChild(row);
    });
  }

  function renderLive() {
    const live = (tonight && tonight.live) || [];
    $('live-sec').hidden = !live.length;
    $('bugs').replaceChildren(...live.map(bug));
  }

  // One timeline for the evening: games and shows by start time, then the
  // day's streaming drops on services the owner has.
  function renderGuide() {
    const list = $('guide');
    if (!tonight) return;
    list.replaceChildren();
    const rows = [];
    // Watchable games lead: national, postseason, in your apps, or on the
    // TV's live channels. The rest (regional nets, League Pass) fold into one
    // line you can open.
    const watchable = (g) => g.fav || g.national || g.postseason || g.apps.length || g.networks.some((n) => OTA.test(n));
    const hiddenGames = tonight.upcoming.filter((g) => !watchable(g));
    tonight.upcoming.filter((g) => showAllGames || watchable(g))
      .forEach((g) => rows.push({ t: Date.parse(g.start), when: timeOf(g.start), g }));
    const day = tonight.date;
    tonight.guide.filter((i) => i.time).forEach((i) => {
      const t = Date.parse(`${day}T${i.time}:00`);   // TVmaze airtime is the network's local (ET) time; the iPad is in ET
      rows.push({ t, when: hhmm(i.time), i });
    });
    rows.sort((a, b) => a.t - b.t);
    if (!rows.length) list.appendChild(el('li', 'muted', 'Nothing else scheduled tonight.'));
    let lastHour = null;
    rows.forEach((r) => {
      const hour = new Date(r.t).getHours();
      if (hour !== lastHour) {
        list.appendChild(el('li', 'guide-hour', new Date(r.t).toLocaleTimeString(undefined, { hour: 'numeric' })));
        lastHour = hour;
      }
      const li = el('li', 'guide-row' + (r.g && r.g.score >= 50 ? ' big' : ''));
      const main = el('div', 'guide-main');
      if (r.g) {
        const g = r.g;
        const teams = el('div', 'guide-teams');
        g.teams.forEach((t, k) => {
          const chip = el('span', 'tchip', t.abbr);
          chip.style.setProperty('--tc', hex(t.color));
          teams.appendChild(chip);
          if (k === 0) teams.appendChild(el('span', 'faint', '@'));
        });
        if (g.fav) teams.appendChild(el('span', 'mine-tag', 'Your team'));
        main.append(teams, el('div', 'guide-sub', [g.note, g.series, g.league].filter(Boolean).join(' · ')));
      } else {
        main.append(el('div', 'guide-title', r.i.title), el('div', 'guide-sub', r.i.episode || ''));
      }
      const nets = r.g ? r.g.networks : [r.i.network];
      li.append(el('span', 'guide-when num', r.when), main, el('span', 'net', (nets || [])[0] || ''), whereRow(r.g ? r.g.apps : r.i.apps, nets, { compact: true }));
      list.appendChild(li);
    });
    if (hiddenGames.length) {
      const leagues = [...new Set(hiddenGames.map((g) => g.league))].join(', ');
      const more = el('li', 'guide-more', showAllGames
        ? `Showing ${hiddenGames.length} regional / League Pass games too.`
        : `+${hiddenGames.length} more ${leagues} on regional nets or League Pass — not in your apps.`);
      const b = el('button', null, showAllGames ? 'Hide' : 'Show');
      b.type = 'button';
      b.addEventListener('click', () => { showAllGames = !showAllGames; renderGuide(); });
      more.appendChild(b);
      list.appendChild(more);
    }
    const drops = tonight.guide.filter((i) => !i.time);
    if (drops.length) {
      list.appendChild(el('li', 'guide-hour', 'New today on your apps'));
      drops.slice(0, 6).forEach((i) => {
        const li = el('li', 'guide-row');
        const main = el('div', 'guide-main');
        main.append(el('div', 'guide-title', i.title), el('div', 'guide-sub', i.episode || ''));
        li.append(el('span', 'guide-when'), main, el('span', 'net', i.network), whereRow(i.apps, [i.network], { compact: true }));
        list.appendChild(li);
      });
    }
    $('tonight-date').textContent = new Date(`${day}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
    const alerts = tonight.alerts && tonight.alerts.enabled;
    $('watch-credit').textContent = [alerts ? 'Close-game alerts on — you’ll get a push with buttons to put it on a TV' : 'Close-game alerts off',
      tonight.credit].filter(Boolean).join(' · ');
    renderPick(); renderSplit(); renderLive(); renderScene();
    const live = (tonight.live || []).length;
    const sub = $('scene-sports-sub');
    if (sub) sub.textContent = live ? `${live} live now` : (tonight.upcoming || []).length ? `Next ${timeOf(tonight.upcoming[0].start)}` : 'Live games, tonight, TVs';
  }

  function renderApps() {
    const wrap = $('apps');
    wrap.replaceChildren();
    (window.HBStreaming || []).forEach((s) => {
      const b = el('button', 'app');
      b.type = 'button';
      b.style.setProperty('--tint', s.tint);
      b.textContent = s.name;
      if (s.free) b.appendChild(el('span', 'app-free', 'Free'));
      b.addEventListener('click', () => {
        const tv = selectedTv();
        if (tv && canControl && s.tv) launch(s.key, tv);
        else window.location.href = s.open;
      });
      wrap.appendChild(b);
    });
  }

  async function loadTonight() {
    try { tonight = await (await fetch('/api/watch/tonight', { credentials: 'same-origin' })).json(); learnChannels(tonight); }
    catch (e) { /* keep last */ }
    renderGuide();
    reschedule();
    { const tv = selectedTv(); if (tv && !$('remote').hidden) renderRemote(tv); }   // live scores on the cable deck
    // Let the rest of the hub (Now's On-TV card, context opening) know.
    if (tonight) { window.HBLastTonight = tonight; window.dispatchEvent(new CustomEvent('hb:tonight', { detail: tonight })); }
  }

  // ═══════════════ Your teams ═══════════════
  let favs = null;
  function shortDay(iso) {
    const d = new Date(iso);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    const dl = diff === 0 ? 'Today' : diff === 1 ? 'Tomorrow' : d.toLocaleDateString(undefined, { weekday: 'short' });
    return `${dl} ${timeOf(iso)}`;
  }
  function favCard({ logo, color, title, sub, lines, live, apps, networks, via }) {
    const c = el('article', 'fav' + (live ? ' live' : ''));
    c.style.setProperty('--tc', hex(color));
    if (logo && /^https:\/\//.test(logo)) { const wm = el('img', 'fav-wm'); wm.src = logo; wm.alt = ''; wm.loading = 'lazy'; c.appendChild(wm); }
    const head = el('div', 'fav-head');
    if (logo && /^https:\/\//.test(logo)) { const i = el('img'); i.src = logo; i.alt = ''; i.loading = 'lazy'; head.appendChild(i); }
    const names = el('div');
    names.append(el('b', 'fav-title', title), el('span', 'fav-sub', sub || ''));
    head.appendChild(names);
    if (live) head.appendChild(el('span', 'fav-live', 'LIVE'));
    c.appendChild(head);
    (lines || []).filter(Boolean).forEach((l) => c.appendChild(el('span', 'fav-line', l)));
    if (via) c.appendChild(el('span', 'fav-via', via));
    if ((apps && apps.length) || (networks && networks.length)) c.appendChild(whereRow(apps, networks, { compact: true }));
    return c;
  }
  function favCards() {
    if (!favs) return [];
    const out = [];
    (favs.teams || []).forEach((t) => {
      const n = t.next, l = t.last;
      const lines = [];
      if (n && n.live) lines.push(`Live: ${t.abbr} ${n.us ?? ''}–${n.them ?? ''} ${n.opponent}`);
      else if (n) lines.push(`Next: ${shortDay(n.start)} · ${n.home ? 'vs' : '@'} ${n.opponent}`);
      if (l) lines.push(`Last: ${l.won ? 'W' : 'L'} ${l.us}–${l.them} ${l.home ? 'vs' : '@'} ${l.opponent}`);
      if (t.season_over) lines.push('Season’s over — back next season.');
      out.push(favCard({ logo: t.logo, color: t.color, title: t.short || t.name,
        sub: t.season_over ? t.league : [t.record, t.standing].filter(Boolean).join(' · '),
        lines, live: n && n.live, apps: n ? n.apps : [], networks: n ? n.networks : [] }));
    });
    const r = favs.nascar;
    if (r) out.push(favCard({ color: 'f5c400', title: 'NASCAR', sub: r.title, live: r.live,
      lines: [r.live ? 'Racing now' : r.start ? `${shortDay(r.start)}` : null], apps: r.apps, networks: r.networks }));
    const f = favs.f1;
    if (f) {
      const st = f.standing;
      out.push(favCard({ color: '1e41ff', title: `F1 · ${f.driver}`,
        sub: st ? `P${st.position} · ${st.points} pts${st.behind ? ` · ${st.behind} behind ${st.leader}` : ''}` : 'Standings unavailable',
        live: f.live, lines: [f.title ? `${f.title}${f.start ? ` · ${shortDay(f.start)}` : ''}` : null], apps: f.apps, networks: f.networks }));
    }
    const g = favs.golf;
    if (g) {
      const pos = g.golfer_pos;
      out.push(favCard({ color: '0a7f3f', title: 'Golf', sub: g.title, live: g.live,
        lines: [g.live ? (pos ? `${g.golfer}: ${pos.order === 1 ? 'leading' : `T${pos.order}`} (${pos.score})` : `${g.golfer} isn’t in this one`) : (g.start ? `Starts ${shortDay(g.start).replace(/ \d.*$/, '')}` : null),   // tournament starts are dates, not tee times
          g.live && g.leader && (!pos || pos.order !== 1) ? `Leader: ${g.leader.name} ${g.leader.score}` : null],
        via: g.via, apps: g.apps, networks: g.networks }));
    }
    // Fight cards: the next UFC event (DWCS included) and the next big boxing card.
    const fight = (e, title, color, dateOnly) => {
      if (!e) return;
      const [a, b] = e.teams || [];
      out.push(favCard({ color, title, sub: e.note, live: e.state === 'in',
        lines: [a && b ? `${a.name} vs ${b.name}${e.series && e.league === 'UFC' ? ` · ${e.series}` : ''}` : null,
          e.state === 'in' ? 'On now' : e.start ? (dateOnly ? shortDay(e.start).replace(/ \d.*$/, '') : `Main card ${shortDay(e.start)}`) : null],
        apps: e.apps, networks: e.networks }));
    };
    fight(favs.ufc, 'UFC', 'd20a0a', false);
    fight(favs.boxing, 'Boxing', 'c9a227', true);   // the boxing source's fight times aren't reliable: date only
    return out;
  }
  function renderFavs() {
    const cards = favCards();
    $('fav-sec').hidden = !cards.length;
    $('fav-row').replaceChildren(...cards);
    if (openScene === 'sports') $('sc-favs').replaceChildren(...favCards());
  }
  async function loadFavs() {
    try { favs = await (await fetch('/api/watch/favorites')).json(); } catch (e) { return; }
    window.HBFavs = favs;
    renderFavs();
    window.dispatchEvent(new CustomEvent('hb:favs', { detail: favs }));
  }

  // ═══════════════ New on your apps ═══════════════
  async function loadNew() {
    let d = null;
    try { d = await (await fetch('/api/watch/new')).json(); } catch (e) { return; }
    const items = (d && d.items) || [];
    $('new-sec').hidden = !items.length;
    const row = $('new-row');
    row.replaceChildren();
    const today = new Date(); today.setHours(0, 0, 0, 0);
    items.forEach((i) => {
      const s = SVC[i.app];
      const card = el('button', 'poster');
      card.type = 'button';
      if (s) card.style.setProperty('--tint', s.tint);
      const art = el('span', 'poster-art');
      if (i.image && /^https:\/\//.test(i.image)) art.style.backgroundImage = `url("${i.image.replace(/"/g, '')}")`;
      const day = new Date(`${i.date}T12:00:00`);
      const diff = Math.round((new Date(`${i.date}T00:00:00`) - today) / 86400000);
      const when = diff <= 0 ? 'Out now' : diff === 1 ? 'Tomorrow' : day.toLocaleDateString(undefined, { weekday: 'short' });
      art.appendChild(el('span', 'poster-when' + (diff <= 0 ? ' now' : ''), when));
      card.append(art, el('b', 'poster-title', i.title), el('span', 'poster-sub', `${i.kind} · ${s ? s.name : i.network}`));
      card.addEventListener('click', () => {
        const tv = selectedTv();
        if (s && tv && canControl && s.tv) launch(s.key, tv); else if (s) window.location.href = s.open;
      });
      row.appendChild(card);
    });
  }

  // ═══════════════ Find anything ═══════════════
  function dayWhen(iso) {
    const d = new Date(iso);
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const day = new Date(d); day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    const dl = diff === 0 ? 'Tonight' : diff === 1 ? 'Tomorrow' : d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    return `${dl} ${timeOf(iso)}`;
  }
  function resultCard(img, title, sub, extra, actions) {
    const card = el('article', 'find-card');
    const art = el('div', 'find-art');
    if (img && /^https:\/\//.test(img)) art.style.backgroundImage = `url("${img.replace(/"/g, '')}")`;
    const body = el('div', 'find-body');
    body.append(el('b', 'find-title', title), el('span', 'find-sub', sub));
    if (extra) body.appendChild(el('span', 'find-extra', extra));
    body.appendChild(actions);
    card.append(art, body);
    return card;
  }
  function renderFind(d) {
    const box = $('find-results');
    box.replaceChildren();
    if (!d || !d.q) { box.hidden = true; return; }
    box.hidden = false;
    (d.teams || []).forEach((t) => {
      const n = t.next;
      const extra = !n ? 'No upcoming games — the season’s over for now.'
        : `${n.state === 'in' ? 'Live now' : dayWhen(n.start)} · ${n.home ? 'vs' : '@'} ${n.opponent}${n.networks.length ? ` · ${n.networks[0]}` : ''}`;
      box.appendChild(resultCard(t.logo, t.name, `${t.league} team`, extra, n ? whereRow(n.apps, n.networks, { compact: true }) : el('span')));
    });
    (d.shows || []).forEach((s) => {
      const sub = [s.network, s.year, s.status === 'Running' ? 'airing' : s.status === 'Ended' ? 'ended' : s.status].filter(Boolean).join(' · ');
      const nx = s.next ? `Next: ${dayWhen(s.next.airstamp)}${s.next.season ? ` · S${s.next.season}E${s.next.number}` : ''}` : null;
      const extra = [nx, s.later ? (s.apps.length ? `${s.later} in ${(SVC[s.apps[0]] || {}).name || s.apps[0]}` : s.later) : null].filter(Boolean).join(' · ');
      box.appendChild(resultCard(s.image, s.title, sub, extra || null, s.apps.length || !s.later ? whereRow(s.apps, s.network ? [s.network] : [], { compact: true }) : el('span')));
    });
    if (!box.childElementCount) box.appendChild(el('p', 'muted', `Nothing found for “${d.q}”.`));
    if (d.note) box.appendChild(el('p', 'faint', d.note));
  }
  (function findBox() {
    const input = $('find-q');
    let t = null, seq = 0;
    input.addEventListener('input', () => {
      clearTimeout(t);
      const q = input.value.trim();
      if (q.length < 2) { renderFind(null); return; }
      t = setTimeout(async () => {
        const my = ++seq;
        $('find-results').hidden = false;
        $('find-results').replaceChildren(el('p', 'muted', 'Looking…'));
        try {
          const d = await (await fetch('/api/watch/find?q=' + encodeURIComponent(q))).json();
          if (my === seq) renderFind(d);
        } catch (e) { if (my === seq) renderFind({ q, teams: [], shows: [] }); }
      }, 350);
    });
    input.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { input.value = ''; renderFind(null); input.blur(); } });
  })();

  // ═══════════════ Scenes ═══════════════
  // A scene is one button that opens everything for an activity. Sports
  // gathers live bugs, tonight's games, the sports apps, and the TVs + the
  // real remote (moved in while open, moved back on close — one remote).
  const SCENE_APPS = { sports: ['espn', 'disney', 'prime', 'peacock', 'paramount', 'max', 'youtube', 'pluto', 'tubi'] };
  let openScene = null;
  const remoteHome = { parent: null, next: null };

  function sceneTvs() {
    const wrap = $('sc-tvs');
    wrap.replaceChildren();
    tvs.forEach((t) => {
      const b = el('button', 'tv-chip');
      b.type = 'button';
      b.setAttribute('aria-pressed', String(t.entity_id === selected));
      b.append(el('span', 'hb-dot ' + (t.on ? 'hb-dot-live' : 'hb-dot-off')), el('b', null, t.name),
        el('span', 'faint', !t.available ? 'unreachable' : t.on ? (tvApp(t.app) || 'on') : 'off'));
      b.addEventListener('click', () => {
        selected = t.entity_id;
        try { localStorage.setItem('hb-watch-tv', selected); } catch (e) {}
        renderTvs(); renderGuide();
      });
      wrap.appendChild(b);
    });
  }

  function renderScene() {
    if (openScene !== 'sports' || !tonight) return;
    const watchable = (g) => g.fav || g.national || g.postseason || g.apps.length || g.networks.some((n) => OTA.test(n));
    const live = (tonight.live || []).filter(watchable);
    $('sc-favs').replaceChildren(...favCards());
    $('sc-live-sec').hidden = !live.length;
    $('sc-live-count').textContent = live.length ? `${live.length} game${live.length === 1 ? '' : 's'}` : '';
    $('sc-bugs').replaceChildren(...live.map(bug));
    const list = $('sc-games');
    list.replaceChildren();
    const ups = (tonight.upcoming || []).filter(watchable);
    if (!ups.length) list.appendChild(el('li', 'muted', live.length ? 'That’s everything tonight.' : 'No more watchable games tonight.'));
    ups.forEach((g) => {
      const li = el('li', 'guide-row' + (g.score >= 50 ? ' big' : ''));
      const main = el('div', 'guide-main');
      const teams = el('div', 'guide-teams');
      g.teams.forEach((t, k) => {
        const chip = el('span', 'tchip', t.abbr);
        chip.style.setProperty('--tc', hex(t.color));
        teams.appendChild(chip);
        if (k === 0) teams.appendChild(el('span', 'faint', '@'));
      });
      main.append(teams, el('div', 'guide-sub', [g.note, g.series, g.league].filter(Boolean).join(' · ')));
      li.append(el('span', 'guide-when num', timeOf(g.start)), main, el('span', 'net', (g.networks || [])[0] || ''), whereRow(g.apps, g.networks, { compact: true }));
      list.appendChild(li);
    });
    const apps = $('sc-apps');
    apps.replaceChildren();
    SCENE_APPS.sports.map((k) => SVC[k]).filter(Boolean).forEach((s) => {
      const b = el('button', 'app');
      b.type = 'button';
      b.style.setProperty('--tint', s.tint);
      b.textContent = s.name;
      if (s.free) b.appendChild(el('span', 'app-free', 'Free'));
      b.addEventListener('click', () => {
        const tv = selectedTv();
        if (tv && canControl && s.tv) launch(s.key, tv); else window.location.href = s.open;
      });
      apps.appendChild(b);
    });
    sceneTvs();
    $('sc-alerts').textContent = tonight.alerts && tonight.alerts.enabled
      ? 'Close-game alerts are on: a push with TV buttons when a game gets close and late.'
      : 'Close-game alerts are off.';
  }

  function showScene(name) {
    openScene = name;
    const remote = $('remote');
    if (!remoteHome.parent) { remoteHome.parent = remote.parentNode; remoteHome.next = remote.nextSibling; }
    $('sc-remote-slot').appendChild(remote);
    $('scene-title').textContent = 'Sports';
    $('scene').hidden = false;
    document.body.classList.add('scene-open');
    renderScene();
    $('scene-close').focus();
  }
  function hideScene() {
    if (!openScene) return;
    openScene = null;
    $('scene').hidden = true;
    document.body.classList.remove('scene-open');
    if (remoteHome.parent) remoteHome.parent.insertBefore($('remote'), remoteHome.next);
  }
  document.querySelectorAll('[data-scene]').forEach((b) => b.addEventListener('click', () => showScene(b.dataset.scene)));
  $('scene-close').addEventListener('click', hideScene);
  $('scene').addEventListener('click', (ev) => { if (ev.target === $('scene')) hideScene(); });
  window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hideScene(); });

  // ═══════════════ Polling ═══════════════
  let tvTimer = null, tonightTimer = null;
  function reschedule() {
    clearTimeout(tonightTimer);
    if (document.visibilityState !== 'visible') return;
    const live = tonight && tonight.live && tonight.live.length;
    tonightTimer = setTimeout(loadTonight, live ? 30000 : 5 * 60000);   // live scores move; the guide doesn't
  }
  function start() {
    stop();
    loadTvs(); loadTonight();
    tvTimer = setInterval(loadTvs, 10000);
  }
  function stop() { clearInterval(tvTimer); clearTimeout(tonightTimer); }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { loadAuth(); start(); } else stop();
  });

  renderApps();
  loadAuth();
  loadNew();
  loadFavs();
  setInterval(() => { if (document.visibilityState === 'visible') loadFavs(); }, 5 * 60 * 1000);
  setInterval(() => { if (document.visibilityState === 'visible') loadNew(); }, 6 * 3600 * 1000);
  if (document.visibilityState === 'visible') start();
})();
