"""The BackupRun row state machine — every write to a run record.

A run row is the only durable trace of what a pipeline did, and it is also a
lock: :func:`app.services.run_dispatch.dispatch` treats any ``status=running``
row as "a run is live for this job" and refuses to start another. A row left
open by a crash therefore locks the job out of *every* future trigger, manual
and scheduled, until someone edits the database by hand. That is why the
closing writes live here rather than being spelled out again in each pipeline:
the backup, prune and check pipelines each used to carry their own copy of
"set finished_at, work out the duration, default the step statuses", and the
two cancel paths had already drifted apart by the time they were merged.

Three invariants hold for every finished run, whatever ended it:

* ``finished_at`` and ``duration_seconds`` are set — the run list and detail
  page show both, and a run with neither reads as still going;
* ``prune_status`` and ``check_status`` are never left NULL — the frontend
  polls a run while ``check_status`` is null (verification still in flight), so
  a NULL on a finished run is a page that refreshes forever;
* the user's Stop click outranks whatever the interrupted step recorded
  (SIGTERM makes restic exit non-zero, so a canceled run has usually written a
  failure by the time :func:`cancel` runs).

Everything takes an ``async_sessionmaker`` rather than reaching for the global
engine, so the caller decides which database it is writing to — which is what
lets the test suite point the whole pipeline at an in-memory SQLite.
"""

import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.logging import get_logger, log_call
from app.db.models import (
    AppSettings,
    BackupRun,
    CheckStatus,
    PruneStatus,
    RunKind,
    RunReason,
    RunStatus,
    TriggeredBy,
)

logger = get_logger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]

# Rows kept per job when no AppSettings row exists yet. Startup seeds settings,
# but a run triggered before that must not trim history against a limit of 0.
DEFAULT_KEEP_LAST_RUNS: int = 100


def session_factory(engine: AsyncEngine | Engine | Any) -> SessionFactory:
    """The session factory every run pipeline uses.

    ``expire_on_commit=False`` is load-bearing, not a preference: the pipelines
    read the finalized row (status, duration, error text) *after* the session
    has closed, to decide which completion push to send. With the default,
    that read would try to refresh the instance on a closed session and raise.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def _duration_since(started_at: datetime, now: datetime) -> int:
    """Whole seconds between a stored ``started_at`` and an aware ``now``.

    ``started_at`` is stored naive (the column is ``DateTime`` without a
    timezone) and holds UTC, so the aware end of the subtraction is stripped
    rather than the naive end being localized.
    """
    return int((now.replace(tzinfo=None) - started_at).total_seconds())


def _fill_skipped_steps(run: BackupRun) -> None:
    """Default the per-step statuses of a finished run.

    Only fills what is still unset: a run that really did apply retention or
    really did verify must keep the outcome it recorded.
    """
    if not run.prune_status:
        run.prune_status = PruneStatus.skipped
    if not run.check_status:
        run.check_status = CheckStatus.skipped


@log_call
async def create_running(
    factory: SessionFactory,
    job_id: str,
    *,
    kind: RunKind,
    triggered_by: TriggeredBy,
) -> str:
    """Insert an open-ended ``status=running`` row and return its id.

    Left deliberately open: the step statuses are filled in as the pipeline
    reaches them, and the row is closed by one of the finalizers below.
    """
    run = BackupRun(
        id=str(uuid.uuid4()),
        job_id=job_id,
        kind=kind,
        status=RunStatus.running,
        started_at=datetime.now(timezone.utc),
        triggered_by=triggered_by,
    )
    async with factory() as s:
        s.add(run)
        await s.commit()
    return run.id


@log_call
async def create_skipped(
    factory: SessionFactory,
    job_id: str,
    *,
    kind: RunKind,
    triggered_by: TriggeredBy,
    reason: RunReason,
) -> str:
    """Insert a terminal ``status=skipped`` audit row and return its id.

    Born finished: it records an attempt that never ran, so no pipeline will
    come back to close it. Leaving ``check_status`` NULL here would make the UI
    poll the row forever waiting for a verification that was never started.
    """
    now = datetime.now(timezone.utc)
    run = BackupRun(
        id=str(uuid.uuid4()),
        job_id=job_id,
        kind=kind,
        status=RunStatus.skipped,
        reason=reason,
        started_at=now,
        finished_at=now,
        triggered_by=triggered_by,
        prune_status=PruneStatus.skipped,
        check_status=CheckStatus.skipped,
    )
    async with factory() as s:
        s.add(run)
        await s.commit()
    return run.id


async def update(
    factory: SessionFactory, run_id: uuid.UUID | str, **columns: Any
) -> Optional[BackupRun]:
    """Write columns to a run row mid-pipeline, leaving it open.

    For everything a step records as it goes: a progress snapshot, the stats
    parsed out of the backup summary, one step's outcome. A missing row is not
    an error — history trimming can delete one under a long-running pipeline,
    and losing a progress write must not take the run down with it.

    Deliberately not decorated with ``@log_call``: the progress sink calls this
    every few seconds for the whole length of a backup, and the payload is the
    retained output — up to 256 KiB that the decorator would repr on every
    call at DEBUG.
    """
    async with factory() as s:
        run: BackupRun | None = await s.get(BackupRun, str(run_id))
        if run is None:
            logger.warning(f"run_id={run_id} update_skipped reason=row_not_found")
            return None
        for column, value in columns.items():
            setattr(run, column, value)
        await s.commit()
        return run


@log_call
async def finalize(
    factory: SessionFactory,
    run_id: uuid.UUID | str,
    *,
    status: Optional[RunStatus] = None,
    only_if_running: bool = False,
    duration_seconds: Optional[int] = None,
    **columns: Any,
) -> Optional[BackupRun]:
    """Close a run row out and return it (detached, still readable).

    ``only_if_running`` is how a pipeline proposes an ending without
    overruling one already recorded: the backup pipeline decides
    success-or-warning at the end, but a step that already failed the run has
    the final say. The row is closed either way — the pipeline is over.

    ``duration_seconds`` is measured from ``started_at`` unless given. The
    pre-flight failures (no password, missing sentinel, unreachable repository)
    pass 0 rather than recording the microseconds it took to notice.
    """
    now: datetime = datetime.now(timezone.utc)
    async with factory() as s:
        run: BackupRun | None = await s.get(BackupRun, str(run_id))
        if run is None:
            logger.warning(f"run_id={run_id} finalize_skipped reason=row_not_found")
            return None

        for column, value in columns.items():
            setattr(run, column, value)

        if status is not None and (
            not only_if_running or run.status == RunStatus.running
        ):
            run.status = status

        run.finished_at = now
        run.duration_seconds = (
            duration_seconds
            if duration_seconds is not None
            else _duration_since(run.started_at, now)
        )
        _fill_skipped_steps(run)
        await s.commit()
        return run


@log_call
async def cancel(
    factory: SessionFactory, run_id: uuid.UUID | str
) -> Optional[BackupRun]:
    """Finalize a run as ``canceled/user_canceled``.

    Unconditional on the current status on purpose. Stop SIGTERMs the restic
    subprocess, which then exits non-zero, so the step that was interrupted has
    usually marked the run failed already — but the user asked for this, and
    reporting their own click back to them as a fault is the one outcome that
    is certainly wrong.

    Any diagnostic the pipeline already captured is kept; the placeholder is
    only for a row that has nothing to show.
    """
    now: datetime = datetime.now(timezone.utc)
    async with factory() as s:
        run: BackupRun | None = await s.get(BackupRun, str(run_id))
        if run is None:
            logger.warning(f"run_id={run_id} cancel_skipped reason=row_not_found")
            return None

        run.status = RunStatus.canceled
        run.reason = RunReason.user_canceled
        run.finished_at = now
        run.duration_seconds = _duration_since(run.started_at, now)
        _fill_skipped_steps(run)
        if not run.error_output:
            run.error_output = "Canceled by user."
        await s.commit()
        return run


def crash_message(label: str, exc: BaseException) -> str:
    """The text a crashed pipeline records. Call from inside the ``except``
    block — the traceback is read from the exception currently being handled,
    and it is the only thing that says which step died."""
    return f"{label} runner crashed: {exc!r}\n\n{traceback.format_exc()}"


@log_call
async def crash(
    factory: SessionFactory, run_id: uuid.UUID | str, **columns: Any
) -> None:
    """Finalize a row stranded at ``running`` by an unhandled exception.

    The last line of defence against the lock-up described at the top of this
    module. Only touches a row that is still running: a pipeline can raise on
    its way out *after* finalizing, and that run's recorded outcome is real.

    The write is itself wrapped — if the database is what broke there is no
    recourse, but raising here would take down the task wrapper that still has
    to release the job from ``active_jobs``.
    """
    try:
        now: datetime = datetime.now(timezone.utc)
        async with factory() as s:
            run: BackupRun | None = await s.get(BackupRun, str(run_id))
            if run is None or run.status != RunStatus.running:
                return
            for column, value in columns.items():
                setattr(run, column, value)
            run.status = RunStatus.failed
            run.finished_at = now
            run.duration_seconds = _duration_since(run.started_at, now)
            _fill_skipped_steps(run)
            await s.commit()
    except Exception as recovery_exc:
        logger.error(f"run_id={run_id} crash_recovery_failed error={recovery_exc!r}")


@log_call
async def trim_history(factory: SessionFactory, job_id: str) -> None:
    """Delete the oldest backup_runs rows beyond AppSettings.keep_last_runs.

    Snapshots in the restic repo are untouched — this only bounds the row
    count in the `backup_runs` table so it doesn't grow forever.
    """
    async with factory() as s:
        settings: AppSettings | None = await s.get(AppSettings, 1)
        keep_n: int = settings.keep_last_runs if settings else DEFAULT_KEEP_LAST_RUNS

        ids_newest_first = (
            (
                await s.execute(
                    select(BackupRun.id)
                    .where(BackupRun.job_id == job_id)
                    .order_by(BackupRun.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if len(ids_newest_first) <= keep_n:
            return
        excess = ids_newest_first[keep_n:]
        for old_id in excess:
            old = await s.get(BackupRun, old_id)
            if old is not None:
                await s.delete(old)
        await s.commit()
        logger.info(f"job_id={job_id} trimmed_runs deleted={len(excess)} kept={keep_n}")
