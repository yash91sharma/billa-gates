"""Tests for the full backup run lifecycle (Steps 1–12)."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import CheckStatus, PruneStatus
from app.services.backup_runner import active_jobs, run_backup, trigger_run

REPO = "/destinations/main"
JOB_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()

BACKUP_SUMMARY = {
    "message_type": "summary",
    "files_new": 10,
    "files_changed": 5,
    "files_unmodified": 1000,
    "dirs_new": 2,
    "dirs_changed": 1,
    "dirs_unmodified": 50,
    "data_added": 1024000,
    "data_added_packed": 900000,
    "total_bytes_processed": 50000000,
    "snapshot_id": "a" * 64,
}


async def _setup_job(engine, **overrides):
    from app.db.models import AppSettings, BackupJob, ScheduleType

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = AppSettings(
            id=1,
            ntfy_server_url="https://ntfy.sh",
            ntfy_topic="",
            notify_on_start=True,
            notify_on_success=True,
            notify_on_failure=True,
            notify_on_verification=True,
            default_job_timeout_hours=24,
        )
        s.add(settings)

        job = BackupJob(
            id=str(JOB_ID),
            name="Test Job",
            source_label=overrides.pop("source_label", "documents"),
            destination_label=overrides.pop("destination_label", "main"),
            restic_password=overrides.pop("restic_password", "s3cret"),
            schedule_type=ScheduleType.interval,
            schedule_value="6h",
            enabled=True,
            **overrides,
        )
        s.add(job)
        await s.commit()
        return job


async def _get_run(engine, run_id: str):
    from app.db.models import BackupRun

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
        assert run is not None, f"BackupRun {run_id} not found"
        return run


# ── Step 2: validate password ─────────────────────────────────────────────────


async def test_step2_empty_password_marks_run_failed(engine):
    await _setup_job(engine, restic_password="")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "password" in run.error_output.lower()
    assert run.prune_status is not None
    assert run.check_status is not None


# ── Step 4: init check ────────────────────────────────────────────────────────


async def test_step4_repo_exists_proceeds(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, '{"v":2}', "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_step4_repo_not_found_inits_repo(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    init_called = {"v": False}

    async def fake_init(*args, **kwargs):
        init_called["v"] = True
        return (0, "created repo", "")

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(1, "", "Fatal: no such file or directory"),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert init_called["v"] is True


async def test_step4_wrong_password_marks_failed(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with patch(
        "app.services.restic.restic_cat_config",
        return_value=(1, "", "wrong password or no key found"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_step4_init_failure_marks_failed(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(1, "", "does not exist"),
        ),
        patch(
            "app.services.restic.restic_init", return_value=(1, "", "permission denied")
        ),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed


# ── Step 4.5: auto-unlock (C1) ───────────────────────────────────────────────


async def test_auto_unlock_called_before_backup_when_enabled(engine):
    """Default is auto_unlock=True. Before each backup, restic_unlock is run
    so that a lock left behind by a previous abrupt termination is cleared.
    Without this, every subsequent run fails on lock acquisition."""
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    unlock_called = {"v": False}

    async def fake_unlock(*args, **kwargs):
        unlock_called["v"] = True
        return (0, "successfully removed locks", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", side_effect=fake_unlock),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert unlock_called["v"] is True
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_auto_unlock_skipped_when_disabled(engine):
    """If the operator turns auto_unlock off (e.g. running multi-writer setups
    where automatic unlock would mask a real concurrency conflict), the
    backup runner must not call restic_unlock."""
    from app.db.models import AppSettings

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.auto_unlock = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    unlock_called = {"v": False}

    async def fake_unlock(*args, **kwargs):
        unlock_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", side_effect=fake_unlock),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert unlock_called["v"] is False


async def test_auto_unlock_failure_does_not_fail_the_run(engine):
    """An empty restic repo (just initialized) has no lock to clear, so
    restic_unlock can legitimately exit non-zero. The run must continue and
    succeed if the actual backup succeeds."""
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_unlock",
            return_value=(1, "", "no locks to clear"),
        ),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


# ── Step 5: backup, rc=11 (lock failure) auto-recovery (C1) ──────────────────


async def test_backup_rc11_triggers_unlock_and_retry(engine):
    """Restic exit code 11 means the repo is locked. The runner must call
    restic_unlock and retry the backup exactly once — without that, a stale
    lock left by a prior abrupt termination breaks all subsequent runs even
    when auto_unlock has been turned off (so unlock didn't run pre-emptively)."""
    from app.db.models import AppSettings

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.auto_unlock = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    backup_calls = {"n": 0}
    unlock_calls = {"n": 0}

    async def fake_backup(*args, **kwargs):
        backup_calls["n"] += 1
        if backup_calls["n"] == 1:
            return (11, "", "unable to create lock in backend: already locked", None)
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    async def fake_unlock(*args, **kwargs):
        unlock_calls["n"] += 1
        return (0, "successfully removed locks", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", side_effect=fake_unlock),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert backup_calls["n"] == 2, "backup must be retried exactly once after unlock"
    assert unlock_calls["n"] == 1, "unlock must be called once between attempts"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_backup_rc11_retry_capped_at_one(engine):
    """If the retry also returns rc=11, the run must fail rather than loop
    forever. Two attempts total — no third."""
    from app.db.models import AppSettings

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.auto_unlock = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    backup_calls = {"n": 0}

    async def fake_backup(*args, **kwargs):
        backup_calls["n"] += 1
        return (11, "", "unable to create lock in backend: already locked", None)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_unlock",
            return_value=(0, "removed locks", ""),
        ),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert backup_calls["n"] == 2, "must not loop past one retry"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "lock" in (run.error_output or "").lower()


# ── Step 5: backup ────────────────────────────────────────────────────────────


async def test_step5_backup_failure_marks_run_failed(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, "", "fatal: source not found", None),
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_step5_backup_timeout_marks_failed(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    async def slow_backup(*args, **kwargs):
        raise asyncio.TimeoutError()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=slow_backup),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "timed out" in (run.error_output or "").lower()


# ── Step 5: rc=3 partial backup → warning ────────────────────────────────────


async def test_step5_backup_rc3_marks_warning_and_runs_prune_and_sync(engine):
    """restic exit code 3 (partial backup, snapshot still created) must be
    recorded as `warning` — not `failed` — and must still run prune (Step 8)
    and snapshot sync (Step 9), otherwise the snapshot will exist in the
    repo but be invisible to the UI and never pruned."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    rc3_summary = {
        **BACKUP_SUMMARY,
        "snapshot_id": "b" * 64,
    }
    rc3_stdout = (
        '{"message_type":"error","error":{"message":"failed to save '
        '/sources/x/locked.db: read /sources/x/locked.db: input/output error"},'
        '"during":"archival","item":"/sources/x/locked.db"}\n' + json.dumps(rc3_summary)
    )

    forget_called = {"v": False}
    snapshots_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    async def fake_snapshots(*args, **kwargs):
        snapshots_called["v"] = True
        return (0, [], "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, rc3_stdout, "", rc3_summary),
        ),
        patch("app.services.restic.restic_forget_prune", side_effect=fake_forget),
        patch("app.services.restic.restic_snapshots", side_effect=fake_snapshots),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert forget_called["v"] is True, "prune must run on rc=3 so repo doesn't bloat"
    assert snapshots_called["v"] is True, "snapshot sync must run on rc=3"
    assert run.snapshot_id == "b" * 64
    assert run.error_output is not None
    assert "/sources/x/locked.db" in run.error_output


async def test_step5_backup_rc3_without_retention_runs_plain_prune(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    prune_called = {"v": False}

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert prune_called["v"] is True
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning


# ── Run-history retention ─────────────────────────────────────────────────────


async def test_run_history_trimmed_to_keep_last_runs(engine):
    """After a run finishes, older runs beyond AppSettings.keep_last_runs for
    this job must be deleted, oldest-first. Snapshots in the restic repo are
    untouched — this only affects the `backup_runs` DB table."""
    from sqlalchemy import func, select

    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.keep_last_runs = 3
        await s.commit()

    base = datetime(2026, 1, 1, 0, 0, 0)
    async with factory() as s:
        for i in range(5):
            s.add(
                BackupRun(
                    id=str(uuid.uuid4()),
                    job_id=str(JOB_ID),
                    status=RunStatus.success,
                    triggered_by=TriggeredBy.manual,
                    started_at=base.replace(hour=i),
                    finished_at=base.replace(hour=i, minute=1),
                )
            )
        await s.commit()

    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    async with factory() as s:
        cnt = await s.scalar(
            select(func.count(BackupRun.id)).where(BackupRun.job_id == str(JOB_ID))
        )
    assert cnt == 3, f"expected keep_last_runs=3 rows, got {cnt}"


async def test_run_history_keeps_newest_runs(engine):
    """When trimming, the rows that survive must be the most-recent ones."""
    from sqlalchemy import select

    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.keep_last_runs = 2
        await s.commit()

    base = datetime(2026, 1, 1, 0, 0, 0)
    old_ids: list[str] = []
    async with factory() as s:
        for i in range(3):
            rid = str(uuid.uuid4())
            old_ids.append(rid)
            s.add(
                BackupRun(
                    id=rid,
                    job_id=str(JOB_ID),
                    status=RunStatus.success,
                    triggered_by=TriggeredBy.manual,
                    started_at=base.replace(hour=i),
                    finished_at=base.replace(hour=i, minute=1),
                )
            )
        await s.commit()

    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    async with factory() as s:
        rows = (
            (
                await s.execute(
                    select(BackupRun.id).where(BackupRun.job_id == str(JOB_ID))
                )
            )
            .scalars()
            .all()
        )
    surviving = set(rows)
    assert run_id in surviving, "current run must always be kept"
    # The two-most-recent original rows are hour=1 and hour=2; the current
    # run is newest; trim to 2 should keep current + hour=2.
    assert old_ids[0] not in surviving
    assert old_ids[1] not in surviving
    assert old_ids[2] in surviving


# ── Step 7: stats update ──────────────────────────────────────────────────────


async def test_step7_stats_populated_from_summary(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.files_new == 10
    assert run.files_changed == 5
    assert run.data_added_bytes == 1024000
    assert run.total_bytes_processed == 50000000


# ── Step 8: prune ─────────────────────────────────────────────────────────────


async def test_step8_prune_called_after_success(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    prune_called = {"v": False}

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert prune_called["v"] is True


async def test_step8_forget_prune_called_when_retention_set(engine):
    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    forget_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget_prune", side_effect=fake_forget),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is True


async def test_step8_prune_failure_nonfatal(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(1, "", "disk full")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.prune_status == PruneStatus.failed
    assert run.prune_error_output is not None


# ── Step 9: snapshot reconciliation ──────────────────────────────────────────


async def test_step9_snapshot_upserted(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    snap = {
        "id": "a" * 64,
        "time": "2024-01-01T12:00:00Z",
        "hostname": "myhost",
        "paths": ["/sources/documents"],
    }

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [snap], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    from sqlalchemy import select

    from app.db.models import Snapshot

    async with factory() as s:
        result = await s.execute(select(Snapshot).where(Snapshot.job_id == str(JOB_ID)))
        snaps = result.scalars().all()
    assert len(snaps) == 1
    assert snaps[0].snapshot_id == "a" * 64


# ── Step 10: finalize ─────────────────────────────────────────────────────────


async def test_step10_success_status_and_duration(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.finished_at is not None
    assert run.duration_seconds is not None
    assert run.duration_seconds >= 0


async def test_step10_check_status_skipped_when_check_disabled(engine):
    await _setup_job(engine, check_enabled=False)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.check_status == CheckStatus.skipped


# ── Step 12: integrity check ──────────────────────────────────────────────────


async def test_step12_check_passed(engine):
    from app.db.models import CheckMode

    await _setup_job(engine, check_enabled=True, check_mode=CheckMode.structural)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.restic.restic_check", return_value=(0, "no errors", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.check_status == CheckStatus.passed


async def test_step12_check_failure_nonfatal(engine):
    from app.db.models import CheckMode

    await _setup_job(engine, check_enabled=True, check_mode=CheckMode.structural)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch(
            "app.services.restic.restic_check", return_value=(1, "", "corrupted pack")
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.check_status == CheckStatus.failed
    assert run.check_error_output is not None


# ── Concurrent run guard ──────────────────────────────────────────────────────


async def testactive_jobs_cleared_after_completion(engine):
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert JOB_ID not in active_jobs


async def testactive_jobs_cleared_after_failure(engine):
    await _setup_job(engine, restic_password="")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    await run_backup(JOB_ID, uuid.UUID(run_id))
    assert JOB_ID not in active_jobs


# ── Notification checks ───────────────────────────────────────────────────────


async def test_step3_notification_sent_on_start(engine):
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "my-topic"
            settings.notify_on_start = True
            await s.flush()
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) >= 1


async def test_notification_skipped_when_topic_empty(engine):
    await _setup_job(engine)

    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append(True)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) == 0


# ── trigger_run: unified entry point for manual + scheduled triggers ─────────


async def test_trigger_run_creates_running_row_and_dispatches(engine):
    """trigger_run with no concurrent run creates a running row, adds the job
    to active_jobs under the lock, and dispatches run_backup as a background
    task. After the task completes, active_jobs is empty again."""
    from sqlalchemy import select

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)

    backup_done = asyncio.Event()
    received: dict = {}

    async def fake_run_backup(jid, rid):
        received["job_id"] = jid
        received["run_id"] = rid
        backup_done.set()

    with patch(
        "app.services.backup_runner.run_backup",
        new=AsyncMock(side_effect=fake_run_backup),
    ):
        run_id = await trigger_run(JOB_ID, TriggeredBy.manual)
        await asyncio.wait_for(backup_done.wait(), timeout=2.0)
        # Yield once more so the cleanup wrapper's finally can run.
        await asyncio.sleep(0)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        result = await s.execute(
            select(BackupRun).where(BackupRun.job_id == str(JOB_ID))
        )
        runs = result.scalars().all()
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status == RunStatus.running
    assert runs[0].triggered_by == TriggeredBy.manual
    assert received["job_id"] == JOB_ID
    assert received["run_id"] == uuid.UUID(run_id)
    assert JOB_ID not in active_jobs


async def test_trigger_run_returns_skipped_when_active_jobs_set(engine):
    """trigger_run creates a skipped/overlapping_run row when active_jobs
    already contains the job, regardless of how the caller is triggered."""
    from app.db.models import BackupRun, RunReason, RunStatus, TriggeredBy

    await _setup_job(engine)

    active_jobs.add(JOB_ID)
    try:
        run_id = await trigger_run(JOB_ID, TriggeredBy.manual)
    finally:
        active_jobs.discard(JOB_ID)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run
    assert run.triggered_by == TriggeredBy.manual
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_trigger_run_returns_skipped_when_db_has_running_row(engine):
    """A pre-existing running BackupRun in the DB (e.g. left from a crashed
    process before the cleanup ran) is also treated as overlap."""
    from app.db.models import BackupRun, RunReason, RunStatus, TriggeredBy

    await _setup_job(engine)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        existing = BackupRun(
            id=str(uuid.uuid4()),
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
        )
        s.add(existing)
        await s.commit()

    run_id = await trigger_run(JOB_ID, TriggeredBy.manual)

    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run


async def test_trigger_run_parallel_only_one_runs(engine):
    """10 parallel trigger_run calls produce exactly 1 running + 9 skipped
    rows — proves the lock + check + row-create is atomic per job."""
    from sqlalchemy import select

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)

    block_until_released = asyncio.Event()

    async def slow_run_backup(jid, rid):
        await block_until_released.wait()

    with patch(
        "app.services.backup_runner.run_backup",
        new=AsyncMock(side_effect=slow_run_backup),
    ):
        run_ids = await asyncio.gather(
            *[trigger_run(JOB_ID, TriggeredBy.manual) for _ in range(10)]
        )
        # All 10 trigger_run calls have returned; the single backup task is
        # still parked on block_until_released. Inspect DB state now.
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            result = await s.execute(
                select(BackupRun).where(BackupRun.job_id == str(JOB_ID))
            )
            runs = result.scalars().all()

        running = [r for r in runs if r.status == RunStatus.running]
        skipped = [r for r in runs if r.status == RunStatus.skipped]
        assert len(running) == 1, (
            f"expected 1 running, got {len(running)} "
            f"(statuses={[r.status.value for r in runs]})"
        )
        assert len(skipped) == 9, (
            f"expected 9 skipped, got {len(skipped)} "
            f"(statuses={[r.status.value for r in runs]})"
        )
        assert all(r.triggered_by == TriggeredBy.manual for r in runs)
        assert len(run_ids) == 10
        assert len(set(run_ids)) == 10  # every call returned a distinct id

        # Release the backup task so cleanup can run.
        block_until_released.set()
        # Allow the create_task'd coroutine to finish and run its finally.
        for _ in range(5):
            await asyncio.sleep(0)

    assert JOB_ID not in active_jobs


async def test_trigger_run_scheduler_then_manual_serializes(engine):
    """A scheduler-triggered run and a manual-triggered run racing against
    each other go through the same critical section: one wins, one is
    recorded as skipped/overlapping_run."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)

    block = asyncio.Event()

    async def slow_run_backup(jid, rid):
        await block.wait()

    with patch(
        "app.services.backup_runner.run_backup",
        new=AsyncMock(side_effect=slow_run_backup),
    ):
        sched_id, manual_id = await asyncio.gather(
            trigger_run(JOB_ID, TriggeredBy.scheduler),
            trigger_run(JOB_ID, TriggeredBy.manual),
        )

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            sched_run = await s.get(BackupRun, sched_id)
            manual_run = await s.get(BackupRun, manual_id)
        assert sched_run is not None and manual_run is not None
        statuses = {sched_run.status, manual_run.status}
        assert statuses == {RunStatus.running, RunStatus.skipped}, (
            f"expected one of each, got sched={sched_run.status.value} "
            f"manual={manual_run.status.value}"
        )

        block.set()
        for _ in range(5):
            await asyncio.sleep(0)

    assert JOB_ID not in active_jobs


async def test_trigger_run_job_not_found_is_noop(engine):
    """trigger_run returns gracefully when the job does not exist; no row
    is created and active_jobs stays clean."""
    from sqlalchemy import select

    from app.db.models import BackupRun, TriggeredBy

    unknown_id = uuid.uuid4()
    result = await trigger_run(unknown_id, TriggeredBy.scheduler)
    assert result is None
    assert unknown_id not in active_jobs

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        rows = (
            (
                await s.execute(
                    select(BackupRun).where(BackupRun.job_id == str(unknown_id))
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_trigger_run_default_triggered_by_is_scheduler(engine):
    """When trigger_run is called with no explicit triggered_by — as the
    APScheduler path does — the row is tagged scheduler."""
    from app.db.models import BackupRun, TriggeredBy

    await _setup_job(engine)

    with patch(
        "app.services.backup_runner.run_backup", new=AsyncMock(return_value=None)
    ):
        run_id = await trigger_run(JOB_ID)
        for _ in range(5):
            await asyncio.sleep(0)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.triggered_by == TriggeredBy.scheduler

    active_jobs.discard(JOB_ID)


# ── Step 6: source path construction ─────────────────────────────────────────


async def test_step6_source_path_uses_source_label(engine):
    await _setup_job(engine, source_label="documents")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_backup(repo, password, source_path, timeout_seconds, **kwargs):
        captured["source_path"] = source_path
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["source_path"] == "/sources/documents"


async def test_step6_source_subpath_appended_to_source_path(engine):
    await _setup_job(engine, source_label="documents", source_subpath="photos")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_backup(repo, password, source_path, timeout_seconds, **kwargs):
        captured["source_path"] = source_path
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["source_path"] == "/sources/documents/photos"


async def test_step6_repo_path_uses_destination_label(engine):
    await _setup_job(engine, destination_label="offsite")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_cat_config(repo, password):
        captured["repo"] = repo
        return (0, "{}", "")

    with (
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert "offsite" in captured["repo"]


# ── Notification behavior ─────────────────────────────────────────────────────


async def test_step11_failure_notification_sent(engine):
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
            settings.notify_on_failure = True
            await s.flush()
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(0, "{}", ""),
        ),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, "", "fatal: disk full", None),
        ),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) >= 1


async def test_notify_on_success_false_skips_success_notification(engine):
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
        settings.notify_on_success = False
        settings.notify_on_start = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append(True)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) == 0


async def test_step11_warning_notification_sent(engine):
    """When a backup finishes with rc=3 (warning status) and notify_on_warning
    is True, a 'Backup completed with warnings' notification must fire."""
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
        settings.notify_on_warning = True
        settings.notify_on_start = False
        settings.notify_on_success = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append({"args": args, "kwargs": kwargs})

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) == 1
    title = notify_calls[0]["args"][2]
    assert "warning" in title.lower()


async def test_notify_on_warning_false_skips_warning_notification(engine):
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
        settings.notify_on_warning = False
        settings.notify_on_start = False
        settings.notify_on_success = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append(True)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) == 0


async def test_notify_on_failure_false_skips_failure_notification(engine):
    await _setup_job(engine)
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
        settings.notify_on_failure = False
        settings.notify_on_start = False
        await s.commit()

    run_id = str(uuid.uuid4())
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    notify_calls = []

    async def fake_notify(*args, **kwargs):
        notify_calls.append(True)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, "", "fatal: error", None),
        ),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert len(notify_calls) == 0


# ── Step 12: check mode details ───────────────────────────────────────────────


async def test_step12_check_subset_passes_percent_to_restic(engine):
    from app.db.models import CheckMode

    await _setup_job(
        engine, check_enabled=True, check_mode=CheckMode.subset, check_subset_percent=10
    )
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_check(repo, password, mode, subset_percent, timeout_seconds):
        captured["mode"] = mode
        captured["subset_percent"] = subset_percent
        return (0, "no errors", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.restic.restic_check", side_effect=fake_check),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["mode"] == "subset"
    assert captured["subset_percent"] == 10


async def test_step12_check_full_mode_passed_correctly(engine):
    from app.db.models import CheckMode

    await _setup_job(engine, check_enabled=True, check_mode=CheckMode.full)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_check(repo, password, mode, subset_percent, timeout_seconds):
        captured["mode"] = mode
        captured["subset_percent"] = subset_percent
        return (0, "no errors", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.restic.restic_check", side_effect=fake_check),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["mode"] == "full"
    assert captured["subset_percent"] is None


async def test_step12_check_uses_job_timeout(engine):
    from app.db.models import CheckMode

    await _setup_job(
        engine,
        check_enabled=True,
        check_mode=CheckMode.structural,
        check_timeout_hours=2,
    )
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    captured = {}

    async def fake_check(repo, password, mode, subset_percent, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        return (0, "ok", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.restic.restic_snapshots", return_value=(0, [], "")),
        patch("app.services.restic.restic_check", side_effect=fake_check),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["timeout_seconds"] == 2 * 3600
