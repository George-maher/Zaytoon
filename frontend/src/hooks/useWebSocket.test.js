/**
 * The WebSocket protocol is the seam between this UI and the backend, and it
 * was completely untested. Two clients depend on it (this browser app and the
 * Python desktop shell), so a silent drift in a message name breaks the product
 * in a way no build step would catch.
 *
 * These tests drive the real hook against a fake socket and assert the exact
 * frames sent and the state transitions produced by each server event.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import useWebSocket from './useWebSocket'

/**
 * Minimal scriptable WebSocket double.
 *
 * The static CONNECTING/OPEN/CLOSING/CLOSED constants are part of the real
 * WebSocket API and are declared here so the double is faithful. The hook no
 * longer reads them (it compares against the literal 1 via `isSocketOpen`),
 * but a double that omits them would exercise the wrong code paths.
 */
class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  static instances = []

  constructor(url) {
    this.url = url
    this.readyState = FakeSocket.CONNECTING
    this.sent = []
    this.closed = null
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    FakeSocket.instances.push(this)
  }

  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }

  /** Deliver a server frame. */
  emit(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }

  send(raw) {
    this.sent.push(JSON.parse(raw))
  }

  /** Every frame of a given type. */
  sentOfType(type) {
    return this.sent.filter((m) => m.type === type)
  }

  close(code, reason) {
    this.readyState = FakeSocket.CLOSED
    this.closed = { code, reason }
    this.onclose?.({ code, reason })
  }
}

/** Render the hook and return its latest result plus the live socket. */
async function mount() {
  const view = renderHook(() => useWebSocket())
  await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(0))
  const socket = FakeSocket.instances[FakeSocket.instances.length - 1]
  await act(async () => {
    socket.open()
  })
  return { result: view.result, socket, view }
}

beforeEach(() => {
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('requestAnimationFrame', (cb) => setTimeout(() => cb(0), 0))
  vi.stubGlobal('cancelAnimationFrame', (id) => clearTimeout(id))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
describe('connection lifecycle', () => {
  it('connects to the backend WebSocket and reports connected', async () => {
    const { result, socket } = await mount()
    expect(socket.url).toMatch(/^ws:\/\/[^/]+:8000\/ws$/)
    await waitFor(() => expect(result.current.isConnected).toBe(true))
  })

  it('exposes the status the backend reports on connect', async () => {
    // The hook does not invent a status on open; the server sends one.
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: 'connected' }))
    expect(result.current.status).toBe('connected')
  })
})

// ---------------------------------------------------------------------------
describe('outbound frames', () => {
  it('sends a chat message', async () => {
    const { result, socket } = await mount()
    act(() => result.current.sendMessage('hello there'))
    expect(socket.sentOfType('message')).toEqual([
      { type: 'message', message: 'hello there' },
    ])
  })

  it('refuses to send while disconnected rather than dropping silently', async () => {
    const { result, socket } = await mount()
    socket.readyState = 3
    act(() => result.current.sendMessage('nope'))
    expect(socket.sentOfType('message')).toHaveLength(0)
  })

  it('answers a confirmation with the matching request id', async () => {
    const { result, socket } = await mount()

    act(() => {
      socket.emit({
        type: 'require_confirmation',
        request_id: 'cfm_abc123',
        action: 'system_command',
        params: { command: 'npm install' },
        level: 'high',
        permission: 'confirm',
        reasons: ['arbitrary terminal execution'],
        summary: 'Run command: npm install',
        text: 'Run command: npm install — arbitrary terminal execution',
        session_id: 's1',
      })
    })

    await waitFor(() =>
      expect(result.current.pendingConfirmation?.request_id).toBe('cfm_abc123')
    )

    act(() => result.current.respondToConfirmation(true))

    expect(socket.sentOfType('tool_confirm')).toEqual([
      { type: 'tool_confirm', request_id: 'cfm_abc123', approved: true },
    ])
  })

  it('denials are sent as approved:false, not omitted', async () => {
    const { result, socket } = await mount()

    act(() => {
      socket.emit({
        type: 'require_confirmation',
        request_id: 'cfm_denied',
        action: 'file_delete',
        params: { path: 'x.txt' },
        level: 'high',
        permission: 'dangerous',
        reasons: ['destructive'],
        summary: 'Delete: x.txt',
        text: 'Delete: x.txt',
      })
    })

    await waitFor(() => expect(result.current.pendingConfirmation).toBeTruthy())
    act(() => result.current.respondToConfirmation(false))

    expect(socket.sentOfType('tool_confirm')[0]).toEqual({
      type: 'tool_confirm',
      request_id: 'cfm_denied',
      approved: false,
    })
  })
})

// ---------------------------------------------------------------------------
describe('inbound: streaming tokens', () => {
  it('accumulates tokens into one streaming assistant bubble', async () => {
    const { result, socket } = await mount()
    const responseId = 'r1'

    act(() => {
      socket.emit({ type: 'token', content: 'Hello', response_id: responseId })
      socket.emit({ type: 'token', content: ' world', response_id: responseId })
    })

    const assistant = result.current.messages.filter((m) => m.role === 'assistant')
    expect(assistant).toHaveLength(1)
    expect(assistant[0].content).toBe('Hello world')
    expect(assistant[0].streaming).toBe(true)
  })

  it('the final message event replaces the streamed bubble', async () => {
    const { result, socket } = await mount()
    const responseId = 'r1'

    act(() => {
      socket.emit({ type: 'token', content: 'partial', response_id: responseId })
      socket.emit({ type: 'message', content: 'partial and complete', response_id: responseId })
    })

    const assistant = result.current.messages.filter((m) => m.role === 'assistant')
    expect(assistant).toHaveLength(1)
    expect(assistant[0].content).toBe('partial and complete')
    expect(assistant[0].streaming).toBe(false)
  })

  it('keeps consecutive turns as separate bubbles', async () => {
    const { result, socket } = await mount()

    act(() => {
      socket.emit({ type: 'message', content: 'first', response_id: 'r1' })
      socket.emit({ type: 'message', content: 'second', response_id: 'r2' })
    })

    const assistant = result.current.messages.filter((m) => m.role === 'assistant')
    expect(assistant.map((m) => m.content)).toEqual(['first', 'second'])
  })

  it('ignores a duplicate final message for the same response id', async () => {
    // The backend replays the authoritative text after a mid-stream model
    // failure; the client must not render it twice.
    const { result, socket } = await mount()

    act(() => {
      socket.emit({ type: 'message', content: 'once', response_id: 'r1' })
      socket.emit({ type: 'message', content: 'once', response_id: 'r1' })
    })

    const assistant = result.current.messages.filter((m) => m.role === 'assistant')
    expect(assistant).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
describe('inbound: status vocabulary', () => {
  // The backend emits exactly these. The hook used to set the status to
  // "processing...", which is not among them, so the HUD rendered a literal
  // "MODE PROCESSING..." label with a default-coloured dot.
  const BACKEND_STATUSES = [
    'connected',
    'transcribing',
    'thinking',
    'listening',
    'speaking',
    'done',
    'awaiting_command',
    'awaiting_confirmation',
  ]

  it.each(BACKEND_STATUSES)('accepts the %s status', async (status) => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: status }))
    expect(result.current.status).toBe(status)
  })

  it('does not invent a status outside the backend vocabulary', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: 'processing...' }))
    // Whatever it renders, it must be one of the real ones.
    expect(BACKEND_STATUSES).toContain(result.current.status)
  })

  it('marks the assistant as typing while thinking', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: 'thinking' }))
    expect(result.current.isTyping).toBe(true)
  })

  it('clears typing on done', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: 'thinking' }))
    act(() => socket.emit({ type: 'status', content: 'done' }))
    expect(result.current.isTyping).toBe(false)
  })
})

// ---------------------------------------------------------------------------
describe('inbound: confirmation flow', () => {
  it('surfaces the permission tier so the UI can render it', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({
        type: 'require_confirmation',
        request_id: 'cfm_x',
        action: 'file_delete',
        params: { path: 'a.txt' },
        level: 'high',
        permission: 'dangerous',
        reasons: ['destructive: deletes data'],
        summary: 'Delete: a.txt',
        text: 'Delete: a.txt',
      })
    })
    await waitFor(() => expect(result.current.pendingConfirmation).toBeTruthy())
    expect(result.current.pendingConfirmation.permission).toBe('dangerous')
    expect(result.current.pendingConfirmation.params).toEqual({ path: 'a.txt' })
  })

  it('enters the awaiting_confirmation status', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({
        type: 'require_confirmation',
        request_id: 'cfm_x',
        action: 'x',
        params: {},
        level: 'high',
        permission: 'confirm',
        reasons: [],
        summary: 's',
        text: 't',
      })
    })
    await waitFor(() => expect(result.current.status).toBe('awaiting_confirmation'))
  })

  it('clears the pending request once resolved', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({
        type: 'require_confirmation',
        request_id: 'cfm_x',
        action: 'x',
        params: {},
        level: 'high',
        permission: 'confirm',
        reasons: [],
        summary: 's',
        text: 't',
      })
    })
    await waitFor(() => expect(result.current.pendingConfirmation).toBeTruthy())

    act(() => {
      socket.emit({
        type: 'confirmation_result',
        request_id: 'cfm_x',
        approved: true,
        resolved: true,
      })
    })
    await waitFor(() => expect(result.current.pendingConfirmation).toBeNull())
  })
})

// ---------------------------------------------------------------------------
describe('inbound: voice', () => {
  it('adopts the language Whisper detected', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'stt_result', language: 'ar', voice: 'ar-EG-ShakirNeural', duration: 1.2 }))
    expect(result.current.language).toBe('ar')
  })

  it('records the speaking voice from audio_start', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'audio_start', language: 'en', voice: 'en-GB-RyanNeural', format: 'mpeg' }))
    expect(result.current.speakingVoice).toBe('en-GB-RyanNeural')
  })

  it('shows a subtitle', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'subtitle', content: 'a subtitle' }))
    expect(result.current.subtitle).toBe('a subtitle')
  })

  it('arms the follow-up window when the wake word is heard', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'wake_word', detected: true }))
    expect(result.current.awaitingCommand).toBe(true)
  })

  it('treats a missed wake word as a hint, not a command', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'wake_word', detected: false }))
    expect(result.current.awaitingCommand).toBe(false)
    expect(
      result.current.messages.some((m) => m.role === 'system')
    ).toBe(true)
  })
})

// ---------------------------------------------------------------------------
describe('inbound: reminders', () => {
  it('stores the reminder list', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({
        type: 'reminder_list',
        reminders: [{ reminder_id: 1, task: 'call mum', scheduled_time: '2026-01-01T09:00' }],
      })
    })
    await waitFor(() => expect(result.current.reminders).toHaveLength(1))
    expect(result.current.reminders[0].task).toBe('call mum')
  })

  it('removes a deleted reminder from the list', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({ type: 'reminder_list', reminders: [{ reminder_id: 1 }, { reminder_id: 2 }] })
    })
    await waitFor(() => expect(result.current.reminders).toHaveLength(2))

    act(() => socket.emit({ type: 'reminder_deleted', id: 1, success: true, message: 'Deleted.' }))
    await waitFor(() => expect(result.current.reminders).toHaveLength(1))
    expect(result.current.reminders[0].reminder_id).toBe(2)
  })

  it('sends a reminder command', async () => {
    const { result, socket } = await mount()
    act(() => result.current.sendReminderCmd('list'))
    expect(socket.sentOfType('reminder_cmd')[0]).toMatchObject({
      type: 'reminder_cmd',
      action: 'list',
    })
  })
})

// ---------------------------------------------------------------------------
describe('robustness', () => {
  it('survives a malformed frame without tearing down the connection', async () => {
    const { result, socket } = await mount()
    socket.onmessage({ data: 'not json at all' })
    expect(result.current.isConnected).toBe(true)
  })

  it('ignores an unknown event type', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'something_new', payload: 1 }))
    expect(result.current.isConnected).toBe(true)
  })

  it('surfaces an error event as a system message', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'error', content: 'backend exploded' }))
    expect(
      result.current.messages.some((m) => m.content.includes('backend exploded'))
    ).toBe(true)
  })

  it('resets transient state on interrupt', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'status', content: 'thinking' }))
    act(() => socket.emit({ type: 'interrupted' }))
    expect(result.current.isTyping).toBe(false)
    expect(result.current.isSpeaking).toBe(false)
  })
})
