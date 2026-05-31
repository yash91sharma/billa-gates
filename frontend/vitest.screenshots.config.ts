/// <reference types="vitest" />
import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Screenshot test config — runs tests in a real headless Chromium via
 * Playwright instead of jsdom, so we can capture pixel-perfect PNGs of
 * pages and components.
 *
 * Kept separate from vite.config.ts (jsdom unit tests) because browser
 * mode is much slower to boot and we only want to pay that cost when
 * generating screenshots, not on every unit-test run.
 */
export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [react()],
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
      provider: 'playwright',
      screenshotDirectory: 'screenshots',
      instances: [{ browser: 'chromium' }],
    },
  },
})
