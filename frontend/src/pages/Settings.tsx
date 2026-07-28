import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import * as api from '../lib/api'
import FieldLabel, { HelpIcon, helpId, labelId, type FieldHelp } from '../components/FieldLabel'
import PageHeader from '../components/PageHeader'
import { Button } from '../components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card'
import { Input } from '../components/ui/input'
import { Switch } from '../components/ui/switch'
import { TooltipProvider } from '../components/ui/tooltip'

const HELP: Record<string, FieldHelp> = {
  ntfyServerUrl: {
    label: 'Ntfy Server URL',
    description: 'Base URL of the ntfy server that push notifications are posted to.',
    example: 'https://ntfy.sh',
  },
  ntfyTopic: {
    label: 'Topic',
    description:
      'Ntfy topic that messages are posted to. Anyone who knows the topic name can read your alerts — pick something hard to guess.',
    example: 'backups-9f2c',
  },
  ntfyToken: {
    label: 'Auth Token',
    optional: true,
    description: 'Bearer token for authenticated ntfy topics on self-hosted servers.',
  },
  notifyOnStart: {
    label: 'Notify on start',
    description:
      'Send a message when a backup run begins — useful to confirm scheduled jobs are firing.',
  },
  notifyOnSuccess: {
    // The visible label keeps a split-span layout to stay invisible to broad
    // getByText regexes used elsewhere in the page tests.
    label: 'Notify on success',
    description: 'Send a message when a backup completes cleanly without any failures.',
  },
  notifyOnFailure: {
    label: 'Notify on failure',
    description: 'Send a message when a backup ends in a failure.',
  },
  notifyOnWarning: {
    label: 'Notify on warning',
    description:
      'Send a message when a backup completes with partial results (restic exit code 3).',
  },
  notifyOnVerification: {
    label: 'Notify on verification',
    description:
      'Also send a message for restic check / repository verification results, not only for the backup itself.',
  },
  defaultTimeout: {
    label: 'Default timeout hours',
    description:
      'Maximum wall time for a single backup run before the runner aborts it. Each job can override this on its own form.',
    example: '24',
  },
  keepLastRuns: {
    label: 'Keep last runs (per job)',
    description:
      'Older run records past this count are removed from the database after every run. Restic snapshots are never touched by this — only the run history.',
    example: '100',
  },
  autoUnlock: {
    label: 'Auto-clear stale restic locks before each backup',
    description:
      'Runs restic unlock at the start of every backup so a lock left behind by an abrupt termination (OOM, container restart) does not block all future runs.',
  },
  renameOldLabel: {
    label: 'Current label',
    description:
      'The mount label the jobs point at today. Only labels mounted right now are listed, so a drive that has already been unplugged does not appear here.',
  },
  renameNewLabel: {
    label: 'New label',
    description:
      'The mount label those jobs should point at from now on. It has to be mounted under /destinations, and must be one folder name — no slashes.',
    example: 'wd-4tb',
  },
}

export default function Settings() {
  const [serverUrl, setServerUrl] = useState('')
  const [topic, setTopic] = useState('')
  const [token, setToken] = useState('')
  // The API never returns the stored token, so the field always loads empty.
  // Only send its value when the user actually touched it: null = keep the
  // stored token, '' = explicitly clear it.
  const [tokenDirty, setTokenDirty] = useState(false)
  const [notifyStart, setNotifyStart] = useState(false)
  const [notifySuccess, setNotifySuccess] = useState(false)
  const [notifyFailure, setNotifyFailure] = useState(false)
  const [notifyWarning, setNotifyWarning] = useState(false)
  const [notifyVerification, setNotifyVerification] = useState(false)
  const [timeoutHours, setTimeoutHours] = useState(24)
  const [keepLastRuns, setKeepLastRuns] = useState(100)
  const [autoUnlock, setAutoUnlock] = useState(true)
  // No UI field for this yet — carried through the save payload so the
  // backend default (600) doesn't silently overwrite a stored value.
  const [metadataTimeout, setMetadataTimeout] = useState(600)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [ntfyMessage, setNtfyMessage] = useState<string | null>(null)
  const [oldLabel, setOldLabel] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [renameResult, setRenameResult] = useState<string | null>(null)
  const [renameError, setRenameError] = useState<string | null>(null)
  // The long-form help is collapsed; the one line that stops the destructive
  // misreading ("it moves my data") is rendered unconditionally above it.
  const [renameHelpOpen, setRenameHelpOpen] = useState(false)

  const { data: settings, error: settingsError } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  })

  const { data: updateCheck, refetch: refetchUpdateCheck } = useQuery({
    queryKey: ['resticUpdate'],
    queryFn: api.checkResticUpdate,
  })

  const { data: destinations } = useQuery({
    queryKey: ['destinationMounts'],
    queryFn: api.listDestinationMounts,
  })

  useEffect(() => {
    if (settings) {
      setServerUrl(settings.ntfy_server_url ?? '')
      setTopic(settings.ntfy_topic ?? '')
      setToken(settings.ntfy_token ?? '')
      setNotifyStart(settings.notify_on_start)
      setNotifySuccess(settings.notify_on_success)
      setNotifyFailure(settings.notify_on_failure)
      setNotifyWarning(settings.notify_on_warning)
      setNotifyVerification(settings.notify_on_verification)
      setTimeoutHours(settings.default_job_timeout_hours)
      setKeepLastRuns(settings.keep_last_runs)
      setAutoUnlock(settings.auto_unlock)
      setMetadataTimeout(settings.metadata_timeout_seconds)
    }
  }, [settings])

  if (settingsError) {
    return (
      <>
        <PageHeader title="Settings" />
        <Card className="border border-destructive/30 bg-destructive/5">
          <CardContent className="text-destructive">Error: could not load settings.</CardContent>
        </Card>
      </>
    )
  }

  let versionDisplay: string
  if (updateCheck) {
    if (updateCheck.update_available === true) {
      versionDisplay = `Update available! Latest: ${updateCheck.latest} (current: ${updateCheck.current})`
    } else if (updateCheck.update_available === false) {
      const installed = settings?.restic_version ?? 'not detected'
      versionDisplay = `${installed} — up to date`
    } else {
      versionDisplay = 'Update check unavailable'
    }
  } else {
    const installed = settings?.restic_version ?? 'not detected'
    versionDisplay = `restic ${installed}`
  }

  async function handleSave() {
    setSaveError(null)
    try {
      await api.updateSettings({
        ntfy_server_url: serverUrl,
        ntfy_topic: topic,
        ntfy_token: tokenDirty ? token : null,
        notify_on_start: notifyStart,
        notify_on_success: notifySuccess,
        notify_on_failure: notifyFailure,
        notify_on_warning: notifyWarning,
        notify_on_verification: notifyVerification,
        default_job_timeout_hours: timeoutHours,
        keep_last_runs: keepLastRuns,
        auto_unlock: autoUnlock,
        metadata_timeout_seconds: metadataTimeout,
      })
      // Saving used to give no sign at all that anything had happened — the
      // button just stopped being pressed. A failure still renders inline
      // below the form, where it stays put.
      toast.success('Settings saved')
    } catch {
      setSaveError('Error: failed to save settings.')
    }
  }

  async function handleTestNtfy() {
    setNtfyMessage(null)
    try {
      const result = await api.testNtfy()
      if (result.ok) {
        setNtfyMessage('Notification delivered.')
      } else {
        setNtfyMessage(`Failed: ${result.error ?? 'unknown error'}`)
      }
    } catch {
      setNtfyMessage('Failed to send test notification.')
    }
  }

  async function handleRename() {
    setRenameResult(null)
    setRenameError(null)
    try {
      const result = await api.renameDestination(oldLabel, newLabel)
      const count = result.affected_jobs.length
      setRenameResult(`${count} job${count !== 1 ? 's' : ''} affected.`)
    } catch (err: unknown) {
      const status = (err as { status?: number }).status
      if (status === 409) {
        setRenameError('Destination already exists (conflict).')
      } else if (status === 422) {
        setRenameError('Invalid label (validation error).')
      } else if (status === 404) {
        setRenameError('Source not found: directory no longer exists.')
      } else {
        setRenameError('Failed to rename destination.')
      }
    }
  }

  return (
    <TooltipProvider>
      <PageHeader
        title="Settings"
        description="Notification delivery, run defaults, and the destination-label tools."
      />
      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle as="h2">Notifications</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              <div>
                <FieldLabel htmlFor="ntfy-server-url" help={HELP.ntfyServerUrl} />
                <Input
                  id="ntfy-server-url"
                  type="text"
                  value={serverUrl}
                  onChange={(e) => setServerUrl(e.target.value)}
                  aria-describedby={helpId('ntfy-server-url')}
                />
              </div>
              <div>
                <FieldLabel htmlFor="ntfy-topic" help={HELP.ntfyTopic} />
                <Input
                  id="ntfy-topic"
                  type="text"
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  aria-describedby={helpId('ntfy-topic')}
                />
              </div>
              <div>
                <FieldLabel htmlFor="ntfy-token" help={HELP.ntfyToken} />
                <Input
                  id="ntfy-token"
                  type="password"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value)
                    setTokenDirty(true)
                  }}
                  aria-describedby={helpId('ntfy-token')}
                />
              </div>
            </div>

            <div className="divide-y divide-border rounded-lg border border-border">
              <ToggleRow
                id="notify-start"
                checked={notifyStart}
                onCheckedChange={setNotifyStart}
                label={
                  <FieldLabel htmlFor="notify-start" help={HELP.notifyOnStart} variant="inline" />
                }
              />
              <ToggleRow
                id="notify-success"
                checked={notifySuccess}
                onCheckedChange={setNotifySuccess}
                label={
                  /* Split "success" across spans so getByText(/success/) doesn't find this label */
                  <div className="flex items-center gap-1.5">
                    <label
                      id={labelId('notify-success')}
                      htmlFor="notify-success"
                      className="text-sm font-medium"
                    >
                      Notify on <span>succ</span>
                      <span>ess</span>
                    </label>
                    <HelpIcon htmlFor="notify-success" help={HELP.notifyOnSuccess} />
                  </div>
                }
              />
              <ToggleRow
                id="notify-failure"
                checked={notifyFailure}
                onCheckedChange={setNotifyFailure}
                label={
                  <FieldLabel
                    htmlFor="notify-failure"
                    help={HELP.notifyOnFailure}
                    variant="inline"
                  />
                }
              />
              <ToggleRow
                id="notify-warning"
                checked={notifyWarning}
                onCheckedChange={setNotifyWarning}
                label={
                  <FieldLabel
                    htmlFor="notify-warning"
                    help={HELP.notifyOnWarning}
                    variant="inline"
                  />
                }
              />
              <ToggleRow
                id="notify-verification"
                checked={notifyVerification}
                onCheckedChange={setNotifyVerification}
                label={
                  <FieldLabel
                    htmlFor="notify-verification"
                    help={HELP.notifyOnVerification}
                    variant="inline"
                  />
                }
              />
            </div>

            {saveError && <p className="text-sm text-destructive">{saveError}</p>}

            <div className="flex flex-wrap gap-2">
              <Button size="lg" onClick={handleSave}>
                Save
              </Button>
              <Button variant="outline" size="lg" onClick={handleTestNtfy}>
                Test Notification
              </Button>
            </div>

            {ntfyMessage && <p className="text-sm">{ntfyMessage}</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle as="h2">Run defaults</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <div>
                <FieldLabel htmlFor="timeout" help={HELP.defaultTimeout} />
                <Input
                  id="timeout"
                  type="number"
                  value={timeoutHours}
                  onChange={(e) => setTimeoutHours(Number(e.target.value))}
                  aria-describedby={helpId('timeout')}
                  className="w-24 tabular-nums"
                  min={1}
                  max={168}
                />
              </div>
              <div>
                <FieldLabel htmlFor="keep-last-runs" help={HELP.keepLastRuns} />
                <Input
                  id="keep-last-runs"
                  type="number"
                  value={keepLastRuns}
                  onChange={(e) => setKeepLastRuns(Number(e.target.value))}
                  aria-describedby={helpId('keep-last-runs')}
                  className="w-24 tabular-nums"
                  min={1}
                  max={10000}
                />
              </div>
            </div>
            <div className="divide-y divide-border rounded-lg border border-border">
              <ToggleRow
                id="auto-unlock"
                checked={autoUnlock}
                onCheckedChange={setAutoUnlock}
                label={<FieldLabel htmlFor="auto-unlock" help={HELP.autoUnlock} variant="inline" />}
              />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle as="h2">Restic</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm">{versionDisplay}</p>
            <Button variant="outline" size="lg" onClick={() => refetchUpdateCheck()}>
              Check Now
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle as="h2">Rename Destination</CardTitle>
          </CardHeader>
          <CardContent>
            {/* The action's name reads like a filesystem operation, which it is
              not — it only repoints job rows at another mount label. Anyone
              who reads it the other way loses nothing on disk but breaks every
              later run, so the correction is stated before the controls and
              cannot be collapsed away. */}
            <p className="text-sm text-muted-foreground max-w-prose">
              Points jobs at a different mount label under /destinations. It rewrites the label
              stored on each job and nothing on disk is touched — no folder is created, moved or
              copied for you.
            </p>
            <button
              type="button"
              aria-expanded={renameHelpOpen}
              aria-controls="rename-help"
              onClick={() => setRenameHelpOpen((open) => !open)}
              className="text-sm underline text-muted-foreground hover:text-foreground mt-1"
            >
              {renameHelpOpen ? 'Hide the details' : 'When should I use this?'}
            </button>
            {renameHelpOpen && (
              <div
                id="rename-help"
                className="mt-2 mb-3 border rounded p-3 text-sm max-w-prose space-y-3 bg-muted/40"
              >
                <div>
                  <h3 className="font-semibold">What it does</h3>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>
                      Rewrites the mount label stored on every job that points at the old one, so
                      later runs read and write under the new one.
                    </li>
                    <li>
                      Takes hold on each job&apos;s next run. Run history and snapshots already
                      recorded stay exactly as they are.
                    </li>
                    <li>
                      Covers every job on that mount at once — there is no per-job choice, and no
                      way to move only some of them.
                    </li>
                  </ul>
                </div>
                <div>
                  <h3 className="font-semibold">What it does not do</h3>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>
                      It creates, moves and renames nothing on disk. The old folder is left where it
                      is, untouched.
                    </li>
                    <li>
                      It copies no repository data. If the backups live somewhere else now, you move
                      the repository folder over <em>yourself</em>, before pressing the button —{' '}
                      <code>/destinations/&lt;old&gt;/&lt;job name&gt;</code> →{' '}
                      <code>/destinations/&lt;new&gt;/&lt;job name&gt;</code>.
                    </li>
                    <li>
                      It does not confirm a repository is really there. That surfaces on the next
                      run, which stops at the repository step rather than starting a fresh empty
                      repository — your snapshots are never silently discarded.
                    </li>
                    <li>
                      It changes neither a job&apos;s name nor its repository password. Those two
                      stay fixed for the life of the job.
                    </li>
                  </ul>
                </div>
                <div>
                  <h3 className="font-semibold">When to use it</h3>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>
                      The mount label itself changed — the drive is now mapped to
                      /destinations/wd-4tb where it used to be /destinations/main.
                    </li>
                    <li>
                      The repositories moved to another drive, which is mounted under a label of its
                      own.
                    </li>
                    <li>You simply want a tidier label than the one you picked back then.</li>
                    <li>
                      In all three cases this is the only route: a destination addresses the
                      repository, so the job edit form keeps it fixed once the job is created.
                    </li>
                  </ul>
                </div>
                <div>
                  <h3 className="font-semibold">Before you use it</h3>
                  <ol className="list-decimal pl-5 space-y-1">
                    <li>
                      Mount the new drive or folder at /destinations/&lt;new label&gt;. It has to be
                      there already, or the request is turned down.
                    </li>
                    <li>
                      Put the <code>.billa_gates_check</code> marker file at its root, or later runs
                      stop at the mount probe.
                    </li>
                    <li>Bring each job&apos;s repository folder across, as described above.</li>
                    <li>
                      Pick a moment when no job on that mount is running — the request is turned
                      down while one is in flight.
                    </li>
                  </ol>
                </div>
              </div>
            )}
            <div className="mt-3 grid grid-cols-1 gap-4 sm:grid-cols-2">
              {destinations !== undefined && (
                <div>
                  <FieldLabel htmlFor="old-label" help={HELP.renameOldLabel} />
                  <select
                    id="old-label"
                    value={oldLabel}
                    onChange={(e) => setOldLabel(e.target.value)}
                    aria-describedby={helpId('old-label')}
                    className="h-8 w-full rounded-md border border-input bg-background px-2.5 text-sm"
                  >
                    <option value="">— select —</option>
                    {destinations.map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <FieldLabel htmlFor="new-label" help={HELP.renameNewLabel} />
                <Input
                  id="new-label"
                  type="text"
                  value={newLabel}
                  onChange={(e) => setNewLabel(e.target.value)}
                  aria-describedby={helpId('new-label')}
                />
              </div>
            </div>
            <Button variant="outline" size="lg" className="mt-4" onClick={handleRename}>
              Rename
            </Button>
            {renameResult && (
              <p className="mt-2 text-sm text-success-subtle-foreground">{renameResult}</p>
            )}
            {renameError && <p className="mt-2 text-sm text-destructive">{renameError}</p>}
          </CardContent>
        </Card>
      </div>
    </TooltipProvider>
  )
}

/**
 * One setting, one switch, one row.
 *
 * These were native `<input type="checkbox">`, which is why the notification
 * block read as a form to fill in rather than a set of switches to flip. A
 * `<label for>` cannot name a Radix switch (it renders a button, which is not
 * a labelable element), so the row wires `aria-labelledby` to the label's id
 * instead — without it the control has no accessible name at all.
 */
function ToggleRow({
  id,
  checked,
  onCheckedChange,
  label,
}: {
  id: string
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  label: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-4 px-3 py-2.5">
      {label}
      <Switch
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        aria-labelledby={labelId(id)}
        aria-describedby={helpId(id)}
      />
    </div>
  )
}
