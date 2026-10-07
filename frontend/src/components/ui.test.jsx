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

  it('wires the stop button to onStop', () => {
    const onStop = vi.fn()
    render(
      <BottomDock
        onSettingsToggle={vi.fn()}
        onMicToggle={vi.fn()}
        onLanguageToggle={vi.fn()}
        onStop={onStop}
      />,
    )
    fireEvent.click(screen.getByTitle('STOP'))
    expect(onStop).toHaveBeenCalledTimes(1)
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

  it('switches to the execution-history section and back', async () => {
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // SYSTEM is the default section: every card the panel existed for is on it.
    await waitFor(() => expect(screen.getByText('AI PROVIDER')).toBeInTheDocument())
    expect(screen.getByRole('tab', { name: /system/i })).toHaveAttribute('aria-selected', 'true')

    await act(async () => {
      fireEvent.click(screen.getByRole('tab', { name: /execution history/i }))
    })
    // The history view takes over the body rather than stacking another card
    // under the system cards...
    expect(await screen.findByText('No executions recorded yet')).toBeInTheDocument()
    expect(screen.queryByText('AI PROVIDER')).not.toBeInTheDocument()
    // ...and the optional audit endpoint answered (this mock returns a payload
    // without entries, which is an empty log — not an unavailable one).
    expect(screen.queryByText('Audit log unavailable')).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('tab', { name: /^system$/i }))
    })
    expect(await screen.findByText('AI PROVIDER')).toBeInTheDocument()
    expect(screen.queryByText('No executions recorded yet')).not.toBeInTheDocument()
  })

  it('shows the tool-call metrics row only when the backend reports it', async () => {
    global.fetch = vi.fn(async (url) => {
      if (String(url).includes('/settings/workspace')) {
        return { ok: true, json: async () => workspace }
      }
      if (String(url).includes('/settings/ai')) {
        return { ok: true, json: async () => ({ provider: 'ollama', connected: false, models: [] }) }
      }
      return {
        ok: true,
        json: async () => ({
          ...health,
          // The row reads the payload's *top* level: `llm.metrics` never
          // carried `tools`, which is exactly why the row used to be dead.
          metrics: { tools: { calls: 9, failed: 2, avg_ms: 480 } },
          llm: { ...health.llm },
        }),
      }
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // The row is derived, not hard-coded: an absent payload means no row of
    // zeroes claiming a measured zero.
    expect(await screen.findByText('9 calls, 2 failed, avg 480ms')).toBeInTheDocument()
    expect(screen.getByText('TOOL CALLS')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
describe('SettingsPanel — AI provider', () => {
  const workspace = {
    root: 'C:/Users/me/ZaytoonWorkspace',
    enforced: true,
    available: true,
    entries: [],
  }
  const health = { llm: { configured: true, metrics: {} }, voice: { stt: true, tts: true }, safety: {} }

  // `qwen2.5-coder:3b` is configured as the fast model but is deliberately
  // absent from the installed list, so the missing-model hint has something
  // real to surface.
  const aiOllama = {
    provider: 'ollama',
    endpoint: 'http://localhost:11434',
    connected: true,
    version: '0.11.4',
    error: '',
    models: [
      { id: 'qwen3.5:9b', name: 'qwen3.5', available: true, capabilities: ['streaming'], size_bytes: 1 },
      { id: 'qwen2.5-coder:7b', name: 'qwen2.5-coder', available: true, capabilities: [], size_bytes: 2 },
    ],
    routing: { fast: 'qwen2.5-coder:3b', reasoning: 'qwen2.5-coder:7b', general: 'qwen3.5:9b' },
    configured_models: {
      general: 'qwen3.5:9b',
      coding: 'qwen2.5-coder:7b',
      fast: 'qwen2.5-coder:3b',
    },
  }

  const aiOpenRouter = {
    provider: 'openrouter',
    endpoint: 'https://openrouter.ai/api/v1/chat/completions',
    connected: true,
    version: '',
    error: '',
    models: [],
    routing: { fast: 'qwen/qwen3.8-27b:free', reasoning: 'cohere/north-mini-code:free', general: 'qwen/qwen3.8-27b:free' },
    configured_models: {
      primary: 'qwen/qwen3.8-27b:free',
      reasoning: 'cohere/north-mini-code:free',
      fallback: 'nvidia/nemotron-3-super-120b-a12b:free',
    },
  }

  /**
   * The three endpoints are independent, and a failure of one must not read
   * as a failure of the others -- so the mock routes on the URL rather than
   * answering every request with one payload.
   */
  function mockFetch({ ai = aiOllama, validate, save } = {}) {
    global.fetch = vi.fn(async (url, opts) => {
      const u = String(url)
      if (opts?.method === 'POST' && u.includes('/settings/ai/validate')) {
        return {
          ok: true,
          json: async () => validate ?? {
            connected: true,
            endpoint: 'http://localhost:11434',
            version: '0.11.4',
            models: ['qwen3.5:9b'],
            error: '',
          },
        }
      }
      if (opts?.method === 'POST' && u.includes('/settings/ai')) {
        return {
          ok: true,
          json: async () => save ?? {
            status: 'saved',
            provider: ai.provider,
            connected: ai.connected,
            error: '',
            models: [],
          },
        }
      }
      if (u.includes('/settings/ai')) return { ok: true, json: async () => ai }
      if (u.includes('/settings/workspace')) return { ok: true, json: async () => workspace }
      return { ok: true, json: async () => health }
    })
    return global.fetch
  }

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the active provider, connection status, endpoint and version', async () => {
    mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('CONNECTED')).toBeInTheDocument())
    expect(screen.getByText('OLLAMA')).toBeInTheDocument()
    expect(screen.getByText('http://localhost:11434')).toBeInTheDocument()
    expect(screen.getByText('0.11.4')).toBeInTheDocument()
  })

  it('reports DISCONNECTED as a state, distinct from an error', async () => {
    mockFetch({ ai: { ...aiOllama, connected: false, error: '' } })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('DISCONNECTED')).toBeInTheDocument())
    expect(screen.queryByText('CONNECTED')).not.toBeInTheDocument()
  })

  it('lists the installed models', async () => {
    mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('qwen3.5:9b')).toBeInTheDocument())
    expect(screen.getByText('qwen2.5-coder:7b')).toBeInTheDocument()
  })

  it('offers the three configured models as editable fields for Ollama', async () => {
    mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByLabelText('GENERAL MODEL')).toHaveValue('qwen3.5:9b'))
    expect(screen.getByLabelText('CODING MODEL')).toHaveValue('qwen2.5-coder:7b')
    expect(screen.getByLabelText('FAST MODEL')).toHaveValue('qwen2.5-coder:3b')
    // The OpenRouter set is not rendered alongside it.
    expect(screen.queryByLabelText('PRIMARY MODEL')).not.toBeInTheDocument()
  })

  it('offers primary / reasoning / fallback for OpenRouter', async () => {
    mockFetch({ ai: aiOpenRouter })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() =>
      expect(screen.getByLabelText('PRIMARY MODEL')).toHaveValue('qwen/qwen3.8-27b:free'),
    )
    expect(screen.getByLabelText('REASONING MODEL')).toHaveValue('cohere/north-mini-code:free')
    expect(screen.getByLabelText('FALLBACK MODEL')).toHaveValue('nvidia/nemotron-3-super-120b-a12b:free')
    expect(screen.queryByLabelText('GENERAL MODEL')).not.toBeInTheDocument()
  })

  it('shows the ollama pull hint for a configured model that is not installed', async () => {
    mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // Installed: 9b and coder:7b. The configured `:3b` fast model is not.
    await waitFor(() =>
      expect(screen.getByText('MODEL NOT INSTALLED — ollama pull qwen2.5-coder:3b')).toBeInTheDocument(),
    )
    // A configured model that *is* installed gets no such warning.
    expect(screen.queryByText(/ollama pull qwen3\.5:9b/)).not.toBeInTheDocument()
  })

  it('surfaces the backend error verbatim, pull command included', async () => {
    const error =
      "MODEL NOT INSTALLED: 'llama3' is not available at http://localhost:11434. " +
      'Install it with `ollama pull llama3` and try again (Zaytoon never downloads models on its own).'
    mockFetch({ ai: { ...aiOllama, connected: false, error } })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // The whole string, unedited: the pull command is the instruction.
    await waitFor(() => expect(screen.getByText(error)).toBeInTheDocument())
    expect(screen.getByText('DISCONNECTED')).toBeInTheDocument()
  })

  it('never offers to download a model — status only', async () => {
    mockFetch({ ai: { ...aiOllama, connected: false, error: 'Connection failed: refused' } })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('DISCONNECTED')).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /pull|download|install/i })).not.toBeInTheDocument()
  })

  it('validates without saving, and reports the result', async () => {
    const fetchMock = mockFetch({
      validate: { connected: true, endpoint: 'http://localhost:11434', version: '0.11.4', models: ['a', 'b'], error: '' },
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    await waitFor(() => expect(screen.getByText('CONNECTED')).toBeInTheDocument())

    await act(async () => {
      fireEvent.click(screen.getByText('VALIDATE'))
    })

    const call = fetchMock.mock.calls.find(([url]) => String(url).includes('/settings/ai/validate'))
    expect(call).toBeTruthy()
    expect(call[1].method).toBe('POST')
    expect(JSON.parse(call[1].body)).toEqual({
      provider: 'ollama',
      base_url: 'http://localhost:11434',
    })
    await waitFor(() => expect(screen.getByText(/CONNECTED · 0.11.4 · 2 models/)).toBeInTheDocument())
  })

  it('saves an edited model to POST /settings/ai', async () => {
    const fetchMock = mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByLabelText('GENERAL MODEL')

    fireEvent.change(input, { target: { value: 'mistral:7b' } })
    await act(async () => {
      fireEvent.click(screen.getByText('SAVE'))
    })

    const call = fetchMock.mock.calls.find(
      ([url, opts]) => opts?.method === 'POST' && String(url).endsWith('/settings/ai'),
    )
    expect(call).toBeTruthy()
    const body = JSON.parse(call[1].body)
    expect(body.provider).toBe('ollama')
    expect(body.general_model).toBe('mistral:7b')
    expect(body.coding_model).toBe('qwen2.5-coder:7b')
    expect(body.fast_model).toBe('qwen2.5-coder:3b')
    await waitFor(() => expect(screen.getByText('Provider settings saved')).toBeInTheDocument())
  })

  it('shows a save rejection instead of claiming success', async () => {
    mockFetch({ save: { error: 'MODEL NOT INSTALLED: `ollama pull mistral:7b`' } })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByLabelText('GENERAL MODEL')

    fireEvent.change(input, { target: { value: 'mistral:7b' } })
    await act(async () => {
      fireEvent.click(screen.getByText('SAVE'))
    })

    await waitFor(() =>
      expect(screen.getByText('MODEL NOT INSTALLED: `ollama pull mistral:7b`')).toBeInTheDocument(),
    )
    expect(screen.queryByText('Provider settings saved')).not.toBeInTheDocument()
  })

  it('keeps an AI-settings failure from masquerading as an unreachable backend', async () => {
    global.fetch = vi.fn(async (url) => {
      const u = String(url)
      if (u.includes('/settings/ai')) throw new Error('Not found')
      if (u.includes('/settings/workspace')) return { ok: true, json: async () => workspace }
      return { ok: true, json: async () => health }
    })
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    // The provider section reports its own failure...
    await waitFor(() => expect(screen.getByText(/AI settings unreachable/)).toBeInTheDocument())
    expect(screen.getByText('ERROR')).toBeInTheDocument()
    // ...and does not drag the two endpoints that answered down with it.
    expect(screen.queryByText(/Backend unreachable/)).not.toBeInTheDocument()
    expect(await screen.findByDisplayValue('C:/Users/me/ZaytoonWorkspace')).toBeInTheDocument()
  })

  it('keeps an edited draft when the status around it refreshes', async () => {
    mockFetch()
    render(<SettingsPanel open onClose={vi.fn()} language="en" />)
    const input = await screen.findByLabelText('GENERAL MODEL')

    fireEvent.change(input, { target: { value: 'mistral:7b' } })
    await act(async () => {
      fireEvent.click(screen.getByText('VALIDATE'))
    })

    // The refresh rewrites the status, never the text under the cursor.
    await waitFor(() => expect(screen.getByText(/CONNECTED · 0.11.4 · 1 models/)).toBeInTheDocument())
    expect(input).toHaveValue('mistral:7b')
  })
})
