// Service worker: a app abre instantânea e instala-se como app (PWA).
// Só faz cache dos ficheiros estáticos da interface; a API e os vídeos NUNCA ficam em cache.
const VERSION = "awls-v1";
const SHELL = ["/static/app.css", "/static/js/main.js", "/static/js/ui.js", "/static/js/api.js", "/static/js/state.js",
  "/static/js/i18n.js", "/static/brand/logo.svg", "/static/brand/favicon.svg", "/static/brand/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/s/")) return;
  if (url.pathname.startsWith("/static/")) {
    // estáticos: rede primeiro (versões novas entram logo), cache se offline
    e.respondWith(fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(VERSION).then((c) => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request)));
    return;
  }
  if (e.request.mode === "navigate") {
    e.respondWith(fetch(e.request).catch(() => new Response(
      "<!doctype html><meta name=viewport content='width=device-width'><body style='font-family:system-ui;background:#0a0a0d;color:#fff;display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'><h2>You're offline</h2><p style='color:#a7a7b3'>Reconnect to keep creating.</p></div>",
      { headers: { "Content-Type": "text/html" } })));
  }
});
