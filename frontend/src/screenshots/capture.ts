import { commands, page } from 'vitest/browser'

// `vitest/browser` is both the runtime module and the one `BrowserCommands` is
// declared in. vitest 4 moved the interface here; vitest 5 moved the runtime
// too — `@vitest/browser/context` is now a stub that throws "can be imported
// only inside the Browser Mode" even inside it, failing every test file that
// imports it. Augmenting the old module silently types
// `commands.writeScreenshotIfChanged` as non-existent.
declare module 'vitest/browser' {
  interface BrowserCommands {
    writeScreenshotIfChanged(relativePath: string, base64: string): Promise<'written' | 'unchanged'>
  }
}

/**
 * Take a screenshot and write it **only if the pixels changed**.
 *
 * Use this instead of `page.screenshot({ path })` everywhere in this directory.
 * The two differ in one way that matters: `page.screenshot` writes the file
 * unconditionally, so a run in which nothing moved still rewrote all 40 PNGs
 * and buried the one image you needed to look at. Here an unchanged image is
 * left untouched — same bytes, same mtime — and every write is announced on
 * stdout, so the run tells you what to review.
 *
 * `save: false` makes the browser hand back base64 instead of writing; the
 * conditional write happens in Node (see write-if-changed.ts), because this
 * code runs inside Chromium and has no filesystem.
 *
 * Fonts are awaited first. Geist is a web font, and a capture taken before it
 * loads renders in fallback metrics — every glyph shifts, which is exactly the
 * kind of difference that would make the output non-reproducible on a cold
 * cache. (Measured stable today; this keeps it that way.)
 */
export async function capture(relativePath: string): Promise<void> {
  await document.fonts.ready
  const base64 = await page.screenshot({ save: false })
  await commands.writeScreenshotIfChanged(relativePath, base64)
}
