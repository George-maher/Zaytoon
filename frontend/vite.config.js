import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Vite dev/build config. Test configuration lives in vitest.config.js so
// `vite build` never has to reason about the test environment.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      '/health': {
        target: 'http://localhost:8000',
      },
      '/settings': {
        target: 'http://localhost:8000',
      },
      '/command': {
        target: 'http://localhost:8000',
      },
      '/stt': {
        target: 'http://localhost:8000',
      },
      '/chat': {
        target: 'http://localhost:8000',
      },
      '/tasks': {
        target: 'http://localhost:8000',
      },
    },
  },
})
