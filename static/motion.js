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

  function reveal(el) {
    var delay = (batch++ % 5) * 45;
    var anim = el.animate(
      [
        { opacity: 0, transform: 'translateY(14px) scale(0.995)' },
        { opacity: 1, transform: 'none' },
      ],
      { duration: 480, easing: EASE, delay: delay, fill: 'backwards' }
    );
    anim.onfinish = function () { el.style.opacity = ''; };
    el.style.opacity = '';   /* WAAPI fill:backwards owns the hidden phase */
  }

  function init() {
    var els = document.querySelectorAll(SELECTOR);
    if (!els.length) return;
    var vh = window.innerHeight || 800;
    var io = new IntersectionObserver(function (entries) {
      /* reset stagger counter per scroll-batch so long pages don't accumulate delay */
      batch = 0;
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(en.target);
        reveal(en.target);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

    els.forEach(function (el) {
      var r = el.getBoundingClientRect();
      /* Only elements clearly below the fold get the entrance — everything
         visible at load stays instantly readable (no content flash). */
      if (r.top > vh * 0.92) {
        el.style.opacity = '0';
        io.observe(el);
      }
    });

    /* Tactile press on cards & buttons — quick dip, springy release. */
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
