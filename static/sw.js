// Minimal service worker: lets browsers treat the site as an installable app.
// It deliberately caches nothing, so documents and pages are never stored by it.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
