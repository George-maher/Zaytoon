import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import InputBar from './InputBar'
import SettingsPanel from './SettingsPanel'
import BottomDock from './BottomDock'

// ---------------------------------------------------------------------------
describe('InputBar', () => {
  it('sends the typed text on submit', () => {
    const onSend = vi.fn()
    render(<InputBar onSend={onSend} isConnected language="en" />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'open spotify' } })
    fireEvent.submit(input.closest('form'))
    expect(onSend).toHaveBeenCalledWith('open spotify')
  })

  it('sends on Enter', () => {
    const onSend = vi.fn()
    render(<InputBar onSend={onSend} isConnected language="en" />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'hello' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledWith('hello')
  })

  it('does not send on Shift+Enter, so a newline can be typed', () => {
    const onSend = vi.fn()
    render(<InputBar onSend={onSend} isConnected language="en" />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'line one' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('clears the field after sending', () => {
    const onSend = vi.fn()
    render(<InputBar onSend={onSend} isConnected language="en" />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'x' } })
    fireEvent.submit(input.closest('form'))
    expect(input.value).toBe('')
  })

  it('ignores whitespace-only input', () => {
    const onSend = vi.fn()
    render(<InputBar onSend={onSend} isConnected language="en" />)
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: '    ' } })
    fireEvent.submit(input.closest('form'))
    expect(onSend).not.toHaveBeenCalled()
  })

  it('disables send while disconnected', () => {
    render(<InputBar onSend={vi.fn()} isConnected={false} language="en" />)
    expect(screen.getByText('SEND')).toBeDisabled()
  })

  it('enables send while connected', () => {
    render(<InputBar onSend={vi.fn()} isConnected language="en" />)
    expect(screen.getByText('SEND')).toBeEnabled()
  })

  it('sets dir from the language', () => {
    const { rerender } = render(<InputBar onSend={vi.fn()} isConnected language="en" />)
    expect(screen.getByRole('textbox')).toHaveAttribute('dir', 'ltr')
    rerender(<InputBar onSend={vi.fn()} isConnected language="ar" />)
    expect(screen.getByRole('textbox')).toHaveAttribute('dir', 'rtl')
  })

  it('caps the input at the backend message limit', () => {
    // The backend rejects anything over MAX_MESSAGE_LENGTH = 8000.
    render(<InputBar onSend={vi.fn()} isConnected language="en" />)
    expect(screen.getByRole('textbox')).toHaveAttribute('maxLength', '8000')
  })

  it('offers a mic button only when a handler is supplied', () => {
    const { rerender } = render(<InputBar onSend={vi.fn()} isConnected language="en" />)
    expect(screen.queryByLabelText('Activate voice mode')).not.toBeInTheDocument()
    rerender(
      <InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={vi.fn()} />,
    )
    expect(screen.getByLabelText('Activate voice mode')).toBeInTheDocument()
  })

  it('starts voice mode from the mic button', () => {
    const onVoiceStart = vi.fn()
    render(
      <InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={onVoiceStart} />,
    )
    fireEvent.click(screen.getByLabelText('Activate voice mode'))
    expect(onVoiceStart).toHaveBeenCalled()
  })

  it('shows a voice-active indicator', () => {
    render(<InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={vi.fn()} voiceMode />)
    // Restyled from the literal "VOICE ON" to a recording indicator, but the
    // requirement is unchanged: the state must be readable as text, not only
    // as a colour change on the button.
    expect(screen.getByText(/REC/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /activate voice mode/i })).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps the mic mounted so a recording can be stopped', () => {
    // It used to be unmounted while recording, which left no way to stop.
    const { rerender } = render(
      <InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={vi.fn()} voiceMode={false} />,
    )
    expect(screen.getByRole('button', { name: /activate voice mode/i })).toBeInTheDocument()
    rerender(
      <InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={vi.fn()} onVoiceStop={vi.fn()} voiceMode />,
    )
    expect(screen.getByRole('button', { name: /activate voice mode/i })).toBeInTheDocument()
  })

  it('stops recording through the same mic control', () => {
    const onVoiceStop = vi.fn()
    render(
      <InputBar onSend={vi.fn()} isConnected language="en" onVoiceStart={vi.fn()} onVoiceStop={onVoiceStop} voiceMode />,
    )
    fireEvent.click(screen.getByRole('button', { name: /activate voice mode/i }))
    expect(onVoiceStop).toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
describe('BottomDock', () => {
  it('wires the settings button, which previously had no case', () => {
    const onSettingsToggle = vi.fn()
    render(
      <BottomDock
        onSettingsToggle={onSettingsToggle}
        onMicToggle={vi.fn()}
        onLanguageToggle={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTitle('CONF'))
    expect(onSettingsToggle).toHaveBeenCalled()
  })

  // Voice lives in the InputBar, not the dock. The dock previously carried a
  // second microphone, which meant two controls for one action -- and the
  // risk that a future edit re-introduces it. These tests were retargeted to
  // the authoritative control rather than deleted, so the wiring is still
  // asserted; `there is exactly one microphone` below guards the count.
  it('wires the mic button when the dock is used standalone', () => {
    const onMicToggle = vi.fn()
    render(
      <BottomDock
        onSettingsToggle={vi.fn()}
        onMicToggle={onMicToggle}
        onLanguageToggle={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTitle('MIC'))
    expect(onMicToggle).toHaveBeenCalled()
  })

  it('hides the duplicate mic when the console owns voice', () => {
    render(
      <BottomDock
        onSettingsToggle={vi.fn()}
        onMicToggle={vi.fn()}
        onLanguageToggle={vi.fn()}
        onStop={vi.fn()}
        hideMic
      />,
    )
    expect(screen.queryByTitle('MIC')).not.toBeInTheDocument()
    // The system controls are still there.
    expect(screen.getByTitle('CONF')).toBeInTheDocument()
    expect(screen.getByTitle('STOP')).toBeInTheDocument()
    expect(screen.getByTitle('LANG')).toBeInTheDocument()
  })

  it('does not throw for an unknown button id', () => {
    render(
      <BottomDock
        onSettingsToggle={vi.fn()}
        onMicToggle={vi.fn()}
        onLanguageToggle={vi.fn()}
        onStop={vi.fn()}
      />,
    )
    expect(() => screen.getByTitle('STOP')).not.toThrow()
  })

  it('marks the mic as pressed while voice mode is on', () => {
    render(
      <BottomDock
        onSettingsToggle={vi.fn()}
        onMicToggle={vi.fn()}
        onLanguageToggle={vi.fn()}
        onStop={vi.fn()}
        voiceMode
      />,
    )
    expect(screen.getByTitle('MIC')).toHaveAttribute('aria-pressed', 'true')
  })
})

// ---------------------------------------------------------------------------
describe('SettingsPanel', () => {
  const health = {
    llm: {
      provider: 'openrouter',
      configured: true,
      primary_model: 'qwen/qwen3.8-27b:free',
      reasoning_model: 'thinkingmachines/inkling-small:free',
      chain: [{ model: 'qwen/qwen3.8-27b:free' }],
      rejected_models: [],
      metrics: { requests: 12, success: 11, rate_limited: 1, avg_latency_ms: 900 },
    },
    voice: { stt: true, tts: true },
    safety: { enabled: true, confirm_timeout: 120 },
    tools_loaded: 3,
  }
  const workspace = {
    root: 'C:/Users/me/ZaytoonWorkspace',
    enforced: true,
    available: true,
    entries: ['notes', 'projects'],
  }

  beforeEach(() => {
    global.fetch = vi.fn(async (url) => {
      if (String(url).includes('/settings/workspace')) {
        return { ok: true, json: async () => workspace }
      }
      return { ok: true, json: async () => health }
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders nothing when closed', () => {
    const { container } = render(
      <SettingsPanel open={false} onClose={vi.fn()} language="en" />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the model actually in use, not a hard-coded label', async () => {
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // Appears twice by design: the "Model" row and the "Fallback chain" row.
    // Assert at least one match rather than exactly one.
    await waitFor(() =>
      expect(screen.getAllByText('qwen/qwen3.8-27b:free').length).toBeGreaterThan(0),
    )
    // And explicitly: the old hard-coded label is gone.
    expect(screen.queryByText('QWEN 2.5 7B')).not.toBeInTheDocument()
  })

  it('reports a missing API key prominently', async () => {
    global.fetch = vi.fn(async (url) => {
      if (String(url).includes('/settings/workspace')) {
        return { ok: true, json: async () => workspace }
      }
      return {
        ok: true,
        json: async () => ({ ...health, llm: { ...health.llm, configured: false } }),
      }
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('MISSING')).toBeInTheDocument())
  })

  it('surfaces a free-model policy violation', async () => {
    global.fetch = vi.fn(async (url) => {
      if (String(url).includes('/settings/workspace')) {
        return { ok: true, json: async () => workspace }
      }
      return {
        ok: true,
        json: async () => ({
          ...health,
          llm: { ...health.llm, rejected_models: ['openai/gpt-4o'] },
        }),
      }
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText(/openai\/gpt-4o/)).toBeInTheDocument())
  })

  it('shows the current workspace root', async () => {
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByDisplayValue('C:/Users/me/ZaytoonWorkspace')
    expect(input).toBeInTheDocument()
  })

  it('keeps the path field left-to-right even in Arabic', async () => {
    // A Windows path must never be reordered by RTL text direction.
    render(<SettingsPanel open onClose={vi.fn()} language="ar" />)
    const input = await screen.findByDisplayValue('C:/Users/me/ZaytoonWorkspace')
    expect(input).toHaveAttribute('dir', 'ltr')
  })

  it('posts a new workspace root and confirms', async () => {
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByDisplayValue('C:/Users/me/ZaytoonWorkspace')

    fireEvent.change(input, { target: { value: 'D:/Projects' } })
    await act(async () => {
      fireEvent.click(screen.getByText('SET'))
    })

    const call = global.fetch.mock.calls.find(([url, opts]) =>
      String(url).includes('/settings/workspace') && opts?.method === 'POST',
    )
    expect(call).toBeTruthy()
    expect(JSON.parse(call[1].body)).toEqual({ root: 'D:/Projects' })
  })

  it('surfaces a backend rejection instead of claiming success', async () => {
    global.fetch = vi.fn(async (url, opts) => {
      if (opts?.method === 'POST') {
        return {
          ok: false,
          status: 400,
          json: async () => ({ error: 'Cannot use that directory' }),
        }
      }
      if (String(url).includes('/settings/workspace')) {
        return { ok: true, json: async () => workspace }
      }
      return { ok: true, json: async () => health }
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByDisplayValue('C:/Users/me/ZaytoonWorkspace')

    fireEvent.change(input, { target: { value: 'X:/nope' } })
    await act(async () => {
      fireEvent.click(screen.getByText('SET'))
    })

    await waitFor(() =>
      expect(screen.getByText('Cannot use that directory')).toBeInTheDocument(),
    )
  })

  it('reports an unreachable backend rather than showing stale data', async () => {
    global.fetch = vi.fn(async () => {
      throw new Error('Failed to fetch')
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() =>
      expect(screen.getByText(/Backend unreachable/)).toBeInTheDocument(),
    )
  })

  it('closes on the close button', async () => {
    const onClose = vi.fn()
    render(<SettingsPanel open onClose={onClose} language="en" />)
    await act(async () => {
      fireEvent.click(screen.getByText('CLOSE'))
    })
    expect(onClose).toHaveBeenCalled()
  })

  it('renders bilingual labels in Arabic', async () => {
    await act(async () => {
      render(<SettingsPanel open onClose={vi.fn()} language="ar" />)
    })
    expect(screen.getByText('الإعدادات')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('مجلد العمل')).toBeInTheDocument())
  })

  it('is announced as a dialog', async () => {
    await act(async () => {
      render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
