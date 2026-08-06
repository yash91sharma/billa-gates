/**
 * Behavioural tests for the PWA service worker (`public/sw.js`).
 *
 * `pwa.test.ts` guards the cache policy by reading the source as text
 * (`expect(sw).not.toContain('/api')`). That proves a string is absent, not
 * that a request to `/api/jobs` is actually left alone — and the property that
 * matters is the behaviour: a worker that intercepts API calls or navigations
 * serves stale job/run/snapshot data, which is the one failure mode a backup
 * tool cannot have (an operator reading a cached "success" for a run that
 * failed an hour ago). The text guard also cannot see a regression written
 * without the literal `/api` (a variable, a regex, a broadened prefix).
 *
 * So the worker is loaded into a stand-in ServiceWorkerGlobalScope here and
 * driven with real events. The rule under test is: `respondWith` is called for
 * same-origin GETs of `/static/assets/` and for nothing else.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import fs from 'fs'
import path from 'path'

const SW_SOURCE = fs.readFileSync(path.resolve(__dirname, '../../public/sw.js'), 'utf-8')

const ORIGIN = 'https://backup.local'
const CACHE_NAME = 'billa-gates-assets-v1'

type Handler = (event: unknown) => void

/** A minimal Cache, recording what was written to it. */
class FakeCache {
  entries = new Map<string, unknown>()
  put = vi.fn(async (request: { url: string }, response: unknown) => {
    this.entries.set(request.url, response)
  })
  match = vi.fn(async (request: { url: string }) => this.entries.get(request.url))
}

/** A minimal CacheStorage over named FakeCaches. */
class FakeCacheStorage {
  caches = new Map<string, FakeCache>()
  open = vi.fn(async (name: string) => {
    let cache = this.caches.get(name)
    if (!cache) {
      cache = new FakeCache()
      this.caches.set(name, cache)
    }
    return cache
  })
  keys = vi.fn(async () => [...this.caches.keys()])
  delete = vi.fn(async (name: string) => this.caches.delete(name))
}

/** A response stand-in; `clone()` is what the worker stores in the cache. */
function makeResponse(body: string, ok = true) {
  const response = {
    ok,
    body,
    clone: vi.fn(() => ({ ok, body, cloned: true })),
  }
  return response
}

/**
 * Evaluate sw.js against stand-in globals and return the captured handlers.
 *
 * The worker is a script, not a module, so it is evaluated as a function body
 * with `self`, `caches` and `fetch` bound as parameters — its top-level
 * `const`s stay scoped to that body and cannot leak between tests.
 */
function loadServiceWorker() {
  const handlers = new Map<string, Handler>()
  const cacheStorage = new FakeCacheStorage()
  const networkFetch = vi.fn(async (_request: unknown) => makeResponse('from network'))

  const scope = {
    addEventListener: (type: string, handler: Handler) => handlers.set(type, handler),
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => undefined) },
    location: { origin: ORIGIN },
  }

  // eslint-disable-next-line no-new-func
  new Function('self', 'caches', 'fetch', SW_SOURCE)(scope, cacheStorage, networkFetch)

  return { handlers, scope, cacheStorage, networkFetch }
}

/**
 * Dispatch a fetch event and report whether the worker took the response over.
 *
 * The return type is annotated explicitly: `responded` is only ever assigned
 * from inside the `respondWith` callback, which TypeScript's control-flow
 * analysis cannot see, so it would otherwise narrow the result to `null`.
 */
async function dispatchFetch(
  handlers: Map<string, Handler>,
  request: { url: string; method?: string }
): Promise<{ intercepted: boolean; response: unknown }> {
  const fetchHandler = handlers.get('fetch')
  if (!fetchHandler) throw new Error('the worker registered no fetch handler')

  let responded: Promise<unknown> | null = null
  fetchHandler({
    request: { method: 'GET', ...request },
    respondWith: (value: Promise<unknown>) => {
      responded = value
    },
  })

  return {
    intercepted: responded !== null,
    response: responded ? await responded : null,
  }
}

let sw: ReturnType<typeof loadServiceWorker>

beforeEach(() => {
  sw = loadServiceWorker()
})

// ── Registration ─────────────────────────────────────────────────────────────

describe('lifecycle', () => {
  it('registers install, activate and fetch handlers', () => {
    // A fetch handler is required for installability.
    expect([...sw.handlers.keys()].sort()).toEqual(['activate', 'fetch', 'install'])
  })

  it('activates immediately on install', () => {
    sw.handlers.get('install')!({})

    expect(sw.scope.skipWaiting).toHaveBeenCalled()
  })

  it('claims open clients and drops caches from older worker versions on activate', async () => {
    sw.cacheStorage.caches.set('billa-gates-assets-v0', new FakeCache())
    sw.cacheStorage.caches.set(CACHE_NAME, new FakeCache())

    let waited: Promise<unknown> | null = null
    sw.handlers.get('activate')!({
      waitUntil: (value: Promise<unknown>) => {
        waited = value
      },
    })
    await waited

    expect(sw.cacheStorage.delete).toHaveBeenCalledWith('billa-gates-assets-v0')
    expect(sw.cacheStorage.delete).not.toHaveBeenCalledWith(CACHE_NAME)
    expect(sw.scope.clients.claim).toHaveBeenCalled()
  })
})

// ── What the worker must never intercept ─────────────────────────────────────

describe('requests the worker must leave to the network', () => {
  /*
   * The live-data CUJs. Not intercepting is the whole contract: anything
   * cached here is a stale job list, run status or snapshot listing.
   */
  const apiPaths = [
    '/api/jobs',
    '/api/jobs/job-1',
    '/api/jobs/job-1/runs',
    '/api/jobs/job-1/snapshots',
    '/api/jobs/job-1/commands',
    '/api/runs/recent?limit=10',
    '/api/runs/run-1',
    '/api/mounts/destinations/usage',
    '/api/settings',
    '/api/health',
  ]

  for (const p of apiPaths) {
    it(`leaves GET ${p} entirely to the network`, async () => {
      const { intercepted } = await dispatchFetch(sw.handlers, { url: `${ORIGIN}${p}` })

      expect(intercepted).toBe(false)
      // Not intercepting means the worker does not fetch it either — the
      // browser does, untouched.
      expect(sw.networkFetch).not.toHaveBeenCalled()
      expect(sw.cacheStorage.open).not.toHaveBeenCalled()
    })
  }

  // Navigations must reach the network so a deployed bundle is picked up and
  // the SPA shell is never served from a stale cache.
  const navigations = ['/', '/jobs', '/jobs/job-1', '/runs/run-1', '/destinations', '/settings']

  for (const p of navigations) {
    it(`leaves the navigation to ${p} to the network`, async () => {
      const { intercepted } = await dispatchFetch(sw.handlers, { url: `${ORIGIN}${p}` })

      expect(intercepted).toBe(false)
      expect(sw.cacheStorage.open).not.toHaveBeenCalled()
    })
  }

  it('leaves non-GET requests alone even under the asset prefix', async () => {
    for (const method of ['POST', 'PUT', 'DELETE', 'HEAD']) {
      const { intercepted } = await dispatchFetch(sw.handlers, {
        url: `${ORIGIN}/static/assets/index-abc123.js`,
        method,
      })
      expect(intercepted, `${method} must not be intercepted`).toBe(false)
    }
  })

  it('leaves cross-origin requests alone', async () => {
    const { intercepted } = await dispatchFetch(sw.handlers, {
      url: 'https://ntfy.sh/static/assets/index-abc123.js',
    })

    expect(intercepted).toBe(false)
  })

  it('leaves the root-scoped service worker script itself alone', async () => {
    // /sw.js is served from the root, outside the hashed-asset prefix; caching
    // it would pin the worker to its own current version.
    const { intercepted } = await dispatchFetch(sw.handlers, { url: `${ORIGIN}/sw.js` })

    expect(intercepted).toBe(false)
  })

  it('leaves unhashed static files at the /static/ root alone', async () => {
    // Only /static/assets/ is content-hashed. The manifest and icons keep
    // stable URLs, so caching them forever would pin them.
    for (const p of ['/static/manifest.webmanifest', '/static/icon-192.png', '/static/sw.js']) {
      const { intercepted } = await dispatchFetch(sw.handlers, { url: `${ORIGIN}${p}` })
      expect(intercepted, `${p} must not be intercepted`).toBe(false)
    }
  })

  it('does not throw on a request whose URL cannot be parsed', async () => {
    const { intercepted } = await dispatchFetch(sw.handlers, { url: 'not a url' })

    // An exception in a fetch handler fails the request outright.
    expect(intercepted).toBe(false)
  })
})

// ── What the worker does cache ───────────────────────────────────────────────

describe('immutable hashed build assets', () => {
  const ASSET = `${ORIGIN}/static/assets/index-abc123.js`

  it('fetches and caches a hashed asset on a cache miss', async () => {
    const { intercepted, response } = await dispatchFetch(sw.handlers, { url: ASSET })

    expect(intercepted).toBe(true)
    expect((response as { body: string }).body).toBe('from network')
    expect(sw.networkFetch).toHaveBeenCalledTimes(1)

    const cache = sw.cacheStorage.caches.get(CACHE_NAME)!
    // The clone is what gets stored — the original is consumed by the page.
    expect(cache.put).toHaveBeenCalledTimes(1)
    expect(cache.entries.get(ASSET)).toMatchObject({ cloned: true })
  })

  it('serves a cached asset without going to the network', async () => {
    const cache = new FakeCache()
    cache.entries.set(ASSET, makeResponse('from cache'))
    sw.cacheStorage.caches.set(CACHE_NAME, cache)

    const { intercepted, response } = await dispatchFetch(sw.handlers, { url: ASSET })

    expect(intercepted).toBe(true)
    expect((response as { body: string }).body).toBe('from cache')
    expect(sw.networkFetch).not.toHaveBeenCalled()
  })

  it('does not cache a failed asset response', async () => {
    // Caching a 404 or a 500 under an immutable URL would pin the failure for
    // the life of the cache — the asset can never be re-fetched.
    sw.networkFetch.mockResolvedValueOnce(makeResponse('not found', false))

    const { response } = await dispatchFetch(sw.handlers, { url: ASSET })

    expect((response as { ok: boolean }).ok).toBe(false)
    const cache = sw.cacheStorage.caches.get(CACHE_NAME)!
    expect(cache.put).not.toHaveBeenCalled()
  })

  it('caches CSS and font assets under the hashed prefix too', async () => {
    for (const asset of [
      `${ORIGIN}/static/assets/index-def456.css`,
      `${ORIGIN}/static/assets/geist-latin-789.woff2`,
    ]) {
      const { intercepted } = await dispatchFetch(sw.handlers, { url: asset })
      expect(intercepted, `${asset} should be cached`).toBe(true)
    }
  })

  it('uses one versioned cache name for every asset', async () => {
    await dispatchFetch(sw.handlers, { url: ASSET })
    await dispatchFetch(sw.handlers, { url: `${ORIGIN}/static/assets/other-xyz789.css` })

    // A second cache name would survive the activate purge and never be dropped.
    expect([...sw.cacheStorage.caches.keys()]).toEqual([CACHE_NAME])
  })
})
