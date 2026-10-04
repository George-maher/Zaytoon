import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import ExecutionHistory from './ExecutionHistory'

/**
 * The audit log and the process registry are *optional* endpoints: a backend
 * that has not shipped them answers 404. The whole point of this component is
 * how it behaves in that case — "Audit log unavailable" is information, a
 * hidden processes card is information, and neither may look like an error
 * or like an empty log that reads as "nothing ever ran".
 */
const entries = [
  { id: 1, time: '2026-10-03T12:00:00Z', action: 'file_write', detail: 'notes.md', success: true },
  { id: 2, timestamp: 1759430000, tool: 'system_command', summary: 'exit 1', success: false },
]

function mockFetch({ audit, processes, clear } = {}) {
  global.fetch = vi.fn(async (url, opts = {}) => {
    const u = String(url)
    const method = opts.method || 'GET'
    if (u.includes('/audit') && method === 'DELETE') {
      if (clear?.status === 404) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => ({ cleared: clear?.cleared ?? 2 }) }
    }
    if (u.includes('/audit')) {
      if (audit?.status === 404) return { ok: false, status: 404, json: async () => ({}) }
      if (audit?.throw) throw new Error('Failed to fetch')
      return { ok: true, status: 200, json: async () => audit ?? { entries: [], total: 0 } }
    }
    if (u.includes('/processes') && method === 'POST') {
      return { ok: true, status: 200, json: async () => ({ stopped: true }) }
    }
    if (u.includes('/processes')) {
      if (processes?.status === 404) return { ok: false, status: 404, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => processes ?? { processes: [] } }
    }
    return { ok: true, status: 200, json: async () => ({}) }
  })
  return global.fetch
}

describe('ExecutionHistory', () => {
  beforeEach(() => {
    mockFetch()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists each entry with an action, a status chip and its detail', async () => {
    mockFetch({ audit: { entries, total: 2 } })
    render(<ExecutionHistory language="en" />)

    expect(await screen.findByText('file_write')).toBeInTheDocument()
    expect(screen.getByText('notes.md')).toBeInTheDocument()
    expect(screen.getByText('system_command')).toBeInTheDocument()
    expect(screen.getByText('exit 1')).toBeInTheDocument()
    // Chips carry a word as well as a colour.
    expect(screen.getByText('OK')).toBeInTheDocument()
    expect(screen.getByText('FAILED')).toBeInTheDocument()
    expect(screen.getByText('TOTAL 2')).toBeInTheDocument()
  })

  it('shows an empty log as empty, not as an error', async () => {
    mockFetch({ audit: { entries: [], total: 0 } })
    render(<ExecutionHistory language="en" />)
    expect(await screen.findByText('No executions recorded yet')).toBeInTheDocument()
  })

  it('reports a 404 as "Audit log unavailable" rather than an empty log', async () => {
    mockFetch({ audit: { status: 404 } })
    render(<ExecutionHistory language="en" />)

    expect(await screen.findByText('Audit log unavailable')).toBeInTheDocument()
    expect(screen.queryByText('No executions recorded yet')).not.toBeInTheDocument()
    // Nothing ran that could be cleared, so the destructive control is gone.
    expect(screen.queryByText('CLEAR')).not.toBeInTheDocument()
  })

  it('surfaces a real fetch failure with the underlying message', async () => {
    mockFetch({ audit: { throw: true } })
    render(<ExecutionHistory language="en" />)
    expect(await screen.findByText(/Audit fetch failed: Failed to fetch/)).toBeInTheDocument()
  })

  it('clears the log only after an explicit second confirmation', async () => {
    const fetchMock = mockFetch({ audit: { entries, total: 2 } })
    render(<ExecutionHistory language="en" />)
    await screen.findByText('file_write')

    // Step 1: arm. Nothing destructive happens here.
    fireEvent.click(screen.getByText('CLEAR'))
    expect(screen.getByText(/Clear all 2 entries\? This cannot be undone\./)).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(([url, opts]) => opts?.method === 'DELETE' && String(url).includes('/audit')),
    ).toBe(false)

    // Step 2: confirm. Only now does DELETE go out, and the list refreshes.
    await act(async () => {
      fireEvent.click(screen.getByText('CONFIRM'))
    })
    expect(
      fetchMock.mock.calls.some(([url, opts]) => opts?.method === 'DELETE' && String(url).includes('/audit')),
    ).toBe(true)
    expect(await screen.findByText('Cleared 2 entries')).toBeInTheDocument()
    expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument()
  })

  it('cancels a pending clear without calling the endpoint', async () => {
    const fetchMock = mockFetch({ audit: { entries, total: 2 } })
    render(<ExecutionHistory language="en" />)
    await screen.findByText('file_write')

    fireEvent.click(screen.getByText('CLEAR'))
    fireEvent.click(screen.getByText('CANCEL'))
    expect(screen.queryByText(/This cannot be undone/)).not.toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(([, opts]) => opts?.method === 'DELETE'),
    ).toBe(false)
  })

  it('hides the processes card entirely when the endpoint is 404', async () => {
    mockFetch({ audit: { entries, total: 1 }, processes: { status: 404 } })
    render(<ExecutionHistory language="en" />)
    await screen.findByText('file_write')
    // Not "PROCESSES — None": the card does not exist, because the backend
    // does not have the registry at all.
    expect(screen.queryByText('PROCESSES')).not.toBeInTheDocument()
    expect(screen.getByText('file_write')).toBeInTheDocument()
  })

  it('stops a process through POST /processes/{id}/stop', async () => {
    const fetchMock = mockFetch({
      audit: { entries: [], total: 0 },
      processes: { processes: [{ id: 7, name: 'whisper', status: 'running' }] },
    })
    render(<ExecutionHistory language="en" />)
    expect(await screen.findByText('whisper')).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByText('STOP'))
    })
    const call = fetchMock.mock.calls.find(
      ([url, opts]) => opts?.method === 'POST' && String(url).includes('/processes/7/stop'),
    )
    expect(call).toBeTruthy()
  })

  it('renders the section in Arabic when asked', async () => {
    mockFetch({ audit: { status: 404 } })
    render(<ExecutionHistory language="ar" />)
    expect(await screen.findByText('سجل العمليات غير متاح')).toBeInTheDocument()
    expect(screen.getByText('سجل التنفيذ')).toBeInTheDocument()
  })
})
