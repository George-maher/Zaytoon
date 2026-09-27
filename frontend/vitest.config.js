import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// The WebSocket hook touches `window`, `Audio`, `Blob` and `MediaRecorder`, so
// the suite runs in jsdom. Kept separate from vite.config.js so `vite build`
// stays free of test-only plugins.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    // jsdom defaults to http://localhost:3000/, which is the *nginx/Docker*
    // branch of the hook's URL resolution. Pin the dev port so the tests
    // exercise the same path a developer actually runs.
    environmentOptions: {
      jsdom: { url: 'http://localhost:5173/' },
    },
    globals: true,
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.test.{js,jsx}'],
    restoreMocks: true,
  },
})
