// Minimal service worker: makes Sunday installable and keeps the shell available offline.
// It never caches family pages (they are personal and change every morning).
const SHELL = "sunday-shell-v1";
const ASSETS = ["/static/sunday.css", "/static/fonts/fonts.css", "/static/img/mark.svg", "/static/img/favicon.svg"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((k) => Promise.all(k.filter((x) => x !== SHELL).map((x) => caches.delete(x)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || !url.pathname.startsWith("/static/")) return;
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); return res; })));
});
