import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
})

// jsdom implements neither the Web Audio API nor MediaRecorder, both of which
// the voice pipeline in `useWebSocket` touches. Stub the minimum surface so a
// component test fails on its own logic rather than on a missing browser API.

if (!globalThis.AudioContext) {
  globalThis.AudioContext = class {
    constructor() {
      this.sampleRate = 24000
      this.state = 'running'
      this.destination = {}
      this.createMediaStreamSource = () => ({ connect: vi.fn() })
      this.createAnalyser = () => ({
        fftSize: 2048,
        frequencyBinCount: 1024,
        connect: vi.fn(),
        disconnect: vi.fn(),
        getByteTimeDomainData: vi.fn(),
      })
      this.createGain = () => ({ gain: { value: 1 }, connect: vi.fn() })
      this.close = vi.fn().mockResolvedValue(undefined)
      this.resume = vi.fn().mockResolvedValue(undefined)
    }
  }
}

if (!globalThis.webkitAudioContext) {
  globalThis.webkitAudioContext = globalThis.AudioContext
}

if (!globalThis.MediaRecorder) {
  globalThis.MediaRecorder = class {
    constructor() {
      this.state = 'inactive'
      this.mimeType = 'audio/webm'
    }
    start() {
      this.state = 'recording'
    }
    stop() {
      this.state = 'inactive'
      if (this.ondataavailable) this.ondataavailable({ data: new Blob([]) })
      if (this.onstop) this.onstop()
    }
  }
  globalThis.MediaRecorder.isTypeSupported = () => true
}

if (!globalThis.navigator.mediaDevices) {
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    writable: true,
    value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [] }) },
  })
}

// `HTMLMediaElement.play` rejects in jsdom because there is no media pipeline.
// The hook treats a rejection as a playback failure, so silence it.
if (window.HTMLMediaElement) {
  window.HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined)
  window.HTMLMediaElement.prototype.pause = vi.fn()
}
