const CACHE_NAME = "tickerboard-shell-v3";
const SHELL_FILES = ["/", "/style.css", "/app.js", "/manifest.json"];

// Same-origin GETs for the app shell and static assets. Anything else -
// POST/DELETE, /api/*, cross-origin CDN requests - is passed straight to the
// network and never written to the cache.
//
// The previous version called cache.put() for EVERY request, which:
//   - threw "Request method POST is unsupported" on every watchlist add and
//     every delete (Cache.put only accepts GET), surfacing as a stream of
//     service-worker console errors;
//   - cached /api/* responses, so a stale watchlist or analysis could be
//     served from disk the moment a fetch failed;
//   - cached cross-origin responses it has no business storing.
const STATIC_EXTENSIONS = [".css", ".js", ".png", ".svg", ".ico", ".webmanifest", ".json"];

function isCacheableRequest(request) {
  if (request.method !== "GET") return false;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith("/api/")) return false;

  if (url.pathname === "/" || SHELL_FILES.includes(url.pathname)) return true;
  return STATIC_EXTENSIONS.some((extension) => url.pathname.endsWith(extension));
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first, so a real code update is never invisible behind a cache
// name that rarely changes. The cache is only an offline fallback.
self.addEventListener("fetch", (event) => {
  const { request } = event;

  if (!isCacheableRequest(request)) {
    return;  // not calling respondWith() lets the browser handle it normally
  }

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Opaque/error responses are not worth storing as the offline copy.
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME)
            .then((cache) => cache.put(request, copy))
            .catch(() => { /* cache full or unavailable - not fatal */ });
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});
