/* The channel guide — Watch's right side.

   One grid, read the way every cable guide is read: channels down the left
   in HTC number order (pinned while you scroll sideways), time across the
   top in half hours (pinned while you scroll down), a line at now. Each
   programme is a block as long as it runs.

   Tap a block: the selected TV's HTC box changes to that channel. If the
   programme is also in one of the owner's apps, its badge opens it there
   instead (through the box, so the light kits follow). Tap a channel's
   number to tune it with nothing selected. The row the TV was last tuned
   to is marked.

   Rules: textContent only; refresh only while visible; never invent a
   programme (an empty row says so and still tunes). Data: /api/watch/grid. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const SVC = Object.fromEntries((window.HBStreaming || []).map((s) => [s.key, s]));
  const hex = (c) => (c && /^[0-9a-f]{6}$/i.test(c) ? `#${c}` : null);
  const PX = 4.2;                 // pixels per minute: a half hour is 126px
  const CATS = ['All', 'Sports', 'News', 'Local', 'Shows', 'Movies', 'Kids', 'Classic & more'];
  let data = null;
  let cat = 'All';
  try { cat = localStorage.getItem('hb-cg-cat') || 'All'; } catch (e) {}
  let placedOnce = false;
  let listedOnly = false;
  try { listedOnly = localStorage.getItem('hb-cg-listed') === '1'; } catch (e) {}

  const t = (iso) => Date.parse(iso);
  const clock = (ms) => new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const half = (ms) => new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }).replace(':00', '');

  function remote() { return window.HBRemote ? window.HBRemote() : { canControl: false, tv: null }; }
  function lastCh(tv) { try { return tv ? Number(localStorage.getItem(`hb-ch:${tv.entity_id}`)) : null; } catch (e) { return null; } }

  function header() {
    const { canControl, tv } = remote();
    const f = $('cg-for');
    f.className = 'cg-for' + (canControl && tv && tv.box ? ' on' : '');
    f.textContent = !canControl ? 'Unlock TV control to tune from here'
      : !tv ? '' : tv.box ? `Tap a show — ${tv.name} changes channel` : `${tv.name} has no cable box`;
  }

  function filters() {
    const wrap = $('cg-filters');
    wrap.replaceChildren();
    const have = new Set((data.channels || []).map((c) => c.cat));
    CATS.filter((c) => c === 'All' || have.has(c)).forEach((c) => {
      const b = el('button', null, c);
      b.type = 'button'; b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(c === cat));
      b.addEventListener('click', () => {
        cat = c;
        try { localStorage.setItem('hb-cg-cat', cat); } catch (e) {}
        render();
      });
      wrap.appendChild(b);
    });
  }

  function tuneTo(ch, from, title) {
    const { canControl, tv } = remote();
    if (!canControl || !tv || !tv.box) {
      flash(from, !canControl ? 'TV control is locked on this device' : `${tv ? tv.name : 'This TV'} has no cable box — HTC ch ${ch}`);
      return;
    }
    window.HBTune(ch, from);
    flash(from, `${tv.name} → ${ch}${title ? ` · ${title}` : ''}`, true);
    mark(ch);
  }
  // ═══════════════ Split View (TiVo MultiView) ═══════════════
  // With Split View on, tapping a show adds its channel to a 2×2 plan
  // instead of tuning; Start sets it up on the TV's HTC box (the server
  // drives MultiView — /api/media/multiview). Plans are kept per TV.
  const POS = ['Top left', 'Top right', 'Bottom left', 'Bottom right'];
  let split = false;
  let plan = { chans: [], audio: 0 };     // chans: [{ch, name, title}]
  let job = null, jobTimer = null;
  const planKey = (tv) => `hb-split:${tv ? tv.entity_id : ''}`;
  function loadPlan() {
    const { tv } = remote();
    try { const p = JSON.parse(localStorage.getItem(planKey(tv)) || 'null'); if (p && Array.isArray(p.chans)) return p; } catch (e) {}
    return { chans: [], audio: 0 };
  }
  function savePlan() { const { tv } = remote(); try { localStorage.setItem(planKey(tv), JSON.stringify(plan)); } catch (e) {} }
  function nowOn(ch) {
    const row = data && (data.channels || []).find((c) => c.ch === ch);
    const n = Date.now();
    const p = row && row.programs.find((x) => t(x.start) <= n && t(x.end) > n);
    return p ? (p.kind === 'game' && p.live && p.teams ? `${p.teams[0].abbr} ${p.teams[0].score ?? ''}–${p.teams[1].score ?? ''} ${p.teams[1].abbr}` : p.title) : '';
  }
  function pick(ch, from, title, row) {
    if (!split) { tuneTo(ch, from, title || (row && row.name)); return; }
    const i = plan.chans.findIndex((x) => x.ch === ch);
    if (i >= 0) plan.chans.splice(i, 1);                       // tapping again removes it
    else if (plan.chans.length < 4) plan.chans.push({ ch, name: row ? (row.short || row.name) : String(ch), title: title || '' });
    else { flash(from, 'Split View holds 4 — remove one first'); return; }
    if (plan.audio >= plan.chans.length) plan.audio = 0;
    savePlan(); renderTray(); markPicked();
  }
  function markPicked() {
    const set = new Set(plan.chans.map((x) => x.ch));
    document.querySelectorAll('#cg .cg-row').forEach((r) => r.classList.toggle('picked', split && set.has(Number(r.dataset.ch))));
  }
  function setSplit(on) {
    split = on;
    if (on) plan = loadPlan();
    $('cg-split').setAttribute('aria-pressed', String(on));
    $('cg-tray').hidden = !on;
    $('cg-sec').classList.toggle('splitting', on);
    renderTray(); markPicked();
  }
  function liveSports() {
    // The best live games on HTC channels right now (the hub's own ranking).
    const games = ((window.HBLastTonight || {}).live || []).filter((g) => (g.cable || []).length)
      .sort((a, b) => (b.fav ? 100 : 0) + (b.score || 0) - ((a.fav ? 100 : 0) + (a.score || 0)));
    const out = [];
    games.forEach((g) => { const c = g.cable[0]; if (out.length < 4 && !out.some((x) => x.ch === c.ch)) out.push({ ch: c.ch, name: c.name, title: `${g.teams[0].abbr} @ ${g.teams[1].abbr}` }); });
    return out;
  }
  function renderTray() {
    const tray = $('cg-tray');
    if (!split) return;
    const { canControl, tv } = remote();
    tray.replaceChildren();
    const head = el('div', 'tray-head');
    head.append(el('b', null, 'Split View'), el('span', 'faint', tv ? `on ${tv.name}` : ''));
    const close = el('button', 'tray-x', 'Done'); close.type = 'button'; close.addEventListener('click', () => setSplit(false));
    head.appendChild(close);
    tray.appendChild(head);

    const grid = el('div', 'tray-grid');
    for (let i = 0; i < 4; i++) {
      const c = plan.chans[i];
      const slot = el('div', 'tray-slot' + (c ? '' : ' empty') + (c && plan.audio === i ? ' audio' : ''));
      slot.append(el('span', 'tray-pos', POS[i]));
      if (c) {
        const live = nowOn(c.ch) || c.title;
        slot.append(el('b', 'num', String(c.ch)), el('span', 'tray-name', c.name), el('span', 'tray-what', live || ''));
        const snd = el('button', 'tray-snd', plan.audio === i ? 'Sound' : 'Mute');
        snd.type = 'button';
        snd.setAttribute('aria-pressed', String(plan.audio === i));
        snd.setAttribute('aria-label', `Play sound from ${c.name}`);
        snd.addEventListener('click', () => { plan.audio = i; savePlan(); renderTray(); });
        const rm = el('button', 'tray-rm', '×'); rm.type = 'button'; rm.setAttribute('aria-label', `Remove ${c.name}`);
        rm.addEventListener('click', () => { plan.chans.splice(i, 1); if (plan.audio >= plan.chans.length) plan.audio = 0; savePlan(); renderTray(); markPicked(); });
        slot.append(snd, rm);
      } else {
        slot.append(el('span', 'tray-hint', i === plan.chans.length ? 'Tap a show in the guide' : ''));
      }
      grid.appendChild(slot);
    }
    tray.appendChild(grid);

    const quick = el('div', 'tray-quick');
    const ls = liveSports();
    if (ls.length) {
      const b = el('button', 'tray-chip', `Live sports (${ls.length})`); b.type = 'button';
      b.addEventListener('click', () => { plan = { chans: ls, audio: 0 }; savePlan(); renderTray(); markPicked(); });
      quick.appendChild(b);
    }
    if (plan.chans.length) {
      const b = el('button', 'tray-chip', 'Clear'); b.type = 'button';
      b.addEventListener('click', () => { plan = { chans: [], audio: 0 }; savePlan(); renderTray(); markPicked(); });
      quick.appendChild(b);
    }
    tray.appendChild(quick);

    const foot = el('div', 'tray-foot');
    const running = job && job.state === 'running';
    const go = el('button', 'tray-go', running ? `Setting up ${job.step || 1} of ${job.total}…` : `Start Split View${plan.chans.length ? ` · ${plan.chans.length}` : ''}`);
    go.type = 'button';
    go.disabled = running || !plan.chans.length || !canControl || !tv || !tv.box;
    go.addEventListener('click', () => start('menu'));
    foot.appendChild(go);
    const bar = el('i', 'tray-bar');
    if (running) {
      const frac = Math.min(0.97, (Date.now() / 1000 - job.started) / job.eta_s);
      bar.style.transform = `scaleX(${frac.toFixed(3)})`;
      foot.classList.add('running');
    }
    foot.appendChild(bar);
    const note = el('p', 'tray-note');
    note.textContent = !canControl ? 'Unlock TV control to start Split View.'
      : tv && !tv.box ? `${tv.name} has no HTC box.`
      : running ? 'The TV flips through each channel, then opens the grid — about 11 seconds a channel.'
      : job && job.state === 'done' ? `Done — sound from ${(plan.chans[plan.audio] || {}).name || 'top left'}. If it opened the wrong screen, open MultiView with your remote and Start again.`
      : job && job.state === 'error' ? `Didn’t finish: ${job.error || 'the box stopped answering'}`
      : 'First open MultiView on the TV with your remote (TiVo → right → right → OK), then tap Start.' + (plan.chans.length < 4 ? ' Fewer than 4? The other windows keep what was there.' : '');
    foot.appendChild(note);
    if (job && job.state !== 'running' && plan.chans.length) {
      const retry = el('button', 'tray-retry', 'Retry from Home'); retry.type = 'button';
      retry.title = 'Use when the TiVo menu was last on its Home page';
      retry.addEventListener('click', () => start('home'));
      foot.appendChild(retry);
    }
    tray.appendChild(foot);
  }
  async function start(firstOpen) {
    const { tv } = remote();
    if (!tv || !plan.chans.length) return;
    try {
      const r = await fetch('/api/media/multiview', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tv: tv.entity_id, channels: plan.chans.map((x) => String(x.ch)), audio: plan.audio, first_open: firstOpen }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(r.status === 403 ? 'TV control is locked on this device' : d.detail || `Failed (${r.status})`);
      job = d;
      renderTray();
      poll(tv.entity_id);
    } catch (e) { job = { state: 'error', error: e.message }; renderTray(); }
  }
  function poll(tvId) {
    clearTimeout(jobTimer);
    jobTimer = setTimeout(async () => {
      try { job = await (await fetch(`/api/media/multiview?tv=${encodeURIComponent(tvId)}`, { credentials: 'same-origin' })).json(); } catch (e) {}
      renderTray();
      if (job && job.state === 'running') poll(tvId);
    }, 1000);
  }
  window.HBSplit = { open: () => { setSplit(true); $('cg-sec').scrollIntoView({ behavior: 'smooth', block: 'start' }); } };

  function flash(node, text, ok) {
    const s = $('cg-credit');
    s.textContent = text;
    s.classList.toggle('ok', !!ok);
    clearTimeout(flash.t);
    flash.t = setTimeout(() => { s.textContent = data ? data.credit : ''; s.classList.remove('ok'); }, 3200);
  }
  function mark(ch) {
    document.querySelectorAll('#cg .cg-row').forEach((r) => r.classList.toggle('tuned', Number(r.dataset.ch) === ch));
  }

  // A programme's words ride along when it started off-screen to the left
  // (sticky against the pinned channel column), like a real guide.
  function txt(...kids) { const w = el('span', 'cg-txt'); w.append(...kids); return w; }
  function block(p, row, t0, now) {
    const s = Math.max(t(p.start), t0), e = t(p.end);
    const b = el('button', 'cg-prog ' + p.kind + (p.live ? ' live' : '') + (p.close ? ' hot' : '') + (p.fav ? ' mine' : ''));
    b.type = 'button';
    b.style.left = `${(s - t0) / 60000 * PX}px`;
    b.style.width = `${Math.max(28, (e - s) / 60000 * PX - 4)}px`;
    if (e <= now) b.classList.add('past');
    else if (t(p.start) <= now) b.classList.add('now');
    if (p.kind === 'game' && p.teams) {
      const [a, h] = p.teams;
      if (hex(a.color)) b.style.setProperty('--ta', hex(a.color));
      if (hex(h.color)) b.style.setProperty('--tb', hex(h.color));
      const top = el('span', 'cg-title');
      top.textContent = p.live && a.score != null ? `${a.abbr} ${a.score} – ${h.score} ${h.abbr}` : p.title;
      b.append(txt(top, el('span', 'cg-sub', p.live ? [p.close, p.sub].filter(Boolean).join(' · ') || 'Live' : `${clock(t(p.start))} · ${p.sub || ''}`.replace(/ · $/, ''))));
    } else {
      const sub = el('span', 'cg-sub');
      if (p.new) sub.appendChild(el('b', 'cg-new', 'New'));
      sub.appendChild(document.createTextNode([`${clock(t(p.start))}–${clock(e)}`, p.sub].filter(Boolean).join(' · ')));
      b.append(txt(el('span', 'cg-title', p.title), sub));
    }
    const app = (p.apps || []).map((k) => SVC[k]).find((x) => x && x.tv);
    if (app) {
      const badge = el('span', 'cg-app', app.name);
      badge.setAttribute('role', 'button');
      badge.style.setProperty('--tint', app.tint);
      badge.title = `Open in ${app.name} instead`;
      badge.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const { canControl, tv } = remote();
        if (canControl && tv) { window.HBLaunch(app.key); flash(b, `Opening ${app.name} on ${tv.name}…`, true); }
        else window.location.href = app.open;
      });
      b.appendChild(badge);
      b.classList.add('has-app');
    }
    b.setAttribute('aria-label', `${row.ch} ${row.name}: ${p.title}, ${clock(t(p.start))} to ${clock(e)}`);
    b.addEventListener('click', () => pick(row.ch, b, p.title, row));
    return b;
  }

  function render() {
    if (!data) return;
    header();
    filters();
    const box = $('cg');
    const keep = { x: box.scrollLeft, y: box.scrollTop };
    box.replaceChildren();
    const t0 = t(data.start), t1 = t(data.end), now = Date.now();
    const width = (t1 - t0) / 60000 * PX;
    const inner = el('div', 'cg-inner');
    inner.style.setProperty('--w', `${width}px`);

    // Time row: half-hour marks, pinned to the top.
    const times = el('div', 'cg-times');
    times.appendChild(el('span', 'cg-corner', ''));
    const tt = el('div', 'cg-ttrack');
    for (let m = t0; m < t1; m += 30 * 60000) {
      const lab = el('span', 'cg-tick' + (new Date(m).getMinutes() === 0 ? ' hour' : ''), half(m));
      lab.style.left = `${(m - t0) / 60000 * PX}px`;
      tt.appendChild(lab);
    }
    times.appendChild(tt);
    inner.appendChild(times);

    const { tv } = remote();
    const tuned = lastCh(tv);
    const rows = (data.channels || []).filter((c) => (cat === 'All' || c.cat === cat) && (!listedOnly || c.programs.length));
    const lb = $('cg-listed');
    lb.setAttribute('aria-pressed', String(listedOnly));
    lb.textContent = listedOnly ? `Listed (${rows.length})` : `All ${(data.channels || []).filter((c) => cat === 'All' || c.cat === cat).length}`;
    if (!rows.length) inner.appendChild(el('p', 'muted cg-empty', 'Nothing listed in this group right now.'));
    rows.forEach((c) => {
      const row = el('div', 'cg-row' + (c.ch === tuned ? ' tuned' : ''));
      row.dataset.ch = String(c.ch);
      const head = el('button', 'cg-ch');
      head.type = 'button';
      head.append(el('b', 'num', String(c.ch)), el('span', null, c.short || c.name));
      head.setAttribute('aria-label', `Tune to ${c.ch} ${c.name}`);
      head.addEventListener('click', () => pick(c.ch, head, null, c));
      const track = el('div', 'cg-track');
      if (!c.programs.length) {
        const gap = el('button', 'cg-prog gap');
        gap.type = 'button';
        gap.style.left = '0px'; gap.style.width = `${width - 4}px`;
        gap.append(txt(el('span', 'cg-title', c.name), el('span', 'cg-sub', 'No listings — tap to tune')));
        gap.addEventListener('click', () => pick(c.ch, gap, null, c));
        track.appendChild(gap);
      }
      c.programs.forEach((p) => track.appendChild(block(p, c, t0, now)));
      row.append(head, track);
      inner.appendChild(row);
    });

    // Now: a line through every row.
    if (now >= t0 && now <= t1) {
      const line = el('i', 'cg-now');
      line.style.left = `calc(var(--chw) + ${(now - t0) / 60000 * PX}px)`;
      inner.appendChild(line);
    }
    box.appendChild(inner);
    if (!placedOnce) { box.scrollLeft = Math.max(0, (now - t0) / 60000 * PX - 40); placedOnce = true; }
    else { box.scrollLeft = keep.x; box.scrollTop = keep.y; }
    $('cg-credit').textContent = data.credit || '';
    cacheGeometry();
    settle();
    markPicked();
  }

  // Refreshes rebuild the grid only when the listings changed, and never
  // mid-scroll; otherwise just the now line moves.
  let sig = '', lastScroll = 0;
  $('cg').addEventListener('scroll', () => { lastScroll = Date.now(); }, { passive: true });
  function moveNow() {
    const line = $('cg').querySelector('.cg-now');
    if (line && data) line.style.left = `calc(var(--chw) + ${(Date.now() - t(data.start)) / 60000 * PX}px)`;
  }
  async function load() {
    try {
      const r = await fetch('/api/watch/grid', { credentials: 'same-origin' });
      if (!r.ok) throw new Error(String(r.status));
      const next = await r.json();
      const nsig = next.start + '|' + JSON.stringify(next.channels);
      if (nsig === sig && data) { moveNow(); return; }
      const apply = () => { sig = nsig; data = next; render(); };
      if (Date.now() - lastScroll < 1500) setTimeout(apply, 1600); else apply();
    } catch (e) {
      if (!data) $('cg').replaceChildren(el('p', 'muted cg-empty', 'Couldn’t load the guide.'));
    }
  }
  // Titles of programmes that began off to the left slide along so they stay
  // readable next to the pinned channel column. Geometry is cached once per
  // render (it comes from the data, not from measuring), and the scroll
  // handler only does arithmetic and writes a transform when one changes —
  // at most once a frame. No layout reads while scrolling.
  let geo = [];
  function cacheGeometry() {
    geo = [];
    $('cg').querySelectorAll('.cg-prog').forEach((b) => {
      const tx = b.querySelector('.cg-txt');
      if (tx) geo.push({ b, tx, left: parseFloat(b.style.left) || 0, w: parseFloat(b.style.width) || 0, shift: 0, narrow: null });
    });
  }
  let queued = false;
  function settle() {
    if (queued) return;
    queued = true;
    let done = false;
    const run = () => {
      if (done) return;
      done = true;
      queued = false;
      const x = $('cg').scrollLeft;
      for (const g of geo) {
        const shift = Math.max(0, Math.min(x - g.left, g.w - 90));
        if (shift !== g.shift) { g.tx.style.transform = shift ? `translate3d(${shift}px,0,0)` : ''; g.shift = shift; }
        const narrow = g.w - shift < 170;
        if (narrow !== g.narrow) { g.b.classList.toggle('narrow', narrow); g.narrow = narrow; }
      }
    };
    // Next frame; a timer backstop in case frames aren't being drawn, so a
    // missed frame can never leave the handler stuck.
    requestAnimationFrame(run);
    setTimeout(run, 100);
  }
  $('cg').addEventListener('scroll', settle, { passive: true });
  $('cg-listed').addEventListener('click', () => {
    listedOnly = !listedOnly;
    try { localStorage.setItem('hb-cg-listed', listedOnly ? '1' : '0'); } catch (e) {}
    render();
  });
  $('cg-nowbtn').addEventListener('click', () => {
    if (!data) return;
    const box = $('cg');
    box.scrollTo({ left: Math.max(0, (Date.now() - t(data.start)) / 60000 * PX - 40), behavior: 'smooth' });
  });
  window.addEventListener('hb:tv', () => { if (split) plan = loadPlan(); job = null; render(); renderTray(); });
  $('cg-split').addEventListener('click', () => setSplit(!split));
  window.addEventListener('hb:tuned', (ev) => mark(ev.detail.ch));
  // The TV list and auth arrive after this script: re-header when they do.
  setTimeout(() => data && header(), 2500);

  load();
  setInterval(() => { if (document.visibilityState === 'visible') load(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
})();
