import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import type { BrowserCommand } from 'vitest/node'

/**
 * The Node half of `capture()` — writes a PNG only when its bytes differ from
 * what is already on disk.
 *
 * `page.screenshot({ path })` writes unconditionally, so every run rewrote all
 * 40 images even though the pixels were identical (verified: two consecutive
 * runs, and a run with a cold Vite cache, produce byte-identical output). That
 * churn is the problem: 40 touched files hide the one or two that actually
 * changed, which is the whole point of a screenshot suite nobody asserts on —
 * the review is you looking at what moved.
 *
 * Runs in Node rather than in the test because the test executes inside
 * Chromium and has no filesystem. Registered as a browser command in
 * vitest.screenshots.config.ts.
 */
export const writeScreenshotIfChanged: BrowserCommand<[string, string]> = async (
  ctx,
  relativePath,
  base64
) => {
  // Resolved against the test file's directory, exactly as `page.screenshot`
  // resolves its `path` option — so the existing OUT constants keep meaning
  // what they meant.
  if (!ctx.testPath) {
    throw new Error('writeScreenshotIfChanged: no testPath on the command context')
  }
  const target = path.resolve(path.dirname(ctx.testPath), relativePath)

  // A command reachable from browser-side code takes a path from the browser,
  // so it refuses to write anywhere but the screenshots tree, and only PNGs.
  const root = path.resolve(path.dirname(ctx.testPath), '../../screenshots')
  if (!target.startsWith(root + path.sep) || path.extname(target) !== '.png') {
    throw new Error(`writeScreenshotIfChanged: refusing to write outside ${root}: ${target}`)
  }

  const next = Buffer.from(base64, 'base64')
  if (existsSync(target) && readFileSync(target).equals(next)) {
    return 'unchanged'
  }

  mkdirSync(path.dirname(target), { recursive: true })
  writeFileSync(target, next)
  // Named on stdout because this is the run's actual result: what moved.
  console.log(`screenshot updated: ${path.relative(root, target)}`)
  return 'written'
}
