import { render, screen, within } from '@testing-library/react'
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
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ source_label: 'meh2', destination_label: 'main' })
      )
    })

    it('points the sentinel hint at the source mount when no subfolder is set', async () => {
      const user = userEvent.setup()
      render(
        <JobForm onSubmit={vi.fn()} sourceMounts={['documents']} destinationMounts={['main']} />
      )
      await user.selectOptions(screen.getByLabelText(/source/i), 'documents')
      expect(screen.getByText('/sources/documents')).toBeInTheDocument()
    })

    it('points the sentinel hint at the subfolder once one is set', async () => {
      // The folder actually backed up is /sources/<label>/<subfolder>, so that
      // is where `.billa_gates_check` has to live — a sentinel at the mount
      // root does not cover it, and the backend refuses the job without one.
      const user = userEvent.setup()
      render(<JobForm onSubmit={vi.fn()} sourceMounts={['nas']} destinationMounts={['main']} />)
      await user.selectOptions(screen.getByLabelText(/source/i), 'nas')
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'photos')
      expect(screen.getByText('/sources/nas/photos')).toBeInTheDocument()
    })

    it('includes source_subpath in the onSubmit payload when filled', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'photos')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ source_subpath: 'photos' }))
    })

    it('sends source_subpath=null when left blank', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ source_subpath: null }))
    })

    it('rejects a subpath containing "/"', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'a/b')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(screen.getByText(/subpath.*must not contain|cannot contain/i)).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('rejects a subpath of ".." (path traversal to the sources root)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), '..')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(screen.getByText(/subpath.*must not contain|cannot contain/i)).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('rejects a subpath of "." (resolves to the mount itself)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), '.')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(screen.getByText(/subpath.*must not contain|cannot contain/i)).toBeInTheDocument()
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it('accepts a normal subpath with dots, hyphens, and spaces', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/subfolder|subpath/i), 'photos 2024.v2-final')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ source_subpath: 'photos 2024.v2-final' })
      )
    })

    it('sends the password under the restic_password key the backend expects', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['meh1']} destinationMounts={['main']} />)
      await user.type(screen.getByLabelText(/password/i), 'hunter2')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
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
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalled()
    })
  })

  describe('edit mode — identity locks', () => {
    // The repository is created and encrypted at job-create time, so name,
    // destination and password all address it and lock as soon as the job
    // exists. There is no "before the first run" window.

    it('locks the password field in edit mode', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/password/i)).toBeDisabled()
    })

    it('locks the name field in edit mode', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/name/i)).toBeDisabled()
    })

    it('leaves the password field editable when creating', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/password/i)).not.toBeDisabled()
    })

    it('leaves the name field editable when creating', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByLabelText(/name/i)).not.toBeDisabled()
    })

    it('shows a lock icon for the locked fields', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getAllByText(/🔒/).length).toBeGreaterThan(0)
    })

    it('explains restic key rotation when the password is locked', () => {
      render(<JobForm job={baseJob} onSubmit={vi.fn()} />)
      expect(screen.getAllByText(/restic key/i).length).toBeGreaterThan(0)
    })

    it('shows the repository path the name maps to', () => {
      render(
        <JobForm
          job={{ ...baseJob, name: 'photos', destination_label: 'main' }}
          onSubmit={vi.fn()}
        />
      )
      expect(screen.getAllByText('/destinations/main/photos').length).toBeGreaterThan(0)
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

  describe('backup options', () => {
    it('exposes exclude_patterns as a textarea (one pattern per line)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      const textarea = screen.getByLabelText(/exclude patterns/i)
      await user.type(textarea, '*.tmp{enter}node_modules/')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ exclude_patterns: ['*.tmp', 'node_modules/'] })
      )
    })

    it('sends null for exclude_patterns when textarea is blank', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
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
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ exclude_caches: true }))
    })

    it('exposes tags as a comma-separated input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/^tags/i), 'daily, important')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
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
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ compression: 'max' }))
    })

    it('exposes timeout_hours as a number input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/^timeout \(hours\)/i), '12')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
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

  describe('integrity verification is not part of the job form', () => {
    // Integrity-check options are configured per-run in the "Trigger Integrity
    // Check" popup on the Job Details page — not stored on the job. The form
    // must therefore expose none of the check_* fields, and must never send
    // them in its onSubmit payload (the backend keeps its defaults).
    it('does not render any integrity-verification controls', () => {
      render(<JobForm onSubmit={vi.fn()} sourceMounts={['m']} destinationMounts={['d']} />)
      expect(screen.queryByText(/integrity verification/i)).not.toBeInTheDocument()
      expect(screen.queryByLabelText(/scheduled integrity check/i)).not.toBeInTheDocument()
      expect(screen.queryByLabelText(/check mode/i)).not.toBeInTheDocument()
      expect(screen.queryByLabelText(/check timeout/i)).not.toBeInTheDocument()
    })

    it('omits every check_* key from the onSubmit payload', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      const payload = onSubmit.mock.calls[0][0] as Record<string, unknown>
      expect(payload).not.toHaveProperty('check_enabled')
      expect(payload).not.toHaveProperty('check_mode')
      expect(payload).not.toHaveProperty('check_subset_percent')
      expect(payload).not.toHaveProperty('check_timeout_hours')
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

    it('explains that retention controls restic snapshots, not run history', async () => {
      const user = userEvent.setup()
      render(<JobForm onSubmit={vi.fn()} />)
      await user.click(screen.getByText(/retention policy/i))
      // The note must reference run history + the global "Keep last runs"
      // setting so the user knows where the other knob lives.
      expect(screen.getByText(/run history.*keep last runs/i)).toBeInTheDocument()
    })

    it('explains that retention policies do not automatically free physical space and require manual prune', async () => {
      const user = userEvent.setup()
      render(<JobForm onSubmit={vi.fn()} />)
      await user.click(screen.getByText(/retention policy/i))
      expect(
        screen.getByText(
          /reclaim physical.*manually run.*prune|reclaim physical.*manual prune|free physical.*manual prune/i
        )
      ).toBeInTheDocument()
    })

    it('shows Backup Options section', () => {
      render(<JobForm onSubmit={vi.fn()} />)
      expect(screen.getByText(/backup options/i)).toBeInTheDocument()
    })
  })

  // The user explicitly asked for tooltips on every field with description +
  // optional flag + example. These tests exercise that contract field-by-field
  // via the accessible description (aria-describedby + sr-only span). Using the
  // a11y description means we don't need to drive Radix's portal in jsdom.
  describe('field help tooltips', () => {
    // Helper: expand every collapsible/conditional region so every field is
    // in the DOM at once.
    async function renderAndExpandAll() {
      const user = userEvent.setup()
      const utils = render(<JobForm onSubmit={vi.fn()} />)
      await user.click(screen.getByText(/retention policy/i))
      return { user, ...utils }
    }

    // Single source of truth for the test matrix — each entry asserts the
    // label exists, has a help button, an accessible description, an example,
    // and the optional/required state matches the spec.
    const fields: Array<{
      label: RegExp
      optional: boolean
      // A keyword expected to appear inside the accessible description so we
      // know the right help text is wired to the right field.
      describes: RegExp
    }> = [
      { label: /^name$/i, optional: false, describes: /name|identify|dashboard/i },
      { label: /^source$/i, optional: false, describes: /mount|read-only|folder/i },
      { label: /^subfolder$/i, optional: true, describes: /subfolder|one level|slash/i },
      { label: /^destination$/i, optional: false, describes: /destination|repo|permanent/i },
      { label: /^password$/i, optional: false, describes: /encryption|password|rotate/i },
      { label: /^enabled$/i, optional: false, describes: /scheduler|schedule|manual/i },
      { label: /^keep last$/i, optional: true, describes: /most recent|--keep-last/i },
      { label: /^keep hourly$/i, optional: true, describes: /hour/i },
      { label: /^keep daily$/i, optional: true, describes: /day/i },
      { label: /^keep weekly$/i, optional: true, describes: /week/i },
      { label: /^keep monthly$/i, optional: true, describes: /month/i },
      { label: /^keep yearly$/i, optional: true, describes: /year/i },
      { label: /^keep within$/i, optional: true, describes: /duration|window|within/i },
      { label: /^keep within hourly$/i, optional: true, describes: /hour/i },
      { label: /^keep within daily$/i, optional: true, describes: /day/i },
      { label: /^keep within weekly$/i, optional: true, describes: /week/i },
      { label: /^keep within monthly$/i, optional: true, describes: /month/i },
      { label: /^keep within yearly$/i, optional: true, describes: /year/i },
      { label: /^exclude patterns$/i, optional: true, describes: /glob|pattern|skip/i },
      { label: /^exclude if present$/i, optional: true, describes: /file|skip|directory/i },
      { label: /^exclude caches$/i, optional: true, describes: /CACHEDIR\.TAG|cache/i },
      { label: /^one file system$/i, optional: true, describes: /filesystem|mount boundar/i },
      { label: /^no scan$/i, optional: true, describes: /pre-scan|progress/i },
      { label: /^tags$/i, optional: true, describes: /label|snapshot|tag/i },
      { label: /^compression$/i, optional: true, describes: /compress|auto|max/i },
      { label: /^pack size/i, optional: true, describes: /pack|MiB|128/i },
      { label: /^read concurrency$/i, optional: true, describes: /parallel|concurren/i },
      { label: /^timeout \(hours\)$/i, optional: true, describes: /timeout|kill|hours/i },
    ]

    // Drive the matrix with it.each so failures point at the specific field.
    it.each(fields)(
      'field $label has an info tooltip with description (optional=$optional)',
      async ({ label, optional, describes }) => {
        await renderAndExpandAll()
        const field = screen.getByLabelText(label)
        // Every field has an accessible description (the help text) wired up.
        expect(field).toHaveAccessibleDescription(describes)
        // Walk from the field's <label htmlFor> element to its row container.
        const labelEl = document.querySelector(`label[for="${field.id}"]`)
        const row = labelEl?.closest('[data-field-row]') as HTMLElement | null
        expect(row).not.toBeNull()
        if (optional) {
          expect(row!).toHaveTextContent(/\(optional\)/i)
        } else {
          expect(row!).not.toHaveTextContent(/\(optional\)/i)
        }
        // The row exposes a visible help button for sighted users.
        expect(within(row!).getByRole('button', { name: /more info/i })).toBeInTheDocument()
      }
    )

    it('every help tooltip text includes an explicit Example: snippet (for fields that have one)', async () => {
      await renderAndExpandAll()
      // Sanity: at least the obvious example-bearing fields advertise an example.
      const exampleFields = [/^name$/i, /^source$/i, /^subfolder$/i, /^keep last$/i, /^tags$/i]
      for (const re of exampleFields) {
        expect(screen.getByLabelText(re)).toHaveAccessibleDescription(/example/i)
      }
    })

    // The user asked tooltips to clearly communicate the "default" value when
    // a field has one. Every entry below describes a field whose blank/unchecked
    // state has a real semantic meaning the user should know about.
    const fieldsWithDefaults: Array<{ label: RegExp; defaultMatcher: RegExp }> = [
      { label: /^enabled$/i, defaultMatcher: /default:\s*on/i },
      { label: /^exclude caches$/i, defaultMatcher: /default:\s*off/i },
      { label: /^one file system$/i, defaultMatcher: /default:\s*off/i },
      { label: /^no scan$/i, defaultMatcher: /default:\s*off/i },
      { label: /^compression$/i, defaultMatcher: /default:\s*auto/i },
      { label: /^pack size/i, defaultMatcher: /default:\s*128/i },
      { label: /^read concurrency$/i, defaultMatcher: /default:.*restic/i },
      { label: /^timeout \(hours\)$/i, defaultMatcher: /default:.*global|default:.*settings/i },
    ]

    it.each(fieldsWithDefaults)(
      'field $label tooltip explicitly states its Default value',
      async ({ label, defaultMatcher }) => {
        await renderAndExpandAll()
        expect(screen.getByLabelText(label)).toHaveAccessibleDescription(defaultMatcher)
      }
    )

    // Fields whose value is one of a fixed set of choices should list the
    // choices under an explicit "Options:" label so users know what to pick.
    const fieldsWithEnumOptions: Array<{ label: RegExp; optionsMatcher: RegExp }> = [
      {
        label: /^compression$/i,
        optionsMatcher: /options:.*auto.*max.*off|options:.*auto.*off.*max/i,
      },
    ]

    it.each(fieldsWithEnumOptions)(
      'field $label tooltip lists its Options',
      async ({ label, optionsMatcher }) => {
        await renderAndExpandAll()
        expect(screen.getByLabelText(label)).toHaveAccessibleDescription(optionsMatcher)
      }
    )
  })

  describe('previously-missing design-doc fields', () => {
    it('exposes exclude_if_present as a textarea (one filename per line)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/exclude if present/i), '.nobackup{enter}.skipme')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ exclude_if_present: ['.nobackup', '.skipme'] })
      )
    })

    it('sends null for exclude_if_present when blank', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ exclude_if_present: null }))
    })

    it('exposes one_file_system as a checkbox (default off)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      const cb = screen.getByLabelText(/one file system/i) as HTMLInputElement
      expect(cb.checked).toBe(false)
      await user.click(cb)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ one_file_system: true }))
    })

    it('exposes no_scan as a checkbox (default off)', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      const cb = screen.getByLabelText(/no scan/i) as HTMLInputElement
      expect(cb.checked).toBe(false)
      await user.click(cb)
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ no_scan: true }))
    })

    it('exposes pack_size as a number input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/pack size/i), '512')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ pack_size: 512 }))
    })

    it('exposes read_concurrency as a number input', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(<JobForm onSubmit={onSubmit} sourceMounts={['m']} destinationMounts={['d']} />)
      await user.type(screen.getByLabelText(/read concurrency/i), '4')
      await user.type(screen.getByLabelText(/name/i), 'Test Job')
      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ read_concurrency: 4 }))
    })

    it('round-trips the new fields when editing an existing job', async () => {
      const onSubmit = vi.fn()
      const user = userEvent.setup()
      render(
        <JobForm
          job={{
            ...baseJob,
            exclude_if_present: ['.nobackup'],
            one_file_system: true,
            no_scan: true,
            pack_size: 256,
            read_concurrency: 2,
          }}
          onSubmit={onSubmit}
          sourceMounts={['documents']}
          destinationMounts={['main']}
        />
      )
      expect((screen.getByLabelText(/exclude if present/i) as HTMLTextAreaElement).value).toBe(
        '.nobackup'
      )
      expect((screen.getByLabelText(/one file system/i) as HTMLInputElement).checked).toBe(true)
      expect((screen.getByLabelText(/no scan/i) as HTMLInputElement).checked).toBe(true)
      expect((screen.getByLabelText(/pack size/i) as HTMLInputElement).value).toBe('256')
      expect((screen.getByLabelText(/read concurrency/i) as HTMLInputElement).value).toBe('2')

      await user.click(screen.getByRole('button', { name: /save|create|submit/i }))
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          exclude_if_present: ['.nobackup'],
          one_file_system: true,
          no_scan: true,
          pack_size: 256,
          read_concurrency: 2,
        })
      )
    })
  })
})
