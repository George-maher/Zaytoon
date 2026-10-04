import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ConfirmationDialog from './ConfirmationDialog'

const request = {
  request_id: 'cfm_1',
  action: 'system_command',
  params: { command: 'npm install' },
  level: 'high',
  permission: 'confirm',
  reasons: ['arbitrary terminal execution'],
  summary: 'Run command: npm install',
  text: 'Run command: npm install — arbitrary terminal execution',
  session_id: 's1',
}

describe('ConfirmationDialog', () => {
  it('renders nothing without a request', () => {
    const { container } = render(
      <ConfirmationDialog request={null} onRespond={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the exact command that will run', () => {
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    // The user must be able to see *what* they are approving, not just that
    // something is being approved. Look inside the parameter block specifically
    // so this cannot be satisfied by the summary text alone.
    const block = document.querySelector('pre')
    expect(block).toBeInTheDocument()
    expect(block.textContent).toContain('command: npm install')
  })

  it('shows the action name', () => {
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    expect(screen.getByText('system_command')).toBeInTheDocument()
  })

  it('renders every parameter, one per line', () => {
    render(
      <ConfirmationDialog
        request={{ ...request, params: { command: 'git push', path: 'src/main.py' } }}
        onRespond={vi.fn()}
      />,
    )
    expect(screen.getByText(/command: git push/)).toBeInTheDocument()
    expect(screen.getByText(/path: src\/main\.py/)).toBeInTheDocument()
  })

  it('serialises non-string parameters rather than rendering [object Object]', () => {
    render(
      <ConfirmationDialog
        request={{ ...request, params: { args: ['a', 'b'], count: 3 } }}
        onRespond={vi.fn()}
      />,
    )
    expect(screen.getByText(/args: \["a","b"\]/)).toBeInTheDocument()
    expect(screen.getByText(/count: 3/)).toBeInTheDocument()
  })

  it('lists the reasons it is asking as a bulleted list', () => {
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    // Scope to the <ul> so the assertion is about the reason list, not about
    // the same phrase also appearing in the summary line.
    const items = screen.getAllByRole('listitem')
    expect(items.some((li) => li.textContent.includes('arbitrary terminal execution'))).toBe(true)
  })

  it('labels the permission tier', () => {
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    expect(screen.getByText('CONFIRMATION REQUIRED')).toBeInTheDocument()
  })

  it('labels a dangerous action as DANGEROUS', () => {
    render(
      <ConfirmationDialog
        request={{ ...request, permission: 'dangerous' }}
        onRespond={vi.fn()}
      />,
    )
    expect(screen.getByText('DANGEROUS')).toBeInTheDocument()
  })

  it('grades the risk from the level when the backend omits the tier', () => {
    // An unrecognised/absent tier must never downgrade an explicit HIGH level
    // to "just confirm" — the badge follows the stricter of the two signals.
    const { permission, ...withoutTier } = request
    render(<ConfirmationDialog request={withoutTier} onRespond={vi.fn()} />)
    expect(screen.getByText('DANGEROUS')).toBeInTheDocument()
  })

  it('defaults to the confirmation tier when neither field grades higher', () => {
    const { permission, level, ...bare } = request
    render(<ConfirmationDialog request={bare} onRespond={vi.fn()} />)
    expect(screen.getByText('CONFIRMATION REQUIRED')).toBeInTheDocument()
  })

  it('boxes the local-execution header', () => {
    // A system prompt must be recognisable as Zaytoon's own gate, not as
    // page content.
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    expect(screen.getByText('ZAYTOON LOCAL EXECUTION')).toBeInTheDocument()
  })

  it('shows the working directory on its own row, left-to-right', () => {
    render(
      <ConfirmationDialog
        request={{
          ...request,
          params: { ...request.params, working_directory: 'C:\\work\\demo' },
        }}
        onRespond={vi.fn()}
      />,
    )
    expect(screen.getByText('Directory:')).toBeInTheDocument()
    const path = screen.getByText('C:\\work\\demo')
    expect(path).toHaveAttribute('dir', 'ltr')
    // ...and it is not left buried only inside the parameter dump.
    expect(document.querySelector('pre').textContent).not.toContain('working_directory')
  })

  it('responds true on allow and false on cancel', () => {
    const onRespond = vi.fn()
    render(<ConfirmationDialog request={request} onRespond={onRespond} />)
    fireEvent.click(screen.getByText('ALLOW'))
    expect(onRespond).toHaveBeenCalledWith(true)
    fireEvent.click(screen.getByText('CANCEL'))
    expect(onRespond).toHaveBeenLastCalledWith(false)
  })

  it('approves on Enter and denies on Escape', () => {
    const onRespond = vi.fn()
    render(<ConfirmationDialog request={request} onRespond={onRespond} />)

    fireEvent.keyDown(window, { key: 'Enter' })
    expect(onRespond).toHaveBeenLastCalledWith(true)

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onRespond).toHaveBeenLastCalledWith(false)
  })

  it('is announced as an alert dialog', () => {
    render(<ConfirmationDialog request={request} onRespond={vi.fn()} />)
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })

  it('renders bilingual labels in Arabic', () => {
    render(
      <ConfirmationDialog request={request} onRespond={vi.fn()} language="ar" />,
    )
    expect(screen.getByText('تأكيد مطلوب')).toBeInTheDocument()
    expect(screen.getByText('موافق')).toBeInTheDocument()
    expect(screen.getByText('إلغاء')).toBeInTheDocument()
  })

  it('keeps the command block left-to-right even in Arabic', () => {
    // A command like `git commit -m "wip"` must not be reordered by RTL text
    // direction, or the user would approve something other than what runs.
    render(
      <ConfirmationDialog
        request={request}
        onRespond={vi.fn()}
        language="ar"
      />,
    )
    expect(document.querySelector('pre')).toHaveAttribute('dir', 'ltr')
  })
})
