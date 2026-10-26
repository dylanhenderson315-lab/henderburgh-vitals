/* Henderburgh iPad shell worker.
   Caches the /hub shell + static assets so the home-screen app opens instantly
   and survives a flaky hotel network. API calls are never cached — live data
   (lights, weather) must always be real or visibly missing, never stale-as-live.
   Bump VERSION whenever hub.html / hub.js / hub.css / tokens.css change. */
const VERSION = 'hb-hub-v9';
const SHELL = [
  '/hub',
  '/static/tokens.css?v=6',
  '/static/hub.css?v=8',
  '/static/hub.js?v=7',
  '/static/house-modes.js?v=1',
  '/static/streaming-services.js?v=2',
  '/static/streaming.js?v=4',
  '/static/icons/icon-192.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;   // always live

  // The shell page: network first (fresh deploys win), cache when offline.
  if (url.pathname === '/hub') {
    e.respondWith(
      fetch(req)
        .then((res) => { const copy = res.clone(); caches.open(VERSION).then((c) => c.put('/hub', copy)); return res; })
        .catch(() => caches.match('/hub'))
    );
    return;
  }

  // Versioned static assets: cache first.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
