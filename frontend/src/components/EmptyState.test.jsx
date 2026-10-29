import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import EmptyState from './EmptyState'

describe('EmptyState', () => {
  it('renders title, description, and glyph', () => {
    render(
      <EmptyState
        title="No executions recorded yet"
        description="Executions appear here when tools run."
        glyph="○"
      />,
    )

    expect(screen.getByText('No executions recorded yet')).toBeInTheDocument()
    expect(
      screen.getByText('Executions appear here when tools run.'),
    ).toBeInTheDocument()
    expect(screen.getByText('○')).toBeInTheDocument()
  })

  it('renders without a title or description when not provided', () => {
    render(<EmptyState glyph="○" />)
    expect(screen.getByText('○')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 3 })).not.toBeInTheDocument()
    expect(screen.queryByText('Executions appear here when tools run.')).not.toBeInTheDocument()
  })

  it('renders as a region with an accessible name from title', () => {
    render(<EmptyState title="No processes" glyph="○" />)

    const region = screen.getByRole('region', { name: 'No processes' })
    expect(region).toBeInTheDocument()
  })

  it('uses ariaLabel prop when provided instead of title', () => {
    render(
      <EmptyState title="No records" ariaLabel="Audit history is empty" glyph="○" />,
    )

    const region = screen.getByRole('region', { name: 'Audit history is empty' })
    expect(region).toBeInTheDocument()
  })

  it('hides the glyph from assistive technology via aria-hidden', () => {
    render(<EmptyState title="No records" glyph="○" />)

    const glyph = screen.getByText('○')
    expect(glyph).toHaveAttribute('aria-hidden', 'true')
  })

  it('renders a semantic h3 heading for the title', () => {
    render(<EmptyState title="No records" glyph="○" />)

    const heading = screen.getByRole('heading', { level: 3 })
    expect(heading).toHaveTextContent('No records')
  })

  it('renders an action slot when provided', () => {
    const { getByText } = render(
      <EmptyState
        title="No records"
        action={<button>Retry</button>}
      />,
    )

    expect(getByText('Retry')).toBeInTheDocument()
  })

  it('renders Arabic labels correctly', () => {
    render(
      <EmptyState
        title="لا توجد عمليات مسجلة بعد"
        description="تظهر العمليات هنا عند تشغيل الأدوات."
        glyph="○"
      />,
    )

    expect(screen.getByText('لا توجد عمليات مسجلة بعد')).toBeInTheDocument()
    expect(
      screen.getByText('تظهر العمليات هنا عند تشغيل الأدوات.'),
    ).toBeInTheDocument()
  })
})
