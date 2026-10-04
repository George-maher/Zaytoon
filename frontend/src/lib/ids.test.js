import { describe, it, expect, vi, afterEach } from 'vitest'
import { clientMsgId } from './ids'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('clientMsgId', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('produces a fresh v4 uuid per call', () => {
    const a = clientMsgId()
    const b = clientMsgId()
    expect(a).toMatch(UUID_V4)
    expect(b).toMatch(UUID_V4)
    // A dedupe id that repeats would silently merge two distinct turns.
    expect(a).not.toBe(b)
  })

  it('falls back to a manual v4 when crypto.randomUUID is unavailable', () => {
    // Some hardened browsers expose `crypto` and still refuse randomUUID;
    // the id is optional on the wire but must never throw and block a send.
    vi.stubGlobal('crypto', {})
    expect(clientMsgId()).toMatch(UUID_V4)
  })

  it('falls back when randomUUID itself throws', () => {
    vi.stubGlobal('crypto', {
      randomUUID() {
        throw new Error('blocked by policy')
      },
    })
    expect(clientMsgId()).toMatch(UUID_V4)
  })

  it('survives crypto being entirely absent', () => {
    vi.stubGlobal('crypto', undefined)
    expect(clientMsgId()).toMatch(UUID_V4)
  })
})
