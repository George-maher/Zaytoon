import { describe, it, expect } from 'vitest'
import { deriveToolRows, formatDuration } from './toolRows'

/**
 * The activity feed folds a flat event log into one row per tool
 * invocation. These tests are the specification for that fold: heartbeat
 * noise must collapse, concurrency must not bleed between rows, and a
 * duration must come from the stamps the hook put on the events — never
 * from a clock inside the reducer.
 */
describe('deriveToolRows', () => {
  it('turns a started event into a running row', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'git_commit', label: 'Committing', at: 1000 },
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      name: 'git_commit',
      label: 'Committing',
      status: 'running',
      startedAt: 1000,
      message: '',
    })
  })

  it('folds heartbeats into the running row instead of adding rows', () => {
    // The old line-per-event feed printed one row per progress frame.
    const rows = deriveToolRows([
      { kind: 'started', name: 'run_npm', label: 'Running tests', at: 10 },
      { kind: 'progress', name: 'run_npm', message: 'jest 42%' },
      { kind: 'progress', name: 'run_npm', message: 'jest 80%' },
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].status).toBe('running')
    expect(rows[0].message).toBe('jest 80%')
  })

  it('closes a row as ok with a duration derived from the event stamps', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'file_write', at: 2000 },
      { kind: 'finished', name: 'file_write', success: true, summary: 'wrote notes.md', at: 2820 },
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].status).toBe('ok')
    expect(rows[0].durationMs).toBe(820)
    expect(rows[0].summary).toBe('wrote notes.md')
    expect(rows[0].message).toBe('')
  })

  it("prefers the backend's own duration_ms over the client stamps", () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'screenshot', at: 0 },
      { kind: 'finished', name: 'screenshot', success: true, durationMs: 1500, at: 10 },
    ])
    expect(rows[0].durationMs).toBe(1500)
  })

  it('closes a row as failed and keeps the reason', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'git_push', at: 1 },
      { kind: 'finished', name: 'git_push', success: false, summary: 'rejected: non-fast-forward', at: 2 },
    ])
    expect(rows[0].status).toBe('failed')
    expect(rows[0].summary).toContain('non-fast-forward')
  })

  it('truncates an over-long summary so one failure cannot fill the rail', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'x', at: 1 },
      { kind: 'finished', name: 'x', success: false, summary: 'e'.repeat(500), at: 2 },
    ])
    expect(rows[0].summary).toHaveLength(160)
  })

  it('keeps two concurrent tools in two independent rows', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'a', label: 'A', at: 1 },
      { kind: 'started', name: 'b', label: 'B', at: 2 },
      { kind: 'finished', name: 'a', success: true, at: 3 },
    ])
    expect(rows).toHaveLength(2)
    expect(rows[0].status).toBe('ok')
    // Finishing `a` must not touch `b` — the bug a per-event log could hide.
    expect(rows[1].status).toBe('running')
    expect(rows[1].message).toBe('')
  })

  it('opens a new row for a second invocation of the same tool', () => {
    const rows = deriveToolRows([
      { kind: 'started', name: 'system_command', at: 1 },
      { kind: 'finished', name: 'system_command', success: true, at: 2 },
      { kind: 'started', name: 'system_command', at: 3 },
    ])
    expect(rows).toHaveLength(2)
    expect(rows[0].status).toBe('ok')
    expect(rows[1].status).toBe('running')
  })

  it('recovers from a progress or finish frame with no observed start', () => {
    // A tool whose `started` frame the client joined late still deserves a
    // row rather than being dropped.
    const progress = deriveToolRows([{ kind: 'progress', name: 'whisper', message: 'decoding' }])
    expect(progress).toHaveLength(1)
    expect(progress[0].status).toBe('running')

    const finish = deriveToolRows([
      { kind: 'finished', name: 'whisper', success: true, summary: 'ok' },
    ])
    expect(finish).toHaveLength(1)
    expect(finish[0].status).toBe('ok')
  })

  it('ignores malformed events rather than throwing in a render', () => {
    expect(deriveToolRows([null, {}, { kind: 'started' }])).toEqual([])
    expect(deriveToolRows()).toEqual([])
  })

  it('yields stable keys for the same event list', () => {
    const events = [
      { kind: 'started', name: 'a', at: 1 },
      { kind: 'started', name: 'b', at: 2 },
    ]
    expect(deriveToolRows(events).map((r) => r.key)).toEqual(deriveToolRows(events).map((r) => r.key))
  })
})

describe('formatDuration', () => {
  it('formats sub-second and multi-second durations', () => {
    expect(formatDuration(820)).toBe('820ms')
    expect(formatDuration(1400)).toBe('1.4s')
    expect(formatDuration(0)).toBe('0ms')
  })

  it('returns null when there is no duration to show', () => {
    // A chip that says "null" or "NaNms" is worse than no chip.
    expect(formatDuration(null)).toBeNull()
    expect(formatDuration(undefined)).toBeNull()
    expect(formatDuration(-5)).toBeNull()
    expect(formatDuration(Number.NaN)).toBeNull()
  })
})
