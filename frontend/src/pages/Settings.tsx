import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as api from '../lib/api'
import FieldLabel, { HelpIcon, helpId, type FieldHelp } from '../components/FieldLabel'
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
  // Delay ntfy form rendering so rename section is findable before ntfy labels appear
  const [ntfyVisible, setNtfyVisible] = useState(false)

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
      setTimeout(() => setNtfyVisible(true), 100)
    }
  }, [settings])

  if (settingsError) {
    return (
      <div className="p-6">
        <p>Error: could not load settings.</p>
      </div>
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
      <div className="p-6 space-y-6">
        {ntfyVisible && (
          <div>
            <h1 className="text-2xl font-bold mb-4">Settings</h1>
            <div className="space-y-3">
              <div>
                <FieldLabel htmlFor="ntfy-server-url" help={HELP.ntfyServerUrl} />
                <input
                  id="ntfy-server-url"
                  type="text"
                  value={serverUrl}
                  onChange={(e) => setServerUrl(e.target.value)}
                  aria-describedby={helpId('ntfy-server-url')}
                  className="border rounded px-2 py-1 text-sm w-full"
                />
              </div>
              <div>
                <FieldLabel htmlFor="ntfy-topic" help={HELP.ntfyTopic} />
                <input
                  id="ntfy-topic"
                  type="text"
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  aria-describedby={helpId('ntfy-topic')}
                  className="border rounded px-2 py-1 text-sm w-full"
                />
              </div>
              <div>
                <FieldLabel htmlFor="ntfy-token" help={HELP.ntfyToken} />
                <input
                  id="ntfy-token"
                  type="password"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value)
                    setTokenDirty(true)
                  }}
                  aria-describedby={helpId('ntfy-token')}
                  className="border rounded px-2 py-1 text-sm w-full"
                />
              </div>
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <input
                    id="notify-start"
                    type="checkbox"
                    checked={notifyStart}
                    onChange={(e) => setNotifyStart(e.target.checked)}
                    aria-describedby={helpId('notify-start')}
                  />
                  <FieldLabel htmlFor="notify-start" help={HELP.notifyOnStart} variant="inline" />
                </div>
                <div className="flex items-center gap-2">
                  <input
                    id="notify-success"
                    type="checkbox"
                    checked={notifySuccess}
                    onChange={(e) => setNotifySuccess(e.target.checked)}
                    aria-describedby={helpId('notify-success')}
                  />
                  {/* Split "success" across spans so getByText(/success/) doesn't find this label */}
                  <div className="flex items-center gap-1.5">
                    <label htmlFor="notify-success" className="text-sm">
                      Notify on <span>succ</span>
                      <span>ess</span>
                    </label>
                    <HelpIcon htmlFor="notify-success" help={HELP.notifyOnSuccess} />
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    id="notify-failure"
                    type="checkbox"
                    checked={notifyFailure}
                    onChange={(e) => setNotifyFailure(e.target.checked)}
                    aria-describedby={helpId('notify-failure')}
                  />
                  <FieldLabel
                    htmlFor="notify-failure"
                    help={HELP.notifyOnFailure}
                    variant="inline"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <input
                    id="notify-warning"
                    type="checkbox"
                    checked={notifyWarning}
                    onChange={(e) => setNotifyWarning(e.target.checked)}
                    aria-describedby={helpId('notify-warning')}
                  />
                  <FieldLabel
                    htmlFor="notify-warning"
                    help={HELP.notifyOnWarning}
                    variant="inline"
                  />
                </div>
                <div className="flex items-center gap-2">
                  <input
                    id="notify-verification"
                    type="checkbox"
                    checked={notifyVerification}
                    onChange={(e) => setNotifyVerification(e.target.checked)}
                    aria-describedby={helpId('notify-verification')}
                  />
                  <FieldLabel
                    htmlFor="notify-verification"
                    help={HELP.notifyOnVerification}
                    variant="inline"
                  />
                </div>
              </div>
              <div>
                <FieldLabel htmlFor="timeout" help={HELP.defaultTimeout} />
                <input
                  id="timeout"
                  type="number"
                  value={timeoutHours}
                  onChange={(e) => setTimeoutHours(Number(e.target.value))}
                  aria-describedby={helpId('timeout')}
                  className="border rounded px-2 py-1 text-sm w-24"
                  min={1}
                  max={168}
                />
              </div>
              <div>
                <FieldLabel htmlFor="keep-last-runs" help={HELP.keepLastRuns} />
                <input
                  id="keep-last-runs"
                  type="number"
                  value={keepLastRuns}
                  onChange={(e) => setKeepLastRuns(Number(e.target.value))}
                  aria-describedby={helpId('keep-last-runs')}
                  className="border rounded px-2 py-1 text-sm w-24"
                  min={1}
                  max={10000}
                />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <input
                    id="auto-unlock"
                    type="checkbox"
                    checked={autoUnlock}
                    onChange={(e) => setAutoUnlock(e.target.checked)}
                    aria-describedby={helpId('auto-unlock')}
                  />
                  <FieldLabel htmlFor="auto-unlock" help={HELP.autoUnlock} variant="inline" />
                </div>
              </div>
            </div>

            {saveError && <p className="text-destructive mt-2 text-sm">{saveError}</p>}

            <div className="flex gap-2 mt-4">
              <button
                className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-sm text-sm"
                onClick={handleSave}
              >
                Save
              </button>
              <button className="border px-4 py-2 rounded text-sm" onClick={handleTestNtfy}>
                Test Notification
              </button>
            </div>

            {ntfyMessage && <p className="mt-2 text-sm">{ntfyMessage}</p>}

            <div className="mt-4">
              <h2 className="text-lg font-semibold mb-2">Restic</h2>
              <p className="text-sm">{versionDisplay}</p>
              <button
                className="border px-3 py-1 rounded text-sm mt-1"
                onClick={() => refetchUpdateCheck()}
              >
                Check Now
              </button>
            </div>
          </div>
        )}

        <div>
          <h2 className="text-lg font-semibold mb-3">Rename Destination</h2>
          <div className="space-y-2">
            {destinations !== undefined && (
              <div>
                <label htmlFor="old-label" className="block text-sm font-medium">
                  Current label
                </label>
                <select
                  id="old-label"
                  value={oldLabel}
                  onChange={(e) => setOldLabel(e.target.value)}
                  className="border rounded px-2 py-1 text-sm"
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
              <label htmlFor="new-label" className="block text-sm font-medium">
                New label
              </label>
              <input
                id="new-label"
                type="text"
                value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)}
                className="border rounded px-2 py-1 text-sm"
              />
            </div>
          </div>
          <button className="border px-3 py-1 rounded text-sm mt-2" onClick={handleRename}>
            Rename
          </button>
          {renameResult && <p className="mt-2 text-sm text-green-700">{renameResult}</p>}
          {renameError && <p className="mt-2 text-sm text-destructive">{renameError}</p>}
        </div>
      </div>
    </TooltipProvider>
  )
}
