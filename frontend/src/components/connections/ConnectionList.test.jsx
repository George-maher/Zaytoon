import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ConnectionList from './ConnectionList'
import { translate } from '../../i18n/strings'

const t = (key) => translate('en', key)

const baseConnection = {
  id: 'conn-1',
  connector_id: 'github',
  state: 'connected',
  has_credentials: true,
  scopes: [],
  error: null,
  config: {},
}

const renderConnectionList = (props = {}) => {
  const onSetActive = vi.fn()
  const onTest = vi.fn()
  const onRefresh = vi.fn()
  const onDisconnect = vi.fn()
  const onDelete = vi.fn()
  const onDiscover = vi.fn()
  return {
    onSetActive,
    onTest,
    onRefresh,
    onDisconnect,
    onDelete,
    onDiscover,
    ...render(
      <ConnectionList
        connections={props.connections ?? [baseConnection]}
        loading={props.loading ?? false}
        _activeConnectionId={props.activeId ?? null}
        onSetActive={onSetActive}
        onTest={onTest}
        onRefresh={onRefresh}
        onDisconnect={onDisconnect}
        onDelete={onDelete}
        onDiscover={onDiscover}
        t={props.t ?? t}
        error={props.error ?? null}
      />
    ),
  }
}

describe('ConnectionList', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a loading state when loading is true', () => {
    renderConnectionList({ loading: true, connections: [] })
    expect(screen.getByText('My connections')).toBeInTheDocument()
    expect(screen.getByText('LOADING…')).toBeInTheDocument()
  })

  it('renders each connection with its state and credential status', () => {
    renderConnectionList()
    expect(screen.getByText('github')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('Credentials stored')).toBeInTheDocument()
  })

  it('shows the connected count in the header', () => {
    renderConnectionList()
    expect(screen.getByText('1 / 1')).toBeInTheDocument()
  })

  it('shows No credentials when a connection has no credentials', () => {
    renderConnectionList({
      connections: [{ ...baseConnection, has_credentials: false }],
    })
    expect(screen.getByText('No credentials')).toBeInTheDocument()
  })

  it('shows Not connected for a not-connected connection', () => {
    renderConnectionList({
      connections: [{ ...baseConnection, state: 'not_connected' }],
    })
    expect(screen.getByText('Not connected')).toBeInTheDocument()
  })

  it('shows empty state when there are no connections', () => {
    renderConnectionList({ connections: [] })
    expect(
      screen.getByText(
        'No connections yet. Browse available connectors and click Connect.'
      )
    ).toBeInTheDocument()
  })

  it('calls onSetActive when a connection row is clicked', () => {
    const { onSetActive } = renderConnectionList()
    fireEvent.click(screen.getByText('github'))
    expect(onSetActive).toHaveBeenCalledWith('conn-1')
  })

  it('shows Test, Refresh and Disconnect for a connected connection', () => {
    renderConnectionList()
    expect(screen.getByText('Test')).toBeInTheDocument()
    expect(screen.getByText('Refresh')).toBeInTheDocument()
    expect(screen.getByText('Disconnect')).toBeInTheDocument()
  })

  it('does not show Test for a not-connected connection', () => {
    renderConnectionList({
      connections: [{ ...baseConnection, state: 'not_connected' }],
    })
    expect(screen.queryByText('Test')).not.toBeInTheDocument()
  })

  it('calls onRefresh when Refresh is clicked', () => {
    const { onRefresh } = renderConnectionList()
    fireEvent.click(screen.getByText('Refresh'))
    expect(onRefresh).toHaveBeenCalledWith('conn-1')
  })

  it('calls onDisconnect when Disconnect is clicked', () => {
    const { onDisconnect } = renderConnectionList()
    fireEvent.click(screen.getByText('Disconnect'))
    expect(onDisconnect).toHaveBeenCalledWith('conn-1')
  })

  it('calls onDelete when Delete is clicked', () => {
    const { onDelete } = renderConnectionList()
    fireEvent.click(screen.getByText('Delete'))
    expect(onDelete).toHaveBeenCalledWith('conn-1')
  })

  it('shows Reconnect for needs_reauthorization state', () => {
    renderConnectionList({
      connections: [{ ...baseConnection, state: 'needs_reauthorization' }],
    })
    expect(screen.getByText('Reconnect')).toBeInTheDocument()
    expect(screen.queryByText('Test')).not.toBeInTheDocument()
  })

  it('shows the connection error in an alert when present', () => {
    renderConnectionList({ error: 'Connection error: timeout' })
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Connection error: timeout'
    )
  })
})
