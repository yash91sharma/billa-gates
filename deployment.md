# Deployment

The image is built from the `Dockerfile` at the repo root.

---

## 1. Build the image

```bash
# Apple Silicon / ARM64 Linux:
docker build --build-arg RESTIC_ARCH=arm64 -t billa-gates:latest .

# Intel/AMD x86-64:
docker build --build-arg RESTIC_ARCH=amd64 -t billa-gates:latest .
```

`RESTIC_ARCH` is **required** — the build fails loudly without it. Only
`arm64` and `amd64` are accepted.

`RESTIC_VERSION` defaults to the version pinned in the `Dockerfile`.

Expected final image size: **~196 MB**.

---

## 2. Create the host data directory

The container persists SQLite + the restic local cache under `/app/data`.
Pick a directory on the host that will hold this state and that survives
container restarts:

```bash
mkdir -p ./data
```

---

## 3. `docker-compose.yml`

Adjust the `volumes` block for actual sources and destinations —
**every backup source must be mounted under `/sources/<label>` read-only**
and **every backup destination must be mounted under `/destinations/<label>`
read-write**. The `<label>` you use here is what shows up in the UI's
**Source** and **Destination** dropdowns on the "Create Job" form —
these are populated by scanning those two directories at request time,
so any volume you add is immediately selectable without a restart of
the container. I use Traefik for reverse-proxy, change if needed.

> [!IMPORTANT]
> **Source & Destination Mount Verification**: Every source folder mounted under `/sources/<label>` and every destination folder mounted under `/destinations/<label>` must contain an empty sentinel file named `.billa_gates_check` at its root (e.g., `/sources/nas/.billa_gates_check` and `/destinations/main/.billa_gates_check`). Billa-Gates checks for these files before every run to verify the mounts are active. If either file is missing (e.g., the underlying SMB/NFS share or USB-C drive disconnected), the run fails immediately to prevent silent failures, data loss, or repository inconsistencies.

```yaml
services:
  billa-gates:
    image: billa-gates:latest
    container_name: billa-gates
    environment:
      - TZ=America/Los_Angeles
      - LOG_LEVEL=INFO
      - RESTIC_CACHE_DIR=/app/data/restic-cache
    # ⚠ SECURITY: this port binding bypasses Traefik and any auth middleware.
    # Anyone on the LAN can reach the UI unauthenticated. For production
    # behind Traefik, remove the ports.
    ports:
      - '12345:12345'
    volumes:
      # ── Sources (read-only) ── must live under /sources/<label>
      - /Users/yash/Documents:/sources/documents:ro
      - /Volumes/YashNAS:/sources/nas:ro
      # - /Users/yash/Photos:/sources/photos:ro

      # ── Destinations (read-write) ── must live under /destinations/<label>
      - /Volumes/BackupDrive:/destinations/main:rw
      # - /Volumes/BackupDrive2:/destinations/offsite:rw

      # ── App data (SQLite + restic cache) ── do not change this path
      - ./data:/app/data:rw
    restart: unless-stopped
    networks:
      - traefik_default

networks:
  traefik_default:
    external: true
```

### Environment variables

| Variable           | Default                  | Purpose                                                                                |
| ------------------ | ------------------------ | -------------------------------------------------------------------------------------- |
| `TZ`               | `UTC`                    | IANA timezone used for schedule evaluation and timestamp display.                      |
| `LOG_LEVEL`        | `INFO`                   | Root log level. Set to `DEBUG` to surface every `@log_call`-decorated function's args. |
| `RESTIC_CACHE_DIR` | `/app/data/restic-cache` | Directory where restic local cache resides. Ensure this is on persistent storage.      |

If you have custom caching needs (e.g. mounting to a high-speed SSD separate from configuration), you can override this by setting the `RESTIC_CACHE_DIR` environment variable in your `docker-compose.yml`. Make sure the path remains persistent across container restarts.

Restic repository passwords are **not** environment variables — each
backup job stores its own password in the SQLite DB (without any encryption), configured via the
UI when the job is created.

---

## 4. Concurrency & Locking Constraints

> [!WARNING]
> Billa-Gates **must** run as a single-process container with exactly one (1) Uvicorn worker, and cannot be scaled horizontally.

- **In-Memory Locking**: The application uses Python's `asyncio.Lock` to coordinate and prevent overlapping backup, prune, or check runs on the same repository.
- **Single Process Constraint**: Because locks are held in memory, running multiple Uvicorn workers (e.g., `--workers 4`) or scaling the container horizontally across multiple nodes will bypass this locking mechanism.
- **Repository Corruption Risk**: If multiple workers or container replicas attempt to perform write operations (like backup, prune, or forget) on the same restic repository simultaneously, it will cause lock conflicts, failed runs, or potential repository write collisions.

---

### Run it

```bash
docker compose up -d
docker compose logs -f billa-gates
```

The first start runs `alembic upgrade head` to materialise the SQLite
schema, then boots `uvicorn` on port 12345. Open the UI at
`http://<host>:12345/` and create your first job.

---

## 6. Cutting a new version (auto build & deploy)

```bash
cd frontend

# Pick ONE, based on the nature of the change (semver):
npm version patch    # 1.0.6 -> 1.0.7  (bug fixes)
npm version minor    # 1.0.6 -> 1.1.0  (new features, backwards-compatible)
npm version major    # 1.0.6 -> 2.0.0  (breaking changes)
```

That single command:

1. Updates `version` in `frontend/package.json` **and** `frontend/package-lock.json`.
2. Creates a git commit containing those two changes.
3. Creates an annotated git tag `vX.Y.Z` pointing at that commit.

### Manual

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Keep the tag's `X.Y.Z` identical to the `version` field you wrote, otherwise
the Docker Hub image tag and the app's reported version will disagree.

---

## 7. Backing up Billa-Gates

The SQLite database at `data/billa-gates.db` contains every job, run, and
snapshot record. The actual restic repositories live under whatever you
mounted to `/destinations/<label>`. To make config + history survive a
catastrophic host loss, periodically copy `data/billa-gates.db` (along with
`data/restic-cache/` if you want to skip rebuilding it) to an off-host
location. It might be a good idea to turn down the container before
copying this file.
