import { expect, test } from 'vitest'

/**
 * The screenshot suite's output must depend only on the code being reviewed.
 *
 * `Sidebar` prints `v${__APP_VERSION__}` in its footer, and the sidebar is in
 * 17 of the 44 captures — every page shot plus the layout ones. With the real
 * `package.json` version compiled in, every `npm version`/version bump repainted
 * all 17 files, so the run that was supposed to say "here is what your change
 * moved" instead handed over 17 modified PNGs whose only difference was three
 * digits in a corner. That is the same failure `capture()`'s write-if-changed
 * was built to prevent, arriving one layer up: the bytes really did change, for
 * a reason that has nothing to do with the visual review.
 *
 * So `vitest.screenshots.config.ts` freezes the define at a placeholder instead
 * of reading `pkg.version`. This test runs inside the real browser bundle, so it
 * fails if that define is ever pointed back at `package.json` — the config on
 * its own is easy to "fix" back, since the version there looks like an
 * oversight rather than a decision.
 *
 * The placeholder keeps the shape of a real version (same digit count, so the
 * footer's layout is still representative) while being obviously not one.
 */
test('the app version compiled into screenshots is frozen, not package.json', () => {
  expect(__APP_VERSION__).toBe('0.0.0')
})
