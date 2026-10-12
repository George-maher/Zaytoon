import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Vite dev/build config. Test configuration lives in vitest.config.js so
// `vite build` never has to reason about the test environment.
// Extra hostnames the dev server may serve. Vite rejects a request whose Host
// header it does not recognise (DNS-rebinding protection), which is the right
// default and is why this is opt-in: a proxied preview serves the app under a
// generated hostname, so that hostname is passed in here rather than being
// allowlisted for everyone. Unset -> Vite's own strict default.
const extraHosts = (process.env.VITE_ALLOWED_HOSTS || '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean)

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    allowedHosts: extraHosts.length ? extraHosts : undefined,
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
      // The Security Workspace is a first-class screen, so it gets a proxy
      // entry like its siblings: without this the page works only when the
      // browser can reach :8000 directly, which a remote preview cannot.
      '/security': {
        target: 'http://localhost:8000',
      },
      // Same reason as '/security': the Virtual Machines screen is a
      // first-class page, and without an entry it only works when the browser
      // can reach :8000 directly.
      '/virtualization': {
        target: 'http://localhost:8000',
      },
    },
  },
})
