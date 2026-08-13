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
import pkg from './package.json' with { type: 'json' }
import { writeScreenshotIfChanged } from './src/screenshots/write-if-changed'

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
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
      screenshotDirectory: 'screenshots',
      instances: [{ browser: 'chromium' }],
      // The tests capture through `capture()` (src/screenshots/capture.ts),
      // which routes the bytes here so an unchanged image is left untouched
      // instead of rewritten. Without it every run touched all 40 files and the
      // ones that actually moved were impossible to spot.
      commands: { writeScreenshotIfChanged },
    },
  },
})
