import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import ConnectDialog from './ConnectDialog'
import { translate } from '../../i18n/strings'

vi.mock('../../lib/api', () => ({
  createConnection: vi.fn(),
  startAuth: vi.fn(),
  completeAuth: vi.fn(),
}))

import { createConnection, startAuth, completeAuth } from '../../lib/api'

const t = (key) => translate('en', key)

const renderConnectDialog = (props = {}) => {
  const onClose = vi.fn()
  const onSuccess = vi.fn()
  const onError = vi.fn()
  return {
    onClose,
    onSuccess,
    onError,
    ...render(
      <ConnectDialog
        connectorId={props.connectorId ?? 'github'}
        onClose={onClose}
        onSuccess={onSuccess}
        onError={onError}
        t={props.t ?? t}
      />
    ),
  }
}

describe('ConnectDialog', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    createConnection.mockResolvedValue({ id: 'conn-1', state: 'not_connected' })
    startAuth.mockResolvedValue({
      auth_method: 'oauth2',
      auth_url: 'https://github.com/login/oauth/authorize?state=abc',
      connection_id: 'conn-1',
    })
    completeAuth.mockResolvedValue({})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the connect panel and creates the connection on mount', async () => {
    renderConnectDialog()
    // Config step starts loading; the Connect button is disabled while it flies.
    expect(screen.getByRole('button', { name: 'LOADING…' })).toBeDisabled()
    await waitFor(() => {
      expect(createConnection).toHaveBeenCalledWith('github', {})
    })
    expect(startAuth).toHaveBeenCalledWith('conn-1', window.location.origin)
    // Moved to the auth step: the OAuth link carries the auth URL.
    expect(screen.getByText('Stored server-side. Never shown again.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connect' })).toHaveAttribute(
      'href',
      'https://github.com/login/oauth/authorize?state=abc'
    )
    expect(screen.getByRole('button', { name: 'Complete' })).toBeInTheDocument()
  })

  it('closes when the close button is clicked', () => {
    const { onClose } = renderConnectDialog()
    fireEvent.click(screen.getByText('✕'))
    expect(onClose).toHaveBeenCalled()
  })

  it('completes auth and calls onSuccess when Complete is clicked', async () => {
    const { onSuccess } = renderConnectDialog()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Complete' })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: 'Complete' }))
    await waitFor(() => {
      expect(completeAuth).toHaveBeenCalledWith('conn-1', {})
    })
    expect(onSuccess).toHaveBeenCalledWith('conn-1')
    expect(screen.getByText('Connected')).toBeInTheDocument()
  })

  it('shows the API key form for api_key auth', async () => {
    startAuth.mockResolvedValue({
      auth_method: 'api_key',
      connection_id: 'conn-1',
    })
    renderConnectDialog()
    await waitFor(() => {
      expect(screen.getByLabelText('API key / personal access token')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled()
  })

  it('sends the API key to completeAuth and calls onSuccess', async () => {
    startAuth.mockResolvedValue({
      auth_method: 'api_key',
      connection_id: 'conn-1',
    })
    const { onSuccess } = renderConnectDialog()
    await waitFor(() => {
      expect(screen.getByLabelText('API key / personal access token')).toBeInTheDocument()
    })
    fireEvent.change(screen.getByLabelText('API key / personal access token'), {
      target: { value: 'ghp_test123' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => {
      expect(completeAuth).toHaveBeenCalledWith('conn-1', { api_key: 'ghp_test123' })
    })
    expect(onSuccess).toHaveBeenCalledWith('conn-1')
  })

  it('shows MCP config fields for mcp auth', async () => {
    startAuth.mockResolvedValue({
      auth_method: 'mcp',
      connection_id: 'conn-1',
    })
    renderConnectDialog()
    await waitFor(() => {
      expect(screen.getByLabelText('Server name')).toBeInTheDocument()
      expect(screen.getByLabelText('Command')).toBeInTheDocument()
      expect(screen.getByLabelText('Arguments')).toBeInTheDocument()
      expect(screen.getByLabelText('Environment variables (JSON)')).toBeInTheDocument()
    })
  })

  it('disables MCP submit until server name and command are entered', async () => {
    startAuth.mockResolvedValue({
      auth_method: 'mcp',
      connection_id: 'conn-1',
    })
    renderConnectDialog()
    await waitFor(() => {
      expect(screen.getByLabelText('Server name')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Connect' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'local-filesystem' },
    })
    fireEvent.change(screen.getByLabelText('Command'), {
      target: { value: 'npx @modelcontextprotocol/server-filesystem' },
    })
    expect(screen.getByRole('button', { name: 'Connect' })).not.toBeDisabled()
  })

  it('reports createConnection failures via onError', async () => {
    createConnection.mockRejectedValue(new Error('Down'))
    const { onError } = renderConnectDialog()
    await waitFor(() => {
      expect(onError).toHaveBeenCalledWith('Down')
    })
  })

  it('renders Arabic labels', async () => {
    const arT = (key) => {
      const ar = {
        'connections.connect': 'اتصل',
        'connections.connected': 'متصل',
        'connections.api_key': 'مفتاح API / رمز دخول شخصي',
        'connections.api_key_stored': 'مخزن من جانب الخادم. لا يُعرض مرة أخرى.',
        'common.loading': 'جارٍ التحميل…',
      }
      return ar[key] || key
    }
    renderConnectDialog({ t: arT })
    expect(screen.getByText('مخزن من جانب الخادم. لا يُعرض مرة أخرى.')).toBeInTheDocument()
    await waitFor(() => {
      expect(createConnection).toHaveBeenCalledWith('github', {})
    })
    expect(screen.getByRole('link', { name: 'اتصل' })).toHaveAttribute(
      'href',
      'https://github.com/login/oauth/authorize?state=abc'
    )
  })
})
