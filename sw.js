var CACHE = 'plex-jqh-omv-v8.45';
var FILES = ['./', './app.js', './preconnect.js', './fallback.html', './fallback.js', './debug.html', './debug.js', './manifest.json', './icon-192-v4.png', './icon-512-v4.png', './icon-maskable-v3.png', './icon-monochrome-v3.png'];

// Two non-obvious requirements stacked here:
// 1. addAll is all-or-nothing — a single 404/timeout/network blip kills
//    the whole precache. Use individual add() + per-file catch so a slow
//    asset can't lock the user out of the new version.
// 2. By default c.add(url) goes through the browser HTTP cache, which
//    can serve a stale older copy of the asset and silently precache it
//    into our brand-new CACHE — defeating the whole point of the bump.
//    Wrap each URL in Request(..., { cache: 'reload' }) to force a fresh
//    network read for every precached asset.
function precache(c) {
  return Promise.all(FILES.map(function(f){
    return c.add(new Request(f, { cache: 'reload' })).catch(function(){});
  }));
}

self.addEventListener('install', function(e) {
  e.waitUntil(caches.open(CACHE).then(precache));
  self.skipWaiting();
});

self.addEventListener('activate', function(e) {
  e.waitUntil(
    caches.keys().then(function(names) {
      return Promise.all(names.filter(function(n) { return n !== CACHE; }).map(function(n) { return caches.delete(n); }));
    }).then(function() {
      // Self-heal: the browser can evict CacheStorage (storage pressure,
      // partial browsing-data cleanup) while the SW registration survives —
      // observed 2026-06-12 on desktop: SW active but cache gone, so offline
      // mode was dead and debug/footer showed no version (both derive it
      // from the cache name). Rebuild the precache when it's empty. Only
      // covers evictions that happened before this SW version activates;
      // an eviction during the SW's lifetime still waits for the next bump.
      return caches.open(CACHE).then(function(c) {
        return c.keys().then(function(keys) {
          return keys.length ? null : precache(c);
        });
      });
    }).then(function() {
      return self.clients.claim();
    })
  );
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
      if (r) return r;
      return fetch(e.request, { cache: 'reload' }).then(function(resp) {
        // Refill the cache on miss: this is the continuous half of the
        // self-heal (the activate-time rebuild only runs once per SW
        // version). After a CacheStorage eviction, normal browsing
        // restores offline support file by file. Same-origin GETs only
        // (already filtered above), successful responses only.
        if (e.request.method === 'GET' && resp.ok) {
          var copy = resp.clone();
          caches.open(CACHE).then(function(c) { c.put(e.request, copy); }).catch(function(){});
        }
        return resp;
      });
    })
  );
});
