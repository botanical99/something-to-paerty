/* Offline shell for the PWA. Only static files are cached - never /api or /ws. */
const CACHE = "lights-shell-v1";
const SHELL = ["/static/app.css", "/static/app.js", "/icons/icon-192.png", "/manifest.webmanifest"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/api") || u.pathname === "/ws" || u.pathname === "/pair" || u.pathname === "/login") return;
  if (u.pathname.startsWith("/static/") || u.pathname.startsWith("/icons/")) {
    // stale-while-revalidate: instant loads, fresh next time
    e.respondWith(caches.open(CACHE).then(async (c) => {
      const hit = await c.match(e.request);
      const net = fetch(e.request).then((r) => { if (r.ok) c.put(e.request, r.clone()); return r; }).catch(() => hit);
      return hit || net;
    }));
  }
});
