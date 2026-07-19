import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'

const read = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')

describe('PWA: index.html head tags', () => {
  const html = read('../../index.html')

  it('links the web manifest', () => {
    expect(html).toContain('<link rel="manifest" href="/static/manifest.webmanifest" />')
  })

  it('declares a theme-color', () => {
    expect(html).toContain('<meta name="theme-color" content="#FAFBFC" />')
  })

  it('declares an apple-touch-icon and standalone capability', () => {
    expect(html).toContain('<link rel="apple-touch-icon" href="/static/apple-touch-icon.png" />')
    expect(html).toContain('<meta name="apple-mobile-web-app-capable" content="yes" />')
    expect(html).toContain('<meta name="mobile-web-app-capable" content="yes" />')
  })

  it('keeps the existing favicon link intact', () => {
    expect(html).toContain('<link rel="icon" type="image/png" href="/src/assets/billa.png" />')
  })
})

describe('PWA: manifest', () => {
  const manifest = JSON.parse(read('../../public/manifest.webmanifest'))

  it('is a standalone app scoped to the site root', () => {
    expect(manifest.start_url).toBe('/')
    expect(manifest.scope).toBe('/')
    expect(manifest.display).toBe('standalone')
    expect(manifest.name).toBe('Billa-Gates')
  })

  it('provides 192, 512 and maskable icons under /static/', () => {
    const bySize = (size: string, purpose: string) =>
      manifest.icons.find((i: any) => i.sizes === size && i.purpose === purpose)

    expect(bySize('192x192', 'any')?.src).toBe('/static/icon-192.png')
    expect(bySize('512x512', 'any')?.src).toBe('/static/icon-512.png')
    expect(bySize('512x512', 'maskable')?.src).toBe('/static/icon-maskable-512.png')
    for (const icon of manifest.icons) {
      expect(icon.src.startsWith('/static/')).toBe(true)
    }
  })
})

describe('PWA: service worker cache policy', () => {
  const sw = read('../../public/sw.js')

  it('has a fetch handler (required for installability)', () => {
    expect(sw).toContain("addEventListener('fetch'")
  })

  it('never caches API responses (guards live-data CUJs)', () => {
    // The worker must only ever cache immutable hashed build assets, and must
    // not reference /api at all — API calls always go straight to the network.
    expect(sw).toContain('/static/assets/')
    expect(sw).not.toContain('/api')
  })
})

describe('PWA: icon assets exist', () => {
  it('ships the referenced PNG icons in public/', () => {
    for (const name of [
      'icon-192.png',
      'icon-512.png',
      'icon-maskable-512.png',
      'apple-touch-icon.png',
    ]) {
      const p = path.resolve(__dirname, '../../public', name)
      expect(fs.existsSync(p), `${name} should exist`).toBe(true)
      expect(fs.statSync(p).size).toBeGreaterThan(0)
    }
  })
})
