// RunLens service worker: cache-first app shell so the PWA works fully
// offline after the first visit. Bump VERSION on every deploy.
const VERSION = 'runlens-v1';
const SHELL = [
  './', './index.html', './manifest.webmanifest', './icons/icon.svg',
  './vendor/chart.umd.js',
  './js/ui.js', './js/db.js', './js/pipeline.js', './js/fit.js',
  './js/ingest.js', './js/origin.js', './js/intervals.js',
  './js/efforts.js', './js/metrics.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request, { ignoreSearch: true }).then(cached =>
      cached || fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(VERSION).then(c => c.put(e.request, copy));
        return resp;
      }).catch(() => cached)
    )
  );
});
