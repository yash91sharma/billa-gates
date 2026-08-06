/**
 * Tests for the API layer (`src/lib/api.ts`).
 *
 * Every page mocks this module wholesale (`vi.mock('../lib/api')`), so the
 * module itself was never executed by the suite — 3.7% function coverage. That
 * is the one place where the request shape (path, method, body) and the error
 * contract are decided, and both are relied on everywhere:
 *
 *   - callers pattern-match on `err.status` (409 = run already active, 404 =
 *     job gone, 422 = validation) and render `err.data.detail`, so a thrown
 *     value that loses either field turns a precise inline message into a
 *     generic failure;
 *   - the error block is hand-written *three* times (`request`, `deleteJob`,
 *     `cancelRun`) because those two endpoints answer 204/202 with no body and
 *     cannot go through `request`'s `resp.json()`. Three copies of one rule is
 *     how the copies drift, so the shape is asserted identically across all
 *     three rather than once on `request`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import * as api from './api'

/** Install a fetch stub that answers every call with the given response. */
function mockFetch(init: {
  ok?: boolean
  status?: number
  statusText?: string
  json?: () => unknown
}) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? 'OK',
    json: init.json ?? (() => Promise.resolve({})),
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** The (url, options) of the single fetch call that was made. */
function callArgs(fetchMock: ReturnType<typeof vi.fn>) {
  expect(fetchMock).toHaveBeenCalledTimes(1)
  const [url, options] = fetchMock.mock.calls[0]
  return { url: url as string, options: (options ?? {}) as RequestInit }
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── The happy path: every endpoint's path, method and body ───────────────────

describe('request shapes', () => {
  it('prefixes every path with /api and sends a JSON content type', async () => {
    const fetchMock = mockFetch({ json: () => Promise.resolve([]) })

    await api.listJobs()

    const { url, options } = callArgs(fetchMock)
    expect(url).toBe('/api/jobs')
    expect((options.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })

  it('returns the parsed JSON body on success', async () => {
    const payload = [{ id: 'job-1', name: 'Photos' }]
    mockFetch({ json: () => Promise.resolve(payload) })

    await expect(api.listJobs()).resolves.toEqual(payload)
  })

  // Each row is [description, call, expected path, expected method, expected body].
  // GET endpoints send no method (fetch defaults to GET) and no body.
  const cases: Array<{
    name: string
    call: () => Promise<unknown>
    path: string
    method?: string
    body?: unknown
  }> = [
    { name: 'listJobs', call: () => api.listJobs(), path: '/api/jobs' },
    { name: 'getJob', call: () => api.getJob('job-1'), path: '/api/jobs/job-1' },
    {
      name: 'createJob',
      call: () => api.createJob({ name: 'Photos' }),
      path: '/api/jobs',
      method: 'POST',
      body: { name: 'Photos' },
    },
    {
      name: 'updateJob',
      call: () => api.updateJob('job-1', { enabled: false }),
      path: '/api/jobs/job-1',
      method: 'PUT',
      body: { enabled: false },
    },
    {
      name: 'triggerRun',
      call: () => api.triggerRun('job-1'),
      path: '/api/jobs/job-1/run',
      method: 'POST',
    },
    {
      name: 'triggerPrune',
      call: () => api.triggerPrune('job-1'),
      path: '/api/jobs/job-1/prune',
      method: 'POST',
    },
    {
      name: 'triggerCheck',
      call: () => api.triggerCheck('job-1', { check_mode: 'subset', check_subset_percent: 5 }),
      path: '/api/jobs/job-1/check',
      method: 'POST',
      body: { check_mode: 'subset', check_subset_percent: 5 },
    },
    {
      name: 'enableJob',
      call: () => api.enableJob('job-1'),
      path: '/api/jobs/job-1/enable',
      method: 'POST',
    },
    {
      name: 'disableJob',
      call: () => api.disableJob('job-1'),
      path: '/api/jobs/job-1/disable',
      method: 'POST',
    },
    {
      name: 'unlockJob',
      call: () => api.unlockJob('job-1'),
      path: '/api/jobs/job-1/unlock',
      method: 'POST',
    },
    { name: 'getJobRuns', call: () => api.getJobRuns('job-1'), path: '/api/jobs/job-1/runs' },
    {
      name: 'getJobSnapshots',
      call: () => api.getJobSnapshots('job-1'),
      path: '/api/jobs/job-1/snapshots',
    },
    {
      name: 'getJobCommands',
      call: () => api.getJobCommands('job-1'),
      path: '/api/jobs/job-1/commands',
    },
    { name: 'getRun', call: () => api.getRun('run-1'), path: '/api/runs/run-1' },
    { name: 'listSourceMounts', call: () => api.listSourceMounts(), path: '/api/mounts/sources' },
    {
      name: 'listDestinationMounts',
      call: () => api.listDestinationMounts(),
      path: '/api/mounts/destinations',
    },
    {
      name: 'renameDestination',
      call: () => api.renameDestination('old', 'new'),
      path: '/api/mounts/destinations/rename',
      method: 'POST',
      body: { old_label: 'old', new_label: 'new' },
    },
    { name: 'getSettings', call: () => api.getSettings(), path: '/api/settings' },
    {
      name: 'updateSettings',
      call: () => api.updateSettings({ ntfy_topic: 'backups' }),
      path: '/api/settings',
      method: 'PUT',
      body: { ntfy_topic: 'backups' },
    },
    {
      name: 'testNtfy',
      call: () => api.testNtfy(),
      path: '/api/settings/test-ntfy',
      method: 'POST',
    },
    {
      name: 'checkResticUpdate',
      call: () => api.checkResticUpdate(),
      path: '/api/settings/restic-update-check',
    },
    { name: 'getHealth', call: () => api.getHealth(), path: '/api/health' },
  ]

  for (const c of cases) {
    it(`${c.name} calls ${c.method ?? 'GET'} ${c.path}`, async () => {
      const fetchMock = mockFetch({ json: () => Promise.resolve({}) })

      await c.call()

      const { url, options } = callArgs(fetchMock)
      expect(url).toBe(c.path)
      expect(options.method).toBe(c.method)
      if (c.body === undefined) {
        expect(options.body).toBeUndefined()
      } else {
        expect(JSON.parse(options.body as string)).toEqual(c.body)
      }
    })
  }
})

// ── Query parameters ─────────────────────────────────────────────────────────

describe('query parameters', () => {
  it('getRecentRuns defaults to a limit of 10', async () => {
    const fetchMock = mockFetch({ json: () => Promise.resolve([]) })

    await api.getRecentRuns()

    expect(callArgs(fetchMock).url).toBe('/api/runs/recent?limit=10')
  })

  it('getRecentRuns passes an explicit limit through', async () => {
    const fetchMock = mockFetch({ json: () => Promise.resolve([]) })

    await api.getRecentRuns(50)

    expect(callArgs(fetchMock).url).toBe('/api/runs/recent?limit=50')
  })

  it('getDestinationUsage serves the cached measurement by default', async () => {
    const fetchMock = mockFetch({ json: () => Promise.resolve({ destinations: [] }) })

    await api.getDestinationUsage()

    // No ?refresh — the backend's 300s cache is the point on a page load; the
    // drives must not be spun up for a number nothing has changed.
    expect(callArgs(fetchMock).url).toBe('/api/mounts/destinations/usage')
  })

  it('getDestinationUsage forces a re-probe when refresh is requested', async () => {
    const fetchMock = mockFetch({ json: () => Promise.resolve({ destinations: [] }) })

    await api.getDestinationUsage(true)

    // What the Refresh button sends: a button handing back a five-minute-old
    // number reads as broken.
    expect(callArgs(fetchMock).url).toBe('/api/mounts/destinations/usage?refresh=true')
  })

  it('deleteJob keeps the repository by default', async () => {
    const fetchMock = mockFetch({ status: 204 })

    await api.deleteJob('job-1')

    const { url, options } = callArgs(fetchMock)
    // No ?delete_repository — the repo holds the snapshots and a job recreated
    // with the same name adopts it.
    expect(url).toBe('/api/jobs/job-1')
    expect(options.method).toBe('DELETE')
  })

  it('deleteJob asks for repository destruction only when told to', async () => {
    const fetchMock = mockFetch({ status: 204 })

    await api.deleteJob('job-1', true)

    expect(callArgs(fetchMock).url).toBe('/api/jobs/job-1?delete_repository=true')
  })
})

// ── The error contract ───────────────────────────────────────────────────────

describe('error contract', () => {
  it('throws with status and data so callers can pattern-match on the code', async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: () => Promise.resolve({ detail: 'Run already active' }),
    })

    // JobDetail branches on err.status === 409 to say "a run is already going"
    // rather than showing a generic failure.
    await expect(api.triggerRun('job-1')).rejects.toMatchObject({
      status: 409,
      data: { detail: 'Run already active' },
      message: 'Run already active',
    })
  })

  it('throws a real Error, not a bare object', async () => {
    mockFetch({ ok: false, status: 500, json: () => Promise.resolve({ detail: 'boom' }) })

    await expect(api.listJobs()).rejects.toBeInstanceOf(Error)
  })

  it('falls back to statusText when the error body is not JSON', async () => {
    // A 502 from a proxy answers with HTML, so resp.json() rejects. The caller
    // must still get a usable status and message instead of an unhandled parse
    // error surfacing as a blank screen.
    mockFetch({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: () => Promise.reject(new SyntaxError('Unexpected token <')),
    })

    await expect(api.listJobs()).rejects.toMatchObject({
      status: 502,
      message: 'Bad Gateway',
      data: { detail: 'Bad Gateway' },
    })
  })

  it('falls back to statusText when the JSON body carries no detail', async () => {
    mockFetch({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: () => Promise.resolve({ something_else: true }),
    })

    await expect(api.listJobs()).rejects.toMatchObject({
      status: 500,
      message: 'Internal Server Error',
      data: { something_else: true },
    })
  })

  it('preserves a validation detail string for inline display', async () => {
    mockFetch({
      ok: false,
      status: 422,
      json: () => Promise.resolve({ detail: 'name: must be a safe path component' }),
    })

    await expect(api.createJob({ name: '../etc' })).rejects.toMatchObject({
      status: 422,
      data: { detail: 'name: must be a safe path component' },
    })
  })

  /*
   * The three hand-written copies of the error block must stay identical.
   * `deleteJob` and `cancelRun` answer with an empty body on success (204/202)
   * so they cannot go through `request`'s `resp.json()` — which is exactly how
   * their error handling came to be duplicated, and how it could drift.
   */
  const throwers: Array<{ name: string; call: () => Promise<void> }> = [
    { name: 'deleteJob', call: () => api.deleteJob('job-1') },
    { name: 'cancelRun', call: () => api.cancelRun('run-1') },
  ]

  for (const t of throwers) {
    it(`${t.name} rejects with the same { status, data } shape as request()`, async () => {
      mockFetch({ ok: false, status: 409, json: () => Promise.resolve({ detail: 'nope' }) })

      await expect(t.call()).rejects.toMatchObject({
        status: 409,
        data: { detail: 'nope' },
        message: 'nope',
      })
    })

    it(`${t.name} falls back to statusText when the body is not JSON`, async () => {
      mockFetch({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: () => Promise.reject(new SyntaxError('not json')),
      })

      await expect(t.call()).rejects.toMatchObject({
        status: 503,
        message: 'Service Unavailable',
      })
    })
  }
})

// ── Body-less success responses ──────────────────────────────────────────────

describe('204/202 responses', () => {
  it('deleteJob resolves without parsing a body', async () => {
    // 204 No Content: calling resp.json() would reject. The stub makes that
    // failure loud rather than silent.
    const fetchMock = mockFetch({
      status: 204,
      json: () => Promise.reject(new Error('json() must not be called on a 204')),
    })

    await expect(api.deleteJob('job-1')).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('cancelRun resolves without parsing a body', async () => {
    const fetchMock = mockFetch({
      status: 202,
      json: () => Promise.reject(new Error('json() must not be called on a 202')),
    })

    await expect(api.cancelRun('run-1')).resolves.toBeUndefined()

    const { url, options } = callArgs(fetchMock)
    expect(url).toBe('/api/runs/run-1/cancel')
    expect(options.method).toBe('POST')
  })
})
