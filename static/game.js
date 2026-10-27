/* Game Center + the scoring moment.

   Game Center: tap any live game (Now's lead, a Watch bug, the pick) and a
   sheet opens with everything about it — both teams in their colours with
   their logos and records, period-by-period scores, ESPN's win-probability
   curve, head-to-head stats as meeting bars, each side's leaders with
   headshots, and the scoring summary. Data: /api/watch/game.

   The scoring moment: when a poll shows a team's score went up, every
   surface showing that game (Now's lead, the Watch bug, the open sheet, the
   idle screen) gets a team-colour takeover — GOAL / TOUCHDOWN / HOME RUN,
   the logo, the scorer when ESPN names one — then settles back. It's an
   overlay only: the real score underneath is already updated, and a timer
   removes the overlay even if the animation never runs (hidden pane).
   Basketball doesn't get it: baskets come every thirty seconds.

   Rules: textContent only; poll only while open and visible; never invent. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  const hex = (c) => (c && /^[0-9a-f]{6}$/i.test(c) ? `#${c}` : '#3a3a42');
  const safe = (u) => (u && /^https:\/\//.test(u) ? u : null);
  function img(src, cls) {
    const u = safe(src);
    if (!u) return el('span', cls + ' none');
    const i = el('img', cls); i.src = u; i.alt = ''; i.decoding = 'async';
    i.addEventListener('error', () => i.classList.add('none'));
    return i;
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = (tag, attrs) => { const n = document.createElementNS(svgNS, tag); Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v)); return n; };
  const PERIOD = { NHL: (i) => (i < 3 ? String(i + 1) : i === 3 ? 'OT' : 'SO'), NBA: (i) => (i < 4 ? `Q${i + 1}` : `OT${i > 4 ? i - 3 : ''}`),
    NFL: (i) => (i < 4 ? `Q${i + 1}` : 'OT'), NCAAF: (i) => (i < 4 ? `Q${i + 1}` : `OT${i > 4 ? i - 3 : ''}`), MLB: (i) => String(i + 1) };

  // ═══════════════ Game Center ═══════════════
  let open = null;        // { league, id, game }
  let timer = null;
  let lastFocus = null;

  function hero(d) {
    const h = el('div', 'gs-hero');
    const [a, b] = d.teams;
    h.style.setProperty('--ta', hex(a.color)); h.style.setProperty('--tb', hex(b.color));
    h.dataset.game = d.id;
    const side = (t, cls) => {
      const s = el('div', 'gs-side ' + cls);
      s.append(img(t.logo, 'gs-logo'));
      const n = el('div', 'gs-names');
      n.append(el('span', 'gs-loc', t.location || ''), el('b', 'gs-name', t.short || t.abbr), el('span', 'gs-rec', [t.record, t.home ? 'Home' : 'Away'].filter(Boolean).join(' · ')));
      s.append(n);
      return s;
    };
    const mid = el('div', 'gs-mid');
    const sc = el('div', 'gs-score');
    sc.append(el('span', 'num' + (a.winner ? ' won' : ''), a.score ?? '–'), el('span', 'gs-dash', '–'), el('span', 'num' + (b.winner ? ' won' : ''), b.score ?? '–'));
    const st = el('span', 'gs-status' + (d.state === 'in' ? ' live' : ''));
    if (d.state === 'in') st.append(el('i', 'hb-dot hb-dot-live'));
    st.append(document.createTextNode(d.detail || ''));
    mid.append(sc, st);
    h.append(side(a, 'away'), mid, side(b, 'home'));
    return h;
  }

  function linescore(d) {
    const n = Math.max(...d.teams.map((t) => (t.periods || []).length), 0);
    if (!n) return null;
    const lab = PERIOD[d.league] || ((i) => String(i + 1));
    const t = el('table', 'gs-line');
    const head = el('tr');
    head.append(el('th', null, ''));
    for (let i = 0; i < n; i++) head.append(el('th', null, lab(i)));
    head.append(el('th', 'tot', 'T'));
    t.append(head);
    d.teams.forEach((tm) => {
      const r = el('tr');
      const name = el('th', 'gs-line-team');
      name.style.setProperty('--tc', hex(tm.color));
      name.append(img(tm.logo, 'gs-line-logo'), document.createTextNode(tm.abbr || ''));
      r.append(name);
      for (let i = 0; i < n; i++) r.append(el('td', 'num', (tm.periods || [])[i] ?? ''));
      r.append(el('td', 'tot num', tm.score ?? ''));
      t.append(r);
    });
    return t;
  }

  // Win probability: home % over the game. The band above the midline fills
  // with the home colour, below with the away colour.
  function winChart(d) {
    const w = d.winprob;
    if (!w || w.length < 4) return null;
    const [a, b] = d.teams;
    const W = 600, H = 120;
    const box = el('div', 'gs-wp');
    const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
    const x = (i) => (i / (w.length - 1)) * W;
    const y = (p) => H - p * H;
    const line = w.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p).toFixed(1)}`).join('');
    const id = `wp${d.id}`;
    const defs = svg('defs');
    const clipTop = svg('clipPath', { id: `${id}t` }); clipTop.append(svg('rect', { x: 0, y: 0, width: W, height: H / 2 }));
    const clipBot = svg('clipPath', { id: `${id}b` }); clipBot.append(svg('rect', { x: 0, y: H / 2, width: W, height: H / 2 }));
    defs.append(clipTop, clipBot);
    const area = `${line}L${W},${H / 2}L0,${H / 2}Z`;
    s.append(defs,
      svg('path', { d: area, fill: hex(b.color), opacity: '0.55', 'clip-path': `url(#${id}t)` }),
      svg('path', { d: area, fill: hex(a.color), opacity: '0.55', 'clip-path': `url(#${id}b)` }),
      svg('line', { x1: 0, x2: W, y1: H / 2, y2: H / 2, class: 'gs-wp-mid' }),
      svg('path', { d: line, class: 'gs-wp-line' }));
    const last = w[w.length - 1];
    const lead = last >= 0.5 ? b : a;
    const pct = Math.round(Math.max(last, 1 - last) * 100);
    const head = el('div', 'gs-wp-head');
    head.append(el('span', 'hb-label', 'Win probability'), el('b', null, d.state === 'post' ? `${lead.abbr} won` : `${lead.abbr} ${pct}%`));
    const legend = el('div', 'gs-wp-legend');
    legend.append(el('span', null, b.abbr), el('span', null, a.abbr));
    box.append(head, s, legend);
    return box;
  }

  function stats(d) {
    if (!(d.stats || []).length) return null;
    const [a, b] = d.teams;
    const box = el('div', 'gs-stats');
    box.style.setProperty('--ta', hex(a.color)); box.style.setProperty('--tb', hex(b.color));
    d.stats.forEach((s) => {
      const row = el('div', 'gs-stat');
      const total = (s.a || 0) + (s.h || 0);
      const pa = total > 0 ? (s.a || 0) / total : 0.5;
      const bar = el('div', 'gs-bar');
      const ba = el('i', 'a'), bb = el('i', 'b');
      ba.style.transform = `scaleX(${pa.toFixed(3)})`; bb.style.transform = `scaleX(${(1 - pa).toFixed(3)})`;
      bar.append(ba, bb);
      row.append(el('span', 'gs-v' + (s.edge === 'away' ? ' edge' : ''), s.away ?? '—'), el('span', 'gs-k', s.label), el('span', 'gs-v r' + (s.edge === 'home' ? ' edge' : ''), s.home ?? '—'), bar);
      box.append(row);
    });
    return box;
  }

  function leaders(d) {
    const sides = d.teams.map((t) => ({ t, rows: (d.leaders || {})[t.id] || [] })).filter((x) => x.rows.length);
    if (!sides.length) return null;
    const box = el('div', 'gs-leaders');
    sides.forEach(({ t, rows }) => {
      const col = el('div', 'gs-lcol');
      col.style.setProperty('--tc', hex(t.color));
      const h = el('div', 'gs-lhead'); h.append(img(t.logo, 'gs-line-logo'), el('b', null, t.short || t.abbr));
      col.append(h);
      rows.forEach((r) => {
        const row = el('div', 'gs-lrow');
        const face = el('span', 'gs-face');
        const u = safe(r.headshot);
        if (u) face.style.backgroundImage = `url("${u.replace(/"/g, '')}")`;
        const txt = el('div', 'gs-ltxt');
        txt.append(el('span', 'gs-lcat', r.cat || ''), el('b', null, r.name || ''), el('span', 'gs-lval', r.value || ''));
        row.append(face, txt);
        col.append(row);
      });
      box.append(col);
    });
    return box;
  }

  function scoring(d) {
    if (!(d.scoring || []).length) return null;
    const byId = Object.fromEntries(d.teams.map((t) => [t.id, t]));
    const lab = PERIOD[d.league] || ((i) => String(i + 1));
    const list = el('ol', 'gs-plays');
    [...d.scoring].reverse().forEach((p) => {
      const t = byId[p.team];
      const li = el('li', 'gs-play');
      if (t) li.style.setProperty('--tc', hex(t.color));
      const when = el('span', 'gs-when', [p.period ? lab(p.period - 1) : null, p.clock].filter(Boolean).join(' · '));
      const body = el('div', 'gs-pbody');
      body.append(el('span', 'gs-ptext', p.text));
      if (p.kind) body.append(el('span', 'gs-pkind', p.kind));
      const after = p.away != null && p.home != null ? `${p.away}–${p.home}` : '';
      li.append(when, t ? img(t.logo, 'gs-line-logo') : el('span'), body, el('span', 'gs-after num', after));
      list.append(li);
    });
    return list;
  }

  function section(label, node) {
    if (!node) return null;
    const s = el('section', 'gs-sec');
    s.append(el('div', 'wsec-head', null));
    s.firstChild.append(el('span', 'hb-label', label));
    s.append(node);
    return s;
  }

  function render(d) {
    const panel = $('gs-panel');
    const keepScroll = $('gs-body') ? $('gs-body').scrollTop : 0;
    panel.replaceChildren();
    const top = el('header', 'gs-top');
    top.append(el('span', 'hb-label', [d.league, d.series].filter(Boolean).join(' · ')));
    const close = el('button', 'key', 'Done'); close.type = 'button'; close.addEventListener('click', closeGame);
    top.append(close);
    const body = el('div', 'sheet-body gs-body'); body.id = 'gs-body';
    const main = el('div', 'gs-main'), side = el('div', 'gs-aside');
    [section('By period', linescore(d)), winChart(d), section('Team stats', stats(d))].filter(Boolean).forEach((n) => main.append(n));
    [section('Leaders', leaders(d)), section('Scoring', scoring(d))].filter(Boolean).forEach((n) => side.append(n));
    const g = open && open.game;
    if (g && window.HBWhereRow) { const w = window.HBWhereRow(g.apps, g.networks); w.classList.add('gs-where'); main.prepend(w); }
    const foot = el('p', 'faint gs-foot', [d.venue, d.attendance ? `${Number(d.attendance).toLocaleString()} attending` : null].filter(Boolean).join(' · '));
    body.append(main, side);
    if (foot.textContent) body.append(foot);
    panel.append(top, hero(d), body);
    body.scrollTop = keepScroll;
  }

  async function load() {
    if (!open) return;
    try {
      const r = await fetch(`/api/watch/game?league=${encodeURIComponent(open.league)}&id=${encodeURIComponent(open.id)}`, { credentials: 'same-origin' });
      if (!r.ok) throw new Error(String(r.status));
      const d = await r.json();
      if (open && d.id === open.id) { open.data = d; render(d); }
    } catch (e) {
      if (open && !open.data) { $('gs-panel').replaceChildren(el('p', 'muted gs-err', 'Couldn’t load this game from ESPN.')); }
    }
    clearTimeout(timer);
    if (open && document.visibilityState === 'visible' && (!open.data || open.data.state === 'in')) timer = setTimeout(load, 20000);
  }

  function openGame(league, id, game) {
    if (!league || !id || !PERIOD[league]) return;   // fight cards have no box score
    lastFocus = document.activeElement;
    open = { league, id: String(id), game: game || null, data: null };
    const sheet = $('game-sheet');
    $('gs-panel').replaceChildren(el('p', 'muted gs-err', 'Loading the game…'));
    sheet.hidden = false;
    load();
  }
  function closeGame() {
    open = null;
    clearTimeout(timer);
    $('game-sheet').hidden = true;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  $('game-sheet').addEventListener('click', (ev) => { if (ev.target === $('game-sheet')) closeGame(); });
  window.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && open) closeGame(); });
  document.addEventListener('visibilitychange', () => { if (open && document.visibilityState === 'visible') load(); });
  window.HBOpenGame = openGame;

  // ═══════════════ The scoring moment ═══════════════
  const prev = {};   // game id -> [away, home] scores
  function word(league, delta, play) {
    const kind = ((play && play.kind) || '').toLowerCase();
    if (league === 'NHL') return 'GOAL';
    if (league === 'MLB') return /home run/.test(kind) ? 'HOME RUN' : delta > 1 ? `${delta} RUNS` : 'RUN SCORES';
    if (league === 'NFL' || league === 'NCAAF') return delta >= 6 ? 'TOUCHDOWN' : delta === 3 ? 'FIELD GOAL' : delta === 2 ? 'SAFETY' : null;
    return null;   // basketball and anything else: no takeover
  }
  function burst(target, g, idx, text) {
    const t = g.teams[idx];
    const o = el('div', 'burst ' + (idx === 0 ? 'from-l' : 'from-r') + (g.fav ? ' mine' : ''));
    o.style.setProperty('--tc', hex(t.color)); o.style.setProperty('--ta', hex(t.alt || t.color));
    o.setAttribute('aria-hidden', 'true');
    const inner = el('div', 'burst-in');
    inner.append(img(t.logo, 'burst-logo'));
    const words = el('div', 'burst-words');
    words.append(el('b', 'burst-word', text));
    const p = g.play;
    const who = p && p.scored && String(p.team) === String(t.id) && p.who ? p.who : null;
    const line = el('span', 'burst-line', [who, `${g.teams[0].abbr} ${g.teams[0].score} – ${g.teams[1].score} ${g.teams[1].abbr}`].filter(Boolean).join(' · '));
    words.append(line);
    inner.append(words);
    if (who && p.headshot) inner.append(img(p.headshot, 'burst-face'));
    o.append(inner, el('i', 'burst-shine'));
    target.append(o);
    // Backstop: the overlay leaves on a timer, animation or not.
    setTimeout(() => o.classList.add('out'), g.fav ? 3600 : 2800);
    setTimeout(() => o.remove(), g.fav ? 4000 : 3200);
  }
  function celebrate(g, idx, delta) {
    const text = word(g.league, delta, g.play);
    if (!text) return;
    document.querySelectorAll(`[data-game="${CSS.escape(String(g.id))}"]`).forEach((n) => {
      if (n.offsetParent === null && !n.closest('.ambient')) return;   // not on screen
      if (n.querySelector(':scope > .burst')) return;
      burst(n, g, idx, text);
    });
  }
  window.addEventListener('hb:tonight', (ev) => {
    const live = ((ev.detail || {}).live || []);
    live.forEach((g) => {
      const now = g.teams.map((t) => Number(t.score));
      const was = prev[g.id];
      prev[g.id] = now;
      if (!was) return;
      now.forEach((s, i) => { if (s > was[i]) celebrate(g, i, s - was[i]); });
    });
  });
  // For checking the takeover by eye: HBTestScore(gameId, 0|1).
  window.HBTestScore = (id, idx) => {
    const g = (((window.HBLastTonight || {}).live) || []).find((x) => String(x.id) === String(id)) || (((window.HBLastTonight || {}).live) || [])[0];
    if (g) celebrate(g, idx || 0, g.league === 'NFL' ? 7 : 1);
    return !!g;
  };
})();
