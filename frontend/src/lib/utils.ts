import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  const GB = 1073741824
  const MB = 1048576
  const KB = 1024
  if (bytes >= GB) {
    const val = bytes / GB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} GB`
  }
  if (bytes >= MB) {
    const val = bytes / MB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} MB`
  }
  if (bytes >= KB) {
    const val = bytes / KB
    return `${val % 1 === 0 ? val.toFixed(0) : val.toFixed(1)} KB`
  }
  return `${bytes} B`
}
