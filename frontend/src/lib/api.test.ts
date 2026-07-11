import { afterEach, describe, expect, it, vi } from 'vitest'
import { deleteJob } from './api'

// These tests exercise the REAL api module (no vi.mock) against a stubbed
// global fetch, because the defect they guard against lives in the fetch
// wrapper itself: a non-ok DELETE response must reject, not resolve.

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('deleteJob', () => {
  it('resolves on 204 No Content', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(deleteJob('job-1')).resolves.toBeUndefined()
  })

  it('rejects with status and detail on 409 (run in progress)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'A backup run is in progress for this job' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        })
      )
    )
    await expect(deleteJob('job-1')).rejects.toMatchObject({
      status: 409,
      data: { detail: 'A backup run is in progress for this job' },
    })
  })

  it('rejects with status on non-JSON error responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('gateway error', { status: 502 }))
    )
    await expect(deleteJob('job-1')).rejects.toMatchObject({ status: 502 })
  })
})
