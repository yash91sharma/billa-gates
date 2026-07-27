"""The per-job critical section every run trigger passes through.

Four things start work against a job's repository — a scheduled tick, the Run
Now button, the Prune button and the Integrity Check button — and restic
cannot tolerate two writers against one repository. They therefore all funnel
through :func:`dispatch`, which holds the job's lock while it decides whether
a run may start and writes the row that records the decision.

Backup, prune and check used to carry a transcription of this logic each: same
lock, same overlap check, same two row shapes, ~65 lines apiece. Three copies
of a concurrency guard is three places for the guard to be subtly different,
and only one of them gets fixed when something is found.

The overlap check is deliberately double-sided, and both halves matter:

* ``active_jobs`` — the in-memory set of jobs this process is running. Covers
  the window where a pipeline is live.
* a ``status=running`` row in the database. Covers the gap between the row
  insert and the pipeline's first statement, *and* a row left open by a
  previous container that was killed mid-run — which the in-memory set knows
  nothing about after a restart.

Both are consulted under the job's ``asyncio.Lock``, which is what makes
"check, then insert" atomic per job. It is a plain asyncio lock because this is
a single-process, single-event-loop deployment; it is not a cross-process lock
and does not need to be. The lock is also the *only* thing providing that
atomicity — the check and the insert are separate sessions, and wrapping them
in one SQLite transaction would not have helped either, since a deferred
transaction takes no write lock until its first write.
"""

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from sqlalchemy import select

from app.core.logging import get_logger
from app.core.tasks import create_tracked_task
from app.db.models import (
    BackupJob,
    BackupRun,
    RunKind,
    RunReason,
    RunStatus,
    TriggeredBy,
)
from app.services import run_records
from app.services.run_records import SessionFactory

logger = get_logger(__name__)

# Jobs with a live pipeline in this process. Read by the API layer too: editing
# or deleting a job mid-run would race the pipeline, which re-reads job fields
# (paths, password, retention) between steps.
active_jobs: Set[uuid.UUID] = set()

# One lock per job, created on first use. Dropped when a job is deleted so the
# map does not grow forever.
job_locks: Dict[uuid.UUID, asyncio.Lock] = {}

# What a trigger hands over to run in the background: given the new run's id,
# return the coroutine that carries out the work.
Pipeline = Callable[[uuid.UUID], Awaitable[Any]]


async def _run_and_release(
    job_id: uuid.UUID, run_id: uuid.UUID, pipeline: Pipeline
) -> None:
    """Run the pipeline and always release the job.

    The release lives here rather than inside the pipelines so that the code
    which claimed the job is the code that gives it back — a pipeline that
    raised on its way out would otherwise leave the job permanently
    unstartable, with no error visible anywhere except the task's traceback.
    """
    try:
        await pipeline(run_id)
    finally:
        active_jobs.discard(job_id)


async def dispatch(
    factory: SessionFactory,
    job_id: uuid.UUID,
    *,
    kind: RunKind,
    triggered_by: TriggeredBy,
    pipeline: Pipeline,
    log_label: str,
) -> Optional[str]:
    """Start a run for a job, or record that it could not start.

    Returns the id of the row written — ``running`` when the pipeline was
    dispatched, ``skipped``/``overlapping_run`` when something else already
    holds the job. The skipped row is kept rather than swallowed: it documents
    the attempt, and the API layer turns it into a 409 so the UI can say a run
    is already active. Returns ``None`` only when the job does not exist.

    The pipeline itself runs as a tracked background task — the trigger returns
    as soon as the row is committed, because a backup can take hours and the
    caller is an HTTP request or a scheduler tick.
    """
    async with factory() as s:
        job: BackupJob | None = await s.get(BackupJob, str(job_id))
    if not job:
        logger.warning(f"{log_label} job_id={job_id} not_found")
        return None

    lock: asyncio.Lock = job_locks.setdefault(job_id, asyncio.Lock())

    async with lock:
        # Under the lock, the in-memory active_jobs set and any DB row with
        # status=running are equally authoritative for "is a run live?".
        async with factory() as s:
            result = await s.execute(
                select(BackupRun).where(
                    BackupRun.job_id == str(job_id),
                    BackupRun.status == RunStatus.running,
                )
            )
            running_row: BackupRun | None = result.scalars().first()

        if running_row is not None or job_id in active_jobs:
            skipped_id: str = await run_records.create_skipped(
                factory,
                str(job_id),
                kind=kind,
                triggered_by=triggered_by,
                reason=RunReason.overlapping_run,
            )
            logger.info(
                f"{log_label} job_id={job_id} run_id={skipped_id} "
                f"triggered_by={triggered_by.value} status=skipped "
                f"reason=overlapping_run"
            )
            return skipped_id

        run_id_str: str = await run_records.create_running(
            factory, str(job_id), kind=kind, triggered_by=triggered_by
        )
        active_jobs.add(job_id)
        logger.info(
            f"{log_label} job_id={job_id} run_id={run_id_str} "
            f"triggered_by={triggered_by.value} status=dispatched"
        )

    create_tracked_task(_run_and_release(job_id, uuid.UUID(run_id_str), pipeline))
    return run_id_str
