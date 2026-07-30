<p align="center">
  <img src="frontend/src/assets/billa.png" alt="Billa-Gates logo" width="120" />
</p>

# Billa-Gates

A web app for scheduled [restic](https://restic.net/) backups — create
backup jobs, run them on a schedule, apply retention, verify integrity, and get
notified.

> Note: I have used AI tools (Claude Code, Codex, Antigravity) to build this project, and I
> use this to backup my NAS directories (Photos, documents, log files). If you are using this,
> please first test (backup+restore) to make sure it works for your use case. Please do start
> and issue if you find a bug.

![Dashboard](frontend/screenshots/pages/Dashboard.png)

## Features

- **Scheduled backups** — cron expressions or simple intervals (`6h`, `1d`, `30m`).
- **Retention & pruning** — `forget`/`prune` policies applied after each run.
- **Integrity checks** — optional `restic check` verification per job.
- **Snapshot browsing** — view snapshots and run history in the UI.
- **Drive capacity** — a Backup Destinations page showing total, used and free space per drive.
- **Notifications** — push run results to [ntfy](https://ntfy.sh/).
- **Single container** — FastAPI serves both the API and the React frontend.

## Screenshots

| Jobs                                         | Job detail                                              | Settings                                             |
| -------------------------------------------- | ------------------------------------------------------- | ---------------------------------------------------- |
| ![Jobs](frontend/screenshots/pages/Jobs.png) | ![Job detail](frontend/screenshots/pages/JobDetail.png) | ![Settings](frontend/screenshots/pages/Settings.png) |

## Note

Published images are **arm64 only**. If you need `amd64`, build your own image — instructions are in [deployment.md](deployment.md).

## Deploy

```bash
mkdir billa-gates && cd billa-gates
mkdir data   # holds the SQLite DB, restic cache etc.
```

```yaml
services:
  billa-gates:
    image: yash91sharma/billa-gates:latest
    container_name: billa-gates
    ports:
      - '12345:12345'
    environment:
      - TZ=America/Los_Angeles
    volumes:
      # ── Sources (read-only) ── one line per folder you want to back up.
      # The label after /sources/ is what shows up in the UI dropdown.
      - /path/to/back/up:/sources/documents:ro
      # - /path/to/photos:/sources/photos:ro

      # ── Destinations (read-write) ── your backup drives.
      # Each job creates its own restic repo at /destinations/{label}/{job name}.
      - /path/to/backup/drive:/destinations/main

      # ── App state (SQLite DB + restic cache) ── do not change the /app/data side.
      - ./data:/app/data
    restart: unless-stopped
```

## Sentinel files

Billa-Gates won't run against a folder missing a sentinel file (`.billa_gates_check`) at its root:

```bash
touch /path/to/back/up/.billa_gates_check       # each source mount
touch /path/to/backup/drive/.billa_gates_check  # each destination
```

- **Sources** are mounted read-only under `/sources/{label}` and become selectable in the UI. A job backs up the whole mount, so the sentinel goes at its root.
- **Destinations** are backup drives mounted under `/destinations/{label}`. Each job gets its own restic repository on that drive at `/destinations/{label}/{job name}`, created when you create the job.

| Job configuration                     | Effective backup path | Sentinel file                           |
| ------------------------------------- | --------------------- | --------------------------------------- |
| Source `documents`                    | `/sources/documents`  | `/sources/documents/.billa_gates_check` |
| Destination `main` (any job using it) | `/destinations/main`  | `/destinations/main/.billa_gates_check` |

- Create the file once, on the underlying drive/share — not on a temporary mountpoint.
- If a run fails with a missing-sentinel error, it usually means the mount actually dropped. Check the mount before recreating the file.
- Application state (SQLite DB + restic cache) lives in `/app/data`.

## Backup Destinations page

**Destinations** in the sidebar lists every drive mounted under `/destinations` (plus any destination a job still points at, even if its folder has gone) with total, used and free space.

A few things about the numbers:

- **Used + free does not add up to total.** Filesystems hold blocks back. **Free** is the figure to plan against: it is what a backup can actually write, and it matches the `Avail` column of `df -h` exactly.
- **The percentage is measured against total**, so it is derivable from the numbers beside it. `df` computes its `Use%` against used + free instead, so `df` reads a few points higher on the same drive. A drive can also show 95% used with **zero bytes free**, which is why the "no space left" flag keys on free space rather than on the percentage.
- **Nothing is stored and no history is kept.** Figures are read from the filesystem when the page loads, and re-read after any job run finishes; **Refresh** forces a fresh read. Restarting the container simply measures again.
- **Two labels can be folders on one drive** (or a label may not be a real mount at all, in which case it reports the container's own filesystem). Those rows are marked, and their capacities must not be added together — which is why the page shows no total.
- **A drive that is detached, unreadable, or too slow to answer** shows dashes and a reason for that row alone; the other drives still load.

Note restic deduplicates and compresses, so a source larger than the free space shown may still fit. The page sizes the drive, not the next snapshot.

## Recovering a job after losing the database

A job's repository is named after the job itself — `/destinations/{destination}/{job name}` — so it never depends on the database to be found again. If `/app/data` is lost, or you delete a job by mistake, create a new job with the **same name, same destination, and same password**: it adopts the existing repository and carries on, with all previous snapshots intact and retention still applying to them.

Because of that, three fields are fixed once a job is created — `name`, `destination`, and `password` — since together they are the address of the repository. Deleting a job leaves its repository on disk by default. Select **Also permanently delete the repository** in the delete dialog if you really want the snapshots gone.

## Quirks

- **Interval schedules reset on container restart**:
  Interval schedules (`6h`, `1d`, `30m`) count from application startup, not from the last backup. Restarting the container resets the countdown. If your container restarts frequently, use a cron schedule instead — cron fires at fixed wall-clock times and is unaffected by restarts.

- Billa-Gates **must** run as a single-process container with exactly one (1) Uvicorn worker, and cannot be scaled horizontally:
  The application uses Python's `asyncio.Lock` to coordinate and prevent overlapping backup, prune, or check runs on the same repository. Because locks are held in memory, running multiple Uvicorn workers (e.g., `--workers 4`) or scaling the container horizontally across multiple nodes will bypass this locking mechanism. If multiple workers or container replicas attempt to perform write operations (like backup, prune, or forget) on the same restic repository simultaneously, it will cause lock conflicts, failed runs, or potential repository write collisions.

## License

[MIT](LICENSE)
