var CACHE = 'plex-jqh-omv-v8.6';
var FILES = ['./', './app.js', './preconnect.js', './fallback.html', './fallback.js', './debug.html', './debug.js', './manifest.json', './icon-192.png', './icon-512.png'];

self.addEventListener('install', function(e) {
  // Two non-obvious requirements stacked here:
  // 1. addAll is all-or-nothing — a single 404/timeout/network blip kills
  //    the whole install. Use individual add() + per-file catch so a slow
  //    asset can't lock the user out of the new version.
  // 2. By default c.add(url) goes through the browser HTTP cache, which
  //    can serve a stale older copy of the asset and silently precache it
  //    into our brand-new CACHE — defeating the whole point of the bump.
  //    Wrap each URL in Request(..., { cache: 'reload' }) to force a fresh
  //    network read for every precached asset.
  e.waitUntil(caches.open(CACHE).then(function(c) {
    return Promise.all(FILES.map(function(f){
      return c.add(new Request(f, { cache: 'reload' })).catch(function(){});
    }));
  }));
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.filter(function(n) { return n !== CACHE; }).map(function(n) { return caches.delete(n); }));
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(e) {
  // Only cache our own files on same origin, let everything else pass through
  var url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;
  e.respondWith(
    caches.match(e.request, {ignoreSearch: true}).then(function(r) {
      // On cache miss (e.g. a file that failed to precache during install),
      // bypass the browser's HTTP cache and force a network fetch — otherwise
      // a stale older version may be served from the browser HTTP cache.
      return r || fetch(e.request, { cache: 'reload' });
    })
  );
});
