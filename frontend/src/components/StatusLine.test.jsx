import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import StatusLine from './StatusLine'

/**
 * Presentation contract for the derived status line. The *sentences* are
 * `lib/statusLabels`' job; this component owns the announcement semantics
 * (role/aria-live), the tone colour, and rendering nothing when there is
 * nothing to say.
 */
describe('StatusLine', () => {
  it('is announced politely as a status region', () => {
    render(<StatusLine text="Thinking…" language="en" />)
    const region = screen.getByRole('status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveTextContent('Thinking…')
    expect(region).toHaveTextContent('STATUS')
  })

  it('never renders colour-only: the sentence is the text', () => {
    render(<StatusLine text="Calling git_push…" language="en" tone="warn" />)
    expect(screen.getByText('Calling git_push…')).toBeInTheDocument()
    // The tone reaches the element as the LED/line colour variable.
    const region = screen.getByRole('status')
    expect(region.style.getPropertyValue('--state-color')).toContain('hud-amber')
  })

  it('renders nothing when there is no text', () => {
    const { container } = render(<StatusLine text="" language="en" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('labels the line bilingually', () => {
    render(<StatusLine text="جاهز" language="ar" />)
    expect(screen.getByText('الحالة')).toBeInTheDocument()
    expect(screen.getByText('جاهز')).toBeInTheDocument()
  })

  it('falls back to the accent tone for an unknown one', () => {
    render(<StatusLine text="Ready" tone="not-a-tone" />)
    expect(screen.getByRole('status').style.getPropertyValue('--state-color')).toContain('hud-cyan')
  })
})
