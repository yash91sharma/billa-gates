import { useState } from 'react'

export interface ScheduleValue {
  type: 'cron' | 'interval'
  value: string
}

export interface ScheduleInputProps {
  value: ScheduleValue
  onChange: (v: ScheduleValue) => void
}

// First digit must be 1-9 (no zero, no negative)
const INTERVAL_REGEX = /^([1-9][0-9]*)(h|d|m)$/

function validateCron(expr: string): boolean {
  const parts = expr.trim().split(/\s+/)
  if (parts.length !== 5) return false
  return parts.every((p) => /^[\d*,\-/]+$/.test(p))
}

function getIntervalPreview(val: string): string | null {
  const match = val.match(INTERVAL_REGEX)
  if (!match) return null
  const n = parseInt(match[1], 10)
  const unit = match[2]
  const label =
    unit === 'h'
      ? `hour${n !== 1 ? 's' : ''}`
      : unit === 'd'
        ? `day${n !== 1 ? 's' : ''}`
        : `minute${n !== 1 ? 's' : ''}`
  return `every ${n} ${label}`
}

function getIntervalError(val: string): string | null {
  if (!val) return null
  if (!INTERVAL_REGEX.test(val)) return 'Use format like 6h, 1d, or 30m'
  return null
}

function getCronError(val: string): string | null {
  if (!val) return null
  if (!validateCron(val)) return 'Invalid cron expression (e.g. 0 2 * * *)'
  return null
}

export default function ScheduleInput({ value, onChange }: ScheduleInputProps) {
  // Local state so typing accumulates correctly in controlled-component tests
  const [inputValue, setInputValue] = useState(value.value)

  const isInterval = value.type === 'interval'
  const isCron = value.type === 'cron'

  const intervalError = isInterval ? getIntervalError(inputValue) : null
  const cronError = isCron ? getCronError(inputValue) : null
  const intervalPreview = isInterval && !intervalError ? getIntervalPreview(inputValue) : null
  const cronPreview = isCron && !cronError && inputValue ? 'next: (valid cron schedule)' : null

  const inputId = isInterval ? 'schedule-interval-input' : 'schedule-cron-input'
  const inputLabel = isInterval ? 'Interval (e.g. 6h, 1d, 30m)' : 'Cron Expression'

  function switchMode(newType: 'cron' | 'interval') {
    setInputValue('')
    onChange({ type: newType, value: '' })
  }

  function handleInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const newVal = e.target.value
    setInputValue(newVal)
    onChange({ type: value.type, value: newVal })
  }

  return (
    <div data-testid="schedule-input">
      <div className="flex gap-2 mb-2">
        <button
          type="button"
          aria-pressed={isInterval ? 'true' : 'false'}
          onClick={() => switchMode('interval')}
          className="px-3 py-1 rounded border text-sm"
        >
          Interval
        </button>
        <button
          type="button"
          aria-pressed={isCron ? 'true' : 'false'}
          onClick={() => switchMode('cron')}
          className="px-3 py-1 rounded border text-sm"
        >
          Cron
        </button>
      </div>
      <div>
        <label htmlFor={inputId} className="block text-sm font-medium mb-1">
          {inputLabel}
        </label>
        <input
          id={inputId}
          type="text"
          value={inputValue}
          onChange={handleInputChange}
          className="border rounded px-2 py-1 text-sm w-full"
        />
        {intervalError && <p className="text-destructive text-xs mt-1">{intervalError}</p>}
        {cronError && <p className="text-destructive text-xs mt-1">{cronError}</p>}
        {intervalPreview && <p className="text-muted-foreground text-xs mt-1">{intervalPreview}</p>}
        {cronPreview && <p className="text-muted-foreground text-xs mt-1">{cronPreview}</p>}
      </div>
    </div>
  )
}
