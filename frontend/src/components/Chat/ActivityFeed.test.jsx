import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ActivityFeed from './ActivityFeed'

/**
 * The feed's job is one row per tool invocation with a state chip that
 * survives greyscale: a glyph, a word, a colour. These tests pin the
 * rendering contract that `lib/toolRows` feeds.
 */
describe('ActivityFeed', () => {
  it('renders nothing while there is no plan, tool or activity', () => {
    const { container } = render(<ActivityFeed plan={[]} activeTool={null} activity={[]} language="en" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('announces the whole feed as one polite live region', () => {
    render(<ActivityFeed plan={['Step one']} language="en" />)
    const region = screen.getByLabelText('Assistant activity')
    expect(region).toHaveAttribute('role', 'status')
    expect(region).toHaveAttribute('aria-live', 'polite')
  })

  it('renders the announced plan as an ordered list', () => {
    render(<ActivityFeed plan={['Create the folder', 'Write the file']} language="en" />)
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('Create the folder')
    expect(items[1]).toHaveTextContent('Write the file')
  })

  it('shows a running tool once, with its heartbeat message', () => {
    render(
      <ActivityFeed
        activity={[
          { kind: 'started', name: 'run_npm', label: 'Running tests', at: 1 },
          { kind: 'progress', name: 'run_npm', message: '42 of 60 passed' },
        ]}
        activeTool={{ name: 'run_npm', label: 'Running tests' }}
        language="en"
      />,
    )
    // One chip for the invocation, not one per event…
    expect(screen.getAllByText('RUNNING')).toHaveLength(1)
    expect(screen.getByText('Running tests')).toBeInTheDocument()
    // …and the progress frame is visible as the row's detail.
    expect(screen.getByText('42 of 60 passed')).toBeInTheDocument()
    // The dedicated callout would print the same tool a second time.
    expect(screen.queryByText('TOOL')).not.toBeInTheDocument()
  })

  it('marks a completed tool DONE with its duration', () => {
    render(
      <ActivityFeed
        activity={[
          { kind: 'started', name: 'file_write', label: 'Writing notes.md', at: 1000 },
          { kind: 'finished', name: 'file_write', success: true, at: 1820 },
        ]}
        language="en"
      />,
    )
    expect(screen.getByText('DONE')).toBeInTheDocument()
    expect(screen.getByText('820ms')).toBeInTheDocument()
    expect(screen.queryByText('RUNNING')).not.toBeInTheDocument()
  })

  it('marks a failed tool FAILED and keeps the reason', () => {
    render(
      <ActivityFeed
        activity={[
          { kind: 'started', name: 'git_push', label: 'Pushing', at: 1 },
          { kind: 'finished', name: 'git_push', success: false, summary: 'rejected: non-fast-forward', at: 2 },
        ]}
        language="en"
      />,
    )
    expect(screen.getByText('FAILED')).toBeInTheDocument()
    expect(screen.getByText(/rejected: non-fast-forward/)).toBeInTheDocument()
  })

  it('keeps two concurrent tools as two rows', () => {
    render(
      <ActivityFeed
        activity={[
          { kind: 'started', name: 'a', label: 'Tool A', at: 1 },
          { kind: 'started', name: 'b', label: 'Tool B', at: 2 },
          { kind: 'finished', name: 'a', success: true, at: 3 },
        ]}
        language="en"
      />,
    )
    expect(screen.getAllByText('DONE')).toHaveLength(1)
    expect(screen.getAllByText('RUNNING')).toHaveLength(1)
    expect(screen.getByText('Tool A')).toBeInTheDocument()
    expect(screen.getByText('Tool B')).toBeInTheDocument()
  })

  it('falls back to the active-tool callout when no row covers it', () => {
    render(<ActivityFeed activeTool={{ name: 'get_time', label: 'Reading the clock' }} language="en" />)
    expect(screen.getByText('TOOL')).toBeInTheDocument()
    expect(screen.getByText('Reading the clock')).toBeInTheDocument()
  })

  it('uses Arabic chips when the session is Arabic', () => {
    render(
      <ActivityFeed
        activity={[
          { kind: 'started', name: 'a', label: 'أداة', at: 1 },
          { kind: 'finished', name: 'a', success: true, at: 2 },
          { kind: 'started', name: 'b', label: 'أداة', at: 3 },
          { kind: 'finished', name: 'b', success: false, at: 4 },
        ]}
        language="ar"
      />,
    )
    expect(screen.getByText('تم')).toBeInTheDocument()
    expect(screen.getByText('فشل')).toBeInTheDocument()
  })
})
