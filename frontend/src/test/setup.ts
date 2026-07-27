import '@testing-library/jest-dom'
import { configure } from '@testing-library/react'

// Reduce waitFor timeout from the 1000ms default so failing tests don't stall.
// 200ms is enough for mocked async resolution while keeping the suite fast.
configure({ asyncUtilTimeout: 200 })

// Radix's popper (used by Tooltip/Select) observes its trigger with a
// ResizeObserver, which jsdom does not implement — opening a tooltip in a test
// throws without this. A no-op stub is enough: nothing here measures layout.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class StubResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  ;(globalThis as unknown as { ResizeObserver: typeof StubResizeObserver }).ResizeObserver =
    StubResizeObserver
}
