import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ActionTooltip from './ActionTooltip'
import { TooltipProvider } from './ui/tooltip'

function renderTooltip(ui: React.ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>)
}

describe('ActionTooltip', () => {
  it('renders the control it wraps', () => {
    renderTooltip(
      <ActionTooltip content="what this does">
        <button>Run Now</button>
      </ActionTooltip>
    )
    expect(screen.getByRole('button', { name: 'Run Now' })).toBeInTheDocument()
  })

  it('reveals the explanation on hover', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button>Run Now</button>
      </ActionTooltip>
    )
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    await user.hover(screen.getByRole('button', { name: 'Run Now' }))

    await waitFor(() =>
      expect(screen.getAllByText('Starts a backup right now.').length).toBeGreaterThan(0)
    )
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
  })

  // WCAG 1.4.13 (Content on Hover or Focus) requires hover-revealed content to
  // be dismissible without moving the pointer. Pointer-out dismissal is not
  // asserted here: Radix keeps the content hoverable (also required by 1.4.13)
  // via a grace area computed from element geometry, which jsdom reports as all
  // zeros — that path can only be verified in a real browser.
  it('can be dismissed with Escape', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button>Run Now</button>
      </ActionTooltip>
    )
    await user.hover(screen.getByRole('button', { name: 'Run Now' }))
    await waitFor(() => expect(screen.getByRole('tooltip')).toBeInTheDocument())

    await user.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('tooltip')).not.toBeInTheDocument())
  })

  // Keyboard users never hover; Radix opens on focus, which only works because
  // focus events bubble from the wrapped control up to the trigger element.
  it('reveals the explanation on keyboard focus', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button>Run Now</button>
      </ActionTooltip>
    )

    await user.tab()

    expect(screen.getByRole('button', { name: 'Run Now' })).toHaveFocus()
    await waitFor(() =>
      expect(screen.getAllByText('Starts a backup right now.').length).toBeGreaterThan(0)
    )
  })

  // The whole point of a tooltip on a disabled control is explaining *why* it
  // is disabled. Browsers drop pointer events on a disabled button, so the
  // trigger has to be the wrapper — this is the case most likely to regress.
  it('reveals the explanation for a disabled control', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="A run is in progress." disabled>
        <button disabled>Unlock</button>
      </ActionTooltip>
    )

    await user.hover(screen.getByTestId('action-tooltip-trigger'))

    await waitFor(() =>
      expect(screen.getAllByText('A run is in progress.').length).toBeGreaterThan(0)
    )
  })

  // A disabled button is not focusable, so without a tabbable wrapper the
  // explanation would be unreachable by keyboard.
  it('makes a disabled control reachable by keyboard', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="A run is in progress." disabled>
        <button disabled>Unlock</button>
      </ActionTooltip>
    )

    await user.tab()

    expect(screen.getByTestId('action-tooltip-trigger')).toHaveFocus()
    await waitFor(() =>
      expect(screen.getAllByText('A run is in progress.').length).toBeGreaterThan(0)
    )
  })

  // ...but an enabled control must not gain a second tab stop, or every action
  // button in the header would take two presses to move past.
  it('does not add a tab stop when the control is enabled', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button>Run Now</button>
      </ActionTooltip>
    )
    expect(screen.getByTestId('action-tooltip-trigger')).not.toHaveAttribute('tabindex')

    await user.tab()

    expect(screen.getByRole('button', { name: 'Run Now' })).toHaveFocus()
  })

  it('still fires the wrapped control click handler', async () => {
    const user = userEvent.setup()
    const onClick = vi.fn()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button onClick={onClick}>Run Now</button>
      </ActionTooltip>
    )

    await user.click(screen.getByRole('button', { name: 'Run Now' }))

    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('describes the control for assistive tech while open', async () => {
    const user = userEvent.setup()
    renderTooltip(
      <ActionTooltip content="Starts a backup right now.">
        <button>Run Now</button>
      </ActionTooltip>
    )

    await user.hover(screen.getByRole('button', { name: 'Run Now' }))

    await waitFor(() =>
      expect(screen.getByTestId('action-tooltip-trigger')).toHaveAttribute('aria-describedby')
    )
  })
})
