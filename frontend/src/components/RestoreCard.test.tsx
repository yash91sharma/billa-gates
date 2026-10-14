import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import RestoreCard from './RestoreCard'
import type { RestoreInstructions } from '../lib/types'

// What GET /api/jobs/{id}/restore returns for a job named "Family Photos"
// backing up /sources/photos to the "usb" drive. The commands are the backend's,
// already shell-quoted; the card must print them exactly as given.
function makeInstructions(overrides: Partial<RestoreInstructions> = {}): RestoreInstructions {
  return {
    repository_folder: 'Family Photos',
    repository_contents: ['config', 'data/', 'index/', 'keys/', 'locks/', 'snapshots/'],
    drive_marker: '.billa_gates_check',
    destination_label: 'usb',
    destination_path: '/destinations/usb',
    repository_path: '/destinations/usb/Family Photos',
    snapshot_path: '/sources/photos',
    drive_placeholder: '/path/to/backup-drive',
    target_placeholder: '/path/to/empty-folder',
    compose_mapping: '/path/to/backup-drive:/destinations/usb',
    restore_root: '/restore',
    restore_root_mounted: true,
    linux_commands: [
      "sudo restic -r '/path/to/backup-drive/Family Photos' snapshots",
      "sudo restic -r '/path/to/backup-drive/Family Photos' restore latest:/sources/photos --target '/path/to/empty-folder' --sparse",
    ],
    desktop_commands: [
      "restic -r '/path/to/backup-drive/Family Photos' snapshots",
      "restic -r '/path/to/backup-drive/Family Photos' restore latest:/sources/photos --target '/path/to/empty-folder' --sparse",
    ],
    container_commands: [
      'docker exec -it billa-gates sh',
      "restic -r '/destinations/usb/Family Photos' snapshots",
      "restic -r '/destinations/usb/Family Photos' restore latest:/sources/photos --target '/restore/Family Photos' --sparse",
    ],
    ...overrides,
  }
}

const GIB = 1073741824

/** The <code> element holding exactly this command. Matched on its whole text
 *  because each argument inside it is its own element (see the wrap test). */
const isCommand = (command: string) => (_: string, el: Element | null) =>
  el?.tagName === 'CODE' && el.textContent === command

describe('RestoreCard', () => {
  it('opens on the Linux method and prints its commands exactly as the API sent them', () => {
    const info = makeInstructions()
    render(<RestoreCard instructions={info} latestSnapshotBytes={5 * GIB} />)

    expect(screen.getByRole('tab', { name: /linux/i })).toHaveAttribute('data-state', 'active')
    for (const command of info.linux_commands) {
      // One element per command, so a selection copies one runnable line.
      expect(screen.getByText(isCommand(command))).toBeInTheDocument()
    }
  })

  it('wraps a long command only between arguments, never inside one', () => {
    // A browser breaks after a hyphen, which split `--sparse` into `--` and
    // `sparse` on two lines. Each argument is an unbreakable span instead, and
    // the spaces between them stay plain text, so a selection still copies
    // the command exactly.
    const info = makeInstructions()
    render(<RestoreCard instructions={info} latestSnapshotBytes={null} />)
    const restore = info.linux_commands[1]
    const code = screen.getByText(isCommand(restore))

    const args = Array.from(code.querySelectorAll('span')).map((s) => s.textContent)
    expect(args).toEqual(restore.split(' '))
    expect(args).toContain('--sparse')
    for (const span of code.querySelectorAll('span')) {
      expect(span).toHaveClass('whitespace-nowrap')
    }
  })

  it('offers the three places a restore can happen', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />)

    expect(screen.getByRole('tab', { name: /linux/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /inside the container/i })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /macos.*windows/i })).toBeInTheDocument()
  })

  it('says exactly what to put in place of the drive placeholder and how to check it', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />)
    const panel = screen.getByRole('tabpanel')

    // Where the drive is: the left side of the compose line the operator wrote.
    expect(within(panel).getByText('- /path/to/backup-drive:/destinations/usb')).toBeInTheDocument()
    // How to recognize it, and the repository inside it.
    expect(within(panel).getAllByText('.billa_gates_check').length).toBeGreaterThan(0)
    expect(within(panel).getAllByText('Family Photos').length).toBeGreaterThan(0)
    for (const entry of ['config', 'data/', 'index/', 'keys/', 'locks/', 'snapshots/']) {
      expect(within(panel).getByText(entry)).toBeInTheDocument()
    }
  })

  it('sizes the empty folder from the latest snapshot', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={5 * GIB} />)
    expect(screen.getByRole('tabpanel')).toHaveTextContent(/at least 5 GB free/)
  })

  it('still explains the empty folder when no snapshot size is known', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />)
    expect(screen.getByRole('tabpanel')).toHaveTextContent(/enough free space/i)
  })

  it('never prints a password and says restic will ask for it', () => {
    const { container } = render(
      <RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />
    )
    expect(container.textContent).not.toMatch(/RESTIC_PASSWORD/)
    expect(screen.getByRole('tabpanel')).toHaveTextContent(/asks for this job's password/i)
  })

  it('covers older versions, single folders and read-only drives', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />)
    const panel = screen.getByRole('tabpanel')

    expect(panel).toHaveTextContent(/replace latest with a snapshot id/i)
    expect(within(panel).getByText('latest:/sources/photos/some/folder')).toBeInTheDocument()
    expect(within(panel).getByText('--no-lock')).toBeInTheDocument()
  })

  it('explains why the Linux commands use sudo', () => {
    render(<RestoreCard instructions={makeInstructions()} latestSnapshotBytes={null} />)
    expect(screen.getByRole('tabpanel')).toHaveTextContent(/readable only by root/i)
  })

  describe('macOS / Windows', () => {
    it('warns that a full restore there can silently lose files', async () => {
      const user = userEvent.setup()
      const info = makeInstructions()
      render(<RestoreCard instructions={info} latestSnapshotBytes={null} />)

      await user.click(screen.getByRole('tab', { name: /macos.*windows/i }))
      const panel = screen.getByRole('tabpanel')

      expect(panel).toHaveTextContent(/without any error/i)
      expect(panel).toHaveTextContent(/upper.{0,10}lower.?case/i)
      expect(panel).toHaveTextContent(/restore whole folders on linux/i)
      for (const command of info.desktop_commands) {
        expect(within(panel).getByText(isCommand(command))).toBeInTheDocument()
      }
      expect(panel.textContent).not.toMatch(/\bsudo restic/)
    })
  })

  describe('inside the container', () => {
    it('prints the container commands when /restore is mounted', async () => {
      const user = userEvent.setup()
      const info = makeInstructions()
      render(<RestoreCard instructions={info} latestSnapshotBytes={null} />)

      await user.click(screen.getByRole('tab', { name: /inside the container/i }))
      const panel = screen.getByRole('tabpanel')

      for (const command of info.container_commands) {
        expect(within(panel).getByText(isCommand(command))).toBeInTheDocument()
      }
    })

    it('withholds the commands and says how to mount /restore when it is not mounted', async () => {
      // Without the mount a restore lands on the container's own disk and is
      // gone the next time the container is recreated.
      const user = userEvent.setup()
      render(
        <RestoreCard
          instructions={makeInstructions({ restore_root_mounted: false, container_commands: [] })}
          latestSnapshotBytes={null}
        />
      )

      await user.click(screen.getByRole('tab', { name: /inside the container/i }))
      const panel = screen.getByRole('tabpanel')

      expect(panel).toHaveTextContent(/no folder is mounted at \/restore/i)
      expect(within(panel).getByText('- /path/on/host:/restore')).toBeInTheDocument()
      expect(panel.textContent).not.toMatch(/restic -r/)
    })
  })

  it('shows a loading state instead of commands until the instructions arrive', () => {
    render(<RestoreCard instructions={undefined} latestSnapshotBytes={null} />)

    expect(screen.getByTestId('restore-loading')).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
  })

  it('says the instructions could not be loaded instead of rendering nothing', () => {
    render(
      <RestoreCard
        instructions={undefined}
        error={{ status: 500, data: { detail: 'boom' } }}
        latestSnapshotBytes={null}
      />
    )

    expect(screen.getByText(/could not load the restore instructions/i)).toBeInTheDocument()
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
  })
})
