/* HENDERBURGH motion layer — the motion.dev/bklit design language on the
   native Web Animations API. Zero dependencies, zero build step.
   - Cards/sections below the fold reveal with a soft staggered rise as you scroll.
   - Elements already on screen at load are left alone (no flash, no jank).
   - Fully disabled for prefers-reduced-motion. */
(function () {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (!('IntersectionObserver' in window) || !Element.prototype.animate) return;

  var SELECTOR = [
    '.card', '.live-card', '.metric-card', '.game-card', '.clip-card',
    '.top-option', '.hero-card', '.card-bg', '.upload-zone',
  ].join(',');

  var EASE = 'cubic-bezier(.22,1,.36,1)';   /* gentle spring-out */
  var batch = 0;
  var pressBound = false;

  /* Animations that must never be allowed to strand an element hidden. */
  var running = [];

  function prune(anim) {
    for (var i = running.length - 1; i >= 0; i--) {
      if (running[i].anim === anim) running.splice(i, 1);
    }
  }

  /* Force an element to its visible resting state. Cancelling matters: a WAAPI
     animation's effect overrides inline style, so clearing opacity alone would
     NOT beat a frozen fill:'backwards'. Cancel drops the effect entirely and
     the element falls back to its own (visible) base style. */
  function settle(el, anim) {
    try { if (anim && anim.playState !== 'finished') anim.cancel(); } catch (e) {}
    el.style.opacity = '';
    el.style.transform = '';
  }

  function reveal(el) {
    /* Visibility is resolved FIRST and unconditionally; the animation may only
       decorate. This used to be inverted: fill:'backwards' pinned the element
       at the opening keyframe (opacity 0) until the animation ran — and a
       backgrounded tab FREEZES the document timeline, so it never ran and the
       card stayed blank permanently. The old sweep() net didn't help because
       it recovered by calling reveal() again, re-arming the identical trap.
       Same failure shape as the two innerHTML/anime.js cases already fixed. */
    el.style.opacity = '';
    el.style.transform = '';

    /* No running timeline => no animation is possible. The element is already
       visible above, so stop here rather than hiding it behind a frozen one. */
    if (document.visibilityState !== 'visible') return;

    var delay = (batch++ % 5) * 45;
    var anim = el.animate(
      [
        { opacity: 0, transform: 'translateY(14px) scale(0.995)' },
        { opacity: 1, transform: 'none' },
      ],
      { duration: 480, easing: EASE, delay: delay, fill: 'backwards' }
    );
    running.push({ el: el, anim: anim });
    anim.onfinish = function () { settle(el, anim); prune(anim); };
    /* Backstop: however the animation ends up — frozen, interrupted, dropped —
       this element is visible shortly after. */
    setTimeout(function () { settle(el, anim); prune(anim); }, delay + 900);
  }

  /* Returning to a backgrounded tab: rescue only the animations that never
     progressed (currentTime still 0/null). In-flight ones are left to finish
     so we don't snap a partially-played entrance. */
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    for (var i = running.length - 1; i >= 0; i--) {
      var r = running[i], t = null;
      try { t = r.anim.currentTime; } catch (e) {}
      if (t === null || t === 0) { settle(r.el, r.anim); running.splice(i, 1); }
    }
  });

  /* ── Safety net ───────────────────────────────────────────────────────────
     Reveal used to depend ENTIRELY on IntersectionObserver firing. If the
     observer was throttled, suppressed (iOS Low Power Mode), lost to a bfcache
     restore, or simply missed during a fast flick-scroll, the element stayed at
     opacity:0 FOREVER — a blank card with no path to recovery. Content
     visibility must never hinge on one callback, so sweep() independently
     reveals anything that is hidden while actually on screen. */
  var hidden = [];
  var io = null;
  var sweepQueued = false;
  var watchTimer = null;

  function revealEl(el) {
    var i = hidden.indexOf(el);
    if (i !== -1) hidden.splice(i, 1);
    if (io) { try { io.unobserve(el); } catch (e) {} }
    reveal(el);
  }

  function sweep() {
    sweepQueued = false;
    var vh = window.innerHeight || 800;
    for (var i = hidden.length - 1; i >= 0; i--) {
      var el = hidden[i];
      if (!el.isConnected) { hidden.splice(i, 1); continue; }   /* swapped away */
      var r = el.getBoundingClientRect();
      if (r.top < vh && r.bottom > 0) revealEl(el);
    }
    if (!hidden.length) stopWatch();
  }

  function queueSweep() {
    if (sweepQueued) return;
    sweepQueued = true;
    requestAnimationFrame(sweep);
  }

  function startWatch() {
    if (watchTimer) return;
    window.addEventListener('scroll', queueSweep, { passive: true });
    window.addEventListener('resize', queueSweep, { passive: true });
    window.addEventListener('pageshow', queueSweep);
    /* Cheap backstop for the case where even scroll events don't reach us.
       Self-cancels the moment nothing is left hidden. */
    watchTimer = setInterval(sweep, 1000);
  }

  function stopWatch() {
    if (!watchTimer) return;
    window.removeEventListener('scroll', queueSweep);
    window.removeEventListener('resize', queueSweep);
    window.removeEventListener('pageshow', queueSweep);
    clearInterval(watchTimer);
    watchTimer = null;
  }

  function init() {
    var els = document.querySelectorAll(SELECTOR);
    if (!els.length) return;
    var vh = window.innerHeight || 800;
    if (!io) {
      io = new IntersectionObserver(function (entries) {
        /* reset stagger counter per scroll-batch so long pages don't accumulate delay */
        batch = 0;
        entries.forEach(function (en) {
          if (!en.isIntersecting) return;
          revealEl(en.target);
        });
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });
    }

    els.forEach(function (el) {
      if (el.dataset.moSeen) return;      /* don't re-hide after an HTMX swap */
      el.dataset.moSeen = '1';
      var r = el.getBoundingClientRect();
      /* Only elements clearly below the fold get the entrance — everything
         visible at load stays instantly readable (no content flash). */
      if (r.top > vh * 0.92) {
        el.style.opacity = '0';
        hidden.push(el);
        io.observe(el);
      }
    });
    if (hidden.length) { startWatch(); queueSweep(); }

    /* Tactile press on cards & buttons — quick dip, springy release.
       Bound once: init() can run again after an HTMX swap. */
    if (pressBound) return;
    pressBound = true;
    document.addEventListener('pointerdown', function (e) {
      var t = e.target.closest(SELECTOR + ',button,.btn-primary,.btn-ghost');
      if (!t || t.dataset.moPress) return;
      t.animate(
        [{ transform: 'scale(1)' }, { transform: 'scale(0.985)' }],
        { duration: 90, easing: 'ease-out', fill: 'forwards' }
      );
      var up = function () {
        t.animate(
          [{ transform: 'scale(0.985)' }, { transform: 'scale(1)' }],
          { duration: 320, easing: EASE, fill: 'forwards' }
        );
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
      };
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
    }, { passive: true });
  }

  /* Count-up: hero numbers roll from 0 to their value once, on reveal.
     Opt-in via [data-count]. Integers only; commas/format preserved; the exact
     original text is restored at the end so nothing is ever misrepresented. */
  function countUp(el) {
    var raw = el.textContent.trim();
    if (/\d[.]\d/.test(raw)) return;                 /* leave decimals exact */
    var target = parseInt(raw.replace(/[^0-9]/g, ''), 10);
    if (!isFinite(target) || target <= 0) return;
    var hadComma = raw.indexOf(',') !== -1;
    var fmt = function (n) { return hadComma ? n.toLocaleString() : String(n); };
    var dur = Math.min(1300, 450 + target * 0.03);
    var t0 = performance.now();
    el.textContent = fmt(0);
    (function tick(now) {
      var p = Math.min(1, (now - t0) / dur);
      var e = 1 - Math.pow(1 - p, 3);                /* easeOutCubic */
      el.textContent = fmt(Math.round(target * e));
      if (p < 1) requestAnimationFrame(tick);
      else el.textContent = raw;                     /* restore exact original */
    })(t0);
  }

  function initCounts() {
    var nums = document.querySelectorAll('[data-count]');
    if (!nums.length) return;
    var vh = window.innerHeight || 800;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        countUp(en.target);
      });
    }, { threshold: 0.6 });
    nums.forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.top < vh && r.bottom > 0) countUp(el);   /* visible now */
      else io.observe(el);
    });
  }

  function boot() { init(); initCounts(); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  /* HTMX replaces #main-content wholesale; re-run so swapped-in cards get the
     same treatment (and are tracked by the safety net) instead of being skipped. */
  document.body && document.body.addEventListener('htmx:afterSwap', function () {
    setTimeout(boot, 30);
  });
  /* Restoring from bfcache can skip observer callbacks entirely. */
  window.addEventListener('pageshow', function (e) { if (e.persisted) queueSweep(); });

  window.__moBoot = boot;   /* let pages re-init after their own DOM updates */
})();
