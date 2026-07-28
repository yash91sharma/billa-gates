import { MemoryRouter } from 'react-router-dom'
import { render, screen } from '@testing-library/react'
import PageHeader from './PageHeader'

function renderHeader(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe('PageHeader', () => {
  it('renders the title as the page heading', () => {
    // Every page gets exactly one h1. The Dashboard used to have none at all
    // while Jobs had one, so the heading outline changed shape as you moved
    // between them.
    renderHeader(<PageHeader title="Jobs" />)
    expect(screen.getByRole('heading', { level: 1, name: 'Jobs' })).toBeInTheDocument()
  })

  it('renders the description when given one', () => {
    renderHeader(<PageHeader title="Jobs" description="Scheduled backups and their history." />)
    expect(screen.getByText('Scheduled backups and their history.')).toBeInTheDocument()
  })

  it('renders actions', () => {
    renderHeader(<PageHeader title="Jobs" actions={<button>Create Job</button>} />)
    expect(screen.getByRole('button', { name: 'Create Job' })).toBeInTheDocument()
  })

  it('renders a breadcrumb trail with links for every item but the last', () => {
    renderHeader(
      <PageHeader
        title="Documents Backup"
        breadcrumb={[{ label: 'Jobs', to: '/jobs' }, { label: 'Documents Backup' }]}
      />
    )
    const nav = screen.getByRole('navigation', { name: /breadcrumb/i })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Jobs' })).toHaveAttribute('href', '/jobs')
    // The current page is not a link to itself.
    expect(screen.queryByRole('link', { name: 'Documents Backup' })).toBeNull()
  })

  it('omits the breadcrumb nav entirely when there are no items', () => {
    renderHeader(<PageHeader title="Jobs" />)
    expect(screen.queryByRole('navigation', { name: /breadcrumb/i })).toBeNull()
  })

  it('renders a status slot beside the title', () => {
    renderHeader(<PageHeader title="Documents Backup" status={<span>Enabled</span>} />)
    expect(screen.getByText('Enabled')).toBeInTheDocument()
  })
})
