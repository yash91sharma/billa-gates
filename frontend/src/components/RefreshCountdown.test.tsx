import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import RefreshCountdown from './RefreshCountdown'

const MINUTE = 60_000

// The ticker's setState is driven by a timer rather than by an event or a
// resolving query, so nothing wraps it for us the way `waitFor` wraps React
// Query's — advancing the clock inside act() is what keeps the re-render
// flushed and the suite free of act() warnings.
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('RefreshCountdown', () => {
  describe('while auto-refresh is on', () => {
    it('names the mode and shows a full interval immediately after an update', () => {
      render(<RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />)

      expect(screen.getByText('Auto-updating')).toBeInTheDocument()
      expect(screen.getByText('Next update in 1:00')).toBeInTheDocument()
    })

    it('counts down as the clock advances', async () => {
      // The countdown is driven by a 1s interval — advance a fake clock rather
      // than waiting real seconds (see Dashboard.test.tsx).
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        render(<RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />)
        expect(screen.getByText('Next update in 1:00')).toBeInTheDocument()

        await advance(18_000)
        expect(screen.getByText('Next update in 0:42')).toBeInTheDocument()

        await advance(37_000)
        expect(screen.getByText('Next update in 0:05')).toBeInTheDocument()
      } finally {
        vi.useRealTimers()
      }
    })

    it('says it is updating once the countdown runs out', async () => {
      // The refetch fires at the interval but takes time to land, so a bare
      // "0:00" would sit there looking stuck for the whole round trip.
      vi.useFakeTimers({ shouldAdvanceTime: true })
      try {
        render(<RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />)

        await advance(MINUTE)

        expect(screen.getByText('Updating now…')).toBeInTheDocument()
        expect(screen.queryByText(/Next update in/)).not.toBeInTheDocument()
      } finally {
        vi.useRealTimers()
      }
    })

    it('says it is updating while a fetch is in flight, even mid-countdown', () => {
      render(
        <RefreshCountdown
          updatedAt={Date.now()}
          intervalMs={MINUTE}
          isFetching
          onRefresh={() => {}}
        />
      )

      expect(screen.getByText('Updating now…')).toBeInTheDocument()
      expect(screen.queryByText(/Next update in/)).not.toBeInTheDocument()
    })

    it('uses the info tokens rather than a hardcoded colour', () => {
      const { container } = render(
        <RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />
      )

      const bar = container.querySelector('[data-slot="refresh-countdown"]')
      expect(bar).toHaveClass('bg-info-subtle')
      expect(bar).toHaveClass('text-info-subtle-foreground')
    })
  })

  describe('while auto-refresh is off', () => {
    it('names the mode and shows no countdown', () => {
      render(<RefreshCountdown updatedAt={Date.now()} intervalMs={null} onRefresh={() => {}} />)

      expect(screen.getByText('Auto-update off')).toBeInTheDocument()
      expect(screen.queryByText(/Next update in/)).not.toBeInTheDocument()
      expect(screen.queryByText('Updating now…')).not.toBeInTheDocument()
    })

    it('says how long ago the data landed', () => {
      render(
        <RefreshCountdown
          updatedAt={Date.now() - 3 * MINUTE}
          intervalMs={null}
          onRefresh={() => {}}
        />
      )

      expect(screen.getByText('Updated 3m ago')).toBeInTheDocument()
    })

    it('shows no timestamp at all before the first fetch has landed', () => {
      // dataUpdatedAt is 0 until a query first succeeds; "Updated 56y ago" is
      // worse than saying nothing.
      render(<RefreshCountdown updatedAt={0} intervalMs={null} onRefresh={() => {}} />)

      expect(screen.getByText('Auto-update off')).toBeInTheDocument()
      expect(screen.queryByText(/Updated/)).not.toBeInTheDocument()
    })

    it('uses the neutral tokens rather than a hardcoded colour', () => {
      const { container } = render(
        <RefreshCountdown updatedAt={Date.now()} intervalMs={null} onRefresh={() => {}} />
      )

      const bar = container.querySelector('[data-slot="refresh-countdown"]')
      expect(bar).toHaveClass('bg-neutral-subtle')
      expect(bar).toHaveClass('text-neutral-subtle-foreground')
    })
  })

  describe('the Refresh button', () => {
    it('calls onRefresh when clicked', async () => {
      const user = userEvent.setup()
      const onRefresh = vi.fn()
      render(<RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={onRefresh} />)

      await user.click(screen.getByRole('button', { name: /refresh/i }))

      expect(onRefresh).toHaveBeenCalledTimes(1)
    })

    it('is offered while auto-refresh is off too', async () => {
      const user = userEvent.setup()
      const onRefresh = vi.fn()
      render(<RefreshCountdown updatedAt={Date.now()} intervalMs={null} onRefresh={onRefresh} />)

      await user.click(screen.getByRole('button', { name: /refresh/i }))

      expect(onRefresh).toHaveBeenCalledTimes(1)
    })

    it('blocks itself and says so while a refresh is in flight', async () => {
      const user = userEvent.setup()
      const onRefresh = vi.fn()
      render(
        <RefreshCountdown
          updatedAt={Date.now()}
          intervalMs={MINUTE}
          isRefreshing
          onRefresh={onRefresh}
        />
      )

      const button = screen.getByRole('button', { name: /refreshing/i })
      expect(button).toBeDisabled()

      await user.click(button)
      expect(onRefresh).not.toHaveBeenCalled()
    })
  })

  describe('accessibility', () => {
    it('conveys the mode as text, never by the status dot alone', () => {
      // WCAG 1.4.1: the dot is decorative and announced by nothing, so the
      // wording has to carry the state on its own.
      const { container } = render(
        <RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />
      )

      const dot = container.querySelector('[data-slot="refresh-countdown-dot"]')
      expect(dot).toHaveAttribute('aria-hidden', 'true')
      expect(screen.getByText('Auto-updating')).toBeInTheDocument()
    })

    it('does not announce the ticking countdown to a screen reader', () => {
      // A per-second live region is read out relentlessly for the whole of a
      // multi-hour backup.
      const { container } = render(
        <RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />
      )

      expect(container.querySelector('[aria-live]')).toBeNull()
    })
  })

  it('stops its ticker when unmounted', () => {
    vi.useFakeTimers()
    try {
      const { unmount } = render(
        <RefreshCountdown updatedAt={Date.now()} intervalMs={MINUTE} onRefresh={() => {}} />
      )
      expect(vi.getTimerCount()).toBeGreaterThan(0)

      unmount()

      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
