import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import ConnectorList from './ConnectorList'
import { translate } from '../../i18n/strings'

// Render in English. `translate('en', 'common.language_ar')` is 'العربية',
// which would flip the auth-method chip to its Arabic label; override it so
// the English chip is asserted like a real English UI.
const t = (key) => {
  if (key === 'common.language_ar') return 'English'
  return translate('en', key)
}

const baseConnectors = [
  {
    id: 'github',
    name: 'GitHub',
    description: 'Git repositories.',
    auth_method: 'oauth2',
    category: 'development',
    scopes: ['repo', 'read:user'],
    capabilities: [],
    version: '1.0.0',
    status: 'available',
  },
  {
    id: 'slack',
    name: 'Slack',
    description: 'Team chat.',
    auth_method: 'api_key',
    category: 'communication',
    scopes: [],
    capabilities: [],
    version: '1.0.0',
    status: 'available',
  },
]

const renderConnectorList = (props = {}) => {
  const onConnect = vi.fn()
  return {
    onConnect,
    ...render(
      <ConnectorList
        connectors={props.connectors ?? baseConnectors}
        loading={props.loading ?? false}
        onConnect={onConnect}
        t={props.t ?? t}
      />
    ),
  }
}

describe('ConnectorList', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows a loading state when loading is true', () => {
    renderConnectorList({ loading: true })
    expect(screen.getByText('Available connectors')).toBeInTheDocument()
    expect(screen.getAllByText('LOADING…')).toHaveLength(1)
  })

  it('renders each connector with name, description, auth method and scopes', () => {
    renderConnectorList()
    expect(screen.getByText('GitHub')).toBeInTheDocument()
    expect(screen.getByText('Git repositories.')).toBeInTheDocument()
    expect(screen.getByText('OAuth 2.0')).toBeInTheDocument()
    expect(screen.getByText('Scopes: repo, read:user')).toBeInTheDocument()
  })

  it('calls onConnect with the connector id when Connect is clicked', () => {
    const { onConnect } = renderConnectorList()
    fireEvent.click(screen.getAllByRole('button', { name: 'Connect' })[0])
    expect(onConnect).toHaveBeenCalledWith('github')
  })

  it('shows empty state when no connectors are available', () => {
    renderConnectorList({ connectors: [] })
    expect(screen.getByText('No connectors available.')).toBeInTheDocument()
  })

  it('filters connectors by search text', () => {
    renderConnectorList()
    const input = screen.getByPlaceholderText('No connectors available.')
    fireEvent.change(input, { target: { value: 'slack' } })
    expect(screen.queryByText('GitHub')).not.toBeInTheDocument()
    expect(screen.getByText('Slack')).toBeInTheDocument()
  })

  it('is case-insensitive in search', () => {
    renderConnectorList()
    const input = screen.getByPlaceholderText('No connectors available.')
    fireEvent.change(input, { target: { value: 'GITHUB' } })
    expect(screen.getByText('GitHub')).toBeInTheDocument()
  })

  it('renders Arabic labels when language is Arabic', () => {
    const arT = (key) => {
      const ar = {
        'connections.browse_connectors': 'استعرض الوصلات',
        'connections.connector': 'الموصل',
        'connections.no_connectors': 'لا توجد وصلات متاحة.',
        'connections.connect': 'اتصل',
        'common.loading': 'جارٍ التحميل…',
        'common.language_ar': 'العربية',
      }
      return ar[key] || key
    }
    renderConnectorList({ t: arT })
    expect(screen.getByText('استعرض الوصلات')).toBeInTheDocument()
    expect(screen.getAllByText('اتصل').length).toBeGreaterThan(0)
  })
})
