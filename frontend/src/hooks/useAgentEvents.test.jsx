import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, renderHook, waitFor, act } from '@testing-library/react'
import useWebSocket from './useWebSocket'
import ActivityFeed from '../components/Chat/ActivityFeed'

/** Scriptable WebSocket double. */
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
    this.onopen = this.onmessage = this.onclose = this.onerror = null
    FakeSocket.instances.push(this)
  }
  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }
  emit(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
  send(raw) {
    this.sent.push(JSON.parse(raw))
  }
  close() {
    this.readyState = FakeSocket.CLOSED
    this.onclose?.({})
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

afterEach(() => vi.unstubAllGlobals())

// ---------------------------------------------------------------------------
describe('agent progress events', () => {
  it('records the plan', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'plan', steps: ['Creating the project', 'Running tests'] }))
    expect(result.current.plan).toEqual(['Creating the project', 'Running tests'])
  })

  it('enters the planning status', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'plan', steps: ['Step one'] }))
    expect(result.current.status).toBe('planning')
  })

  it('tolerates a plan with no steps', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'plan' }))
    expect(result.current.plan).toEqual([])
  })

  it('tracks the active tool', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_started', name: 'create_project', label: 'Creating the project' }))
    expect(result.current.activeTool).toEqual({
      name: 'create_project',
      label: 'Creating the project',
    })
    expect(result.current.status).toBe('executing')
  })

  it('falls back to the name when no label is sent', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_started', name: 'git_commit' }))
    expect(result.current.activeTool.label).toBe('git_commit')
  })

  it('clears the active tool when it finishes', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_started', name: 'x', label: 'X' }))
    act(() => socket.emit({ type: 'tool_finished', name: 'x', success: true, summary: 'done' }))
    expect(result.current.activeTool).toBeNull()
  })

  it('appends a started entry to the activity log', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_started', name: 'run_tests', label: 'Running tests' }))
    expect(result.current.activity).toHaveLength(1)
    expect(result.current.activity[0].kind).toBe('started')
  })

  it('records a successful finish', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_finished', name: 'run_tests', success: true, summary: '12 passed' }))
    expect(result.current.activity[0].kind).toBe('finished')
    expect(result.current.activity[0].success).toBe(true)
    expect(result.current.activity[0].summary).toBe('12 passed')
  })

  it('records a failed finish', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_finished', name: 'run_tests', success: false, summary: '2 failed' }))
    expect(result.current.activity[0].success).toBe(false)
  })

  it('truncates an over-long summary', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_finished', name: 'x', success: true, summary: 'y'.repeat(5000) }))
    expect(result.current.activity[0].summary.length).toBeLessThanOrEqual(200)
  })

  it('records progress messages', async () => {
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'tool_progress', name: 'run_npm', message: 'added 42 packages' }))
    expect(result.current.activity[0].kind).toBe('progress')
    expect(result.current.activity[0].message).toBe('added 42 packages')
  })

  it('keeps a multi-step turn in order', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({ type: 'plan', steps: ['Creating the project', 'Installing deps'] })
      socket.emit({ type: 'tool_started', name: 'create_project', label: 'Creating the project' })
      socket.emit({ type: 'tool_finished', name: 'create_project', success: true, summary: 'created 8 files' })
      socket.emit({ type: 'tool_started', name: 'run_npm', label: 'Running npm' })
      socket.emit({ type: 'tool_finished', name: 'run_npm', success: true, summary: 'added 42 packages' })
    })
    expect(result.current.activity.map((a) => a.kind)).toEqual([
      'started', 'finished', 'started', 'finished',
    ])
    expect(result.current.activeTool).toBeNull()
  })

  it('clears progress with the transcript', async () => {
    const { result, socket } = await mount()
    act(() => {
      socket.emit({ type: 'plan', steps: ['a'] })
      socket.emit({ type: 'tool_started', name: 'x', label: 'X' })
      socket.emit({ type: 'message', content: 'hi', response_id: 'r1' })
    })
    act(() => result.current.clearChat())
    expect(result.current.plan).toEqual([])
    expect(result.current.activeTool).toBeNull()
    expect(result.current.activity).toEqual([])
  })

  it('accepts the new statuses without falling back', async () => {
    const { result, socket } = await mount()
    for (const status of ['planning', 'executing']) {
      act(() => socket.emit({ type: 'status', content: status }))
      expect(result.current.status).toBe(status)
    }
  })

  it('does not leak tool JSON into the transcript', async () => {
    // The regression this whole event layer exists to prevent: the model's
    // tool call used to be streamed to the user as if it were the answer.
    const { result, socket } = await mount()
    act(() => socket.emit({ type: 'message', content: 'Created the project.', response_id: 'r1' }))
    const transcript = JSON.stringify(result.current.messages)
    expect(transcript).not.toContain('"action"')
  })
})

// ---------------------------------------------------------------------------
describe('ActivityFeed', () => {
  it('renders nothing when idle', () => {
    const { container } = render(<ActivityFeed />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders the plan steps', () => {
    render(<ActivityFeed plan={['Creating the project', 'Running tests']} />)
    expect(screen.getByText('Creating the project')).toBeInTheDocument()
    expect(screen.getByText('Running tests')).toBeInTheDocument()
  })

  it('numbers the plan steps', () => {
    render(<ActivityFeed plan={['First', 'Second']} />)
    expect(screen.getByText('1.')).toBeInTheDocument()
    expect(screen.getByText('2.')).toBeInTheDocument()
  })

  it('renders the active tool', () => {
    render(<ActivityFeed activeTool={{ name: 'run_npm', label: 'Running npm' }} />)
    expect(screen.getByText('Running npm')).toBeInTheDocument()
  })

  it('renders the activity log', () => {
    render(
      <ActivityFeed
        activity={[
          { id: '1', kind: 'started', name: 'git_commit', label: 'Committing' },
          { id: '2', kind: 'finished', name: 'git_commit', success: true, summary: 'ok' },
        ]}
      />,
    )
    expect(screen.getByText('Committing')).toBeInTheDocument()
    expect(screen.getByText(/git_commit — ok/)).toBeInTheDocument()
  })

  it('marks a failure distinctly', () => {
    render(
      <ActivityFeed
        activity={[
          { id: '1', kind: 'finished', name: 'run_tests', success: false, summary: '2 failed' },
        ]}
      />,
    )
    expect(screen.getByText(/2 failed/)).toBeInTheDocument()
  })

  it('is announced politely for screen readers', () => {
    render(<ActivityFeed plan={['a']} />)
    expect(screen.getByLabelText('Assistant activity')).toHaveAttribute('aria-live', 'polite')
  })

  it('has Arabic labels', () => {
    render(<ActivityFeed plan={['خطوة']} language="ar" />)
    expect(screen.getByText('الخطة')).toBeInTheDocument()
  })
})
