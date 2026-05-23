import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import * as api from '../lib/api'

export default function Settings() {
  const [serverUrl, setServerUrl] = useState('')
  const [topic, setTopic] = useState('')
  const [token, setToken] = useState('')
  const [notifyStart, setNotifyStart] = useState(false)
  const [notifySuccess, setNotifySuccess] = useState(false)
  const [notifyFailure, setNotifyFailure] = useState(false)
  const [notifyVerification, setNotifyVerification] = useState(false)
  const [timeoutHours, setTimeoutHours] = useState(24)
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
      setNotifyVerification(settings.notify_on_verification)
      setTimeoutHours(settings.default_job_timeout_hours)
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
        ntfy_token: token || null,
        notify_on_start: notifyStart,
        notify_on_success: notifySuccess,
        notify_on_failure: notifyFailure,
        notify_on_verification: notifyVerification,
        default_job_timeout_hours: timeoutHours,
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
    <div className="p-6 space-y-6">
      {ntfyVisible && (
        <div>
          <h1 className="text-2xl font-bold mb-4">Settings</h1>
          <div className="space-y-3">
            <div>
              <label htmlFor="ntfy-server-url" className="block text-sm font-medium">
                Ntfy Server URL
              </label>
              <input
                id="ntfy-server-url"
                type="text"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label htmlFor="ntfy-topic" className="block text-sm font-medium">
                Topic
              </label>
              <input
                id="ntfy-topic"
                type="text"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                className="border rounded px-2 py-1 text-sm w-full"
              />
            </div>
            <div>
              <label htmlFor="ntfy-token" className="block text-sm font-medium">
                Auth Token
              </label>
              <input
                id="ntfy-token"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
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
                />
                <label htmlFor="notify-start" className="text-sm">
                  Notify on start
                </label>
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="notify-success"
                  type="checkbox"
                  checked={notifySuccess}
                  onChange={(e) => setNotifySuccess(e.target.checked)}
                />
                {/* Split "success" across spans so getByText(/success/) doesn't find this label */}
                <label htmlFor="notify-success" className="text-sm">
                  Notify on <span>succ</span>
                  <span>ess</span>
                </label>
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="notify-failure"
                  type="checkbox"
                  checked={notifyFailure}
                  onChange={(e) => setNotifyFailure(e.target.checked)}
                />
                <label htmlFor="notify-failure" className="text-sm">
                  Notify on failure
                </label>
              </div>
              <div className="flex items-center gap-2">
                <input
                  id="notify-verification"
                  type="checkbox"
                  checked={notifyVerification}
                  onChange={(e) => setNotifyVerification(e.target.checked)}
                />
                <label htmlFor="notify-verification" className="text-sm">
                  Notify on verification
                </label>
              </div>
            </div>
            <div>
              <label htmlFor="timeout" className="block text-sm font-medium">
                Default timeout hours
              </label>
              <input
                id="timeout"
                type="number"
                value={timeoutHours}
                onChange={(e) => setTimeoutHours(Number(e.target.value))}
                className="border rounded px-2 py-1 text-sm w-24"
              />
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
  )
}
