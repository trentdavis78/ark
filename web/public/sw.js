/**
 * Service worker: offline shell + the share-target receiver.
 *
 * Two jobs.
 *
 * 1. Keep the app openable with no signal. Cellular in parts of Morris and
 *    Hunterdon counties is unreliable, and an app that will not start is worse
 *    than useless to someone parked on a shoulder.
 *
 * 2. Catch the POST that Android's share sheet sends when the driver shares a
 *    screenshot. A share target POST cannot be read by the page directly, so
 *    it is stashed here and handed over on the redirect.
 */

const CACHE = "blacktop-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/icon.svg",
               "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

/** The most recently shared screenshot, awaiting pickup by the page. */
let pendingShare = null;

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Android share sheet -> installed PWA.
  if (event.request.method === "POST" && url.pathname === "/") {
    event.respondWith((async () => {
      try {
        const form = await event.request.formData();
        const file = form.get("screenshot");
        if (file) pendingShare = file;
      } catch {
        // A malformed share should still land the driver in the app.
      }
      return Response.redirect("/?shared=1", 303);
    })());
    return;
  }

  // Hand the stashed screenshot to the page that just opened.
  if (url.pathname === "/__shared-screenshot") {
    event.respondWith((async () => {
      if (!pendingShare) return new Response(null, { status: 204 });
      const file = pendingShare;
      pendingShare = null;
      return new Response(file, {
        headers: { "content-type": file.type || "image/png" },
      });
    })());
    return;
  }

  // The vision endpoint must never be served stale.
  if (url.pathname.startsWith("/api/")) return;

  if (event.request.method !== "GET") return;

  // Cache-first for the shell: startup speed matters more than freshness for
  // a driver opening this at a red light.
  event.respondWith(
    caches.match(event.request).then((hit) =>
      hit ?? fetch(event.request).then((res) => {
        if (res.ok && url.origin === self.location.origin) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return res;
      }).catch(() => caches.match("/index.html").then((f) => f ?? Response.error())),
    ),
  );
});
