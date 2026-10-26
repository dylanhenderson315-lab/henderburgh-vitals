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
    const tv = selectedTv();
    $('remote').hidden = !canControl || !tv;
    $('watch-lock').hidden = canControl;
    if (tv) $('remote-for').textContent = tv.name;
  }

  async function loadTvs() {
    try { tvs = (await (await fetch('/api/media/tvs', { credentials: 'same-origin' })).json()).tvs || []; }
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
  async function send(body) {
    const tv = selectedTv();
    if (!tv) return;
    try { await post('/api/media/command', { tv: tv.entity_id, ...body }); status(''); }
    catch (e) { status(e.message, 'err'); }
  }
  document.querySelectorAll('#remote [data-cmd]').forEach((b) => b.addEventListener('click', () => send({ command: b.dataset.cmd })));
  document.querySelectorAll('#remote [data-power]').forEach((b) => b.addEventListener('click', async () => {
    await send({ power: b.dataset.power === 'on' });
    setTimeout(loadTvs, 2000);
  }));
  // Swipe pad: a swipe is one D-pad step in its direction, a tap is OK. The
  // pad opts out of the space swipe (touch-action:none).
  (function pad() {
    const p = $('pad');
    const ripple = $('pad-ripple');
    let x0 = null, y0 = null, t0 = 0;
    const flash = (x, y, dir) => {
      const r = p.getBoundingClientRect();
      ripple.style.left = `${x - r.left}px`; ripple.style.top = `${y - r.top}px`;
      ripple.dataset.dir = dir;
      ripple.classList.remove('go'); void ripple.offsetWidth; ripple.classList.add('go');
    };
    p.addEventListener('pointerdown', (ev) => { x0 = ev.clientX; y0 = ev.clientY; t0 = Date.now(); p.setPointerCapture(ev.pointerId); });
    p.addEventListener('pointerup', (ev) => {
      if (x0 == null) return;
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      x0 = null;
      const dist = Math.hypot(dx, dy);
      if (dist < 18 && Date.now() - t0 < 600) { flash(ev.clientX, ev.clientY, 'ok'); send({ command: 'DPAD_CENTER' }); return; }
      if (dist < 18) return;
      const dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'RIGHT' : 'LEFT') : (dy > 0 ? 'DOWN' : 'UP');
      flash(ev.clientX, ev.clientY, dir.toLowerCase());
      send({ command: `DPAD_${dir}` });
    });
    p.addEventListener('keydown', (ev) => {
      const map = { ArrowUp: 'DPAD_UP', ArrowDown: 'DPAD_DOWN', ArrowLeft: 'DPAD_LEFT', ArrowRight: 'DPAD_RIGHT', Enter: 'DPAD_CENTER', ' ': 'DPAD_CENTER' };
      if (map[ev.key]) { ev.preventDefault(); ev.stopPropagation(); send({ command: map[ev.key] }); }
    });
  })();

  // ═══════════════ What's on ═══════════════
  // Where something can be watched, in words + the one-tap action.
  function whereRow(apps, networks, { compact } = {}) {
    const row = el('div', 'where');
    const tv = selectedTv();
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
      row.appendChild(el('span', 'where-none', ota ? `${ota} — on the TV’s live channels` : `${networks[0]} — not in your apps`));
    }
    return row;
  }

  function teamLine(t, winPct) {
    const line = el('div', 'bug-team');
    line.style.setProperty('--tc', hex(t.color));
    const sw = el('i', 'bug-sw');
    const abbr = el('b', 'bug-abbr', t.abbr || '?');
    const rec = el('span', 'bug-rec', t.record || '');
    const score = el('span', 'bug-score num', t.score != null ? String(t.score) : '');
    line.append(sw, abbr, rec, score);
    if (winPct != null) line.dataset.win = String(winPct);
    return line;
  }

  // The arcade's sports bug, on glass: team colours, score, clock, and win %
  // only when ESPN actually publishes it.
  function bug(g) {
    const card = el('article', 'bug' + (g.close ? ' hot' : ''));
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
    const watchable = (g) => g.national || g.postseason || g.apps.length || g.networks.some((n) => OTA.test(n));
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
    renderPick(); renderSplit(); renderLive();
  }

  function renderApps() {
    const wrap = $('apps');
    wrap.replaceChildren();
    (window.HBStreaming || []).filter((s) => s.key !== 'applemusic').forEach((s) => {
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
    try { tonight = await (await fetch('/api/watch/tonight', { credentials: 'same-origin' })).json(); }
    catch (e) { /* keep last */ }
    renderGuide();
    reschedule();
    // Let the rest of the hub (Now's On-TV card, context opening) know.
    if (tonight) { window.HBLastTonight = tonight; window.dispatchEvent(new CustomEvent('hb:tonight', { detail: tonight })); }
  }

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
  if (document.visibilityState === 'visible') start();
})();
