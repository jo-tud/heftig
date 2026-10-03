"use strict";
// Service worker of the installed app, served at /sw.js (scope: the whole site). It keeps one
// page in the browser - /offline, with its stylesheet, script and icon - and shows it when Heftig
// cannot be reached. Nothing else is cached: pages and documents always come from the server.
const CONFIG = { version: "dev", assets: [] }; // filled in by the /sw.js route

const CACHE = "heftig-offline-" + CONFIG.version;
const OFFLINE = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll([OFFLINE, ...CONFIG.assets]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("heftig-") && key !== CACHE) await caches.delete(key);
    }
    // the page request starts while the worker is still starting up
    if (self.registration.navigationPreload) await self.registration.navigationPreload.enable();
    await self.clients.claim();
  })());
});

async function offlinePage() {
  return (await caches.match(OFFLINE)) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== location.origin || req.method !== "GET") return;
  if (CONFIG.assets.includes(url.pathname + url.search)) {
    // the offline page's own files (versioned URLs, so the stored copy is the right one)
    event.respondWith(caches.match(req).then((r) => r || fetch(req)));
    return;
  }
  if (req.mode !== "navigate" || req.destination !== "document") return;
  // answers meant for programs (health checks report a failure as 503) stay as they are - but
  // still taken from the preloaded request, else the browser cancels it and asks a second time
  if (/^\/(api|static)\//.test(url.pathname) || ["/health", "/ready"].includes(url.pathname)) {
    event.respondWith((async () => (await event.preloadResponse) || fetch(req))());
    return;
  }
  event.respondWith((async () => {
    try {
      const r = (await event.preloadResponse) || (await fetch(req));
      // a reverse proxy in front of a stopped Heftig answers with one of these
      return r.status >= 502 && r.status <= 504 ? offlinePage() : r;
    } catch (_) {
      return offlinePage(); // no connection
    }
  })());
});
