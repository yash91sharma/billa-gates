/// <reference types="vitest" />
import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
// Vitest 4 takes `browser.provider` as a factory rather than the string
// 'playwright' it accepted in v3; the provider itself moved out into this
// package. A stale string here is a startup error, not a silent fallback.
import { playwright } from '@vitest/browser-playwright'

/**
 * Screenshot test config — runs tests in a real headless Chromium via
 * Playwright instead of jsdom, so we can capture pixel-perfect PNGs of
 * pages and components.
 *
 * Kept separate from vite.config.ts (jsdom unit tests) because browser
 * mode is much slower to boot and we only want to pay that cost when
 * generating screenshots, not on every unit-test run.
 */
import { writeScreenshotIfChanged } from './src/screenshots/write-if-changed'

/**
 * Frozen on purpose — this is deliberately **not** `pkg.version` the way
 * vite.config.ts has it.
 *
 * `Sidebar` prints `v${__APP_VERSION__}`, and the sidebar is in 17 of the 44
 * captures, so compiling the real version in meant every version bump rewrote
 * all 17 — a review list of files whose only difference was three digits in a
 * corner, which is exactly the noise `capture()`'s write-if-changed exists to
 * remove. The suite asserts nothing; its whole value is that a changed file
 * means a changed design, and a version string breaks that.
 *
 * It keeps a real version's shape (same digit count) so the footer's layout
 * stays representative, while reading as an obvious placeholder to anyone
 * looking at the gallery. `src/screenshots/determinism.test.ts` pins it.
 */
const FROZEN_APP_VERSION = '0.0.0'

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(FROZEN_APP_VERSION) },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  // Tailwind must be wired up here too, not just in vite.config.ts: this is a
  // standalone Vite config, and since v4 there is no postcss.config.js for it
  // to pick the framework up from. Without it every PNG renders unstyled.
  plugins: [react(), tailwindcss()],
  test: {
    globals: true,
    setupFiles: ['./src/test/screenshot-setup.ts'],
    include: ['src/screenshots/**/*.test.{ts,tsx}'],
    clearMocks: true,
    // CSS must be loaded for accurate visual rendering (the jsdom unit-test
    // config disables it for speed; screenshot tests need real styles).
    css: true,
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      // Where vitest drops its *own* screenshots — the automatic one it takes
      // when a test fails (`screenshotFailures`, on by default). Deliberately
      // outside `screenshots/`, which is the reviewed gallery: pointed at it,
      // a failing run buried 13 files named after test titles
      // (`screenshots/src/screenshots/pages.test.tsx/Jobs---populated-1.png`)
      // in the tree, where they were committed and then outlived the failure
      // that produced them. This path is gitignored, so failure shots stay
      // available for debugging without ever entering the review list.
      screenshotDirectory: '.vitest-screenshot-failures',
      instances: [{ browser: 'chromium' }],
      // The tests capture through `capture()` (src/screenshots/capture.ts),
      // which routes the bytes here so an unchanged image is left untouched
      // instead of rewritten. Without it every run touched all 40 files and the
      // ones that actually moved were impossible to spot.
      commands: { writeScreenshotIfChanged },
    },
  },
})
