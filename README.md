<p align="center">
  <img src="frontend/src/assets/billa.png" alt="Billa-Gates logo" width="120" />
</p>

# Billa-Gates

A self-hosted web UI for scheduled [restic](https://restic.net/) backups — create
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

## Quick start

Published images are **arm64 only**. Please feel free to build your own `amd64` image if you need, instructions are in the [deployment.md](deployment.md) file.

```bash
docker run -d \
  --name billa-gates \
  -p 12345:12345 \
  -e TZ=America/New_York \
  -v billa-gates-data:/app/data \
  -v /path/to/back/up:/sources/documents:ro \
  -v /path/to/restic/repo:/destinations/main \
  yash91sharma/billa-gates:latest
```

Then open <http://localhost:12345>.

- **Sources** are mounted read-only under `/sources/{label}` and become selectable in the UI.
- **Destinations** are backup drives mounted under `/destinations/{label}`. Each job gets its own restic repository on that drive at `/destinations/{label}/{job name}`, created when you create the job.
- Application state (SQLite DB + restic cache) lives in `/app/data` — keep it on a volume.

## Recovering a job after losing the database

A job's repository is named after the job itself — `/destinations/{destination}/{job name}` — so it never depends on the database to be found again. If `/app/data` is lost, or you delete a job by mistake, create a new job with the **same name, same destination, and same password**: it adopts the existing repository and carries on, with all previous snapshots intact and retention still applying to them.

Because of that, three fields are fixed once a job is created — `name`, `destination`, and `password` — since together they are the address of the repository. Deleting a job leaves its repository on disk by default; tick **Also permanently delete the repository** in the delete dialog if you really want the snapshots gone.

## Quirks

- **Interval schedules reset on container restart**:
  Interval schedules (`6h`, `1d`, `30m`) count from application startup, not from the last backup. Restarting the container resets the countdown, so a container that restarts more often than the interval (auto-updaters, nightly host reboots) will keep pushing the next run into the future and the job may never fire. If your container restarts frequently, use a cron schedule instead — cron fires at fixed wall-clock times and is unaffected by restarts.
- **Mount Check Safeguards (`.billa_gates_check`)**:
  To protect against silent data loss when backing up network shares (like SMB/NFS mounts that can drop and appear empty) or external USB drives that might disconnect, Billa-Gates requires a sentinel file named `.billa_gates_check` to be present at the root of **every** mounted source folder (e.g. `/sources/documents/.billa_gates_check`) and **every** destination folder (e.g. `/destinations/main/.billa_gates_check`). If either file is missing, the backup or integrity check will fail immediately rather than running on an empty path or unmounted target.

## License

[MIT](LICENSE)
