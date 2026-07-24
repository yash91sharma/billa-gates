<p align="center">
  <img src="frontend/src/assets/billa.png" alt="Billa-Gates logo" width="120" />
</p>

# Billa-Gates

A web app for scheduled [restic](https://restic.net/) backups — create
backup jobs, run them on a schedule, apply retention, verify integrity, and get
notified.

Note: I have used AI tools (Claude Code, Codex, Antigravity) to build this project, and I
use this to backup my NAS directories (Photos, documents, log files). If you are using this,
please first test (backup+restore) to make sure it works for your use case. Please do start
and issue if you find a bug.

![Dashboard](frontend/screenshots/pages/Dashboard.png)

## Features

- **Scheduled backups** — cron expressions or simple intervals (`6h`, `1d`, `30m`).
- **Retention & pruning** — `forget`/`prune` policies applied after each run.
- **Integrity checks** — optional `restic check` verification per job.
- **Snapshot browsing** — view snapshots and run history in the UI.
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
touch /path/to/back/up/.billa_gates_check         # each source mount
touch /path/to/back/up/photos/.billa_gates_check  # each subfolder a job backs up
touch /path/to/backup/drive/.billa_gates_check    # each destination
```

> [!IMPORTANT]
> Do this **before** starting the container.

Then open <http://localhost:12345> and create your first job.

- **Sources** are mounted read-only under `/sources/{label}` and become selectable in the UI.
- If a job sets a **Subfolder**, the folder it backs up is `/sources/{label}/{subfolder}` — that is its effective backup path, and it needs its own sentinel file. A sentinel at the mount root does not cover it.
- **Destinations** are backup drives mounted under `/destinations/{label}`. Each job gets its own restic repository on that drive at `/destinations/{label}/{job name}`, created when you create the job.

| Job configuration                     | Effective backup path | Sentinel file                            |
| ------------------------------------- | --------------------- | ---------------------------------------- |
| Source `documents`, no subfolder      | `/sources/documents`  | `/sources/documents/.billa_gates_check`  |
| Source `nas` + subfolder `photos`     | `/sources/nas/photos` | `/sources/nas/photos/.billa_gates_check` |
| Destination `main` (any job using it) | `/destinations/main`  | `/destinations/main/.billa_gates_check`  |

- Create the file once, on the underlying drive/share — not on a temporary mountpoint.
- If a run fails with a missing-sentinel error, it usually means the mount actually dropped. Check the mount before recreating the file.
- Application state (SQLite DB + restic cache) lives in `/app/data` — keep it on a volume.

## Recovering a job after losing the database

A job's repository is named after the job itself — `/destinations/{destination}/{job name}` — so it never depends on the database to be found again. If `/app/data` is lost, or you delete a job by mistake, create a new job with the **same name, same destination, and same password**: it adopts the existing repository and carries on, with all previous snapshots intact and retention still applying to them.

Because of that, three fields are fixed once a job is created — `name`, `destination`, and `password` — since together they are the address of the repository. Deleting a job leaves its repository on disk by default; tick **Also permanently delete the repository** in the delete dialog if you really want the snapshots gone.

## Quirks

- **Interval schedules reset on container restart**:
  Interval schedules (`6h`, `1d`, `30m`) count from application startup, not from the last backup. Restarting the container resets the countdown. If your container restarts frequently, use a cron schedule instead — cron fires at fixed wall-clock times and is unaffected by restarts.

- Billa-Gates **must** run as a single-process container with exactly one (1) Uvicorn worker, and cannot be scaled horizontally:
  The application uses Python's `asyncio.Lock` to coordinate and prevent overlapping backup, prune, or check runs on the same repository. Because locks are held in memory, running multiple Uvicorn workers (e.g., `--workers 4`) or scaling the container horizontally across multiple nodes will bypass this locking mechanism. If multiple workers or container replicas attempt to perform write operations (like backup, prune, or forget) on the same restic repository simultaneously, it will cause lock conflicts, failed runs, or potential repository write collisions.

## License

[MIT](LICENSE)
