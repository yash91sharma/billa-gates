import '@testing-library/jest-dom'
import '../index.css'

// Disable animations, transitions, and blinking caret to ensure 100% deterministic screenshot captures
const style = document.createElement('style')
style.textContent = `
  *, *::before, *::after {
    animation-duration: 0s !important;
    animation-delay: 0s !important;
    transition-duration: 0s !important;
    transition-delay: 0s !important;
    caret-color: transparent !important;
  }
`
if (typeof document !== 'undefined' && document.head) {
  document.head.appendChild(style)
}
