import { render, screen } from '@testing-library/react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from './table'

function renderTable() {
  return render(
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Job</TableHead>
          <TableHead numeric>Duration</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableRow>
          <TableCell>Documents Backup</TableCell>
          <TableCell numeric>120s</TableCell>
        </TableRow>
      </TableBody>
    </Table>
  )
}

describe('Table', () => {
  it('renders real table semantics so screen readers announce rows and columns', () => {
    renderTable()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Job' })).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(2)
  })

  it('right-aligns numeric cells with tabular figures', () => {
    // Sizes, durations and file counts are compared down a column, not read as
    // prose. Proportional digits make that comparison harder than it needs to
    // be, and left-aligned numbers make it impossible at a glance.
    renderTable()
    const cell = screen.getByRole('cell', { name: '120s' })
    expect(cell.className).toContain('text-right')
    expect(cell.className).toContain('tabular-nums')
  })

  it('leaves non-numeric cells alone', () => {
    renderTable()
    expect(screen.getByRole('cell', { name: 'Documents Backup' }).className).not.toContain(
      'tabular-nums'
    )
  })

  it('scrolls the table itself rather than the page', () => {
    // A ten-column run history on a narrow window must not drag the whole
    // layout sideways.
    const { container } = renderTable()
    const wrapper = container.querySelector('[data-slot="table-container"]')
    expect(wrapper?.className).toContain('overflow-x-auto')
  })
})
