"""Tests for app/services/run_dispatch.py — the per-job concurrency guard.

This is the critical section every trigger goes through: a scheduled tick, a
manual Run Now click, a Prune click and an Integrity Check click all land here,
and restic cannot tolerate two writers against one repository. Backup, prune
and check used to carry a copy of this logic each — three transcriptions of the
same lock, overlap check and row insert — so the guard is tested once here and
inherited by all three.

The overlap check is deliberately double-sided: the in-memory `active_jobs` set
covers runs this process dispatched, and a `status=running` DB row covers both
the window between the row insert and the pipeline actually starting, and a row
left open by a previous container that died mid-run.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    BackupJob,
    BackupRun,
    CheckStatus,
    PruneStatus,
    RunKind,
    RunReason,
    RunStatus,
    ScheduleType,
    TriggeredBy,
)
from app.services import run_dispatch

JOB_ID = uuid.uuid4()


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _make_job(factory) -> None:
    async with factory() as s:
        s.add(
            BackupJob(
                id=str(JOB_ID),
                name="Test Job",
                source_label="documents",
                destination_label="main",
                restic_password="s3cret",
                schedule_type=ScheduleType.interval,
                schedule_value="6h",
                enabled=True,
            )
        )
        await s.commit()


async def _rows(factory) -> list[BackupRun]:
    async with factory() as s:
        result = await s.execute(
            select(BackupRun).where(BackupRun.job_id == str(JOB_ID))
        )
        return list(result.scalars().all())


async def _dispatch(factory, pipeline, *, kind=RunKind.backup, **overrides):
    return await run_dispatch.dispatch(
        factory,
        JOB_ID,
        kind=kind,
        triggered_by=overrides.pop("triggered_by", TriggeredBy.manual),
        pipeline=pipeline,
        log_label=overrides.pop("log_label", "trigger_run"),
    )


async def _settle() -> None:
    """Let a dispatched task run its body and its finally."""
    for _ in range(5):
        await asyncio.sleep(0)


# ── The happy path ────────────────────────────────────────────────────────────


async def test_dispatch_creates_a_running_row_and_starts_the_pipeline(factory):
    await _make_job(factory)
    started = asyncio.Event()
    seen: dict = {}

    async def pipeline(run_id):
        seen["run_id"] = run_id
        started.set()

    run_id = await _dispatch(factory, pipeline)
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await _settle()

    rows = await _rows(factory)
    assert len(rows) == 1
    assert rows[0].id == run_id
    assert rows[0].status == RunStatus.running
    assert seen["run_id"] == uuid.UUID(run_id)


async def test_dispatch_tags_the_row_with_the_kind_and_trigger(factory):
    await _make_job(factory)
    await _make_job_run_noop(factory, kind=RunKind.check)

    rows = await _rows(factory)
    assert rows[0].kind == RunKind.check
    assert rows[0].triggered_by == TriggeredBy.scheduler


async def _make_job_run_noop(factory, **overrides):
    async def pipeline(run_id):
        return None

    run_id = await _dispatch(
        factory, pipeline, triggered_by=TriggeredBy.scheduler, **overrides
    )
    await _settle()
    return run_id


async def test_dispatch_releases_the_job_when_the_pipeline_finishes(factory):
    await _make_job(factory)
    await _make_job_run_noop(factory)
    assert JOB_ID not in run_dispatch.active_jobs


async def test_dispatch_releases_the_job_when_the_pipeline_raises(factory):
    """A pipeline that dies without releasing the job would lock it out of
    every future trigger for the lifetime of the container."""
    await _make_job(factory)

    async def exploding(run_id):
        raise RuntimeError("pipeline died")

    await _dispatch(factory, exploding)
    await _settle()

    assert JOB_ID not in run_dispatch.active_jobs


async def test_dispatch_holds_the_job_while_the_pipeline_runs(factory):
    await _make_job(factory)
    release = asyncio.Event()

    async def slow(run_id):
        await release.wait()

    await _dispatch(factory, slow)
    await _settle()
    assert JOB_ID in run_dispatch.active_jobs

    release.set()
    await _settle()
    assert JOB_ID not in run_dispatch.active_jobs


# ── Overlap ───────────────────────────────────────────────────────────────────


async def test_unknown_job_is_a_noop(factory):
    async def pipeline(run_id):
        raise AssertionError("must not run")

    result = await run_dispatch.dispatch(
        factory,
        uuid.uuid4(),
        kind=RunKind.backup,
        triggered_by=TriggeredBy.scheduler,
        pipeline=pipeline,
        log_label="trigger_run",
    )

    assert result is None
    assert await _rows(factory) == []


async def test_overlap_via_the_in_memory_set_records_a_skipped_row(factory):
    await _make_job(factory)
    run_dispatch.active_jobs.add(JOB_ID)

    async def pipeline(run_id):
        raise AssertionError("must not run")

    try:
        run_id = await _dispatch(factory, pipeline)
    finally:
        run_dispatch.active_jobs.discard(JOB_ID)

    (row,) = await _rows(factory)
    assert row.id == run_id
    assert row.status == RunStatus.skipped
    assert row.reason == RunReason.overlapping_run
    assert row.prune_status == PruneStatus.skipped
    assert row.check_status == CheckStatus.skipped


async def test_overlap_via_a_running_db_row_records_a_skipped_row(factory):
    """Covers a row left behind by a process that died before its cleanup ran —
    the in-memory set knows nothing about it."""
    await _make_job(factory)
    async with factory() as s:
        s.add(
            BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(JOB_ID),
                status=RunStatus.running,
                triggered_by=TriggeredBy.scheduler,
                started_at=__import__("datetime").datetime.now(),
            )
        )
        await s.commit()

    async def pipeline(run_id):
        raise AssertionError("must not run")

    run_id = await _dispatch(factory, pipeline)

    async with factory() as s:
        row = await s.get(BackupRun, run_id)
    assert row is not None
    assert row.status == RunStatus.skipped
    assert row.reason == RunReason.overlapping_run


async def test_the_skipped_row_keeps_the_kind_of_the_attempt(factory):
    """The audit row says what the operator tried to start, not what is already
    running — a prune refused during a backup is a prune row."""
    await _make_job(factory)
    run_dispatch.active_jobs.add(JOB_ID)

    async def pipeline(run_id):
        raise AssertionError("must not run")

    try:
        run_id = await _dispatch(factory, pipeline, kind=RunKind.prune)
    finally:
        run_dispatch.active_jobs.discard(JOB_ID)

    async with factory() as s:
        row = await s.get(BackupRun, run_id)
    assert row is not None
    assert row.kind == RunKind.prune


async def test_a_live_run_blocks_every_other_kind(factory):
    """Backup, prune and check share one lock because they share one repository
    — restic cannot tolerate two writers, whatever they are doing."""
    await _make_job(factory)
    release = asyncio.Event()

    async def slow(run_id):
        await release.wait()

    async def must_not_run(run_id):
        raise AssertionError("must not run")

    await _dispatch(factory, slow, kind=RunKind.backup)
    await _settle()

    prune_id = await _dispatch(factory, must_not_run, kind=RunKind.prune)
    check_id = await _dispatch(factory, must_not_run, kind=RunKind.check)

    async with factory() as s:
        prune_row = await s.get(BackupRun, prune_id)
        check_row = await s.get(BackupRun, check_id)
    assert prune_row is not None and prune_row.status == RunStatus.skipped
    assert check_row is not None and check_row.status == RunStatus.skipped

    release.set()
    await _settle()


async def test_parallel_dispatches_leave_exactly_one_run_running(factory):
    """The race this closes: a scheduled tick and a manual click arriving in the
    same event-loop turn both passing the overlap check and spawning two restic
    processes against one repository."""
    await _make_job(factory)
    release = asyncio.Event()

    async def slow(run_id):
        await release.wait()

    run_ids = await asyncio.gather(*[_dispatch(factory, slow) for _ in range(10)])
    await _settle()

    rows = await _rows(factory)
    assert len([r for r in rows if r.status == RunStatus.running]) == 1
    assert len([r for r in rows if r.status == RunStatus.skipped]) == 9
    assert len(set(run_ids)) == 10, "every caller gets its own audit row back"

    release.set()
    await _settle()


async def test_a_second_run_is_allowed_once_the_first_finishes(factory):
    """The guard must not latch: a job that has finished a run has to be
    startable again."""
    await _make_job(factory)
    await _make_job_run_noop(factory)

    async def pipeline(run_id):
        return None

    # The first run's row is still status=running in the DB unless the pipeline
    # closed it — that is the pipeline's job, so close it here.
    async with factory() as s:
        for row in (await s.execute(select(BackupRun))).scalars().all():
            row.status = RunStatus.success
        await s.commit()

    second = await _dispatch(factory, pipeline)
    await _settle()

    async with factory() as s:
        row = await s.get(BackupRun, second)
    assert row is not None
    assert row.status == RunStatus.running


# ── Task tracking ─────────────────────────────────────────────────────────────


async def test_the_pipeline_task_is_tracked(factory):
    """An untracked create_task can be garbage-collected mid-run: the pipeline
    dies silently, no finally runs, and the row is stranded at running."""
    await _make_job(factory)
    release = asyncio.Event()

    async def slow(run_id):
        await release.wait()

    with pytest.MonkeyPatch.context() as mp:
        seen: list = []
        real = run_dispatch.create_tracked_task

        def spy(coro):
            task = real(coro)
            seen.append(task)
            return task

        mp.setattr(run_dispatch, "create_tracked_task", spy)
        await _dispatch(factory, slow)
        await _settle()

    assert len(seen) == 1
    release.set()
    await _settle()
