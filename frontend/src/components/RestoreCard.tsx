import { Fragment, useId, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import type { RestoreInstructions } from '../lib/types'
import { formatCapacity, parseApiError } from '../lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from './ui/card'
import { Skeleton } from './ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs'

// Billa-Gates never restores; the operator runs restic by hand, often after
// something has already gone wrong and sometimes on a computer where this app
// isn't running at all. So the card is built around where the restore happens,
// and every command comes from the backend (GET /api/jobs/{id}/restore),
// shell-quoted and printed verbatim. It used to be a template here: it printed
// an unquoted repository path, so a job named "Documents Offsite" got
// "repository does not exist", and a relative `--target ./restored` that
// landed on the container's own disk.

interface RestoreCardProps {
  instructions: RestoreInstructions | undefined
  error?: unknown
  /** Size of the newest snapshot, to say how much room the target needs. */
  latestSnapshotBytes: number | null | undefined
}

function Code({ children }: { children: ReactNode }) {
  return <code className="rounded-sm bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
}

/** One element per command, so selecting a line copies one runnable command.
 *
 *  Long commands wrap rather than scroll: a restore line cut off at the edge
 *  hid its trailing `--sparse`. They wrap only between arguments, because a
 *  browser also breaks after a hyphen and split `--sparse` into `--` and
 *  `sparse` on two lines. Each argument is an unbreakable span, while the
 *  spaces between them stay plain text, so a selection copies the command
 *  exactly. The hanging indent marks a wrapped line as a continuation. The
 *  `overflow-x-auto` only matters for a single argument wider than the card. */
function CommandBlock({ commands, label }: { commands: string[]; label: string }) {
  return (
    <pre aria-label={label} className="overflow-x-auto rounded-sm bg-muted p-3 text-xs">
      {commands.map((command) => (
        <code key={command} className="block pl-4 -indent-4 whitespace-pre-wrap">
          {command.split(' ').map((arg, i) => (
            <Fragment key={i}>
              {i > 0 && ' '}
              <span className="whitespace-nowrap">{arg}</span>
            </Fragment>
          ))}
        </code>
      ))}
    </pre>
  )
}

function Warning({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
      <div className="space-y-2">{children}</div>
    </div>
  )
}

/** What to type in place of the two placeholders, and how to tell the right
 *  folder from the wrong one before typing a password into it. */
function FillIn({
  info,
  freeSpace,
  desktop,
}: {
  info: RestoreInstructions
  freeSpace: string
  desktop: boolean
}) {
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">Fill in</h3>
      <dl className="space-y-3 text-sm">
        <div className="space-y-1">
          <dt>
            <Code>{info.drive_placeholder}</Code>
          </dt>
          <dd className="space-y-1 text-muted-foreground">
            <p>
              Where the <strong className="text-foreground">{info.destination_label}</strong> backup
              drive is mounted on that computer. On the NAS, it's the folder on the left of this
              line in your docker-compose.yml:
            </p>
            <pre className="overflow-x-auto rounded-sm bg-muted p-2 text-xs">
              <code>{`- ${info.compose_mapping}`}</code>
            </pre>
            {desktop && (
              <p>
                On a Mac that's usually <Code>/Volumes/&lt;drive name&gt;</Code>, on Windows a drive
                letter such as <Code>D:/</Code>. A drive formatted on the NAS (ext4, btrfs) usually
                can't be read there without extra software.
              </p>
            )}
            <p>
              The drive's top level holds a <Code>{info.drive_marker}</Code> file and one folder per
              job. This job's folder is <Code>{info.repository_folder}</Code>, and it must contain{' '}
              {info.repository_contents.map((entry, i) => (
                <span key={entry}>
                  <Code>{entry}</Code>
                  {i < info.repository_contents.length - 2
                    ? ', '
                    : i === info.repository_contents.length - 2
                      ? ' and '
                      : '.'}
                </span>
              ))}
            </p>
          </dd>
        </div>
        <div className="space-y-1">
          <dt>
            <Code>{info.target_placeholder}</Code>
          </dt>
          <dd className="text-muted-foreground">
            An empty folder on the computer you're restoring to, not on the backup drive, with{' '}
            {freeSpace}.
          </dd>
        </div>
      </dl>
    </div>
  )
}

/** The same few things go wrong wherever the restore runs. */
function Notes({
  info,
  sudo,
  readOnly,
}: {
  info: RestoreInstructions
  sudo: boolean
  readOnly: boolean
}) {
  return (
    <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
      <li>
        restic asks for this job's password. The app never shows it again, so keep a copy somewhere
        other than this NAS.
      </li>
      <li>
        To restore an older version, replace <Code>latest</Code> with a snapshot ID from the list.
      </li>
      <li>
        To restore one folder, add its path to the snapshot path, for example{' '}
        <Code>{`latest:${info.snapshot_path}/some/folder`}</Code>.
      </li>
      <li>
        If restic says the path isn't in the snapshot, use the one listed under Paths by{' '}
        <Code>restic snapshots</Code>: a snapshot taken before the job's source was changed holds a
        different path.
      </li>
      {readOnly && (
        <li>
          If the drive is mounted read-only, add <Code>--no-lock</Code>.
        </li>
      )}
      {sudo ? (
        <li>
          <Code>sudo</Code>: the repository is readable only by root, and only root can restore the
          original file owners.
        </li>
      ) : null}
    </ul>
  )
}

export default function RestoreCard({
  instructions,
  error,
  latestSnapshotBytes,
}: RestoreCardProps) {
  const titleId = useId()

  let body: ReactNode
  if (!instructions && error) {
    body = (
      <p className="text-sm text-destructive">
        Could not load the restore instructions.{' '}
        {parseApiError(error).message ?? 'The request failed.'}
      </p>
    )
  } else if (!instructions) {
    body = (
      <div data-testid="restore-loading" className="space-y-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-16 w-full" />
      </div>
    )
  } else {
    const info = instructions
    const freeSpace = latestSnapshotBytes
      ? `at least ${formatCapacity(latestSnapshotBytes)} free (the size of the latest snapshot)`
      : 'enough free space for the files you restore'
    const hostRestoreLine = `- /path/on/host:${info.restore_root}`

    body = (
      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Billa-Gates doesn't restore; restic does. Choose where you're restoring.
        </p>
        <Tabs defaultValue="linux">
          <div className="border-b border-border">
            <TabsList variant="line" className="w-fit justify-start">
              <TabsTrigger value="linux" className="flex-none px-3">
                Linux / NAS
              </TabsTrigger>
              <TabsTrigger value="container" className="flex-none px-3">
                Inside the container
              </TabsTrigger>
              <TabsTrigger value="desktop" className="flex-none px-3">
                macOS / Windows
              </TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="linux" className="space-y-4 pt-3">
            <p className="text-sm">
              On the NAS itself, or any Linux computer with the backup drive attached. This works
              even if the app is gone. Needs restic 0.16 or newer.
            </p>
            <FillIn info={info} freeSpace={freeSpace} desktop={false} />
            <CommandBlock commands={info.linux_commands} label="Restore commands for Linux" />
            <Notes info={info} sudo readOnly />
          </TabsContent>

          <TabsContent value="container" className="space-y-4 pt-3">
            {info.restore_root_mounted ? (
              <>
                <p className="text-sm">
                  Restores into <Code>{`${info.restore_root}/${info.repository_folder}`}</Code>,
                  inside the host folder mapped to <Code>{info.restore_root}</Code> in your
                  docker-compose.yml. The first command opens a shell in the app's container; use
                  your container's name if it differs.
                </p>
                <CommandBlock
                  commands={info.container_commands}
                  label="Restore commands inside the container"
                />
                <Notes info={info} sudo={false} readOnly={false} />
              </>
            ) : (
              <Warning>
                <p>
                  No folder is mounted at <Code>{info.restore_root}</Code>, so a restore inside the
                  container would land on the container's own disk and disappear the next time the
                  container is recreated.
                </p>
                <p>
                  To restore here, add this line under the app's <Code>volumes:</Code> in
                  docker-compose.yml and recreate the container:
                </p>
                <pre className="overflow-x-auto rounded-sm bg-muted p-2 text-xs">
                  <code>{hostRestoreLine}</code>
                </pre>
                <p>Or restore with one of the other methods.</p>
              </Warning>
            )}
          </TabsContent>

          <TabsContent value="desktop" className="space-y-4 pt-3">
            <Warning>
              <p>
                <strong>Use this for picking out individual files.</strong> macOS and Windows treat
                names that differ only in upper/lower case (<Code>IMG_0042.JPG</Code>,{' '}
                <Code>img_0042.jpg</Code>) or in how an accented letter is encoded as the same file,
                so a restore keeps one and drops the other without any error. Names those systems
                can't store (not valid UTF-8 on macOS, or containing <Code>{': * ? " < > |'}</Code>{' '}
                on Windows) are not restored correctly either. Restore whole folders on Linux.
              </p>
            </Warning>
            <FillIn info={info} freeSpace={freeSpace} desktop />
            <CommandBlock
              commands={info.desktop_commands}
              label="Restore commands for macOS and Windows"
            />
            <Notes info={info} sudo={false} readOnly />
          </TabsContent>
        </Tabs>
      </div>
    )
  }

  return (
    <Card role="region" aria-labelledby={titleId}>
      <CardHeader>
        <CardTitle as="h2" id={titleId}>
          Restore
        </CardTitle>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  )
}
