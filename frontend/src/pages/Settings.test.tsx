import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as api from '../lib/api'
import type { AppSettings, RenameDestinationResult, ResticUpdateCheck } from '../lib/types'
import { renderWithProviders } from '../test/utils'
import Settings from './Settings'

vi.mock('../lib/api')

const makeSettings = (overrides: Partial<AppSettings> = {}): AppSettings => ({
  id: 1,
  ntfy_server_url: 'https://ntfy.sh',
  ntfy_topic: 'backup-alerts',
  ntfy_token: null,
  notify_on_start: false,
  notify_on_success: true,
  notify_on_failure: true,
  notify_on_warning: true,
  notify_on_verification: false,
  restic_version: '0.17.3',
  default_job_timeout_hours: 24,
  keep_last_runs: 100,
  auto_unlock: true,
  metadata_timeout_seconds: 600,
  ...overrides,
})

const updateCheckUpToDate: ResticUpdateCheck = {
  current: '0.17.3',
  latest: '0.17.3',
  update_available: false,
}

const updateCheckAvailable: ResticUpdateCheck = {
  current: '0.16.0',
  latest: '0.17.3',
  update_available: true,
}

const updateCheckUnknown: ResticUpdateCheck = {
  current: null,
  latest: null,
  update_available: null,
}

beforeEach(() => {
  vi.mocked(api.getSettings).mockResolvedValue(makeSettings())
  vi.mocked(api.updateSettings).mockResolvedValue(makeSettings())
  vi.mocked(api.testNtfy).mockResolvedValue({ ok: true })
  vi.mocked(api.checkResticUpdate).mockResolvedValue(updateCheckUpToDate)
  vi.mocked(api.listDestinationMounts).mockResolvedValue(['main', 'backup'])
  vi.mocked(api.renameDestination).mockResolvedValue({ affected_jobs: [] })
})

describe('Settings', () => {
  describe('ntfy form fields', () => {
    it('shows ntfy server URL field', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/server.*url|ntfy.*url/i)).toBeInTheDocument()
      )
    })

    it('shows ntfy topic field', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByLabelText(/topic/i)).toBeInTheDocument())
    })

    it('shows optional token field', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByLabelText(/token/i)).toBeInTheDocument())
    })

    it('shows notify_on_success checkbox', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/notify.*success|on.*success/i)).toBeInTheDocument()
      )
    })

    it('shows notify_on_failure checkbox', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/notify.*failure|on.*failure/i)).toBeInTheDocument()
      )
    })

    it('populates fields with current settings', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(
        makeSettings({ ntfy_topic: 'my-alerts', ntfy_server_url: 'https://ntfy.example.com' })
      )
      renderWithProviders(<Settings />)
      await waitFor(() => {
        expect(screen.getByDisplayValue('my-alerts')).toBeInTheDocument()
        expect(screen.getByDisplayValue('https://ntfy.example.com')).toBeInTheDocument()
      })
    })

    it('calls updateSettings on save', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/topic/i))
      await user.click(screen.getByRole('button', { name: /save/i }))
      expect(vi.mocked(api.updateSettings)).toHaveBeenCalled()
    })

    it('preserves the stored metadata_timeout_seconds when saving', async () => {
      // The Settings page has no field for metadata_timeout_seconds; the
      // backend defaults an omitted value to 600, so the save payload must
      // carry the loaded value through or every save silently resets it.
      const user = userEvent.setup()
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ metadata_timeout_seconds: 1200 }))
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/topic/i))
      await user.click(screen.getByRole('button', { name: /save/i }))
      expect(vi.mocked(api.updateSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ metadata_timeout_seconds: 1200 })
      )
    })

    it('sends null ntfy_token when the field was not touched (keep stored token)', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/topic/i))
      await user.click(screen.getByRole('button', { name: /save/i }))
      expect(vi.mocked(api.updateSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ ntfy_token: null })
      )
    })

    it('sends the typed ntfy_token when the user sets one', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/token/i))
      await user.type(screen.getByLabelText(/token/i), 'tok-123')
      await user.click(screen.getByRole('button', { name: /save/i }))
      expect(vi.mocked(api.updateSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ ntfy_token: 'tok-123' })
      )
    })

    it('sends an empty ntfy_token when the user types and clears it (clear stored token)', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/token/i))
      const tokenInput = screen.getByLabelText(/token/i)
      await user.type(tokenInput, 'tok-123')
      await user.clear(tokenInput)
      await user.click(screen.getByRole('button', { name: /save/i }))
      expect(vi.mocked(api.updateSettings)).toHaveBeenCalledWith(
        expect.objectContaining({ ntfy_token: '' })
      )
    })
  })

  describe('Test Notification button', () => {
    it('shows Test Notification button', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByRole('button', { name: /test.*notif|send.*test/i })).toBeInTheDocument()
      )
    })

    it('shows success message when test succeeds', async () => {
      const user = userEvent.setup()
      vi.mocked(api.testNtfy).mockResolvedValue({ ok: true })
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByRole('button', { name: /test.*notif|send.*test/i }))
      await user.click(screen.getByRole('button', { name: /test.*notif|send.*test/i }))
      await waitFor(() => expect(screen.getByText(/sent|success|delivered/i)).toBeInTheDocument())
    })

    it('shows error message when test fails', async () => {
      const user = userEvent.setup()
      vi.mocked(api.testNtfy).mockResolvedValue({ ok: false, error: 'connection refused' })
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByRole('button', { name: /test.*notif|send.*test/i }))
      await user.click(screen.getByRole('button', { name: /test.*notif|send.*test/i }))
      await waitFor(() =>
        expect(screen.getByText(/failed|error|connection refused/i)).toBeInTheDocument()
      )
    })
  })

  describe('restic update check', () => {
    it('shows "up to date" when current equals latest', async () => {
      vi.mocked(api.checkResticUpdate).mockResolvedValue(updateCheckUpToDate)
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByText(/up.to.date|latest/i)).toBeInTheDocument())
    })

    it('shows update available banner with latest version', async () => {
      vi.mocked(api.checkResticUpdate).mockResolvedValue(updateCheckAvailable)
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByText(/update.*available|new.*version/i)).toBeInTheDocument()
      )
      await waitFor(() => expect(screen.getByText(/0\.17\.3/)).toBeInTheDocument())
    })

    it('shows current version even when update is available', async () => {
      vi.mocked(api.checkResticUpdate).mockResolvedValue(updateCheckAvailable)
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByText(/0\.16\.0/)).toBeInTheDocument())
    })

    it('shows unknown/unreachable state when update_available is null', async () => {
      vi.mocked(api.checkResticUpdate).mockResolvedValue(updateCheckUnknown)
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(
          screen.getByText(/unavailable|unknown|could not.*check|github.*unreachable/i)
        ).toBeInTheDocument()
      )
    })

    it('shows Check Now button', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(
          screen.getByRole('button', { name: /check.*now|check.*update/i })
        ).toBeInTheDocument()
      )
    })

    it('calls checkResticUpdate when Check Now is clicked', async () => {
      const user = userEvent.setup()
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByRole('button', { name: /check.*now|check.*update/i }))
      await user.click(screen.getByRole('button', { name: /check.*now|check.*update/i }))
      expect(vi.mocked(api.checkResticUpdate)).toHaveBeenCalled()
    })
  })

  describe('rename destination form', () => {
    it('shows rename destination section', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByText(/rename.*destination|destination.*rename/i)).toBeInTheDocument()
      )
    })

    it('shows old label dropdown', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/current.*label|old.*label|from/i)).toBeInTheDocument()
      )
    })

    it('shows new label input', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByLabelText(/new.*label|to/i)).toBeInTheDocument())
    })

    it('shows affected jobs count on success', async () => {
      const user = userEvent.setup()
      vi.mocked(api.renameDestination).mockResolvedValue({
        affected_jobs: [
          { id: 'job-1', name: 'Job A' },
          { id: 'job-2', name: 'Job B' },
        ],
      })
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/current.*label|old.*label|from/i))
      await user.selectOptions(screen.getByLabelText(/current.*label|old.*label|from/i), 'main')
      await user.type(screen.getByLabelText(/new.*label|to/i), 'primary')
      await user.click(screen.getByRole('button', { name: /rename/i }))
      await waitFor(() =>
        expect(screen.getByText(/2.*job|job.*updated|affected/i)).toBeInTheDocument()
      )
    })

    it('shows 409 conflict error when new label already exists', async () => {
      const user = userEvent.setup()
      vi.mocked(api.renameDestination).mockRejectedValue(
        Object.assign(new Error('Conflict'), { status: 409 })
      )
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/new.*label|to/i))
      await user.type(screen.getByLabelText(/new.*label|to/i), 'existing')
      await user.click(screen.getByRole('button', { name: /rename/i }))
      await waitFor(() =>
        expect(screen.getByText(/already exists|conflict|409/i)).toBeInTheDocument()
      )
    })

    it('shows 422 validation error for invalid label characters', async () => {
      const user = userEvent.setup()
      vi.mocked(api.renameDestination).mockRejectedValue(
        Object.assign(new Error('Unprocessable Entity'), { status: 422 })
      )
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/new.*label|to/i))
      await user.type(screen.getByLabelText(/new.*label|to/i), 'invalid label!')
      await user.click(screen.getByRole('button', { name: /rename/i }))
      await waitFor(() => expect(screen.getByText(/invalid|validation|422/i)).toBeInTheDocument())
    })

    it('shows 404 error when old label directory no longer exists', async () => {
      const user = userEvent.setup()
      vi.mocked(api.renameDestination).mockRejectedValue(
        Object.assign(new Error('Not Found'), { status: 404 })
      )
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/current.*label|old.*label|from/i))
      await user.selectOptions(screen.getByLabelText(/current.*label|old.*label|from/i), 'main')
      await user.click(screen.getByRole('button', { name: /rename/i }))
      await waitFor(() =>
        expect(screen.getByText(/not found|no longer.*exist|404/i)).toBeInTheDocument()
      )
    })
  })

  describe('default job timeout', () => {
    it('shows default_job_timeout_hours field', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/timeout|default.*timeout/i)).toBeInTheDocument()
      )
    })

    it('shows current timeout value', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ default_job_timeout_hours: 48 }))
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByDisplayValue('48')).toBeInTheDocument())
    })
  })

  describe('notification toggles initial state', () => {
    it('reflects notify_on_success setting', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ notify_on_success: false }))
      renderWithProviders(<Settings />)
      await waitFor(() => {
        const checkbox = screen.getByLabelText(/notify.*success|on.*success/i)
        expect(checkbox).not.toBeChecked()
      })
    })

    it('reflects notify_on_failure setting', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ notify_on_failure: true }))
      renderWithProviders(<Settings />)
      await waitFor(() => {
        const checkbox = screen.getByLabelText(/notify.*failure|on.*failure/i)
        expect(checkbox).toBeChecked()
      })
    })

    it('shows notify_on_start checkbox', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/notify.*start|on.*start/i)).toBeInTheDocument()
      )
    })

    it('shows notify_on_verification checkbox', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/notify.*verif|on.*verif/i)).toBeInTheDocument()
      )
    })

    it('shows notify_on_warning checkbox', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/notify.*warning|on.*warning/i)).toBeInTheDocument()
      )
    })

    it('reflects notify_on_warning setting', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ notify_on_warning: false }))
      renderWithProviders(<Settings />)
      await waitFor(() => {
        const checkbox = screen.getByLabelText(/notify.*warning|on.*warning/i)
        expect(checkbox).not.toBeChecked()
      })
    })

    it('shows keep_last_runs input', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByLabelText(/keep.*last.*run|run.*history/i)).toBeInTheDocument()
      )
    })

    it('reflects keep_last_runs setting', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ keep_last_runs: 250 }))
      renderWithProviders(<Settings />)
      await waitFor(() => {
        const input = screen.getByLabelText(/keep.*last.*run|run.*history/i) as HTMLInputElement
        expect(input.value).toBe('250')
      })
    })
  })

  describe('error states', () => {
    it('shows error when settings API fails to load', async () => {
      vi.mocked(api.getSettings).mockRejectedValue(new Error('Network error'))
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not load/i)).toBeInTheDocument()
      )
    })

    it('shows error when save fails', async () => {
      const user = userEvent.setup()
      vi.mocked(api.updateSettings).mockRejectedValue(
        Object.assign(new Error('Server Error'), { status: 500 })
      )
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/topic/i))
      await user.click(screen.getByRole('button', { name: /save/i }))
      await waitFor(() =>
        expect(screen.getByText(/error|failed|could not save/i)).toBeInTheDocument()
      )
    })
  })

  describe('tooltip help', () => {
    it('renders a "More info" tooltip trigger for every setting field', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/topic/i))
      // One per: server URL, topic, token, notify_on_start, notify_on_success,
      // notify_on_failure, notify_on_warning, notify_on_verification, timeout,
      // keep_last_runs, auto_unlock. That's 11.
      const infoButtons = screen.getAllByRole('button', { name: /more info/i })
      expect(infoButtons.length).toBeGreaterThanOrEqual(11)
    })

    it('links the ntfy server URL input to help text via aria-describedby', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/server.*url|ntfy.*url/i))
      const input = screen.getByLabelText(/server.*url|ntfy.*url/i)
      const id = input.getAttribute('aria-describedby')
      expect(id).toBeTruthy()
      const help = document.getElementById(id!)
      expect(help).toBeInTheDocument()
      expect(help!.textContent).toMatch(/ntfy/i)
    })

    it('links the default timeout input to help text via aria-describedby', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/timeout|default.*timeout/i))
      const input = screen.getByLabelText(/timeout|default.*timeout/i)
      const id = input.getAttribute('aria-describedby')
      expect(id).toBeTruthy()
      expect(document.getElementById(id!)?.textContent).toMatch(/timeout|abort|kill/i)
    })

    it('links keep_last_runs input to help text via aria-describedby', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/keep.*last.*run|run.*history/i))
      const input = screen.getByLabelText(/keep.*last.*run|run.*history/i)
      const id = input.getAttribute('aria-describedby')
      expect(id).toBeTruthy()
      expect(document.getElementById(id!)?.textContent).toMatch(/run|history|deleted|older/i)
    })

    it('links auto_unlock checkbox to help text via aria-describedby', async () => {
      renderWithProviders(<Settings />)
      await waitFor(() => screen.getByLabelText(/auto.*unlock|stale.*lock/i))
      const input = screen.getByLabelText(/auto.*unlock|stale.*lock/i)
      const id = input.getAttribute('aria-describedby')
      expect(id).toBeTruthy()
      expect(document.getElementById(id!)?.textContent).toMatch(/lock|restic|unlock/i)
    })
  })

  describe('restic version display', () => {
    it('shows installed restic version from settings', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ restic_version: '0.17.3' }))
      renderWithProviders(<Settings />)
      await waitFor(() => expect(screen.getByText(/0\.17\.3/)).toBeInTheDocument())
    })

    it('shows "not detected" when restic_version is null', async () => {
      vi.mocked(api.getSettings).mockResolvedValue(makeSettings({ restic_version: null }))
      renderWithProviders(<Settings />)
      await waitFor(() =>
        expect(screen.getByText(/not detected|not installed|not found/i)).toBeInTheDocument()
      )
    })
  })
})
