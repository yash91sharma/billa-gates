/// <reference types="vitest" />
import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

import pkg from './package.json' with { type: 'json' }

export default defineConfig({
  base: '/static/',
  // Surfaced in the sidebar footer. Read from package.json so the number
  // cannot drift from the one that was released.
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://localhost:12345',
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    clearMocks: true,
    // Screenshot tests require browser mode and are run via `npm run
    // screenshots`; exclude them from the default jsdom-only suite.
    exclude: ['node_modules/**', 'dist/**', 'src/screenshots/**'],
    pool: 'threads',
    poolOptions: {
      threads: {
        minThreads: 2,
        maxThreads: 8,
      },
    },
  },
})
