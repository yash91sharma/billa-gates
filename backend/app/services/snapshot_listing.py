"""On-demand snapshot listing — restic is the single source of truth.

This service replaces the old `Snapshot` ORM table. The UI queries restic
directly through `list_snapshots`; a small TTL cache absorbs dashboard
refresh storms. See gaps.md C4-Alt for the architectural motivation —
maintaining a parallel DB copy of snapshot metadata created a class of
reconciliation bugs that don't exist when restic is the only writer.
"""

import json
from time import monotonic as _monotonic
from typing import Any, Dict, List, Tuple

from app.core.logging import get_logger, log_call
from app.services import restic, restic_process

logger = get_logger(__name__)


# Default values are chosen for the local-destination case (sub-second restic
# calls); remote backends can override per call. 60s timeout matches the read
# patience of the surrounding HTTP request; 30s TTL matches typical dashboard
# refresh cadence (5–10s) so a single restic call covers 3–6 polls.
_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_TTL_SECONDS = 30


class SnapshotListingError(Exception):
    """Raised when restic snapshots cannot be listed for any reason.

    Surfaced to the API layer so a failure becomes an explicit HTTP error
    rather than a silent empty list — the empty-list-on-failure path was
    the root of gaps.md C4.
    """


# Cache entry: (snapshots, expiry_monotonic_seconds).
_cache: Dict[str, Tuple[List[Dict[str, Any]], float]] = {}


def _clear_cache() -> None:
    """Invalidate the entire TTL cache.

    Used by tests so cache state from one test does not leak into the next,
    and available as an explicit invalidation hook if the backup runner ever
    needs to force a fresh read after writing a new snapshot.
    """
    _cache.clear()


def _normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map restic's raw snapshot dict to the API response shape.

    Translates restic-internal keys (`id`, `time`, `summary.total_bytes_processed`)
    into the stable names exposed by the API (`snapshot_id`, `snapshot_time`,
    `size_bytes`) so a future restic schema change does not leak through the API
    contract.

    **Size comes out of the `summary` sub-object.** There is no top-level
    `total_size` in restic's snapshot JSON — not in 0.18.1, not in 0.19.1 — and
    reading one returned None for every snapshot ever listed, so the UI's Size
    column was permanently blank. `summary.total_bytes_processed` is the same
    number restic prints in its own `snapshots` table. Snapshots written before
    restic 0.17 carry no `summary` at all, so the size stays unknown for those
    rather than raising. Guarded by tests/test_restic_contract.py, which checks
    this mapping against recorded restic output instead of a fixture.
    """
    summary = raw.get("summary")
    size_bytes = (
        summary.get("total_bytes_processed") if isinstance(summary, dict) else None
    )
    return {
        "snapshot_id": raw.get("id"),
        "snapshot_time": raw.get("time"),
        "hostname": raw.get("hostname") or "",
        "paths": raw.get("paths") or [],
        "tags": raw.get("tags"),
        "size_bytes": size_bytes,
    }


@log_call
async def list_snapshots(
    repo_path: str,
    password: str,
    *,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """List every snapshot in `repo_path`.

    Calls `restic snapshots --json --no-lock`. `--no-lock` is safe for
    snapshot listing (read-only against the index) and avoids being blocked
    by a concurrent backup or a stale lock file.

    No tag filter: each job owns its repository outright
    (/destinations/<label>/<name>), so the repo *is* the scope. This is what
    lets a job recreated over an existing repository see the history it
    inherited — filtering by a per-job identifier would hide every snapshot
    written before the job row existed.

    Successful results are cached for `ttl_seconds` keyed by repo_path so that
    UI refresh storms only trigger one restic call per cache window. Failed
    calls (raised SnapshotListingError) are deliberately NOT cached — a
    transient backend hiccup must not lock the UI into an error for the full
    TTL window.

    Every way of not getting a listing is a SnapshotListingError, including a
    restic process that could not be started or read: the route turns that into
    a 503 with the reason attached, and the alternative — an exception escaping
    into the request — is a 500 that says nothing about the detached drive that
    caused it.

    Raises:
        SnapshotListingError: on non-zero exit, malformed JSON, or timeout.
    """
    now = _monotonic()
    if use_cache:
        cached = _cache.get(repo_path)
        if cached is not None and cached[1] > now:
            return cached[0]

    # argv and environment come from restic.py's builders, and the subprocess
    # contract (registry, timeout, SIGTERM-before-SIGKILL, contained launch
    # failure) from restic_process. This call used to hand-roll all three,
    # which meant an environment variable added for every other restic command
    # silently skipped this one.
    outcome = await restic_process.run_restic(
        restic.build_snapshots_args(),
        env_overrides=restic.build_restic_env_overrides(repo_path, password),
        timeout_seconds=timeout_seconds,
    )
    if outcome.timed_out:
        raise SnapshotListingError(
            f"restic snapshots timed out after {timeout_seconds}s"
        ) from outcome.error
    if outcome.error is not None:
        raise SnapshotListingError(
            f"failed to launch restic: {outcome.error}"
        ) from outcome.error

    if outcome.returncode != 0:
        raise SnapshotListingError(
            f"restic snapshots failed rc={outcome.returncode} "
            f"stderr={outcome.stderr.decode()!r}"
        )

    try:
        raw = json.loads(outcome.stdout.decode())
    except json.JSONDecodeError as exc:
        raise SnapshotListingError(
            f"restic snapshots returned unparseable JSON: {exc}"
        ) from exc

    if not isinstance(raw, list):
        raise SnapshotListingError(
            f"restic snapshots returned non-list JSON: {type(raw).__name__}"
        )

    result: List[Dict[str, Any]] = [_normalize(snap) for snap in raw]

    if use_cache:
        _cache[repo_path] = (result, now + ttl_seconds)

    return result


__all__ = [
    "SnapshotListingError",
    "list_snapshots",
    "_clear_cache",
]
