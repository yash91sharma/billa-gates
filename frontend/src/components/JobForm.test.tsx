import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { BackupJob } from '../lib/types'
import JobForm from './JobForm'

const baseJob: BackupJob = {
  id: 'job-uuid',
  name: 'My Documents',
  source_label: 'documents',
  source_subpath: null,
  destination_label: 'main',
  restic_password: null,
  schedule_type: 'interval',
  schedule_value: '6h',
  enabled: true,
  retain_keep_last: null,
  retain_keep_hourly: null,
  retain_keep_daily: null,
  retain_keep_weekly: null,
  retain_keep_monthly: null,
  retain_keep_yearly: null,
  retain_keep_within: null,
  retain_keep_within_hourly: null,
  retain_keep_within_daily: null,
  retain_keep_within_weekly: null,
  retain_keep_within_monthly: null,
  retain_keep_within_yearly: null,
  exclude_patterns: null,
  exclude_caches: false,
  exclude_if_present: null,
  one_file_system: false,
  no_scan: false,
  tags: null,
  compression: null,
  pack_size: null,
  read_concurrency: null,
  timeout_hours: null,
  check_enabled: false,
  check_mode: null,
  check_subset_percent: null,
  check_timeout_hours: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  next_run_time: null,
  last_run: null,
  has_successful_run: false,
}

describe('JobForm', () => {
  describe('create mode (no job prop)', () => {
    it('renders the form', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByRole('form')).toBeInTheDocument()
    })

    it('shows required name field', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/name/i)).toBeInTheDocument()
    })

    it('shows editable password field', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      const pwField = screen.getByLabelText(/password/i)
      expect(pwField).not.toBeDisabled()
      expect(pwField).not.toHaveAttribute('readonly')
    })

    it('shows enabled checkbox checked by default', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/enabled/i)).toBeChecked()
    })

    it('shows source and destination as select elements', () => {
      render(
        <JobForm onSubmit={vi.fn()} sourceMounts={['meh1', 'meh2']} destinationMounts={['main']} />
      )
      const source = screen.getByLabelText(/source/i)
      const dest = screen.getByLabelText(/destination/i)
      expect(source.tagName).toBe('SELECT')
      expect(dest.tagName).toBe('SELECT')
    })

    it('lists the source mounts as options', () => {
      render(<JobForm onSubmit={vi.fn()} sourceMounts={['meh1', 'meh2']} destinationMounts={[]} />)
      const source = screen.getByLabelText(/source/i) as HTMLSelectElement
      const optionValues = Array.from(source.options).map((o) => o.value)
      expect(optionValues).toContain('meh1')
      expect(optionValues).toContain('meh2')
    })

    it('lists the destination mounts as options', () => {
      render(
        <JobForm onSubmit={vi.fn()} sourceMounts={[]} destinationMounts={['main', 'archive']} />
      )
      const dest = screen.getByLabelText(/destination/i) as HTMLSelectElement
      const optionValues = Array.from(dest.options).map((o) => o.value)
      expect(optionValues).toContain('main')
      expect(optionValues).toContain('archive')
    })

    it('shows a placeholder option when no selection has been made', () => {
      render(<JobForm onSubmit={vi.fn()} sourceMounts={['meh1']} destinationMounts={['main']} />)
      const source = screen.getByLabelText(/source/i) as HTMLSelectElement
      // First option should be a non-value placeholder so users must make an explicit choice.
      expect(source.options[0].value).toBe('')
    })

    it('shows a helpful message when no mounts are configured', () => {
      render(<JobForm onSubmit={vi.fn()} sourceMounts={[]} destinationMounts={[]} />)
      // Render an inline empty hint somewhere near the source field so the user knows
      // they need to mount a volume before they can create a job.
      expect(screen.getAllByText(/no.*mount|configure.*mount/i).length).toBeGreaterThan(0)
    })

    it('includes selected source and destination in onSubmit payload', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(
        <JobForm onSubmit={onSubmit} sourceMounts={['meh1', 'meh2']} destinationMounts={['main']} />
      )
      await user.selectOptions(screen.getByLabelText(/source/i), 'meh2')
      await user.selectOptions(screen.getByLabelText(/destination/i), 'main')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ source_label: 'meh2', destination_label: 'main' })
      )
    })

    it('includes source_subpath in the onSubmit payload when filled', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'photos')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ source_subpath: 'photos' }))
    })

    it('sends source_subpath=null when left blank', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ source_subpath: null }))
    })

    it('rejects a subpath containing "/"', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'a/b')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(screen.getByText(/subpath.*must not contain|cannot contain/i)).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('sends the password under the restic_password key the backend expects', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/password/i), 'hunter2')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
      expect(payload.restic_password).toBe('hunter2')
      expect(payload.password).toBeUndefined()
    })

    it('shows schedule input', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByTestId('schedule-input')).toBeInTheDocument()
    })

    it('submits with form data on submit', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} />)
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalled()
    })
  })

  describe('edit mode — password lock', () => {
    it('shows editable password field when has_successful_run=false', () => {
      render(<JobForm job={{ ...baseJob, has_successful_run: false }} onSubmit={vi.fn()} />)
      const pwField = screen.getByLabelText(/password/i)
      expect(pwField).not.toBeDisabled()
    })

    it('shows note about changeability when has_successful_run=false', () => {
      render(<JobForm job={{ ...baseJob, has_successful_run: false }} onSubmit={vi.fn()} />)
      expect(screen.getByText(/no backups.*run yet|still change/i)).toBeInTheDocument()
    })

    it('shows locked password field when has_successful_run=true', () => {
      render(<JobForm job={{ ...baseJob, has_successful_run: true }} onSubmit={vi.fn()} />)
      const pwField = screen.getByLabelText(/password/i)
      expect(pwField).toBeDisabled()
    })

    it('shows lock icon when password is locked', () => {
      render(<JobForm job={{ ...baseJob, has_successful_run: true }} onSubmit={vi.fn()} />)
      expect(screen.getByText(/🔒|permanent|cannot change/i)).toBeInTheDocument()
    })

    it('shows tooltip about restic key rotation when locked', () => {
      render(<JobForm job={{ ...baseJob, has_successful_run: true }} onSubmit={vi.fn()} />)
      expect(screen.getByText(/restic key/i)).toBeInTheDocument()
    })
  })

  describe('edit mode — source_subpath round-trip', () => {
    it('seeds the subpath input from the existing job and submits the same value when unchanged', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(
        <JobForm
          job={{ ...baseJob, source_subpath: 'photos' }}
          onSubmit={onSubmit}
          sourceMounts={['documents']}
          destinationMounts={['main']}
        />
      )
      expect((screen.getByLabelText(/subfolder|subpath/i) as HTMLInputElement).value).toBe('photos')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ source_subpath: 'photos' }))
    })
  })

  describe('edit mode — destination immutability', () => {
    it('shows destination field as read-only in edit mode', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      const destField = screen.getByLabelText(/destination/i)
      expect(destField).toBeDisabled()
    })

    it('shows explanation about destination immutability', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getByText(/cannot be changed after creation/i)).toBeInTheDocument()
    })

    it('shows link to destinations rename tool', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getByText(/remounted.*new label|rename tool/i)).toBeInTheDocument()
    })
  })

  describe('source label change warning', () => {
    it('shows amber warning banner when source label is changed', async () => {
      const user = userEvent.setup()
      render(
        <JobForm
          job={baseJob}
          onSubmit={vi.fn()}
          sourceMounts={['documents', 'photos']}
          destinationMounts={['main']}
        />
      )
      await user.selectOptions(screen.getByLabelText(/source/i), 'photos')
      expect(screen.getByText(/changing.*source|redirect.*future backups/i)).toBeInTheDocument()
    })

    it('does not show warning banner initially', () => {
      render(
        <JobForm
          job={baseJob}
          onSubmit={vi.fn()}
          sourceMounts={['documents', 'photos']}
          destinationMounts={['main']}
        />
      )
      expect(screen.queryByText(/changing.*source/i)).not.toBeInTheDocument()
    })
  })

  describe('409 conflict banner', () => {
    it('shows conflict banner with link to conflicting job', () => {
      render(<JobForm onSubmit={vi.fn()} conflictingJob={{ id: 'other-id', name: 'Other Job' }} />)
      expect(screen.getByText(/already.*job|conflict/i)).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /Other Job/i })).toBeInTheDocument()
    })
  })

  describe('check_enabled validation', () => {
    it('requires check_mode when check_enabled is toggled on', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} />)
      await user.click(screen.getByLabelText(/enable.*check|check_enabled/i))
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(
        screen.getByText(/check_mode.*required|verification mode required/i)
      ).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('requires check_subset_percent when check_mode is subset', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} />)
      await user.click(screen.getByLabelText(/enable.*check/i))
      await user.selectOptions(screen.getByLabelText(/check mode/i), 'subset')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(screen.getByText(/percent.*required|subset_percent/i)).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })
  })

  describe('backup options', () => {
    it('exposes exclude_patterns as a textarea (one pattern per line)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      const textarea = screen.getByLabelText(/exclude patterns/i)
      await user.type(textarea, '*.tmp{enter}node_modules/')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ exclude_patterns: ['*.tmp', 'node_modules/'] })
      )
    })

    it('sends null for exclude_patterns when textarea is blank', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ exclude_patterns: null }))
    })

    it('exposes exclude_caches as a checkbox (default off)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      const cb = screen.getByLabelText(/exclude caches/i) as HTMLInputElement
      expect(cb.checked).toBe(false)
      await user.click(cb)
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ exclude_caches: true }))
    })

    it('exposes tags as a comma-separated input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/^tags/i), 'daily, important')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ tags: ['daily', 'important'] })
      )
    })

    it('exposes compression as a select (auto/off/max)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.selectOptions(screen.getByLabelText(/^compression/i), 'max')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ compression: 'max' }))
    })

    it('exposes timeout_hours as a number input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/^timeout \(hours\)/i), '12')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ timeout_hours: 12 }))
    })

    it('round-trips backup options when editing an existing job', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(
        <JobForm
          job={{
            ...baseJob,
            exclude_patterns: ['*.tmp'],
            exclude_caches: true,
            tags: ['daily'],
            compression: 'max',
            timeout_hours: 6,
          }}
          onSubmit={onSubmit}
          sourceMounts={['documents']}
          destinationMounts={['main']}
        />
      )
      expect((screen.getByLabelText(/exclude patterns/i) as HTMLTextAreaElement).value).toBe(
        '*.tmp'
      )
      expect((screen.getByLabelText(/exclude caches/i) as HTMLInputElement).checked).toBe(true)
      expect((screen.getByLabelText(/^tags/i) as HTMLInputElement).value).toBe('daily')
      expect((screen.getByLabelText(/^compression/i) as HTMLSelectElement).value).toBe('max')
      expect((screen.getByLabelText(/^timeout \(hours\)/i) as HTMLInputElement).value).toBe('6')

      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          exclude_patterns: ['*.tmp'],
          exclude_caches: true,
          tags: ['daily'],
          compression: 'max',
          timeout_hours: 6,
        })
      )
    })
  })

  describe('collapsible sections', () => {
    it('shows Basic section expanded by default', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByText(/basic/i)).toBeInTheDocument()
      expect(screen.getByLabelText(/name/i)).toBeVisible()
    })

    it('shows Retention Policy section', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByText(/retention policy/i)).toBeInTheDocument()
    })

    it('expands Retention Policy section on click', async () => {
      const user = userEvent.setup()
      render(<JobForm onSubmit={vi.fn()} />)
      await user.click(screen.getByText(/retention policy/i))
      expect(screen.getByLabelText(/keep last/i)).toBeVisible()
    })

    it('shows Backup Options section', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByText(/backup options/i)).toBeInTheDocument()
    })

    it('shows Verification section', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByText(/verification/i)).toBeInTheDocument()
    })
  })
})
