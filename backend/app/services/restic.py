"""The restic command surface: which command, which flags, what the result means.

One wrapper per restic subcommand. Everything a wrapper needs beyond that is
owned elsewhere and shared by all of them, so that a fix lands once:

* :mod:`app.services.restic_process` — the one place that spawns a restic
  process, and the contract it is run under: registered so Stop can reach it,
  bounded by a timeout, SIGTERMed before it is ever SIGKILLed, and never
  allowed to raise a launch failure into a pipeline.
* :mod:`app.services.restic_stream` — the bounded digestion of `restic backup
  --json` output, which arrives for hours and must never be buffered.

What stays here: the argv builders, the repository environment, and the small
amount of interpretation each command needs (parsing a version string, picking
the parent snapshot, deciding when a summary is trustworthy).

**Every argv comes from a `build_*_args()` function.** They are pure so the Job
detail page can render exactly the command a run will issue (see
app/services/job_commands.py). Never inline an argv in a wrapper: the moment
the preview and the subprocess are assembled by two different pieces of code,
the page starts telling operators that flags are applied which are not — and
retention or exclude mistakes of that kind are invisible until data is already
lost.
"""

import asyncio
import json
import os
import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

from app.core.logging import get_logger, log_call
from app.services import restic_process
from app.services.restic_stream import (
    BackupOutputCollector,
    BoundedOutput,
    format_duration,
    pump_stream,
)

logger = get_logger(__name__)


class ResticError(Exception):
    """Raised when a restic operation fails or times out."""

    pass


@log_call
def build_restic_env_overrides(repo_path: str, password: str) -> Dict[str, str]:
    """The environment variables every restic command is given, on top of the
    inherited process environment.

    The single description of what a restic subprocess is told about the
    repository. `restic_process.run_restic` layers it over `os.environ`, and
    the job command preview (app/services/job_commands.py) shows the operator
    these same variables rather than a second hand-written copy that would
    drift.
    """
    return {
        "RESTIC_REPOSITORY": repo_path,
        "RESTIC_PASSWORD": password,
        "RESTIC_CACHE_DIR": os.environ.get(
            "RESTIC_CACHE_DIR", "/app/data/restic-cache"
        ),
    }


# ── Command-line builders ────────────────────────────────────────────────────

RESTIC_BIN: str = "restic"


def build_version_args() -> List[str]:
    """argv for the startup version probe (the only command needing no repo)."""
    return [RESTIC_BIN, "version"]


def build_cat_config_args() -> List[str]:
    """argv for the repo/password verification step."""
    return [RESTIC_BIN, "cat", "config"]


def build_init_args() -> List[str]:
    """argv for repository provisioning (job creation only, never a run)."""
    return [RESTIC_BIN, "init"]


def build_snapshots_args() -> List[str]:
    """argv for listing every snapshot in the repo (the Snapshots tab).

    Deliberately unfiltered — no tag, no host, no path. The repository at
    /destinations/<label>/<name> belongs to exactly one job, so the repo *is*
    the scope; filtering would hide every snapshot written before the current
    job row existed, which is precisely the history a recreated job adopts.

    `--no-lock` keeps this read-only listing from blocking on a write lock held
    by a concurrent backup or left behind as a stale lock file.
    """
    return [RESTIC_BIN, "snapshots", "--json", "--no-lock"]


def build_latest_snapshot_args() -> List[str]:
    """argv for the parent-snapshot lookup.

    `--no-lock` keeps this read-only step from blocking on a write lock held by
    a concurrent backup or left behind as a stale lock file.

    `--latest 1` bounds the output, it does not select the parent: restic
    applies it *per (host, paths) group*, so this can return several rows. The
    parent is chosen from them by :func:`_newest_snapshot` — see the reasoning
    there before changing either.
    """
    return [RESTIC_BIN, "snapshots", "--latest", "1", "--json", "--no-lock"]


def build_unlock_args(*, remove_all: bool = False) -> List[str]:
    """argv for lock removal — stale-only by default, every lock with
    `remove_all`.

    The two callers need different commands, and the difference is not a
    preference. Bare `restic unlock` removes only locks restic judges *stale*,
    and it judges nothing stale that is younger than ~30 minutes or was written
    under a hostname other than the current one. A container that is killed
    mid-prune and then **recreated** (a new container id is a new hostname)
    leaves exactly such a lock: every later run fails with "repository is
    already locked", and bare `unlock` answers by exiting 0 having removed
    nothing.

    So the automatic pre-run step keeps the narrow form — force-removing a lock
    another process is still holding is the two-writer accident locks exist to
    prevent — and the operator-triggered Unlock button passes remove_all=True.
    That button is safe precisely because a repository belongs to exactly one
    job (destination_label + name is unique), so no other job's run can hold a
    lock in it, and a run of *this* job is refused with 409 before we get here.
    """
    args = [RESTIC_BIN, "unlock"]
    if remove_all:
        args.append("--remove-all")
    return args


def build_list_locks_args() -> List[str]:
    """argv for enumerating the repository's lock ids.

    `--no-lock` is not an optimization here: this command runs precisely when
    the repository may be locked, and taking a lock in order to ask what holds
    a lock would block on the answer.
    """
    return [RESTIC_BIN, "list", "locks", "--no-lock"]


def build_cat_lock_args(lock_id: str) -> List[str]:
    """argv for one lock's metadata (who holds it, since when, exclusive?).

    Read *before* removing, since the lock is gone afterwards — naming what was
    removed is the whole point of the response.
    """
    return [RESTIC_BIN, "cat", "lock", lock_id, "--no-lock"]


def build_prune_args() -> List[str]:
    """argv for a standalone prune (no retention flags — see restic_forget)."""
    return [RESTIC_BIN, "prune"]


def build_check_args(mode: str, subset_percent: Union[int, str, None]) -> List[str]:
    """argv for an integrity check. `structural` needs no extra flag.

    `subset_percent` is an int in every execution path. The union widens it for
    the command preview alone, which passes a placeholder for the percentage
    the operator types into the check dialog — so the flag's spelling still
    comes from here rather than being re-written by the preview.
    """
    args: List[str] = [RESTIC_BIN, "check"]
    if mode == "full":
        args.append("--read-data")
    elif mode == "subset" and subset_percent is not None:
        args.append(f"--read-data-subset={subset_percent}%")
    return args


def build_backup_args(
    source_path: str,
    *,
    parent_snapshot_id: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """argv for `restic backup`, assembled from a job's options.

    `--host` is pinned to a fixed string so retention isn't silently split
    per container ID (each rebuild gets a new hostname, and `restic forget`
    groups by host+paths by default).

    Snapshots carry no per-job identity tag: the repo at
    /destinations/<label>/<name> belongs to exactly one job, so the repo is
    already the scope. Retention across path changes (gaps.md C3) is handled
    by `restic forget --group-by ''`, which collapses host and paths into a
    single group. Any --tag values below are the user's own, from job.tags.
    """
    args: List[str] = [
        RESTIC_BIN,
        "backup",
        "--host",
        "billa-gates",
    ]

    # Explicit --parent lets restic skip the full-tree rescan even when host
    # or paths have changed; without it, any source path change makes the
    # next backup re-read every file from disk (gaps.md C5). Omit on the
    # genuine first run — passing a bogus --parent would fail the backup.
    if parent_snapshot_id:
        args.extend(["--parent", parent_snapshot_id])

    if kwargs.get("exclude_patterns"):
        for pattern in kwargs["exclude_patterns"]:
            args.extend(["--exclude", pattern])

    if kwargs.get("exclude_caches"):
        args.append("--exclude-caches")

    if kwargs.get("exclude_if_present"):
        for file in kwargs["exclude_if_present"]:
            args.extend(["--exclude-if-present", file])

    if kwargs.get("one_file_system"):
        args.append("--one-file-system")

    if kwargs.get("no_scan"):
        args.append("--no-scan")

    if kwargs.get("tags"):
        for tag in kwargs["tags"]:
            args.extend(["--tag", tag])

    if kwargs.get("compression"):
        args.extend(["--compression", kwargs["compression"]])

    if kwargs.get("pack_size"):
        args.extend(["--pack-size", str(kwargs["pack_size"])])

    if kwargs.get("read_concurrency"):
        args.extend(["--read-concurrency", str(kwargs["read_concurrency"])])

    # Always emit JSON so summary/error lines are machine-parseable. Never
    # add --verbose: it makes restic print one JSON line per new/changed
    # file, which on a multi-million-file source is hundreds of MB of output
    # for no post-mortem value. Error lines and the final summary are emitted
    # regardless of verbosity.
    args.append("--json")

    args.append(source_path)
    return args


# Retention kwargs → `restic forget` flags. The only place the mapping lives.
FORGET_FLAG_MAP: Dict[str, str] = {
    "retain_keep_last": "--keep-last",
    "retain_keep_hourly": "--keep-hourly",
    "retain_keep_daily": "--keep-daily",
    "retain_keep_weekly": "--keep-weekly",
    "retain_keep_monthly": "--keep-monthly",
    "retain_keep_yearly": "--keep-yearly",
    "retain_keep_within": "--keep-within",
    "retain_keep_within_hourly": "--keep-within-hourly",
    "retain_keep_within_daily": "--keep-within-daily",
    "retain_keep_within_weekly": "--keep-within-weekly",
    "retain_keep_within_monthly": "--keep-within-monthly",
    "retain_keep_within_yearly": "--keep-within-yearly",
}


def build_forget_args(**retention_flags: Any) -> List[str]:
    """argv for `restic forget` with the job's retention policy.

    --group-by '' puts every snapshot in the repo into one retention group,
    so the policy applies across any historical path or host change. The
    original --group-by paths kept old-path snapshots forever whenever a
    job's source path changed (gaps.md C3).

    Deliberately unfiltered: the repo holds exactly one job's snapshots.
    Scoping by a per-job tag would strand every snapshot taken before the
    current job row existed — they would never be pruned, and the repo would
    grow without bound after a job is recreated.
    """
    args: List[str] = [
        RESTIC_BIN,
        "forget",
        "--group-by",
        "",
    ]

    for kwarg_name, flag_name in FORGET_FLAG_MAP.items():
        if kwarg_name in retention_flags and retention_flags[kwarg_name] is not None:
            args.extend([flag_name, str(retention_flags[kwarg_name])])

    return args


# ── The captured-output commands ─────────────────────────────────────────────


def _launch_failure_message(error: BaseException) -> str:
    """What to put in the stderr slot when restic never ran.

    Names the exception type as well as its text: `[Errno 2] No such file or
    directory` on its own reads like something the destination did, and sends
    an operator to look at a drive that is fine. The class is what says the
    process could not be created at all — a missing binary, a failed fork, a
    lost executable bit — which is a problem with the image, not the backup.
    """
    return f"restic could not be started: {type(error).__name__}: {error}"


@log_call
async def _run_captured(
    argv: List[str],
    repo_path: str,
    password: str,
    timeout_seconds: float,
    *,
    run_id: Optional[_uuid.UUID],
    timed_out_message: str,
) -> Tuple[int, str, str]:
    """Run a command whose output fits in memory; return (rc, stdout, stderr).

    The shape every wrapper below reports in: a real exit code with restic's
    two streams, or rc=-1 with the reason in place of stderr. rc=-1 is what
    callers branch on — restic's own codes are documented and stable from 0.17
    (10 = repo not found, 11 = lock failed, 12 = wrong password), and -1 can
    never collide with one.

    Decoding happens here rather than inside the runner so a non-UTF-8 byte in
    restic's output raises where it happened, instead of being reported as a
    failure to launch restic at all.

    The two rc=-1 messages stand in for stderr all the way to the run page, so
    each has to say which of the two it is. A timeout names the deadline it
    hit — "prune timed out" alone cannot be told apart from a 60-second
    metadata read and a 24-hour one, which is the difference between a slow
    destination and one that never answered. A launch failure says restic never
    started, because a bare OSError repr in the stderr slot reads like
    something the repository did.
    """
    outcome = await restic_process.run_restic(
        argv,
        env_overrides=build_restic_env_overrides(repo_path, password),
        timeout_seconds=timeout_seconds,
        run_id=run_id,
    )
    if outcome.timed_out:
        return (-1, "", f"{timed_out_message} after {format_duration(timeout_seconds)}")
    if outcome.error is not None:
        return (-1, "", _launch_failure_message(outcome.error))
    return outcome.returncode, outcome.stdout.decode(), outcome.stderr.decode()


# `restic version` is bounded far tighter than any repository command: it
# touches no backend, so anything slower than this is a broken binary.
_VERSION_TIMEOUT_SECONDS: int = 10


@log_call
async def restic_version() -> Optional[str]:
    """Parse restic version. Returns None on any failure.

    Runs with the inherited environment — no repository, no password. Every
    failure mode is the same answer: a missing binary, a hung probe and an
    unreadable version string all mean "unknown", which is what the health
    endpoint reports.
    """
    try:
        outcome = await restic_process.run_restic(
            build_version_args(), timeout_seconds=_VERSION_TIMEOUT_SECONDS
        )
        if outcome.error is not None:
            return None
        match = re.search(r"restic\s+([0-9.]+)", outcome.stdout.decode())
        return match.group(1) if match else None
    except Exception:
        return None


@log_call
async def restic_cat_config(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Check repo exists and password correct.

    A 60s timeout is applied because this command runs on every backup as the
    init-check step. Without it, a hung remote backend (NFS unresponsive,
    SMB offline, cloud mount stalled) would block run_backup indefinitely,
    holding the run row at status=running and locking the job out of every
    future trigger via trigger_run's overlap check.
    """
    return await _run_captured(
        build_cat_config_args(),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="cat config timed out",
    )


@log_call
async def restic_init(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Initialize a new restic repo.

    Same lock-up hazard as restic_cat_config — if the init-check decides the
    repo doesn't exist and restic_init then hangs on an unresponsive backend,
    the runner is wedged indefinitely. A 60s timeout is more than enough for
    a healthy backend (init is metadata-only).
    """
    return await _run_captured(
        build_init_args(),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="init timed out",
    )


@log_call
async def restic_forget(
    repo_path: str,
    password: str,
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
    **retention_flags: Any,
) -> Tuple[int, str, str]:
    """Apply retention policy by removing snapshot pointers.

    Forget is cheap — it just rewrites the index to drop snapshot references.
    Prune is the heavy operation that rewrites pack files; it is *not* invoked
    here. Run :func:`restic_prune` separately (manual trigger or its own
    schedule) so a backup window stays predictable (gaps.md H1).
    """
    return await _run_captured(
        build_forget_args(**retention_flags),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="forget/prune timed out",
    )


@log_call
async def restic_prune(
    repo_path: str,
    password: str,
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Standalone prune (no retention flags). Returns (returncode, stdout, stderr)."""
    return await _run_captured(
        build_prune_args(),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="prune timed out",
    )


@log_call
async def restic_check(
    repo_path: str,
    password: str,
    mode: str,
    subset_percent: Optional[int],
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Verify repo integrity."""
    return await _run_captured(
        build_check_args(mode, subset_percent),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="check timed out",
    )


@log_call
async def restic_unlock(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
    remove_all: bool = False,
) -> Tuple[int, str, str]:
    """Remove locks — stale ones by default, all of them with `remove_all`.

    Called during init-check stale-lock recovery and in Step 4.5 auto-unlock
    (both stale-only), and from the Unlock button (`remove_all=True` — see
    :func:`build_unlock_args` for why the button needs a different command).
    A hung backend during unlock previously wedged the runner; 60s is far more
    than needed for a metadata-only delete on a healthy backend.

    **The exit code says nothing about what was removed.** restic exits 0
    whether it deleted every lock or judged them all live and deleted none, so
    a caller reporting success from rc=0 is guessing. Read the lock list on
    both sides with :func:`restic_list_locks` instead.
    """
    return await _run_captured(
        build_unlock_args(remove_all=remove_all),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="unlock timed out",
    )


@log_call
async def restic_list_locks(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """List the repository's lock ids, one per line on stdout.

    Parse with :func:`parse_lock_ids`. A non-zero rc means the repository could
    not be read at all (a detached destination), which is not the same as "no
    locks" — the caller must keep those apart, exactly as the snapshots route
    does.
    """
    return await _run_captured(
        build_list_locks_args(),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="list locks timed out",
    )


@log_call
async def restic_cat_lock(
    repo_path: str,
    password: str,
    lock_id: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """One lock's metadata as JSON on stdout. Parse with
    :func:`parse_lock_details`, which never raises: a lock whose metadata
    cannot be read was still removed, and saying so still helps."""
    return await _run_captured(
        build_cat_lock_args(lock_id),
        repo_path,
        password,
        timeout_seconds,
        run_id=run_id,
        timed_out_message="cat lock timed out",
    )


# `restic list locks` prints one 64-char hex id per line. Matching the shape
# rather than taking every line keeps a stray diagnostic on stdout from
# becoming a phantom lock in the UI.
_LOCK_ID_RE = re.compile(r"^[0-9a-f]{64}$")


@log_call
def parse_lock_ids(stdout: str) -> List[str]:
    """The lock ids in a `restic list locks` response, in restic's order."""
    return [
        line.strip()
        for line in stdout.splitlines()
        if _LOCK_ID_RE.fullmatch(line.strip())
    ]


@log_call
def parse_lock_details(lock_id: str, stdout: str) -> Dict[str, Any]:
    """One lock described for the operator: who holds it, since when, and
    whether it blocks everything or only writers.

    Every field is optional and the function never raises. It is called while
    reporting locks that have just been deleted, so a lock restic could not
    describe must still come back named — an id alone is more useful than an
    error, and an exception here would turn a successful unlock into a 500.
    """
    info: Dict[str, Any] = {
        "id": lock_id,
        "short_id": lock_id[:8],
        "created_at": None,
        "hostname": None,
        "username": None,
        "pid": None,
        "exclusive": None,
    }
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return info
    if not isinstance(raw, dict):
        return info

    info["created_at"] = _parse_snapshot_time(raw.get("time"))
    for key in ("hostname", "username"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            info[key] = value
    # bool is a subclass of int, so the pid check must exclude it explicitly.
    pid = raw.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        info["pid"] = pid
    exclusive = raw.get("exclusive")
    if isinstance(exclusive, bool):
        info["exclusive"] = exclusive
    return info


# ── The parent-snapshot lookup ───────────────────────────────────────────────


def _parse_snapshot_time(value: object) -> Optional[datetime]:
    """Parse restic's snapshot `time` field into an aware UTC datetime.

    restic writes RFC3339 with the **local offset of the machine that made the
    snapshot** (`2026-07-27T09:07:46.96747001-07:00` under the TZ the README
    recommends) and nanosecond precision; `fromisoformat` handles both on 3.11+.
    Naive values are read as UTC — the convention `app/api/schemas/base.py`
    already applies to stored timestamps — because comparing a naive datetime
    against an aware one raises, and an exception in the parent lookup is not a
    slow backup, it is no backup.

    Returns None for anything unparseable so the caller can fall back rather
    than fail.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@log_call
def _newest_snapshot(snapshots: List[Any]) -> Optional[Dict[str, Any]]:
    """The most recent snapshot in a `restic snapshots --latest 1` response.

    **`--latest 1` is not "the newest snapshot".** restic applies it per
    `(host, paths)` group and returns the groups oldest-first, so a repository
    that has ever held more than one source path or hostname comes back with
    several rows. That happens whenever a job's source_label is edited, a job
    is recreated over an adopted repo with a different source, or
    somebody runs `restic backup` by hand from a shell — that snapshot carries
    the machine's own hostname instead of the `--host billa-gates` this app
    pins. Taking `[0]` therefore passed `--parent` the newest snapshot of the
    *stale* group, and a parent whose tree does not match the source makes
    restic re-read and re-hash every file in it, every run, for as long as the
    stale group survives retention (measured on 0.19.1: 60,001 of 60,001 files
    re-read instead of 0, and the run then reports every one of them as "new").
    Adding `--group-by ''` does not fix it — verified against 0.19.1, it does
    not collapse the groups `--latest` uses.

    Picking the maximum by instant is correct whatever restic groups by: the
    newest snapshot in the repository is by definition the newest of its own
    group, so it is always one of the rows returned.
    """
    if len(snapshots) > 1:
        logger.debug(
            "parent lookup: %d snapshot group(s) returned, selecting newest",
            len(snapshots),
        )

    newest: Optional[Dict[str, Any]] = None
    newest_moment: Optional[datetime] = None
    # restic emits oldest-first, so the last usable row is the best guess left
    # if no timestamp can be read at all.
    fallback: Optional[Dict[str, Any]] = None

    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        fallback = snapshot
        moment = _parse_snapshot_time(snapshot.get("time"))
        if moment is None:
            continue
        # >= keeps restic's own ordering as the tie-break for identical stamps.
        if newest_moment is None or moment >= newest_moment:
            newest, newest_moment = snapshot, moment

    if newest is not None:
        return newest
    if fallback is not None:
        logger.warning(
            "parent lookup: no snapshot carried a readable 'time'; "
            "falling back to the last row restic returned"
        )
    return fallback


@log_call
async def restic_latest_snapshot_id(
    repo_path: str,
    password: str,
    *,
    timeout_seconds: int = 60,
    run_id: Optional[_uuid.UUID] = None,
) -> Optional[str]:
    """Return the id of the most recent snapshot in the repo, or None if no
    prior snapshot exists / the lookup fails. Used to pass --parent to
    `restic backup` so a host or path change doesn't force a full-tree rescan
    (gaps.md C5). Read-only — uses --no-lock so it never blocks on a write
    lock held by a concurrent backup or by a stale lock file.

    The repo belongs to exactly one job, so the newest snapshot in it is by
    definition this job's parent — no tag filter needed. Which row that is
    must be decided by timestamp (:func:`_newest_snapshot`), never by position:
    the response carries one row per (host, paths) group, oldest first.

    Raises ResticError rather than returning a sentinel: the caller records the
    failure on the run row and skips the backup, because backing up without a
    parent silently re-reads the entire source.
    """
    outcome = await restic_process.run_restic(
        build_latest_snapshot_args(),
        env_overrides=build_restic_env_overrides(repo_path, password),
        timeout_seconds=timeout_seconds,
        run_id=run_id,
    )
    if outcome.timed_out:
        raise ResticError(
            f"snapshots command timed out after {timeout_seconds} seconds"
        ) from outcome.error
    if outcome.error is not None:
        raise ResticError(
            f"failed to launch restic snapshots: {outcome.error}"
        ) from outcome.error

    if outcome.returncode != 0:
        raise ResticError(
            f"snapshots command failed with exit code {outcome.returncode}: "
            f"{outcome.stderr.decode()}"
        )
    try:
        snapshots = json.loads(outcome.stdout.decode())
    except json.JSONDecodeError as exc:
        raise ResticError(f"snapshots command returned malformed JSON: {exc}") from exc
    if not isinstance(snapshots, list):
        raise ResticError(
            f"snapshots command returned non-list JSON: {type(snapshots).__name__}"
        )
    if not snapshots:
        return None
    newest = _newest_snapshot(snapshots)
    if newest is None:
        raise ResticError("snapshots command returned no usable snapshot record")
    snap_id = newest.get("id")
    if not isinstance(snap_id, str):
        raise ResticError("snapshots command returned snapshot without a string ID")
    return snap_id


# ── The streamed command ─────────────────────────────────────────────────────

# How often the caller may be handed a snapshot of the retained output. At the
# tens-of-lines-per-second restic can emit, flushing per line would mean tens of
# DB writes per second.
_DEFAULT_PROGRESS_INTERVAL_SECONDS: float = 15.0


@log_call
async def restic_backup(
    repo_path: str,
    password: str,
    source_path: str,
    timeout_seconds: int,
    *,
    parent_snapshot_id: Optional[str] = None,
    run_id: Optional[_uuid.UUID] = None,
    on_output: Optional[Callable[[str], Awaitable[None]]] = None,
    progress_interval_seconds: Optional[float] = None,
    **kwargs: Any,
) -> Tuple[int, str, str, Optional[Dict[str, Any]]]:
    """Run a backup.

    Returns (returncode, retained stdout, retained stderr, parsed summary).
    Both output strings are bounded (see app/services/restic_stream.py) —
    restic's progress firehose is collapsed into a single progress line rather
    than buffered. Never go back to `communicate()` here: on a 210 MB stream
    (~5 h of backup) that cost ~1 GB of RSS to end up storing ~300 bytes, and
    OOM-killed the container on exactly the long initial-seed backups it needed
    to survive.

    `on_output`, when given, is awaited with a snapshot of the retained stdout
    at most every `progress_interval_seconds` (and once as soon as there is
    anything to show). The backup runner uses it to keep the run row up to date
    while a long backup is still in flight. Exceptions from it are logged and
    swallowed: persistence is a side-effect and must never abort a backup.
    """
    args: List[str] = build_backup_args(
        source_path, parent_snapshot_id=parent_snapshot_id, **kwargs
    )

    collector = BackupOutputCollector(password)
    stderr_output = BoundedOutput(password)
    interval: float = (
        _DEFAULT_PROGRESS_INTERVAL_SECONDS
        if progress_interval_seconds is None
        else progress_interval_seconds
    )
    # A monotonic deadline of 0 means the first line flushes immediately, so a
    # run that is slow to start still shows something before the first
    # interval elapses.
    next_flush: float = 0.0

    async def _handle_stdout_line(line: str) -> None:
        nonlocal next_flush
        collector.feed(line)
        if on_output is None:
            return
        now = time.monotonic()
        if now < next_flush:
            return
        next_flush = now + interval
        try:
            await on_output(collector.text())
        except Exception as exc:
            logger.warning(f"progress flush failed (non-fatal): {exc!r}")

    async def _handle_stderr_line(line: str) -> None:
        stderr_output.add(stderr_output.scrub(line.rstrip("\r")))

    async def _consume(proc: asyncio.subprocess.Process) -> None:
        stdout_stream = proc.stdout
        stderr_stream = proc.stderr
        assert stdout_stream is not None and stderr_stream is not None
        # Both pipes must be drained concurrently: a full stderr pipe would
        # block restic even while stdout is being consumed.
        await asyncio.gather(
            pump_stream(stdout_stream, _handle_stdout_line),
            pump_stream(stderr_stream, _handle_stderr_line),
        )
        await proc.wait()

    outcome = await restic_process.run_restic(
        args,
        env_overrides=build_restic_env_overrides(repo_path, password),
        timeout_seconds=timeout_seconds,
        run_id=run_id,
        consume=_consume,
    )

    if outcome.timed_out:
        # Keep what was collected: on a timeout the partial record is the only
        # evidence of where the run got to. The limit goes in the message
        # because this is the *only* place it is recorded — the wrapper
        # contains the deadline, so the runner's own timeout branch never
        # fires and never gets to name the job's configured hours.
        return (
            -1,
            collector.text(),
            f"backup timed out after {format_duration(timeout_seconds)}",
            None,
        )
    if outcome.error is not None:
        return (-1, "", _launch_failure_message(outcome.error), None)

    # rc=3 is restic's "partial backup completed; snapshot was created" code —
    # the summary line is present and must be used so the snapshot can be
    # recorded as a warning run.
    summary = collector.summary if outcome.returncode in (0, 3) else None

    return outcome.returncode, collector.text(), stderr_output.text(), summary
