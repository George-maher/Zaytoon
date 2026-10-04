import { describe, it, expect } from 'vitest'
import { statusLabel } from './statusLabels'

/**
 * The status line is the app's answer to "what is happening right now", so
 * the precedence order is the contract: a screen-reader user and a sighted
 * user must be told the same thing, and a blocked-on-a-human turn must never
 * claim to be working.
 */
describe('statusLabel', () => {
  it('starts at Connecting… before the socket has ever been open', () => {
    expect(statusLabel({ connected: false, everConnected: false })).toBe('Connecting…')
  })

  it('says Reconnecting… once a live link has dropped', () => {
    // The distinction matters: "Connecting" is the first attempt, "Reconnect"
    // tells the user their session is trying to come back.
    expect(statusLabel({ connected: false, everConnected: true })).toBe('Reconnecting…')
  })

  it('puts a pending approval above everything else', () => {
    const label = statusLabel({
      connected: true,
      pendingConfirmation: { request_id: 'cfm_1' },
      isSpeaking: true,
      streaming: true,
      status: 'executing',
      activeTool: { name: 'git_push', label: 'Pushing' },
    })
    expect(label).toBe('Waiting for your approval…')
  })

  it('treats the backend awaiting_confirmation status as approval too', () => {
    expect(statusLabel({ connected: true, status: 'awaiting_confirmation' })).toBe(
      'Waiting for your approval…',
    )
  })

  it('prefers Speaking… while audio is playing, over streaming tokens', () => {
    expect(statusLabel({ connected: true, isSpeaking: true, streaming: true })).toBe('Speaking…')
  })

  it('reports Generating response… while tokens are arriving', () => {
    expect(statusLabel({ connected: true, streaming: true, status: 'thinking' })).toBe(
      'Generating response…',
    )
  })

  it('keys Planning… off the status frame, not off the plan length', () => {
    expect(statusLabel({ connected: true, status: 'planning', plan: [] })).toBe('Planning…')
    // A finished turn keeps its plan on screen until the transcript is
    // cleared — if the label keyed off plan.length the line would say
    // "Planning…" for the rest of the session.
    expect(statusLabel({ connected: true, status: 'done', plan: ['step one'] })).toBe('Ready')
  })

  it('names the tool it is calling, with its human label', () => {
    const label = statusLabel({
      connected: true,
      status: 'executing',
      intent: 'tools',
      activeTool: { name: 'git_push', label: 'Pushing to origin' },
    })
    expect(label).toBe('Calling Pushing to origin…')
  })

  it('falls back to the tool name when the backend sent no label', () => {
    const label = statusLabel({
      connected: true,
      status: 'executing',
      intent: 'tools',
      activeTool: { name: 'get_time' },
    })
    expect(label).toBe('Calling get_time…')
  })

  it('distinguishes a mid-plan step from generic thinking', () => {
    // Executing with an announced plan: the engine is between steps.
    expect(statusLabel({ connected: true, status: 'executing', plan: ['a', 'b'] })).toBe('Planning…')
    expect(statusLabel({ connected: true, status: 'executing' })).toBe('Thinking…')
  })

  it('says Understanding… when the router has already named an intent', () => {
    expect(statusLabel({ connected: true, status: 'thinking', intent: 'scheduling' })).toBe(
      'Understanding…',
    )
    expect(statusLabel({ connected: true, status: 'thinking' })).toBe('Thinking…')
  })

  it('reports Transcribing… during speech-to-text', () => {
    expect(statusLabel({ connected: true, status: 'transcribing' })).toBe('Transcribing…')
  })

  it('returns to Ready when the turn is over', () => {
    for (const status of ['done', 'connected', 'awaiting_command', 'listening', '']) {
      // awaiting_command has its own word — checked below — the rest are ready.
      if (status === 'awaiting_command') continue
      expect(statusLabel({ connected: true, status })).toBe('Ready')
    }
    expect(statusLabel({ connected: true, status: 'awaiting_command' })).toBe('Say your command…')
  })

  it('reads an open socket with no server status yet as Ready', () => {
    // The hook invents no status on open (the server sends one), so the
    // initial "disconnected" string can sit next to a live socket. That must
    // not render as a phantom "Reconnecting…".
    expect(statusLabel({ connected: true, everConnected: true, status: 'disconnected' })).toBe('Ready')
  })

  it('never claims a working state while the socket is down', () => {
    expect(
      statusLabel({ connected: false, everConnected: true, status: 'thinking', isTyping: true }),
    ).toBe('Reconnecting…')
  })

  it('translates every state into Arabic', () => {
    expect(statusLabel({ connected: true, language: 'ar' })).toBe('جاهز')
    expect(statusLabel({ connected: false, everConnected: false, language: 'ar' })).toBe('أتّصل…')
    expect(
      statusLabel({ connected: true, pendingConfirmation: {}, language: 'ar' }),
    ).toBe('في انتظار موافقتك…')
    expect(statusLabel({ connected: true, streaming: true, language: 'ar' })).toBe('أكتب الرد…')
  })
})
