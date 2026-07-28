"""The three run pipelines: backup, prune and integrity check.

This module is the *story* of a run — which restic command is issued, in what
order, and what each exit code means. The machinery every run needs is owned
elsewhere and shared by all three pipelines, so that a fix lands once:

* :mod:`app.services.run_dispatch` — the per-job lock, the overlap check, and
  the tracked background task. All four triggers (schedule, Run Now, Prune,
  Integrity Check) go through it, which is what stops two restic processes
  from being pointed at one repository.
* :mod:`app.services.run_records` — every write to the ``BackupRun`` row, and
  the invariants a finished run must satisfy (closed out, step statuses never
  NULL, a cancel outranking whatever the interrupted step recorded).
* :mod:`app.services.run_notifications` — the one read of ``AppSettings`` a
  pipeline makes, and the pushes it gates on the operator's per-event switches.
* :mod:`app.services.run_output` — the parsing and formatting of restic's
  streams into the text the run page shows.

What stays here: the mount sentinels, the source-path and option builders (the
Job detail page's command preview is assembled from the same functions —
app/services/job_commands.py), and the pipelines themselves.
"""

import asyncio
import os
import uuid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import fs
from app.core.logging import get_logger, log_call
from app.db.database import engine
from app.db.models import (
    BackupJob,
    CheckStatus,
    PruneStatus,
    RunKind,
    RunStatus,
    TriggeredBy,
)
from app.services import (
    process_registry,
    repository,
    restic,
    run_dispatch,
    run_output,
    run_records,
    snapshot_listing,
)

# The live-run bookkeeping belongs to run_dispatch, which is the only thing
# that mutates it. Re-exported here because this module is what the API layer
# and the scheduler deal with: `backup_runner.active_jobs` and
# `run_dispatch.active_jobs` are the same objects, so a `.clear()` or a
# `.pop()` through either name is seen by both. Never rebind these names — that
# would split them into two sets and quietly disable the overlap check.
from app.services.run_dispatch import active_jobs, job_locks  # noqa: F401
from app.services.run_notifications import RunNotifier, RunSettings
from app.services.run_output import RETENTION_SKIPPED_PARTIAL_NOTE

logger = get_logger(__name__)

SOURCES_ROOT: str = "/sources"
DESTINATIONS_ROOT: str = "/destinations"

# Restic exit codes (stable contract since 0.17). Branching on these is the
# only reliable way to classify a failure — stderr message wording is not a
# contract and has changed between restic releases (gaps.md H5). Defined in
# `repository`, which also provisions repos on those same codes; re-exported
# here so the pipeline reads naturally and existing patches keep working.
RESTIC_RC_REPO_NOT_FOUND: int = repository.RESTIC_RC_REPO_NOT_FOUND
RESTIC_RC_LOCK_FAILED: int = repository.RESTIC_RC_LOCK_FAILED
RESTIC_RC_WRONG_PASSWORD: int = repository.RESTIC_RC_WRONG_PASSWORD


def _session_factory() -> async_sessionmaker[AsyncSession]:
    """The session factory the pipelines write through.

    Built per call rather than at import so `engine` is read at run time — the
    test suite patches this module's `engine` to point a whole pipeline at an
    in-memory database.
    """
    return run_records.session_factory(engine)


# ── Mount sentinels and job option builders ──────────────────────────────────


@log_call
def build_source_path(source_label: str) -> str:
    """Return the path a run actually reads: ``/sources/<label>``.

    A job backs up a whole mount, so the label is the entire address. Single
    source of truth for the backup source all the same, so the path the sentinel
    is checked at can never drift from the path handed to restic.
    """
    return os.path.join(SOURCES_ROOT, source_label)


@log_call
def check_mount_file_exists(source_label: str) -> bool:
    """Verify that the backup source contains the sentinel check file.

    Checks for .billa_gates_check at the root of the mount, which is the
    directory that is actually backed up. The sentinel is what tells a live
    drive apart from an empty mountpoint left behind by a detached one — the
    directory exists either way, and restic would turn the empty one into a
    0-file snapshot that `restic forget` then prunes the real history against.
    """
    check_file_path = os.path.join(
        build_source_path(source_label), ".billa_gates_check"
    )
    return os.path.exists(check_file_path)


@log_call
def check_destination_mount_file_exists(destination_label: str) -> bool:
    """Verify that the destination mount contains the required sentinel check file.

    Checks for .billa_gates_check at the root of the volume mount.
    """
    check_file_path = os.path.join(
        DESTINATIONS_ROOT, destination_label, ".billa_gates_check"
    )
    return os.path.exists(check_file_path)


def _destination_sentinel_error(destination_label: str) -> str:
    """The message every pipeline uses when the destination sentinel is absent.

    An empty mountpoint left behind by a detached drive passes an isdir check
    but is not the repository; restic would otherwise operate against nothing.
    """
    return (
        f"Destination mount check failed: '.billa_gates_check' file was not "
        f"found at the root of the destination mount "
        f"'/destinations/{destination_label}' (or the mount did not respond "
        f"within the probe timeout)."
    )


# The job fields that become `restic backup` / `restic forget` inputs. Kept as
# named tuples (rather than inline lists at the call sites) so the command
# preview on the Job detail page is assembled from the same field set the
# pipeline passes to restic — a field added to one and not the other would
# make the page show a backup that isn't the one that runs.
BACKUP_OPTION_FIELDS: Tuple[str, ...] = (
    "exclude_patterns",
    "exclude_caches",
    "exclude_if_present",
    "one_file_system",
    "no_scan",
    "tags",
    "compression",
    "pack_size",
    "read_concurrency",
)

RETENTION_FIELDS: Tuple[str, ...] = (
    "retain_keep_last",
    "retain_keep_hourly",
    "retain_keep_daily",
    "retain_keep_weekly",
    "retain_keep_monthly",
    "retain_keep_yearly",
    "retain_keep_within",
    "retain_keep_within_hourly",
    "retain_keep_within_daily",
    "retain_keep_within_weekly",
    "retain_keep_within_monthly",
    "retain_keep_within_yearly",
)


@log_call
def build_backup_kwargs(job: BackupJob) -> Dict[str, Any]:
    """The `restic backup` option kwargs this job produces.

    Unset (None) fields are dropped so restic is never handed an empty flag.
    """
    return {
        field: getattr(job, field)
        for field in BACKUP_OPTION_FIELDS
        if getattr(job, field) is not None
    }


@log_call
def build_retention_kwargs(job: BackupJob) -> Dict[str, Any]:
    """The `restic forget` retention kwargs this job produces.

    An empty dict means no retention is configured — the pipeline then skips
    `restic forget` entirely, since it would be a no-op.
    """
    return {
        field: getattr(job, field)
        for field in RETENTION_FIELDS
        if getattr(job, field) is not None
    }


# ── Shared pipeline steps ────────────────────────────────────────────────────


async def _finalize_if_canceled(
    factory: run_records.SessionFactory,
    notifier: RunNotifier,
    run_id: uuid.UUID,
    *,
    job_id: uuid.UUID,
    kind_label: str,
) -> bool:
    """Finalize the run as canceled if the user's Stop click has landed.

    Every gap between two restic subprocesses needs one of these. The cancel
    endpoint sets a flag and SIGTERMs whatever is running, but the flag also
    has to be noticed when it arrives *between* two commands — otherwise the
    click goes unobserved until the multi-hour backup it was meant to prevent
    has already finished, and the run is marked canceled after the fact.

    Returns True once the row has been written; the caller must then return
    immediately. Idempotent — the registry flag is cleared, so a later check in
    the same pipeline neither re-fires the push nor re-writes the row.
    """
    if not process_registry.is_canceled(run_id):
        return False

    run = await run_records.cancel(factory, run_id)
    await notifier.canceled(run.duration_seconds if run else 0, kind_label=kind_label)
    process_registry.clear_canceled(run_id)
    logger.info(f"job_id={job_id} run_id={run_id} status=canceled reason=user_canceled")
    return True


async def _fail_missing_destination_sentinel(
    factory: run_records.SessionFactory,
    notifier: RunNotifier,
    job: BackupJob,
    run_id: uuid.UUID,
    *,
    kind_label: str,
    duration_seconds: Optional[int] = None,
) -> None:
    """Finalize a run as failed because the destination sentinel is absent.

    A prune, a check and the write half of a backup all only touch the
    destination repository, so this is the "the drive is really mounted" proof
    they need before spawning restic.
    """
    error_msg = _destination_sentinel_error(job.destination_label)
    logger.error(
        f"job_id={job.id} run_id={run_id} step=verify_mount "
        f"error=destination_mount_check_failed "
        f"destination_label={job.destination_label}"
    )
    await run_records.finalize(
        factory,
        run_id,
        status=RunStatus.failed,
        error_output=error_msg,
        duration_seconds=duration_seconds,
    )
    await notifier.failed(error_msg, kind_label=kind_label)


# ── Triggers ─────────────────────────────────────────────────────────────────
#
# Thin on purpose: the critical section they share lives in run_dispatch, so
# what is left here is which pipeline to run and what to call the row. Each
# returns the id of the row written (running, or skipped/overlapping_run), or
# None if the job does not exist.


async def trigger_run(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.scheduler,
) -> Optional[str]:
    """Start a backup run. The entry point for both the scheduler and the API."""
    return await run_dispatch.dispatch(
        _session_factory(),
        job_id,
        kind=RunKind.backup,
        triggered_by=triggered_by,
        pipeline=lambda run_id: run_backup(job_id, run_id),
        log_label="trigger_run",
    )


async def trigger_prune(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.manual,
) -> Optional[str]:
    """Start a standalone `restic prune` run.

    Prune is decoupled from the backup pipeline (gaps.md H1) because it is the
    heaviest restic operation; bundling it into every backup window made
    backups unpredictably long. It shares the per-job lock with backups because
    it shares the repository.
    """
    return await run_dispatch.dispatch(
        _session_factory(),
        job_id,
        kind=RunKind.prune,
        triggered_by=triggered_by,
        pipeline=lambda run_id: run_prune(job_id, run_id),
        log_label="trigger_prune",
    )


async def trigger_check(
    job_id: uuid.UUID,
    triggered_by: TriggeredBy = TriggeredBy.manual,
    check_mode: str = "structural",
    subset_percent: Optional[int] = None,
    timeout_hours: Optional[int] = None,
) -> Optional[str]:
    """Start a standalone `restic check` verification run."""
    return await run_dispatch.dispatch(
        _session_factory(),
        job_id,
        kind=RunKind.check,
        triggered_by=triggered_by,
        pipeline=lambda run_id: run_check(
            job_id, run_id, check_mode, subset_percent, timeout_hours
        ),
        log_label="trigger_check",
    )


# ── The backup pipeline ──────────────────────────────────────────────────────


async def run_backup(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """The backup lifecycle, against a pre-created `running` BackupRun row.

    Always invoked by :func:`trigger_run`, which owns the concurrency gating;
    this function runs the pipeline and finalizes the row.
    """
    factory = _session_factory()

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} backup_started")

    try:
        # Step 1: Validate password
        logger.debug(f"job_id={job_id} run_id={run_id} step=validate_password")
        job_password: str = job.restic_password
        if not job_password:
            logger.error(
                f"job_id={job_id} run_id={run_id} step=validate_password "
                f"error=no_password_configured"
            )
            # No push: without a password this job has never had a working run,
            # so an alert here is noise about a job that was misconfigured at
            # creation, not news about one that has stopped working.
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.failed,
                error_output="No restic password configured for this job.",
                duration_seconds=0,
            )
            return

        settings: RunSettings = await RunSettings.load(factory)
        notifier: RunNotifier = settings.notifier(job.name)

        async def _abort(message: str, *, fallback: str = "Unknown error") -> None:
            """Fail the run before any snapshot could have been written.

            Duration is recorded as 0 rather than the microseconds it took to
            notice — these are pre-flight refusals, not work that was done.
            """
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.failed,
                error_output=message,
                duration_seconds=0,
            )
            await notifier.failed(message, fallback=fallback)

        async def _canceled() -> bool:
            return await _finalize_if_canceled(
                factory, notifier, run_id, job_id=job_id, kind_label="Backup"
            )

        # Step 2: Mount verification. The sentinel stat() runs through
        # fs.run_probe: on a mounted-but-hung SMB share the kernel call can
        # block for minutes, and doing that on the event loop would freeze
        # the whole app (API, scheduler, every other job). A probe timeout
        # is treated as "mount not verified" — exactly the don't-back-up-now
        # condition the sentinel exists to detect.
        logger.debug(f"job_id={job_id} run_id={run_id} step=verify_mount")
        # Resolved before the check so the path proven live below is byte-for-byte
        # the path handed to `restic backup` further down.
        source_path: str = build_source_path(job.source_label)
        if not await fs.run_probe(
            check_mount_file_exists,
            job.source_label,
            default=False,
        ):
            logger.error(
                f"job_id={job_id} run_id={run_id} step=verify_mount "
                f"error=mount_check_failed source_label={job.source_label}"
            )
            await _abort(
                f"Mount check failed: '.billa_gates_check' file was not found "
                f"at the root of the backup source '{source_path}' "
                f"(or the mount did not respond within the probe timeout)."
            )
            return

        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            await _fail_missing_destination_sentinel(
                factory,
                notifier,
                job,
                run_id,
                kind_label="Backup",
                duration_seconds=0,
            )
            return

        # Step 3: Start notification
        logger.debug(f"job_id={job_id} run_id={run_id} step=start_notification")
        await notifier.backup_started(job.source_label, job.destination_label)

        # Build repo path. `source_path` was resolved back at the mount check
        # (Step 2) — the verified path and the backed-up path are the same
        # string by construction.
        repo_path: str = repository.build_repo_path(job.destination_label, job.name)

        # Step 4: Init check
        #
        # Branch strictly on restic's documented exit codes (≥0.17): 10 = repo
        # not found, 11 = lock failed, 12 = wrong password. Stderr substring
        # matching — the previous approach — silently misclassified errors
        # whenever restic changed its message wording, and could even trigger
        # `restic init` on top of a real-but-temporarily-unreachable repo
        # because the stderr happened not to contain "wrong password"
        # (gaps.md H5). Any unrecognized non-zero rc is treated as a generic
        # failure with stderr surfaced verbatim to the user.
        logger.debug(
            f"job_id={job_id} run_id={run_id} step=init_check repo_path={repo_path}"
        )

        async def _fail_init_check(message: str, *, log_tag: str) -> None:
            logger.error(
                f"job_id={job_id} run_id={run_id} step=init_check error={log_tag}"
            )
            await _abort(message, fallback="Unknown error during init check")

        rc: int
        stderr: str
        metadata_timeout: int = settings.metadata_timeout_seconds
        rc, _, stderr = await restic.restic_cat_config(
            repo_path, job_password, metadata_timeout, run_id=run_id
        )
        if await _canceled():
            return

        # rc=11 means the repo metadata read was blocked by a stale lock. The
        # cheapest fix is to call unlock and retry once. Looping further would
        # hang the runner on a legitimately-contended repo, so we cap retries
        # at exactly one (matches the rc=11 retry policy on `restic backup`).
        if rc == RESTIC_RC_LOCK_FAILED:
            logger.warning(
                f"job_id={job_id} run_id={run_id} step=init_check "
                f"rc=11 stale_lock_suspected attempting_unlock_and_retry"
            )
            try:
                await restic.restic_unlock(
                    repo_path, job_password, metadata_timeout, run_id=run_id
                )
            except Exception as exc:
                logger.warning(
                    f"job_id={job_id} run_id={run_id} step=init_check "
                    f"unlock_exception error={exc!r}"
                )
            if await _canceled():
                return
            rc, _, stderr = await restic.restic_cat_config(
                repo_path, job_password, metadata_timeout, run_id=run_id
            )
            if await _canceled():
                return
            if rc == RESTIC_RC_LOCK_FAILED:
                await _fail_init_check(
                    f"Repository is locked and could not be unlocked: {stderr}",
                    log_tag="lock_failed",
                )
                return

        if rc == RESTIC_RC_WRONG_PASSWORD:
            await _fail_init_check(
                "Repository password is incorrect. Verify the password "
                "matches the one used when the repo was initialized.\n\n"
                f"restic stderr: {stderr}",
                log_tag="wrong_password",
            )
            return

        if rc == RESTIC_RC_REPO_NOT_FOUND:
            # A run never initializes a repository — that happens once, when
            # the job is created (app/services/repository.py::ensure_repository).
            # So rc=10 here is always a fault: the destination was swapped,
            # wiped, or renamed without moving the data. The sentinel mount
            # check cannot catch this because the mount itself is healthy.
            # Initializing would silently start an empty repo while the user
            # believes their snapshot history is intact.
            await _fail_init_check(
                f"Repository not found at '{repo_path}' (restic exit code 10). "
                f"It is created when the job is created, so it should be "
                f"present. Refusing to initialize a new, empty repository over "
                f"the job's history. If the backup disk was replaced or the "
                f"destination folder was renamed or moved, move the existing "
                f"repository directory to this path before running "
                f"again.\n\nrestic stderr: {stderr}",
                log_tag="repo_missing",
            )
            return
        elif rc != 0:
            # Generic failure: network blip, permission glitch, older restic
            # version returning rc=1 for something we'd otherwise classify.
            # Crucially we do NOT init here — that would corrupt the user's
            # mental model of "my repo wasn't found" when in fact the repo
            # is fine but temporarily unreachable.
            await _fail_init_check(
                f"Failed to access repository (restic exit code {rc}): {stderr}",
                log_tag=f"unrecognized_rc_{rc}",
            )
            return

        # Step 5: Auto-unlock — clear any stale lock left behind by an
        # abrupt termination (OOM, container kill, host reboot). Failure is
        # logged but non-fatal: a freshly initialized repo legitimately has
        # no lock to remove, and any deeper problem (missing restic binary,
        # network down) will be surfaced by the backup step that follows.
        if settings.auto_unlock:
            try:
                unlock_rc, _, unlock_err = await restic.restic_unlock(
                    repo_path, job_password, metadata_timeout, run_id=run_id
                )
                if unlock_rc == 0:
                    logger.info(
                        f"job_id={job_id} run_id={run_id} step=auto_unlock status=ok"
                    )
                else:
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} step=auto_unlock "
                        f"status=nonzero rc={unlock_rc} error={unlock_err!r}"
                    )
            except Exception as exc:
                logger.warning(
                    f"job_id={job_id} run_id={run_id} step=auto_unlock "
                    f"status=exception error={exc!r}"
                )

        if await _canceled():
            return

        # Step 6: Backup
        job_timeout_hours: int | None = job.timeout_hours
        default_timeout: int = settings.default_job_timeout_hours
        timeout_seconds: int = (job_timeout_hours or default_timeout) * 3600

        # Look up the latest snapshot for this job so restic can do an
        # incremental rescan instead of re-reading every source file. Without
        # an explicit --parent, any change to host or paths (e.g. a
        # source_label edit) makes restic treat the next backup as a fresh
        # first run (gaps.md C5). Returns None on genuine first run.
        parent_lookup_success = True
        parent_snapshot_id: Optional[str] = None
        try:
            parent_snapshot_id = await restic.restic_latest_snapshot_id(
                repo_path,
                job_password,
                timeout_seconds=metadata_timeout,
                run_id=run_id,
            )
            logger.info(
                f"job_id={job_id} run_id={run_id} step=parent_lookup "
                f"parent_snapshot_id={parent_snapshot_id}"
            )
        except restic.ResticError as exc:
            logger.error(
                f"job_id={job_id} run_id={run_id} step=parent_lookup "
                f"status=failed error={exc!r}"
            )
            await run_records.update(
                factory,
                run_id,
                status=RunStatus.failed,
                error_output=f"snapshots command failed: {exc}",
            )
            parent_lookup_success = False

        # The parent lookup is itself a restic subprocess, so Stop can land on
        # it — and terminating that lookup does nothing to prevent the backup.
        if await _canceled():
            return

        async def _persist_output_snapshot(output_text: str) -> None:
            """Write a bounded progress snapshot to the run row mid-backup.

            RunDetail polls the run row for the whole (possibly multi-hour)
            run, but until now had nothing to show: backup_output was written
            only after restic exited. `restic_backup` hands us a small snapshot
            — the newest progress line plus any errors so far — every few
            seconds, and the existing poll surfaces it. The final value is
            still written by the stats step below, which wins.
            """
            await run_records.update(factory, run_id, backup_output=output_text)

        backup_success: bool = False
        # rc=3 means restic ran but some files couldn't be read; the snapshot
        # was still saved. We treat it as success-with-warnings: the stats and
        # the snapshot are recorded, the final status is `warning` (not
        # `success`), and `restic forget` is withheld — see
        # RETENTION_SKIPPED_PARTIAL_NOTE.
        backup_warning: bool = False
        # `restic forget` *is* the retention policy — the only thing that drops
        # old snapshots. Its usual failure causes (stale lock, permissions,
        # disk full) persist across runs, so reporting such a run as `success`
        # meant the badge, the run list and the ntfy push all looked healthy
        # while the repo grew without bound until the destination filled up
        # months later. The snapshot *was* written, so the run is not `failed`
        # either — it is a `warning`.
        retention_failed: bool = False
        # Set when a partial backup withheld retention. Distinct from
        # `retention_failed` on purpose: nothing broke, the policy was held back
        # deliberately, and telling the operator "forget failed" would send them
        # hunting a stale lock that does not exist.
        retention_skipped_partial: bool = False
        # Carried out of the backup step so the completion push can say how
        # many items failed, not just that something did.
        failed_item_count: int = 0
        # Set when restic exited 0 but still reported unreadable items on
        # stderr — its scan pass swallows those (see the rc==0 branch below).
        # Separate from `backup_warning` because it must not withhold retention.
        scan_errors_found: bool = False
        scan_error_count: int = 0
        summary: Optional[Dict[str, Any]] = None
        stdout: str = ""

        if parent_lookup_success:
            logger.info(
                f"job_id={job_id} run_id={run_id} step=backup_execution "
                f"source_path={source_path} timeout_seconds={timeout_seconds}"
            )
            backup_kwargs: Dict[str, Any] = build_backup_kwargs(job)

            try:
                rc, stdout, stderr, summary = await restic.restic_backup(
                    repo_path,
                    job_password,
                    source_path,
                    timeout_seconds,
                    parent_snapshot_id=parent_snapshot_id,
                    run_id=run_id,
                    on_output=_persist_output_snapshot,
                    **backup_kwargs,
                )
                if await _canceled():
                    return
                # Exit code 11 = restic failed to acquire the repo lock. The most
                # common cause is a stale lock left by a previous abrupt
                # termination. Clear it and retry exactly once — never loop, or
                # a genuinely-contended repo would hang the runner forever.
                if rc == RESTIC_RC_LOCK_FAILED:
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} "
                        f"step=backup_execution rc=11 stale_lock_suspected "
                        f"attempting_unlock_and_retry"
                    )
                    try:
                        await restic.restic_unlock(
                            repo_path,
                            job_password,
                            metadata_timeout,
                            run_id=run_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"job_id={job_id} run_id={run_id} step=lock_retry "
                            f"unlock_exception error={exc!r}"
                        )
                    if await _canceled():
                        return
                    rc, stdout, stderr, summary = await restic.restic_backup(
                        repo_path,
                        job_password,
                        source_path,
                        timeout_seconds,
                        parent_snapshot_id=parent_snapshot_id,
                        run_id=run_id,
                        **backup_kwargs,
                    )
                    if await _canceled():
                        return

                if rc == 0:
                    backup_success = True
                    # A clean exit is not proof restic read everything. Its scan
                    # pass hands an unlistable directory to ScannerError, which
                    # prints to stderr, returns nil and leaves `error_count`
                    # alone — the process still exits 0. Those subtrees drop out
                    # of `total_files`/`total_bytes`, which is what made a 40 GB
                    # source report `43% · 72/3,086 files · 1.6 GiB/3.7 GiB`.
                    # This branch used to discard stderr entirely, so the only
                    # record of why was deleted and the run went out green.
                    scan_errors = run_output.extract_failed_items(stderr)
                    scan_error_count = len(scan_errors)
                    logger.info(
                        f"job_id={job_id} run_id={run_id} "
                        f"step=backup_execution status=success "
                        f"scan_errors={scan_error_count}"
                    )
                    if scan_errors:
                        # Deliberately *not* `backup_warning`: that flag also
                        # withholds retention (step 8), which is right for a
                        # partial snapshot and wrong here — the archiver walks
                        # the tree itself and reported no read failure, so this
                        # snapshot is complete and must count toward the policy.
                        scan_errors_found = True
                        logger.warning(
                            f"job_id={job_id} run_id={run_id} "
                            f"step=backup_execution rc=0 scan_errors_swallowed "
                            f"count={scan_error_count} first={scan_errors[:3]}"
                        )
                        await run_records.update(
                            factory,
                            run_id,
                            error_output=run_output.format_scan_errors(scan_errors),
                        )
                elif rc == 3:
                    backup_success = True
                    backup_warning = True
                    # stderr first: that is where restic puts the per-file
                    # error lines. Passing only stdout here (as this did) made
                    # every partial backup report zero failed items, so the run
                    # page named no file and the log recorded only a count.
                    failed_items = run_output.extract_failed_items(stderr, stdout)
                    failed_item_count = len(failed_items)
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} "
                        f"step=backup_execution status=warning rc=3 "
                        f"failed_items={failed_item_count} "
                        f"first_failed={failed_items[:3]}"
                    )
                    await run_records.update(
                        factory,
                        run_id,
                        error_output=run_output.format_partial_backup_error(
                            failed_items, stderr
                        ),
                    )
                else:
                    # stderr first, exactly as the rc=3 branch does. restic puts
                    # its `message_type=error` lines on **stderr**; stdout
                    # carries only status and summary. This branch passed stdout
                    # alone, so `json_errors` was empty on every failed run and
                    # the "Per-file errors" section never rendered — the same
                    # bug that made rc=3 report zero failed items, left standing
                    # in the sibling branch. Stitching both gives the operator
                    # the *which file* context a post-mortem fatal does not
                    # (gaps.md H5), and falls back gracefully when either is
                    # empty.
                    logger.error(
                        f"job_id={job_id} run_id={run_id} "
                        f"step=backup_execution status=failed rc={rc}"
                    )
                    json_errors = run_output.extract_failed_items(stderr, stdout)
                    await run_records.update(
                        factory,
                        run_id,
                        status=RunStatus.failed,
                        error_output=run_output.format_backup_error(
                            rc, json_errors, stderr
                        ),
                    )
            except asyncio.TimeoutError:
                hours: int = job_timeout_hours or default_timeout
                logger.error(
                    f"job_id={job_id} run_id={run_id} step=backup_execution "
                    f"error=timeout timeout_hours={hours}"
                )
                await run_records.update(
                    factory,
                    run_id,
                    status=RunStatus.failed,
                    error_output=f"Backup timed out after {hours} hours",
                )

        # Step 7: Record what the backup did (only if it produced a snapshot)
        if backup_success:
            stats: Dict[str, Any] = {
                "backup_output": run_output.filter_backup_output(stdout)
            }
            if summary:
                stats.update(
                    {
                        "files_new": summary.get("files_new"),
                        "files_changed": summary.get("files_changed"),
                        "files_unmodified": summary.get("files_unmodified"),
                        "dirs_new": summary.get("dirs_new"),
                        "dirs_changed": summary.get("dirs_changed"),
                        "dirs_unmodified": summary.get("dirs_unmodified"),
                        "data_added_bytes": summary.get("data_added"),
                        "data_added_packed_bytes": summary.get("data_added_packed"),
                        "total_bytes_processed": summary.get("total_bytes_processed"),
                        "snapshot_id": summary.get("snapshot_id"),
                    }
                )
            await run_records.update(factory, run_id, **stats)

            # Step 8: Retention
            #
            # When retention is configured, run `restic forget` only — never
            # `restic prune`. Prune is the heaviest restic operation (rewrites
            # every pack file) and bundling it into the backup window made
            # backups unpredictably long (gaps.md H1). Prune is now manual
            # (POST /api/jobs/{id}/prune) or scheduled separately.
            # When no retention is set, forget would be a no-op and prune
            # without forget cannot reclaim anything (no snapshots removed),
            # so we skip the whole step.
            logger.debug(f"job_id={job_id} run_id={run_id} step=forget")
            retention_kwargs: Dict[str, Any] = build_retention_kwargs(job)

            if retention_kwargs and backup_warning:
                # A partial snapshot is missing precisely the files restic could
                # not read. Counting it toward the policy deletes a complete
                # snapshot to make room for an incomplete one: with --keep-last 3
                # against a source that has started failing (bad sectors, a
                # permission change, a handle held open over SMB), three runs
                # leave nothing but partials and the last good copy of those
                # files is gone from the repository. Withholding instead trades
                # that silent, irreversible loss for repository growth, which is
                # visible — every such run is already a `warning`, and the note
                # below says so on the run page.
                retention_skipped_partial = True
                logger.warning(
                    f"job_id={job_id} run_id={run_id} step=forget "
                    f"skipped reason=partial_backup"
                )
                await run_records.update(
                    factory,
                    run_id,
                    prune_status=PruneStatus.skipped,
                    prune_error_output=RETENTION_SKIPPED_PARTIAL_NOTE,
                )
            elif retention_kwargs:
                logger.info(
                    f"job_id={job_id} run_id={run_id} step=forget applying_retention"
                )
                rc, _, forget_err = await restic.restic_forget(
                    repo_path,
                    job_password,
                    timeout_seconds,
                    run_id=run_id,
                    **retention_kwargs,
                )
                if await _canceled():
                    return

                if rc == 0:
                    await run_records.update(
                        factory, run_id, prune_status=PruneStatus.passed
                    )
                    logger.info(
                        f"job_id={job_id} run_id={run_id} step=forget status=passed"
                    )
                else:
                    await run_records.update(
                        factory,
                        run_id,
                        prune_status=PruneStatus.failed,
                        prune_error_output=forget_err,
                    )
                    retention_failed = True
                    logger.warning(
                        f"job_id={job_id} run_id={run_id} step=forget "
                        f"status=failed rc={rc}"
                    )
            else:
                logger.info(
                    f"job_id={job_id} run_id={run_id} step=forget "
                    f"skipped reason=no_retention"
                )
                await run_records.update(
                    factory, run_id, prune_status=PruneStatus.skipped
                )

            # There is no snapshot table — restic is the source of truth and the
            # snapshot listing route queries it on demand (gaps.md C4-Alt).
            # BackupRun.snapshot_id, set above from the backup summary, is
            # enough to link this run to the snapshot it produced. Invalidate
            # the listing cache so the UI sees the new snapshot immediately
            # rather than waiting out the TTL.
            snapshot_listing._clear_cache()

        # Step 9: Finalize. A run that never got as far as retention or
        # verification has those recorded as skipped by the finalizer, so no
        # column is left NULL for the UI to poll on forever.
        final_run = await run_records.finalize(
            factory,
            run_id,
            status=(
                RunStatus.warning
                if (backup_warning or retention_failed or scan_errors_found)
                else RunStatus.success
            ),
            # A step that already failed the run has the final say.
            only_if_running=True,
        )

        # Step 10: Completion notification
        if final_run:
            if final_run.status == RunStatus.success:
                await notifier.backup_succeeded(
                    final_run.duration_seconds, final_run.files_changed
                )
            elif final_run.status == RunStatus.warning:
                # Name what actually went wrong. A run can be a warning because
                # files were unreadable, because retention failed, or both —
                # a body hardcoded to one of them misinforms the operator.
                reasons: list[str] = []
                if backup_warning and failed_item_count:
                    reasons.append(
                        f"{failed_item_count} item(s) could not be read; "
                        "snapshot was still saved"
                    )
                elif backup_warning:
                    reasons.append(
                        "some files could not be read; snapshot was still saved"
                    )
                if scan_errors_found:
                    reasons.append(
                        f"{scan_error_count} item(s) could not be read while "
                        "sizing the source; the snapshot was saved, but the "
                        "size and percentage reported for this run were "
                        "computed against an under-counted source"
                    )
                if retention_failed:
                    reasons.append(
                        "retention (restic forget) failed — old snapshots were "
                        "not removed, so the repository will keep growing"
                    )
                if retention_skipped_partial:
                    reasons.append(
                        "retention was held back so an incomplete snapshot "
                        "could not push a complete one out of the policy — "
                        "the repository keeps growing until a clean backup runs"
                    )
                await notifier.backup_warned(final_run.duration_seconds, reasons)
            elif final_run.status == RunStatus.failed:
                await notifier.failed(final_run.error_output)

    except Exception as exc:
        # Top-level safety net: if any unhandled exception bubbles out of the
        # pipeline above (SQLite OperationalError, network failure, subprocess
        # crash, unforeseen edge case), the run row would otherwise stay at
        # status=running forever — and the overlap check would then skip every
        # future trigger, manual or scheduled, until the operator edits the DB
        # by hand or restarts the container.
        logger.exception(
            f"job_id={job_id} run_id={run_id} backup_runner_crashed error={exc!r}"
        )
        await run_records.crash(
            factory, run_id, error_output=run_records.crash_message("Backup", exc)
        )
    finally:
        # active_jobs cleanup is handled by run_dispatch so the lifecycle owner
        # holds full responsibility for the in-memory state. The pipeline
        # focuses on the backup itself + the history trim.
        await run_records.trim_history(factory, str(job_id))
        logger.info(f"job_id={job_id} run_id={run_id} backup_completed")


# ── The prune pipeline ───────────────────────────────────────────────────────


async def run_prune(job_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Execute `restic prune` for a job's repo and finalize the run row.

    Prune is the heaviest restic operation: it scans every pack file in the
    repo, rewrites packs to drop unreferenced blobs, and updates the index.
    It is invoked here as a standalone step — never from the backup
    pipeline (gaps.md H1).

    Concurrency gating lives in :func:`trigger_prune`; this function runs prune
    and updates the row. Failures land in prune_error_output; the run status
    reflects the prune outcome (success / failed).
    """
    factory = _session_factory()

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"run_prune job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} prune_started")

    try:
        settings: RunSettings = await RunSettings.load(factory)
        notifier: RunNotifier = settings.notifier(job.name)

        # Per-job timeout_hours applies the same to prune as it does to
        # backup — operators tune one knob, not two. The pipeline still has
        # the 1-hr-fallback safety net via _terminate_then_kill inside the
        # restic wrapper.
        prune_timeout: int = (
            job.timeout_hours or settings.default_job_timeout_hours
        ) * 3600
        repo_path: str = repository.build_repo_path(job.destination_label, job.name)

        # A cancel that lands before the subprocess spawns must stop the
        # (possibly hours-long) prune from starting at all.
        if await _finalize_if_canceled(
            factory, notifier, run_id, job_id=job_id, kind_label="Prune"
        ):
            return

        # Probed on a worker thread so a hung mount can't freeze the event loop.
        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            await _fail_missing_destination_sentinel(
                factory, notifier, job, run_id, kind_label="Prune"
            )
            return

        rc, _, prune_err = await restic.restic_prune(
            repo_path, job.restic_password, prune_timeout, run_id=run_id
        )
        if await _finalize_if_canceled(
            factory, notifier, run_id, job_id=job_id, kind_label="Prune"
        ):
            return

        if rc == 0:
            logger.info(f"job_id={job_id} run_id={run_id} step=prune status=passed")
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.success,
                prune_status=PruneStatus.passed,
                # Prune runs don't drive a check; keep that column tidy so the
                # UI's "check_status missing" polling hook doesn't wait forever
                # on rows that will never have one.
                check_status=CheckStatus.skipped,
            )
        else:
            logger.warning(
                f"job_id={job_id} run_id={run_id} step=prune status=failed rc={rc}"
            )
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.failed,
                prune_status=PruneStatus.failed,
                prune_error_output=prune_err,
                check_status=CheckStatus.skipped,
            )

    except Exception as exc:
        logger.exception(
            f"job_id={job_id} run_id={run_id} prune_runner_crashed error={exc!r}"
        )
        await run_records.crash(
            factory,
            run_id,
            prune_status=PruneStatus.failed,
            prune_error_output=run_records.crash_message("Prune", exc),
            check_status=CheckStatus.skipped,
        )
    finally:
        await run_records.trim_history(factory, str(job_id))
        logger.info(f"job_id={job_id} run_id={run_id} prune_completed")


# ── The integrity-check pipeline ─────────────────────────────────────────────


async def run_check(
    job_id: uuid.UUID,
    run_id: uuid.UUID,
    check_mode: str,
    subset_percent: Optional[int],
    timeout_hours: Optional[int],
) -> None:
    """Execute `restic check` for a job's repo and finalize the run row."""
    factory = _session_factory()

    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))

    if not job:
        logger.warning(f"run_check job_id={job_id} not found in database")
        return

    logger.info(f"job_id={job_id} run_id={run_id} check_started mode={check_mode}")

    try:
        settings: RunSettings = await RunSettings.load(factory)
        notifier: RunNotifier = settings.notifier(job.name)

        hours: int = (
            timeout_hours
            or job.check_timeout_hours
            or settings.default_job_timeout_hours
        )
        check_timeout: int = hours * 3600
        repo_path: str = repository.build_repo_path(job.destination_label, job.name)

        # A cancel that lands before the subprocess spawns must stop the
        # check (and its start notification) from firing at all.
        if await _finalize_if_canceled(
            factory, notifier, run_id, job_id=job_id, kind_label="Verification"
        ):
            return

        # Verified before any "started" notification: an empty mountpoint left
        # by a detached backup drive passes an isdir check but is not the repo.
        if not await fs.run_probe(
            check_destination_mount_file_exists, job.destination_label, default=False
        ):
            await _fail_missing_destination_sentinel(
                factory, notifier, job, run_id, kind_label="Verification"
            )
            return

        await notifier.verification_started(check_mode)

        rc, _, check_err = await restic.restic_check(
            repo_path,
            job.restic_password,
            check_mode,
            subset_percent,
            check_timeout,
            run_id=run_id,
        )
        if await _finalize_if_canceled(
            factory, notifier, run_id, job_id=job_id, kind_label="Verification"
        ):
            return

        if rc == 0:
            logger.info(
                f"job_id={job_id} run_id={run_id} step=integrity_check status=passed"
            )
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.success,
                check_status=CheckStatus.passed,
                prune_status=PruneStatus.skipped,
            )
        else:
            logger.warning(
                f"job_id={job_id} run_id={run_id} step=integrity_check "
                f"status=failed rc={rc}"
            )
            await run_records.finalize(
                factory,
                run_id,
                status=RunStatus.failed,
                check_status=CheckStatus.failed,
                check_error_output=check_err,
                prune_status=PruneStatus.skipped,
            )

        await notifier.verification_finished(passed=rc == 0)

    except Exception as exc:
        logger.exception(
            f"job_id={job_id} run_id={run_id} check_runner_crashed error={exc!r}"
        )
        await run_records.crash(
            factory,
            run_id,
            check_status=CheckStatus.failed,
            check_error_output=run_records.crash_message("Check", exc),
            prune_status=PruneStatus.skipped,
        )
    finally:
        await run_records.trim_history(factory, str(job_id))
        logger.info(f"job_id={job_id} run_id={run_id} check_completed")
