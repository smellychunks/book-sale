/* Service worker: keeps the app itself (page, script, styles, icons) available with no connection.
 * The library data is NOT handled here: it lives in IndexedDB and is fetched by app.js, so a bad
 * connection can never serve the app a half-cached library. */
const CACHE = 'sale-lookup-shell-v1';
const SHELL = ['./', 'index.html', 'styles.css', 'app.js', 'manifest.webmanifest',
               'icon.svg', 'icon-180.png', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.includes('/data/')) return;
  const key = req.mode === 'navigate' ? 'index.html' : req;
  // Serve from the saved copy at once; refresh it in the background when there is a connection.
  e.respondWith(caches.match(key, { ignoreSearch: true }).then((hit) => {
    const net = fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
      return res;
    }).catch(() => hit);
    return hit || net;
  }));
});
