import { Inbox } from 'lucide-react'
import { render, screen } from '@testing-library/react'
import EmptyState from './EmptyState'

describe('EmptyState', () => {
  it('renders the title', () => {
    render(<EmptyState title="No backup jobs yet" />)
    expect(screen.getByText('No backup jobs yet')).toBeInTheDocument()
  })

  it('renders the description when given one', () => {
    render(<EmptyState title="No runs yet" description="Runs appear here once the job fires." />)
    expect(screen.getByText('Runs appear here once the job fires.')).toBeInTheDocument()
  })

  it('omits the description element entirely when there is none', () => {
    const { container } = render(<EmptyState title="No runs yet" />)
    expect(container.querySelector('[data-slot="empty-state-description"]')).toBeNull()
  })

  it('renders an action when given one', () => {
    render(<EmptyState title="No backup jobs yet" action={<button>Create Job</button>} />)
    expect(screen.getByRole('button', { name: 'Create Job' })).toBeInTheDocument()
  })

  it('hides the icon from assistive tech — the title already says it', () => {
    const { container } = render(<EmptyState icon={Inbox} title="No runs yet" />)
    const icon = container.querySelector('svg')
    expect(icon).not.toBeNull()
    expect(icon).toHaveAttribute('aria-hidden', 'true')
  })

  it('renders without an icon', () => {
    const { container } = render(<EmptyState title="No runs yet" />)
    expect(container.querySelector('svg')).toBeNull()
  })
})
