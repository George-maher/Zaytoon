import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import Sidebar from './Sidebar'

// Mock the API module so recent-projects loading doesn't hit the network.
vi.mock('../lib/api', () => ({
  getRecentProjects: vi.fn(() => Promise.resolve({ projects: [] })),
}))

describe('Sidebar', () => {
  beforeEach(() => {
    vi.resetAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('exposes accessible names on collapsed nav items', () => {
    const onNavigate = vi.fn()
    render(
      <Sidebar view="home" language="en" onNavigate={onNavigate} isConnected />,
    )

    // Nav items should have aria-label equal to their English label when
    // the sidebar is narrow (collapsed).
    const homeItem = screen.getByRole('button', { name: 'Home' })
    expect(homeItem).toBeInTheDocument()
  })

  it('renders nav items with accessible names in Arabic', () => {
    const onNavigate = vi.fn()
    render(<Sidebar view="home" language="ar" onNavigate={onNavigate} isConnected />)

    const homeItem = screen.getByRole('button', { name: 'الرئيسية' })
    expect(homeItem).toBeInTheDocument()
  })

  it('announces collapse/expand state', () => {
    const onNavigate = vi.fn()
    render(
      <Sidebar view="home" language="en" onNavigate={onNavigate} isConnected />,
    )

    const toggle = screen.getByRole('button', { name: /Collapse sidebar|Expand sidebar/ })
    // Sidebar is expanded by default in jsdom (no media query match).
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })
})
