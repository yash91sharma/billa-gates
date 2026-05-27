import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'

describe('Favicon Configuration', () => {
  it('should use billa.png as the favicon in index.html', () => {
    const indexPath = path.resolve(__dirname, '../../index.html')
    const htmlContent = fs.readFileSync(indexPath, 'utf-8')

    // Expecting the favicon to be billa.png as an image/png
    expect(htmlContent).toContain(
      '<link rel="icon" type="image/png" href="/src/assets/billa.png" />'
    )
    expect(htmlContent).not.toContain('href="/vite.svg"')
  })
})
