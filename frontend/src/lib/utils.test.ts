import { formatCapacity, formatPercent } from './utils'

describe('formatCapacity', () => {
  const TB = 1024 ** 4
  const GB = 1024 ** 3

  it('uses TB for drive-sized values', () => {
    // Backup drives are sold in TB, and "4096 GB" is not how anyone reads a
    // 4 TB disk.
    expect(formatCapacity(4 * TB)).toBe('4 TB')
    expect(formatCapacity(1.8 * TB)).toBe('1.8 TB')
  })

  it('hands anything below a terabyte to formatBytes unchanged', () => {
    expect(formatCapacity(500 * GB)).toBe('500 GB')
    expect(formatCapacity(12 * GB)).toBe('12 GB')
    expect(formatCapacity(1024)).toBe('1 KB')
  })

  it('renders an em-dash for a value that could not be measured', () => {
    expect(formatCapacity(null)).toBe('—')
    expect(formatCapacity(undefined)).toBe('—')
  })

  it('renders a genuinely empty drive as zero, not unknown', () => {
    expect(formatCapacity(0)).toBe('0 B')
  })
})

describe('formatPercent', () => {
  it('renders one decimal place', () => {
    expect(formatPercent(82.44)).toBe('82.4%')
  })

  it('keeps a trailing zero so a column of percentages stays aligned', () => {
    expect(formatPercent(60)).toBe('60.0%')
  })

  it('renders zero as a percentage, not as unknown', () => {
    // A drive that really is empty is a fact worth showing; only an unmeasured
    // one is unknown.
    expect(formatPercent(0)).toBe('0.0%')
  })

  it('renders an em-dash for a value that could not be measured', () => {
    expect(formatPercent(null)).toBe('—')
    expect(formatPercent(undefined)).toBe('—')
  })
})
