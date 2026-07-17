import asyncio
import contextlib
import json
import os
import re
import uuid as _uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.core.logging import get_logger, log_call
from app.services import process_registry
from app.services.process_registry import _terminate_then_kill

logger = get_logger(__name__)


class ResticError(Exception):
    """Raised when a restic operation fails or times out."""

    pass


@contextlib.contextmanager
def _tracked(
    run_id: Optional[_uuid.UUID], proc: asyncio.subprocess.Process
) -> Iterator[None]:
    """Register the subprocess in the registry for the lifetime of the call.

    No-op when run_id is None — every restic command keeps backwards-compat
    callers (e.g. restic_version) working unchanged. Cleanup in the finally
    keeps the registry from leaking even when the wrapper raises.
    """
    if run_id is None:
        yield
        return
    process_registry.register(run_id, proc)
    try:
        yield
    finally:
        process_registry.unregister(run_id)


def _get_restic_env(repo_path: str, password: str) -> Dict[str, str]:
    """Build the process environment dictionary with configured repository path,
    password, and cache directory. Respects RESTIC_CACHE_DIR from host environment.
    """
    return {
        **os.environ,
        "RESTIC_REPOSITORY": repo_path,
        "RESTIC_PASSWORD": password,
        "RESTIC_CACHE_DIR": os.environ.get(
            "RESTIC_CACHE_DIR", "/app/data/restic-cache"
        ),
    }


@log_call
async def restic_version() -> Optional[str]:
    """Parse restic version. Returns None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            await _terminate_then_kill(proc)
            return None

        output = stdout.decode()
        match = re.search(r"restic\s+([0-9.]+)", output)
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
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "cat",
            "config",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "cat config timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


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
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "init",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "init timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


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
    definition this job's parent — no tag filter needed.
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "snapshots",
            "--latest",
            "1",
            "--json",
            "--no-lock",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                await _terminate_then_kill(proc)
                raise ResticError(
                    f"snapshots command timed out after {timeout_seconds} seconds"
                ) from exc
    except ResticError:
        raise
    except Exception as e:
        raise ResticError(f"failed to launch restic snapshots: {e}") from e

    if proc.returncode != 0:
        raise ResticError(
            f"snapshots command failed with exit code {proc.returncode}: "
            f"{stderr.decode()}"
        )
    try:
        snapshots = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise ResticError(f"snapshots command returned malformed JSON: {exc}") from exc
    if not isinstance(snapshots, list):
        raise ResticError(
            f"snapshots command returned non-list JSON: {type(snapshots).__name__}"
        )
    if not snapshots:
        return None
    snap_id = snapshots[0].get("id")
    if not isinstance(snap_id, str):
        raise ResticError("snapshots command returned snapshot without a string ID")
    return snap_id


@log_call
async def restic_backup(
    repo_path: str,
    password: str,
    source_path: str,
    timeout_seconds: int,
    *,
    parent_snapshot_id: Optional[str] = None,
    run_id: Optional[_uuid.UUID] = None,
    **kwargs: Any,
) -> Tuple[int, str, str, Optional[Dict[str, Any]]]:
    """Run a backup."""
    env = _get_restic_env(repo_path, password)

    # --host is pinned to a fixed string so retention isn't silently split
    # per container ID (each rebuild gets a new hostname, and `restic forget`
    # groups by host+paths by default).
    #
    # Snapshots carry no per-job identity tag: the repo at
    # /destinations/<label>/<name> belongs to exactly one job, so the repo is
    # already the scope. Retention across path changes (gaps.md C3) is handled
    # by `restic forget --group-by ''`, which collapses host and paths into a
    # single group. Any --tag values below are the user's own, from job.tags.
    args: List[str] = [
        "restic",
        "backup",
        "--host",
        "billa-gates",
    ]

    # Explicit --parent lets restic skip the full-tree rescan even when host
    # or paths have changed; without it, any source_subpath change makes the
    # next backup re-read every file from disk (gaps.md C5). Omit on the
    # genuine first run — passing a bogus --parent would fail the backup.
    if parent_snapshot_id:
        args.extend(["--parent", parent_snapshot_id])

    # Add flags from kwargs
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
    # file, which on a multi-million-file source is hundreds of MB buffered
    # through communicate() and persisted to the run row. Error lines and
    # the final summary are emitted regardless of verbosity.
    args.append("--json")

    # Add source path
    args.append(source_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "backup timed out", None)
    except Exception as e:
        return (-1, "", str(e), None)

    stdout_str: str = stdout.decode()
    stderr_str: str = stderr.decode()

    # Strip password from both streams — stderr is persisted verbatim into
    # BackupRun.error_output on failure, so it needs the same treatment.
    stdout_str = stdout_str.replace(password, "")
    stderr_str = stderr_str.replace(password, "")

    # Parse JSON summary from last line. rc=3 is restic's "partial backup
    # completed; snapshot was created" code — the summary line is present and
    # must be parsed so the snapshot can be recorded as a warning run.
    summary: Optional[Dict[str, Any]] = None
    assert proc.returncode is not None
    if proc.returncode in (0, 3):
        for line in reversed(stdout_str.split("\n")):
            if line.strip().startswith("{"):
                try:
                    parsed = json.loads(line)
                    if parsed.get("message_type") == "summary":
                        summary = parsed
                        break
                except json.JSONDecodeError:
                    pass

    return proc.returncode, stdout_str, stderr_str, summary


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
    env = _get_restic_env(repo_path, password)

    # --group-by '' puts every snapshot in the repo into one retention group,
    # so the policy applies across any historical path or host change. The
    # original --group-by paths kept old-path snapshots forever whenever a
    # job's source_subpath changed (gaps.md C3).
    #
    # Deliberately unfiltered: the repo holds exactly one job's snapshots.
    # Scoping by a per-job tag would strand every snapshot taken before the
    # current job row existed — they would never be pruned, and the repo would
    # grow without bound after a job is recreated.
    args: List[str] = [
        "restic",
        "forget",
        "--group-by",
        "",
    ]

    # Map retention_flags kwargs to CLI arguments
    flag_map: Dict[str, str] = {
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

    for kwarg_name, flag_name in flag_map.items():
        if kwarg_name in retention_flags and retention_flags[kwarg_name] is not None:
            args.extend([flag_name, str(retention_flags[kwarg_name])])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "forget/prune timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_prune(
    repo_path: str,
    password: str,
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Standalone prune (no retention flags). Returns (returncode, stdout, stderr)."""
    env = _get_restic_env(repo_path, password)

    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "prune",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "prune timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


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
    env = _get_restic_env(repo_path, password)

    args: List[str] = ["restic", "check"]

    if mode == "full":
        args.append("--read-data")
    elif mode == "subset" and subset_percent is not None:
        args.append(f"--read-data-subset={subset_percent}%")
    # mode == "structural" needs no extra args

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "check timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_unlock(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Remove stale locks.

    Called both during init-check stale-lock recovery and in Step 4.5
    auto-unlock. A hung backend during unlock previously wedged the runner;
    60s is far more than needed for a metadata-only delete on a healthy
    backend.
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "unlock",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "unlock timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()
