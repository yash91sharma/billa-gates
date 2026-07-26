# Recorded restic output — `restic 0.19.1 compiled with go1.26.4 on linux/arm64`

Captured 2026-07-25 by running the real binary against a real local repository.
**Every file here is verbatim restic output**, not hand-written. That is the
whole point: the unit suite mocks `asyncio.create_subprocess_exec`, so without a
recording the mocks are the only description of restic in the repo — and an
unverified one. `tests/test_restic_contract.py` runs the real parsers over these
bytes and checks the hand-written fixtures against them.

This is what caught `size_bytes`: a fixture invented a top-level `total_size`
key, tests asserted the mapping worked, and production returned `None` on every
call because restic emits no such field (see `snapshots.json` — the size lives
at `summary.total_bytes_processed`).

## Regenerating (do this on every restic version bump)

`capture.sh` in this directory regenerates every file here. Point it at the new
binary, re-run, diff the output, and fix whatever the contract test then reports.
A diff here is the signal to re-read the restic changelog — it means a shape the
app parses moved.

```bash
# inside the container, as root
RESTIC=/path/to/restic bash backend/tests/fixtures/restic_0_19_1/capture.sh
cp /tmp/captured/* backend/tests/fixtures/restic_0_19_1/
```

Rename the directory to match the new version, then update `RECORDED_VERSION` and
`FIXTURES` in `tests/test_restic_contract.py` — that test asserts the recordings
and the Dockerfile's `RESTIC_VERSION` describe the same binary, so it fails until
both are done.

These files are **byte-verbatim captures, not source**: `backend/tests/fixtures/`
is in `.prettierignore` so the formatter cannot rewrite the JSON ones. Keep it
that way, or every re-capture becomes a whitespace diff. The commands each file
comes from:

| File                            | Command                                                                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `version.json` / `version.txt`  | `restic version [--json]`                                                                                                      |
| `cat_config.json`               | `restic cat config`                                                                                                            |
| `backup_rc0.stdout`             | `restic backup --host billa-gates --tag daily --tag important --json /sources/documents`                                       |
| `backup_status_line.json`       | first `message_type=status` line of a backup long enough to emit one                                                           |
| `backup_rc3.stdout` / `.stderr` | same backup over a source containing a directory the running user cannot read (needs a non-root uid — `setpriv --reuid=65534`) |
| `backup_missing_source.stderr`  | `restic backup --json /sources/does-not-exist`                                                                                 |
| `snapshots.json`                | `restic snapshots --json --no-lock`                                                                                            |
| `snapshots_latest.json`         | `restic snapshots --latest 1 --json --no-lock`                                                                                 |
| `cat_config_rc10.stderr`        | `restic cat config` against a path with no repository                                                                          |
| `cat_config_rc12.stderr`        | `restic cat config` with the wrong password                                                                                    |
| `forget.stdout`                 | `restic forget --group-by '' --keep-last 1`                                                                                    |
| `check.stdout`, `prune.stdout`  | `restic check`, `restic prune`                                                                                                 |

## Exit codes observed (unchanged from 0.18.1)

| Code | Situation                                                                 |
| ---- | ------------------------------------------------------------------------- |
| 0    | success                                                                   |
| 1    | fatal — including _all_ backup source paths missing                       |
| 3    | partial backup; snapshot **was** created and the summary **is** on stdout |
| 10   | repository does not exist                                                 |
| 12   | wrong password                                                            |
| 130  | terminated by SIGINT **or SIGTERM** (was 1 before 0.19.0)                 |

`snapshots.json` deliberately holds two records with different shapes: the first
has `tags` and no `parent`, the second has a `parent` and **no `tags` key at
all**. Optional keys being absent rather than null is a real restic behaviour the
normalizer has to survive.
