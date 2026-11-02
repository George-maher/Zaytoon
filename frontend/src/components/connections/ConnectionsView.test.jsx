import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import ConnectionsView from './ConnectionsView'

vi.mock('../../lib/api', () => ({
  getConnectors: vi.fn(),
  getConnections: vi.fn(),
}))

vi.mock('../../i18n/strings', () => ({
  translate: vi.fn((lang, key) => {
    const en = {
      'connections.title': 'Connections',
      'connections.subtitle': 'Browse and connect to services, or configure an MCP server.',
      'connections.browse_connectors': 'Available connectors',
      'connections.my_connections': 'My connections',
      'connections.no_connectors': 'No connectors available.',
      'connections.no_connections': 'No connections yet. Browse available connectors and click Connect.',
      'connections.connect': 'Connect',
      'common.loading': 'LOADING…',
    }
    return en[key] ?? key
  }),
}))

import { getConnectors, getConnections } from '../../lib/api'

const baseConnectors = [
  {
    id: 'github',
    name: 'GitHub',
    description: 'Git repositories.',
    auth_method: 'oauth2',
    category: 'development',
    scopes: [],
    capabilities: [],
    version: '1.0.0',
    status: 'available',
  },
]

const baseConnections = [
  {
    id: 'conn-1',
    connector_id: 'github',
    state: 'connected',
    has_credentials: true,
    scopes: [],
    error: null,
    config: {},
  },
]

const renderView = () => render(<ConnectionsView language="en" />)

describe('ConnectionsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getConnectors.mockResolvedValue({ connectors: baseConnectors, error: null })
    getConnections.mockResolvedValue({ connections: baseConnections, error: null })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the page title and subtitle', async () => {
    renderView()
    await waitFor(() => {
      expect(screen.getByText('Connections')).toBeInTheDocument()
    })
    expect(
      screen.getByText(
        'Browse and connect to services, or configure an MCP server.'
      )
    ).toBeInTheDocument()
  })

  it('shows loading state while connectors and connections load', () => {
    renderView()
    expect(screen.getAllByText('LOADING…')).toHaveLength(2)
  })

  it('renders connectors and connections after loading', async () => {
    renderView()
    await waitFor(() => {
      expect(screen.getByText('GitHub')).toBeInTheDocument()
    })
    expect(screen.getByText('My connections')).toBeInTheDocument()
    expect(screen.getByText('github')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument()
  })

  it('shows empty connector list when no connectors are available', async () => {
    getConnectors.mockResolvedValue({ connectors: [], error: null })
    renderView()
    await waitFor(() => {
      expect(screen.getByText('No connectors available.')).toBeInTheDocument()
    })
  })

  it('shows empty connection list when no connections exist', async () => {
    getConnections.mockResolvedValue({ connections: [], error: null })
    renderView()
    await waitFor(() => {
      expect(
        screen.getByText(
          'No connections yet. Browse available connectors and click Connect.'
        )
      ).toBeInTheDocument()
    })
  })

  it('shows an error alert when getConnectors fails', async () => {
    // The API layer returns errors as `{ error }`, never thrown.
    getConnectors.mockResolvedValue({ connectors: [], error: 'Server error' })
    renderView()
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Server error')
    })
  })

  it('calls getConnectors and getConnections on mount', async () => {
    renderView()
    await waitFor(() => {
      expect(getConnectors).toHaveBeenCalledTimes(1)
      expect(getConnections).toHaveBeenCalledTimes(1)
    })
  })
})
