"""Tests for app/services/run_records.py — the BackupRun row state machine.

Every write to a run row goes through this module, so the invariants it holds
are the ones the whole app depends on:

* a run that stops for any reason gets ``finished_at`` and
  ``duration_seconds`` — the UI shows both, and a row left at
  ``status=running`` locks the job out of every future trigger;
* ``prune_status`` / ``check_status`` are never left NULL on a finished run —
  the frontend polls while ``check_status`` is null, so a NULL there means a
  run page that refreshes forever;
* the user's Stop click outranks any intermediate status the pipeline wrote.

These used to be re-implemented once per pipeline (backup, prune, check), which
is how the two cancel paths drifted apart. Test them here once, and the three
pipelines inherit the behavior.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    AppSettings,
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
from app.services import run_records

JOB_ID = str(uuid.uuid4())


@pytest.fixture
def factory(engine):
    return run_records.session_factory(engine)


async def _make_job(factory) -> None:
    async with factory() as s:
        s.add(
            BackupJob(
                id=JOB_ID,
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


async def _make_run(factory, **overrides) -> str:
    """Insert a running row started `started_ago` seconds in the past."""
    started_ago: int = overrides.pop("started_ago", 0)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=JOB_ID,
                status=overrides.pop("status", RunStatus.running),
                triggered_by=overrides.pop("triggered_by", TriggeredBy.manual),
                started_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(seconds=started_ago),
                **overrides,
            )
        )
        await s.commit()
    return run_id


async def _get(factory, run_id: str) -> BackupRun:
    async with factory() as s:
        row = await s.get(BackupRun, run_id)
    assert row is not None
    return row


# ── session_factory ───────────────────────────────────────────────────────────


async def test_session_factory_keeps_attributes_readable_after_commit(engine, factory):
    """expire_on_commit=False is not a preference: the pipelines read the
    finalized row (status, duration, error text) *after* the session closes to
    build the completion push. With the default the read would re-query on a
    closed session and raise."""
    await _make_job(factory)
    run_id = await _make_run(factory)

    run = await run_records.finalize(
        factory, uuid.UUID(run_id), status=RunStatus.success
    )

    assert run is not None
    assert run.status == RunStatus.success
    assert run.duration_seconds is not None


# ── create_running / create_skipped ──────────────────────────────────────────


async def test_create_running_row_is_open_ended(factory):
    await _make_job(factory)

    run_id = await run_records.create_running(
        factory, JOB_ID, kind=RunKind.backup, triggered_by=TriggeredBy.scheduler
    )

    run = await _get(factory, run_id)
    assert run.status == RunStatus.running
    assert run.kind == RunKind.backup
    assert run.triggered_by == TriggeredBy.scheduler
    assert run.finished_at is None
    assert run.prune_status is None
    assert run.check_status is None


@pytest.mark.parametrize("kind", (RunKind.backup, RunKind.prune, RunKind.check))
async def test_create_skipped_row_is_born_finished(factory, kind):
    """A skipped row is an audit record of an attempt that never ran, so it is
    terminal the moment it is written — no pipeline will come back to close it,
    and a NULL check_status would make the UI poll it forever."""
    await _make_job(factory)

    run_id = await run_records.create_skipped(
        factory,
        JOB_ID,
        kind=kind,
        triggered_by=TriggeredBy.manual,
        reason=RunReason.overlapping_run,
    )

    run = await _get(factory, run_id)
    assert run.kind == kind
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run
    assert run.finished_at is not None
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


# ── update ────────────────────────────────────────────────────────────────────


async def test_update_writes_columns_without_finishing_the_run(factory):
    """Mid-pipeline writes (progress snapshots, stats, per-step outcomes) must
    not look like the run ended."""
    await _make_job(factory)
    run_id = await _make_run(factory)

    await run_records.update(
        factory, uuid.UUID(run_id), backup_output="progress: 42%", files_new=7
    )

    run = await _get(factory, run_id)
    assert run.backup_output == "progress: 42%"
    assert run.files_new == 7
    assert run.status == RunStatus.running
    assert run.finished_at is None


async def test_update_on_a_deleted_row_is_a_noop(factory):
    """History trimming can delete a row under a still-running pipeline; a
    progress write must not take the run down with it."""
    await _make_job(factory)
    await run_records.update(factory, uuid.uuid4(), backup_output="x")


# ── finalize ──────────────────────────────────────────────────────────────────


async def test_finalize_records_the_duration_from_started_at(factory):
    await _make_job(factory)
    run_id = await _make_run(factory, started_ago=90)

    run = await run_records.finalize(
        factory, uuid.UUID(run_id), status=RunStatus.success
    )

    assert run is not None
    assert run.finished_at is not None
    assert 89 <= (run.duration_seconds or 0) <= 95


async def test_finalize_accepts_an_explicit_duration(factory):
    """The pre-flight failures (no password, missing sentinel, unreachable repo)
    record 0 rather than the microseconds it took to notice."""
    await _make_job(factory)
    run_id = await _make_run(factory, started_ago=90)

    run = await run_records.finalize(
        factory, uuid.UUID(run_id), status=RunStatus.failed, duration_seconds=0
    )

    assert run is not None
    assert run.duration_seconds == 0


async def test_finalize_fills_unset_step_statuses_with_skipped(factory):
    """NULL check_status means "still verifying" to the frontend poller."""
    await _make_job(factory)
    run_id = await _make_run(factory)

    await run_records.finalize(factory, uuid.UUID(run_id), status=RunStatus.failed)

    run = await _get(factory, run_id)
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_finalize_does_not_overwrite_a_step_status_already_recorded(factory):
    await _make_job(factory)
    run_id = await _make_run(factory, prune_status=PruneStatus.passed)

    await run_records.finalize(factory, uuid.UUID(run_id), status=RunStatus.success)

    run = await _get(factory, run_id)
    assert run.prune_status == PruneStatus.passed


async def test_finalize_only_if_running_keeps_an_earlier_terminal_status(factory):
    """The backup pipeline decides success/warning at the end, but a step that
    already failed the run has the final say — otherwise a failed backup would
    be finalized as a success."""
    await _make_job(factory)
    run_id = await _make_run(factory, status=RunStatus.failed)

    run = await run_records.finalize(
        factory, uuid.UUID(run_id), status=RunStatus.success, only_if_running=True
    )

    assert run is not None
    assert run.status == RunStatus.failed
    # It is still closed out: the pipeline is over either way.
    assert run.finished_at is not None
    assert run.duration_seconds is not None


async def test_finalize_writes_arbitrary_outcome_columns(factory):
    await _make_job(factory)
    run_id = await _make_run(factory)

    await run_records.finalize(
        factory,
        uuid.UUID(run_id),
        status=RunStatus.failed,
        prune_status=PruneStatus.failed,
        prune_error_output="restic said no",
    )

    run = await _get(factory, run_id)
    assert run.prune_status == PruneStatus.failed
    assert run.prune_error_output == "restic said no"


async def test_finalize_on_a_deleted_row_returns_none(factory):
    await _make_job(factory)
    assert await run_records.finalize(factory, uuid.uuid4()) is None


# ── cancel ────────────────────────────────────────────────────────────────────


async def test_cancel_finalizes_as_user_canceled(factory):
    await _make_job(factory)
    run_id = await _make_run(factory, started_ago=12)

    run = await run_records.cancel(factory, uuid.UUID(run_id))

    assert run is not None
    assert run.status == RunStatus.canceled
    assert run.reason == RunReason.user_canceled
    assert run.finished_at is not None
    assert 11 <= (run.duration_seconds or 0) <= 17
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped
    assert run.error_output == "Canceled by user."


async def test_cancel_outranks_a_failure_written_by_the_terminated_process(factory):
    """SIGTERM makes restic exit non-zero, so the step that was interrupted may
    have already marked the run failed. The user asked for this — it is a
    cancel, not a fault."""
    await _make_job(factory)
    run_id = await _make_run(factory, status=RunStatus.failed)

    run = await run_records.cancel(factory, uuid.UUID(run_id))

    assert run is not None
    assert run.status == RunStatus.canceled


async def test_cancel_keeps_the_error_output_the_pipeline_already_recorded(factory):
    """Whatever restic managed to say before it died is the only diagnostic
    there is; the placeholder must not overwrite it."""
    await _make_job(factory)
    run_id = await _make_run(factory, error_output="Fatal: connection reset")

    run = await run_records.cancel(factory, uuid.UUID(run_id))

    assert run is not None
    assert run.error_output == "Fatal: connection reset"


# ── crash ─────────────────────────────────────────────────────────────────────


async def test_crash_finalizes_a_stranded_row_to_failed(factory):
    """The lock-up this prevents: an unhandled exception leaves the row at
    status=running, and the overlap check then skips every future trigger of
    the job — manual and scheduled — until someone edits the DB by hand."""
    await _make_job(factory)
    run_id = await _make_run(factory, started_ago=30)

    try:
        raise RuntimeError("sqlite went away")
    except RuntimeError as exc:
        await run_records.crash(
            factory,
            uuid.UUID(run_id),
            error_output=run_records.crash_message("Backup", exc),
        )

    run = await _get(factory, run_id)
    assert run.status == RunStatus.failed
    assert "Backup runner crashed" in (run.error_output or "")
    assert "sqlite went away" in (run.error_output or "")
    assert run.finished_at is not None
    assert (run.duration_seconds or 0) >= 29
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_crash_message_carries_the_traceback(factory):
    try:
        raise ValueError("boom")
    except ValueError as exc:
        message = run_records.crash_message("Prune", exc)

    assert "Prune runner crashed" in message
    assert "ValueError" in message
    assert "test_crash_message_carries_the_traceback" in message, (
        "without the traceback the operator has an exception repr and no idea "
        "which step produced it"
    )


async def test_crash_leaves_an_already_finalized_row_alone(factory):
    """The pipeline can raise on its way out *after* finalizing — that run's
    recorded outcome is real and must not be rewritten as a crash."""
    await _make_job(factory)
    run_id = await _make_run(factory, status=RunStatus.success)

    await run_records.crash(factory, uuid.UUID(run_id), error_output="crashed")

    run = await _get(factory, run_id)
    assert run.status == RunStatus.success
    assert run.error_output is None


async def test_crash_writes_the_step_columns_it_is_given(factory):
    """A prune that crashes has to say so in prune_error_output — the run page
    reads the backup fields for backup runs and the prune fields for prune
    runs."""
    await _make_job(factory)
    run_id = await _make_run(factory, kind=RunKind.prune)

    await run_records.crash(
        factory,
        uuid.UUID(run_id),
        prune_status=PruneStatus.failed,
        prune_error_output="Prune runner crashed: boom",
        check_status=CheckStatus.skipped,
    )

    run = await _get(factory, run_id)
    assert run.status == RunStatus.failed
    assert run.prune_status == PruneStatus.failed
    assert "boom" in (run.prune_error_output or "")


async def test_crash_recovery_failure_is_logged_not_raised(factory, caplog):
    """If the DB is what broke, there is no recourse — but raising here would
    take down the task wrapper that still has to release the job."""
    await _make_job(factory)
    run_id = await _make_run(factory)

    class _Boom:
        def __call__(self):
            raise RuntimeError("db unreachable")

    await run_records.crash(_Boom(), uuid.UUID(run_id), error_output="x")

    assert "crash_recovery_failed" in caplog.text


# ── trim_history ──────────────────────────────────────────────────────────────


async def _seed_runs(factory, count: int) -> list[str]:
    ids: list[str] = []
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    async with factory() as s:
        for i in range(count):
            run_id = str(uuid.uuid4())
            ids.append(run_id)
            s.add(
                BackupRun(
                    id=run_id,
                    job_id=JOB_ID,
                    status=RunStatus.success,
                    triggered_by=TriggeredBy.manual,
                    started_at=base - timedelta(minutes=count - i),
                )
            )
        await s.commit()
    return ids  # oldest first


async def test_trim_history_keeps_the_newest_rows(factory):
    await _make_job(factory)
    async with factory() as s:
        s.add(AppSettings(id=1, keep_last_runs=3))
        await s.commit()
    ids = await _seed_runs(factory, 10)

    await run_records.trim_history(factory, JOB_ID)

    async with factory() as s:
        kept = set(
            (await s.execute(select(BackupRun.id).where(BackupRun.job_id == JOB_ID)))
            .scalars()
            .all()
        )
    assert kept == set(ids[-3:])


async def test_trim_history_leaves_a_short_history_alone(factory):
    await _make_job(factory)
    async with factory() as s:
        s.add(AppSettings(id=1, keep_last_runs=100))
        await s.commit()
    ids = await _seed_runs(factory, 4)

    await run_records.trim_history(factory, JOB_ID)

    async with factory() as s:
        kept = (
            (await s.execute(select(BackupRun.id).where(BackupRun.job_id == JOB_ID)))
            .scalars()
            .all()
        )
    assert set(kept) == set(ids)


async def test_trim_history_without_a_settings_row_falls_back_to_100(factory):
    """Startup seeds AppSettings, but a run triggered before that (or against a
    half-migrated DB) must not delete history on a guessed-at limit of zero."""
    await _make_job(factory)
    ids = await _seed_runs(factory, 5)

    await run_records.trim_history(factory, JOB_ID)

    async with factory() as s:
        kept = (
            (await s.execute(select(BackupRun.id).where(BackupRun.job_id == JOB_ID)))
            .scalars()
            .all()
        )
    assert set(kept) == set(ids)
