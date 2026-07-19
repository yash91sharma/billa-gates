/*
 * Billa-Gates service worker.
 *
 * Deliberately conservative: this worker exists to make the app installable
 * (Add to Home Screen / standalone window), NOT to change any behaviour. It
 * is network-only for everything except immutable, content-hashed build
 * assets under /static/assets/. In particular it NEVER touches API requests
 * or navigations, so live job/run/snapshot data is always fetched from the
 * network exactly as it is without a service worker installed.
 */

const CACHE_NAME = 'billa-gates-assets-v1'

// Vite fingerprints these with a content hash, so a given URL is immutable and
// safe to cache forever. Anything else falls through to the network untouched.
const HASHED_ASSET_PREFIX = '/static/assets/'

self.addEventListener('install', () => {
  // Activate this worker as soon as it finishes installing.
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // Drop any caches from older worker versions.
      const keys = await caches.keys()
      await Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
      await self.clients.claim()
    })()
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event

  // Only ever consider GETs of our own immutable, hashed static assets.
  // Everything else — API requests, navigations, cross-origin — is left
  // entirely to the browser's default network handling (no interception).
  if (request.method !== 'GET') return

  let url
  try {
    url = new URL(request.url)
  } catch {
    return
  }

  if (url.origin !== self.location.origin) return
  if (!url.pathname.startsWith(HASHED_ASSET_PREFIX)) return

  // Cache-first for immutable assets; populate the cache on first miss.
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE_NAME)
      const cached = await cache.match(request)
      if (cached) return cached

      const response = await fetch(request)
      if (response && response.ok) {
        cache.put(request, response.clone())
      }
      return response
    })()
  )
})
