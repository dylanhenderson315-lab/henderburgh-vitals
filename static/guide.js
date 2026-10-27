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
      b.append(txt(el('span', 'cg-title', p.title), el('span', 'cg-sub', [`${clock(t(p.start))}–${clock(e)}`, p.sub].filter(Boolean).join(' · '))));
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
    }
    b.setAttribute('aria-label', `${row.ch} ${row.name}: ${p.title}, ${clock(t(p.start))} to ${clock(e)}`);
    b.addEventListener('click', () => tuneTo(row.ch, b, p.title));
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
      head.addEventListener('click', () => tuneTo(c.ch, head, c.name));
      const track = el('div', 'cg-track');
      if (!c.programs.length) {
        const gap = el('button', 'cg-prog gap');
        gap.type = 'button';
        gap.style.left = '0px'; gap.style.width = `${width - 4}px`;
        gap.append(txt(el('span', 'cg-title', c.name), el('span', 'cg-sub', 'No listings — tap to tune')));
        gap.addEventListener('click', () => tuneTo(c.ch, gap, c.name));
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
    settle();
  }

  async function load() {
    try {
      const r = await fetch('/api/watch/grid', { credentials: 'same-origin' });
      if (!r.ok) throw new Error(String(r.status));
      data = await r.json();
      render();
    } catch (e) {
      if (!data) $('cg').replaceChildren(el('p', 'muted cg-empty', 'Couldn’t load the guide.'));
    }
  }
  // Titles of programmes that began off to the left slide along so they stay
  // readable next to the pinned channel column (transform only, no layout).
  function settle() {
    const box = $('cg');
    const x = box.scrollLeft;
    box.querySelectorAll('.cg-prog').forEach((b) => {
      const left = b.offsetLeft, w = b.offsetWidth;
      const tx = b.firstChild && b.firstChild.classList && b.firstChild.classList.contains('cg-txt') ? b.firstChild : b.querySelector('.cg-txt');
      if (!tx) return;
      const shift = Math.max(0, Math.min(x - left, w - 90));
      tx.style.transform = shift ? `translateX(${shift}px)` : '';
      b.classList.toggle('narrow', w - shift < 170);   // too little left to show an app badge too
    });
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
  window.addEventListener('hb:tv', () => render());
  window.addEventListener('hb:tuned', (ev) => mark(ev.detail.ch));
  // The TV list and auth arrive after this script: re-header when they do.
  setTimeout(() => data && header(), 2500);

  load();
  setInterval(() => { if (document.visibilityState === 'visible') load(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') load(); });
})();
