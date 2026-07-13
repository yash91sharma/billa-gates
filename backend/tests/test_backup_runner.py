"""Tests for the full backup run lifecycle (Steps 1–12)."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import CheckStatus, PruneStatus
from app.services.backup_runner import (
    active_jobs,
    run_backup,
    run_check,
    run_prune,
    trigger_check,
    trigger_prune,
    trigger_run,
)

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
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_step4_repo_not_found_inits_repo(engine):
    """restic ≥0.17 returns exit code 10 when the repository does not exist;
    the runner must call restic_init in response (gaps.md H5)."""
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
            return_value=(10, "", "Fatal: unable to open config file"),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert init_called["v"] is True


async def test_step4_wrong_password_marks_failed(engine):
    """restic ≥0.17 returns exit code 12 on wrong password; the runner must
    branch on the exit code, never on a stderr substring (gaps.md H5).
    The user-visible error message must explicitly say 'password' so the
    operator knows what to fix."""
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
        return (0, "", "")

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(12, "", "Fatal: wrong password or no key found"),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped
    assert run.error_output is not None
    assert "password" in run.error_output.lower()
    # An rc=12 must never trigger init — the repo is fine, the password is wrong.
    # Initing on top of a real repo would be both pointless and confusing.
    assert init_called["v"] is False


async def test_step4_init_failure_marks_failed(engine):
    """rc=10 → init; if init then fails, the run record must surface the init
    failure's stderr so the operator can see what went wrong."""
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
            return_value=(10, "", "Fatal: unable to open config file"),
        ),
        patch(
            "app.services.restic.restic_init", return_value=(1, "", "permission denied")
        ),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "permission denied" in run.error_output


async def test_step4_init_failure_sends_notification(engine):
    """If init check fails, the runner must send a failure notification
    if notify_on_failure is enabled.
    """
    await _setup_job(engine)

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
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

    notify_called = {"v": False}

    async def fake_notify(url, topic, title, message, **kwargs):
        if "failed" in title.lower() and "alerts" == topic:
            notify_called["v"] = True

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(12, "", "Fatal: wrong password"),
        ),
        patch("app.services.backup_runner.send_notification", side_effect=fake_notify),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert notify_called["v"] is True


# ── Step 4: exit-code branching (H5) ─────────────────────────────────────────


async def test_step4_rc11_stale_lock_unlocks_and_retries(engine):
    """restic exit code 11 from `cat config` means the repo was locked at
    metadata-read time (rare, but possible if a previous abrupt termination
    left a lock and the periodic auto_unlock pass hasn't fired yet). The
    runner must call restic_unlock and retry cat_config once before deciding
    the repo is unreachable (gaps.md H5)."""
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

    cat_calls = {"n": 0}

    async def fake_cat_config(*args, **kwargs):
        cat_calls["n"] += 1
        if cat_calls["n"] == 1:
            return (11, "", "Fatal: unable to create lock in backend: already locked")
        return (0, '{"version":2}', "")

    with (
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.restic.restic_unlock", return_value=(0, "removed", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert cat_calls["n"] == 2, "cat_config must be retried once after unlock"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_step4_rc11_lock_retry_capped_at_one(engine):
    """If unlock + retry still returns rc=11, the run must fail rather than
    loop forever. Two cat_config attempts total — no third."""
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

    cat_calls = {"n": 0}

    async def fake_cat_config(*args, **kwargs):
        cat_calls["n"] += 1
        return (11, "", "Fatal: unable to create lock in backend: already locked")

    with (
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert cat_calls["n"] == 2, "must not loop past one retry"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "lock" in run.error_output.lower()


async def test_step4_generic_failure_does_not_init(engine):
    """Backwards-compat / safety: a generic non-zero exit code (rc=1) — used
    by older restic versions for everything, and by current restic for
    catch-all errors — must NOT trigger init. Initing on top of an
    unreachable-but-existing repo (network blip, permission glitch) would be
    incorrect and the substring-based dispatch this fix replaces was making
    that decision based on whatever happened to be in stderr (gaps.md H5)."""
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
        return (0, "", "")

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(
                1,
                "",
                "Fatal: backend storage error: connection refused",
            ),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert init_called["v"] is False, (
        "generic non-zero rc must not be interpreted as 'repo missing'"
    )
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    # The actual restic stderr must reach the user, not a generic message.
    assert "connection refused" in run.error_output


async def test_step4_generic_failure_surfaces_stderr_to_user(engine):
    """An unknown restic exit code must put the actual stderr into
    error_output so the operator sees the real failure mode — not just
    'backup failed' with no context (gaps.md H5)."""
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

    stderr_msg = (
        "Fatal: unable to open repository: backend storage error: "
        'Get "https://s3.example/bucket/config": dial tcp: lookup s3.example: '
        "no such host"
    )

    with patch(
        "app.services.restic.restic_cat_config", return_value=(2, "", stderr_msg)
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    # The full restic stderr must reach the user verbatim — no truncation,
    # no replacement with a generic message.
    assert stderr_msg in run.error_output


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
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


# ── Step 5: --parent passthrough (C5) ────────────────────────────────────────


async def test_backup_passes_parent_when_prior_snapshot_exists(engine):
    """When restic_latest_snapshot_id returns an id, the orchestrator must
    forward it as parent_snapshot_id to restic_backup so restic does an
    incremental rescan instead of a full-tree re-read (gaps.md C5)."""
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

    captured = {}

    async def fake_backup(*args, **kwargs):
        captured["kwargs"] = kwargs
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    parent_id = "c" * 64
    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch(
            "app.services.restic.restic_latest_snapshot_id",
            return_value=parent_id,
        ),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["kwargs"].get("parent_snapshot_id") == parent_id


async def test_backup_omits_parent_on_first_run(engine):
    """First-ever backup for a job has no prior snapshot; parent_snapshot_id
    must be None so restic_backup doesn't add --parent (which would fail with
    'parent snapshot not found')."""
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

    captured = {}

    async def fake_backup(*args, **kwargs):
        captured["kwargs"] = kwargs
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_latest_snapshot_id", return_value=None),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["kwargs"].get("parent_snapshot_id") is None


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
    assert "source not found" in run.error_output
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_step5_backup_failure_surfaces_json_errors_from_stdout(engine):
    """When restic backup fails (rc!=0, !=3) it may still have emitted
    per-file errors as JSON lines on stdout before giving up. Those messages
    name the specific file/path that caused the failure, while stderr usually
    contains only the final fatal line. error_output must include both so the
    operator can see *which* files restic was working on when it died, not
    just the post-mortem fatal (gaps.md H5)."""
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

    json_errors_stdout = (
        '{"message_type":"error","error":{"message":"open '
        '/sources/x/secrets.kdbx: permission denied"},'
        '"during":"archival","item":"/sources/x/secrets.kdbx"}\n'
        '{"message_type":"error","error":{"message":"open '
        '/sources/x/db.sqlite: device or resource busy"},'
        '"during":"archival","item":"/sources/x/db.sqlite"}\n'
    )
    fatal_stderr = "Fatal: unable to save snapshot: tree blob is missing"

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, json_errors_stdout, fatal_stderr, None),
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    # Per-file JSON errors from stdout must be surfaced …
    assert "/sources/x/secrets.kdbx" in run.error_output
    assert "permission denied" in run.error_output
    assert "/sources/x/db.sqlite" in run.error_output
    # … alongside the final fatal stderr.
    assert "tree blob is missing" in run.error_output


async def test_step5_backup_failure_without_json_errors_still_surfaces_stderr(engine):
    """If stdout has no parseable JSON errors (restic crashed before
    emitting any), error_output must still contain stderr — the user must
    never see an empty error_output on a failed run."""
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

    stderr_msg = "Fatal: repository corruption detected: pack 1a2b3c missing"
    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, "", stderr_msg, None),
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert stderr_msg in run.error_output


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

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, rc3_stdout, "", rc3_summary),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert forget_called["v"] is True, (
        "forget must run on rc=3 so retention applies to the new snapshot"
    )
    # snapshot_id is taken from the JSON summary, not from a post-backup
    # `restic snapshots` reconcile (which was dropped — see gaps.md C4-Alt).
    assert run.snapshot_id == "b" * 64
    assert run.error_output is not None
    assert "/sources/x/locked.db" in run.error_output


async def test_step5_backup_rc3_without_retention_skips_forget_and_prune(engine):
    """rc=3 still produces a snapshot, but with no retention configured there
    is nothing for `restic forget` to do — and `restic prune` is no longer
    bundled into the backup window at all (gaps.md H1). Neither must be
    called; the run finishes with warning + prune_status=skipped."""
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

    forget_called = {"v": False}
    prune_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is False, "no retention → no forget"
    assert prune_called["v"] is False, (
        "backup must never call restic_prune (gaps.md H1)"
    )
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.prune_status == PruneStatus.skipped


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
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.files_new == 10
    assert run.files_changed == 5
    assert run.data_added_bytes == 1024000
    assert run.total_bytes_processed == 50000000


async def test_step7_backup_output_drops_status_lines(engine):
    """Persisted backup_output must exclude restic's JSON progress lines
    (message_type=status) — over a many-hour run those are thousands of
    lines of noise that bloat the DB row and the run-detail page. Error
    lines, the summary line, and any non-JSON output must be kept: they
    are the record of what happened and what failed."""
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

    status_line = json.dumps(
        {"message_type": "status", "percent_done": 0.5, "files_done": 3}
    )
    error_line = json.dumps(
        {
            "message_type": "error",
            "error": {"message": "permission denied"},
            "item": "/sources/documents/locked.txt",
        }
    )
    plain_line = "restic said something un-JSON here"
    stdout = "\n".join(
        [status_line, plain_line, status_line, error_line, json.dumps(BACKUP_SUMMARY)]
    )

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, stdout, "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.backup_output is not None
    assert '"message_type": "status"' not in run.backup_output
    assert "percent_done" not in run.backup_output
    assert "locked.txt" in run.backup_output
    assert '"message_type": "summary"' in run.backup_output
    assert plain_line in run.backup_output


# ── Step 8: forget (gaps.md H1: prune is now a separate operation) ───────────


async def test_step8_forget_and_prune_skipped_when_no_retention(engine):
    """With no retention configured, the backup pipeline must skip both
    `restic forget` and `restic prune` entirely (gaps.md H1): forget would
    be a no-op and prune is now manual / on its own schedule. The run still
    succeeds; prune_status is recorded as `skipped`."""
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

    forget_called = {"v": False}
    prune_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is False
    assert prune_called["v"] is False
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.prune_status == PruneStatus.skipped


async def test_step8_forget_called_when_retention_set(engine):
    """With retention configured, the backup pipeline must call `restic
    forget` — but never `restic prune` (gaps.md H1)."""
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
    prune_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is True
    assert prune_called["v"] is False, "backup pipeline must never call restic_prune"


async def test_step8_forget_failure_nonfatal(engine):
    """If `restic forget` fails (e.g. transient repo error), the run still
    succeeds — forget is a maintenance step. The failure is recorded on the
    run via prune_status=failed + prune_error_output."""
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

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch(
            "app.services.restic.restic_forget",
            return_value=(1, "", "disk full"),
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.prune_status == PruneStatus.failed
    assert run.prune_error_output is not None


# ── Step 9: snapshot reconciliation ──────────────────────────────────────────


async def test_step9_snapshot_listing_cache_invalidated_after_successful_backup(engine):
    """After a successful backup, the snapshot-listing TTL cache must be
    cleared so the UI sees the new snapshot immediately rather than waiting
    out the TTL. Restic itself is the source of truth (gaps.md C4-Alt) —
    the cache only exists to absorb dashboard refresh storms."""
    from app.services import snapshot_listing

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

    # Seed the cache so we can observe it being cleared.
    snapshot_listing._cache["/destinations/fake"] = ([{"sentinel": True}], 9e9)
    assert snapshot_listing._cache, "test setup failed: cache should be seeded"

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    # Successful run must have invalidated every cache entry so the
    # /jobs/{id}/snapshots endpoint will hit restic on the next request.
    assert snapshot_listing._cache == {}
    # The snapshot_id link from run to restic snapshot lives on BackupRun,
    # not in a separate table.
    run_obj = await _get_run(engine, run_id)
    assert run_obj.snapshot_id == BACKUP_SUMMARY["snapshot_id"]


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
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.check_status == CheckStatus.skipped


async def test_run_check_invokes_restic_check_and_marks_success(engine):
    """`run_check` executes `restic check` for the job's repo and finalizes
    the BackupRun row as success when restic exits 0. Check runs reuse the
    BackupRun table with kind=check so the UI can list them."""
    from app.db.models import BackupRun, CheckStatus, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.check,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    check_called = {"v": False, "args": None}

    async def fake_check(
        repo, password, mode, subset_percent, timeout_seconds, *args, **kwargs
    ):
        check_called["v"] = True
        check_called["args"] = (repo, mode, subset_percent, timeout_seconds)
        return (0, "", "")

    with patch("app.services.restic.restic_check", side_effect=fake_check):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    assert check_called["v"] is True
    assert f"/destinations/main/{JOB_ID}" in check_called["args"][0]
    assert check_called["args"][1] == "structural"
    assert check_called["args"][2] is None

    run = await _get_run(engine, run_id)
    assert run.kind == RunKind.check
    assert run.status == RunStatus.success
    assert run.check_status == CheckStatus.passed
    assert run.finished_at is not None
    assert run.duration_seconds is not None


async def test_run_check_marks_failed_on_nonzero_rc(engine):
    """When `restic check` returns non-zero, the check run is marked failed
    and the stderr lands in check_error_output so the operator can see why."""
    from app.db.models import BackupRun, CheckStatus, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.check,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    with patch(
        "app.services.restic.restic_check",
        return_value=(1, "", "corrupted repository"),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.check_status == CheckStatus.failed
    assert "corrupted repository" in (run.check_error_output or "")


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

    async def fake_cat_config(repo, password, *args, **kwargs):
        captured["repo"] = repo
        return (0, "{}", "")

    with (
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
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


async def test_run_check_subset_passes_percent_to_restic(engine):
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.check,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    captured = {}

    async def fake_check(
        repo, password, mode, subset_percent, timeout_seconds, *args, **kwargs
    ):
        captured["mode"] = mode
        captured["subset_percent"] = subset_percent
        return (0, "no errors", "")

    with patch("app.services.restic.restic_check", side_effect=fake_check):
        await run_check(JOB_ID, uuid.UUID(run_id), "subset", 10, None)

    assert captured["mode"] == "subset"
    assert captured["subset_percent"] == 10


async def test_run_check_full_mode_passed_correctly(engine):
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.check,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    captured = {}

    async def fake_check(
        repo, password, mode, subset_percent, timeout_seconds, *args, **kwargs
    ):
        captured["mode"] = mode
        captured["subset_percent"] = subset_percent
        return (0, "no errors", "")

    with patch("app.services.restic.restic_check", side_effect=fake_check):
        await run_check(JOB_ID, uuid.UUID(run_id), "full", None, None)

    assert captured["mode"] == "full"
    assert captured["subset_percent"] is None


async def test_run_check_uses_timeout(engine):
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(
        engine,
        check_timeout_hours=2,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.check,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    captured = {}

    async def fake_check(
        repo, password, mode, subset_percent, timeout_seconds, *args, **kwargs
    ):
        captured["timeout_seconds"] = timeout_seconds
        return (0, "ok", "")

    with patch("app.services.restic.restic_check", side_effect=fake_check):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    assert captured["timeout_seconds"] == 2 * 3600


async def test_trigger_check_creates_running_row_with_kind_check(engine):
    from sqlalchemy import select

    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)

    done = asyncio.Event()

    async def fake_run_check(jid, rid, mode, percent, hours):
        done.set()

    with patch(
        "app.services.backup_runner.run_check",
        new=AsyncMock(side_effect=fake_run_check),
    ):
        run_id = await trigger_check(
            JOB_ID, TriggeredBy.manual, "structural", None, None
        )
        await asyncio.wait_for(done.wait(), timeout=2.0)
        for _ in range(5):
            await asyncio.sleep(0)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        result = await s.execute(
            select(BackupRun).where(BackupRun.job_id == str(JOB_ID))
        )
        runs = result.scalars().all()
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].kind == RunKind.check
    assert runs[0].status == RunStatus.running
    assert runs[0].triggered_by == TriggeredBy.manual
    assert JOB_ID not in active_jobs


# ── Prune runs (gaps.md H1) ──────────────────────────────────────────────────


async def test_run_prune_invokes_restic_prune_and_marks_success(engine):
    """`run_prune` executes `restic prune` for the job's repo and finalizes
    the BackupRun row as success when restic exits 0. Prune runs reuse the
    BackupRun table with kind=prune (gaps.md H1) so the UI can list them
    alongside backup runs without a new table."""
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.prune,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    prune_called = {"v": False, "args": None}

    async def fake_prune(repo, password, timeout_seconds, *args, **kwargs):
        prune_called["v"] = True
        prune_called["args"] = (repo, timeout_seconds)
        return (0, "", "")

    with patch("app.services.restic.restic_prune", side_effect=fake_prune):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    assert prune_called["v"] is True
    # Repo path follows the same /destinations/{label}/{job_id} convention as
    # backup runs so the same restic repo is targeted.
    assert f"/destinations/main/{JOB_ID}" in prune_called["args"][0]

    run = await _get_run(engine, run_id)
    assert run.kind == RunKind.prune
    assert run.status == RunStatus.success
    assert run.finished_at is not None
    assert run.duration_seconds is not None


async def test_run_prune_marks_failed_on_nonzero_rc(engine):
    """When `restic prune` returns non-zero, the prune run is marked failed
    and the stderr lands in prune_error_output so the operator can see why."""
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.prune,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    with patch(
        "app.services.restic.restic_prune",
        return_value=(1, "", "disk full"),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "disk full" in (run.prune_error_output or "")


async def test_trigger_prune_creates_running_row_with_kind_prune(engine):
    """trigger_prune mirrors trigger_run but creates a BackupRun row with
    kind=prune. Dispatches `run_prune` as a background task once the row is
    committed."""
    from sqlalchemy import select

    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)

    done = asyncio.Event()

    async def fake_run_prune(jid, rid):
        done.set()

    with patch(
        "app.services.backup_runner.run_prune",
        new=AsyncMock(side_effect=fake_run_prune),
    ):
        run_id = await trigger_prune(JOB_ID, TriggeredBy.manual)
        await asyncio.wait_for(done.wait(), timeout=2.0)
        # Yield once more so the cleanup wrapper's finally can run.
        for _ in range(5):
            await asyncio.sleep(0)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        result = await s.execute(
            select(BackupRun).where(BackupRun.job_id == str(JOB_ID))
        )
        runs = result.scalars().all()
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].kind == RunKind.prune
    # Right after trigger_prune commits the row it's `running`; we mocked
    # run_prune so finalization to success/failed doesn't happen here.
    assert runs[0].status == RunStatus.running
    assert runs[0].triggered_by == TriggeredBy.manual
    assert JOB_ID not in active_jobs


async def test_trigger_prune_returns_skipped_when_backup_is_running(engine):
    """A scheduled or manual backup currently in flight must block a manual
    prune (and vice versa) — they share the same per-job lock + active_jobs
    set so they never run concurrently against the same restic repo."""
    from app.db.models import BackupRun, RunKind, RunReason, RunStatus, TriggeredBy

    await _setup_job(engine)

    active_jobs.add(JOB_ID)
    try:
        run_id = await trigger_prune(JOB_ID, TriggeredBy.manual)
    finally:
        active_jobs.discard(JOB_ID)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.kind == RunKind.prune
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run


async def test_trigger_prune_job_not_found_returns_none(engine):
    """When the job does not exist, trigger_prune returns None and creates
    no rows — mirrors trigger_run's behavior so the route layer can map this
    to a 404."""
    from sqlalchemy import select

    from app.db.models import BackupRun, TriggeredBy

    unknown_id = uuid.uuid4()
    result = await trigger_prune(unknown_id, TriggeredBy.manual)
    assert result is None

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


# ── Crash recovery: unhandled exceptions must not strand the run row ─────────


async def test_run_backup_unhandled_exception_finalizes_run_to_failed(engine):
    """If any unhandled exception bubbles out of the backup pipeline (DB
    lock, network failure, ntfy hang, subprocess crash, etc.), the BackupRun
    row MUST be finalized to RunStatus.failed with the traceback recorded in
    error_output. Without this safety net the row stays at status=running
    forever, and trigger_run's overlap check (which queries both active_jobs
    and the DB for any status=running row) skips every future trigger as
    overlapping_run — permanently halting backups for the job."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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

    # restic_cat_config is the first restic call in the pipeline. Raise an
    # unhandled exception there — simulating a subprocess error before stdout
    # is returned, or a transient OS-level failure — and confirm the runner
    # does not leave the row stranded at `running`.
    with (
        patch(
            "app.services.restic.restic_cat_config",
            side_effect=RuntimeError("simulated subprocess failure"),
        ),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed, (
        f"expected failed (got {run.status}); a stuck `running` row would "
        f"lock the job out of every future trigger via overlap detection"
    )
    assert run.finished_at is not None
    assert run.duration_seconds is not None
    assert run.error_output is not None
    assert "simulated subprocess failure" in run.error_output
    # Traceback must be present so the operator can diagnose the crash
    # post-mortem without re-running the workload locally.
    assert "Traceback" in run.error_output or "RuntimeError" in run.error_output
    # Prune/check must be marked skipped on a crash so the UI polling loop
    # doesn't wait forever for a status that will never arrive.
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_run_backup_start_notification_failure_does_not_crash_runner(engine):
    """A transient ntfy/network failure during the start notification must
    not abort the backup. The notification is a side-effect; the backup
    pipeline should continue and the run should finalize normally on the
    restic outcome — not on the notification outcome."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "my-topic"
            settings.notify_on_start = True
            settings.notify_on_success = False
            await s.commit()

    run_id = str(uuid.uuid4())
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
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch(
            "app.services.backup_runner.send_notification",
            side_effect=RuntimeError("ntfy server down"),
        ),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    # Backup itself succeeded; the broken notification is non-fatal.
    assert run.status == RunStatus.success, (
        f"expected success (got {run.status}); a broken notification must "
        f"not bring down the backup pipeline"
    )
    assert run.finished_at is not None


async def test_run_backup_handles_cat_config_timeout(engine):
    """End-to-end check that a timed-out init check (unresponsive NFS, SMB
    offline, cloud mount stalled) propagates as a clean run failure rather
    than wedging the runner. restic_cat_config returns
    (-1, '', 'cat config timed out') on a 60s hang; the runner must classify
    that as a generic failure and surface the timeout message in
    error_output for the operator."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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
        return (0, "", "")

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(-1, "", "cat config timed out"),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "cat config timed out" in run.error_output
    # A timeout (rc=-1) must NOT be misclassified as "repo not found" (rc=10),
    # which would trigger a destructive `restic init` against a backend that
    # might still hold a real repo. The runner has to treat unrecognized rc
    # as generic failure (gaps.md H5).
    assert init_called["v"] is False, (
        "restic_init must not be invoked on a timeout — would corrupt the "
        "operator's mental model of 'backend temporarily unreachable' by "
        "overwriting it with a fresh init attempt"
    )
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped


async def test_run_prune_unhandled_exception_finalizes_run_to_failed(engine):
    """Same lock-up hazard applies to run_prune: a crash mid-pipeline would
    leave the prune row stuck at status=running, and trigger_run /
    trigger_prune would then skip every future trigger as overlapping_run.
    The prune runner must finalize to failed with the traceback on any
    unhandled exception."""
    from app.db.models import BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=RunKind.prune,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    with patch(
        "app.services.restic.restic_prune",
        side_effect=RuntimeError("simulated prune crash"),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed, (
        f"expected failed (got {run.status}); a stuck `running` prune row "
        f"would block every future backup and prune trigger for this job"
    )
    assert run.finished_at is not None
    assert run.duration_seconds is not None
    assert run.prune_error_output is not None
    assert "simulated prune crash" in run.prune_error_output
    assert (
        "Traceback" in run.prune_error_output
        or "RuntimeError" in run.prune_error_output
    )


async def test_run_backup_mount_check_fails(engine):
    """Verify that if check_mount_file_exists returns False, the backup is aborted
    immediately, the status is set to failed, and error_output contains the expected
    mount failure message. Restic commands must not be called."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, source_label="documents")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    cat_config_called = False
    backup_called = False

    async def fake_cat_config(*args, **kwargs):
        nonlocal cat_config_called
        cat_config_called = True
        return (0, "{}", "")

    async def fake_backup(*args, **kwargs):
        nonlocal backup_called
        backup_called = True
        return (0, "{}", "", None)

    with (
        patch("app.services.backup_runner.check_mount_file_exists", return_value=False),
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.backup_runner.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "Mount check failed" in run.error_output
    assert ".billa_gates_check" in run.error_output
    assert "documents" in run.error_output

    # Verify restic was not invoked
    assert cat_config_called is False
    assert backup_called is False

    # Verify notification was sent
    assert mock_notify.call_count == 1
    assert "Backup failed" in mock_notify.call_args[0][2]
    assert "Mount check failed" in mock_notify.call_args[0][3]


async def test_run_backup_destination_mount_check_fails(engine):
    """Verify that if check_destination_mount_file_exists returns False,
    the backup is aborted immediately, the status is set to failed, and
    error_output contains the expected destination mount failure message.
    Restic commands must not be called.
    """
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, destination_label="main")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
        run = BackupRun(
            id=run_id,
            job_id=str(JOB_ID),
            status=RunStatus.running,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    cat_config_called = False
    backup_called = False

    async def fake_cat_config(*args, **kwargs):
        nonlocal cat_config_called
        cat_config_called = True
        return (0, "{}", "")

    async def fake_backup(*args, **kwargs):
        nonlocal backup_called
        backup_called = True
        return (0, "{}", "", None)

    with (
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.backup_runner.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "Destination mount check failed" in run.error_output
    assert ".billa_gates_check" in run.error_output
    assert "main" in run.error_output

    # Verify restic was not invoked
    assert cat_config_called is False
    assert backup_called is False

    # Verify notification was sent
    assert mock_notify.call_count == 1
    assert "Backup failed" in mock_notify.call_args[0][2]
    assert "Destination mount check failed" in mock_notify.call_args[0][3]


async def test_run_backup_hung_mount_check_fails_run_promptly(engine):
    """A mounted-but-hung SMB share makes the sentinel stat() block in the
    kernel. The probe must run on a worker thread with a deadline so the run
    fails within the probe timeout instead of freezing the event loop (and
    with it the API, the scheduler, and every other job). Restic must never
    be invoked."""
    import time as _time

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, source_label="documents")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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

    def hung_check(label: str) -> bool:
        _time.sleep(1.0)  # simulates stat() stuck on a dead SMB mount
        return True

    cat_config_called = False

    async def fake_cat_config(*args, **kwargs):
        nonlocal cat_config_called
        cat_config_called = True
        return (0, "{}", "")

    start = _time.monotonic()
    with (
        patch(
            "app.services.backup_runner.check_mount_file_exists",
            side_effect=hung_check,
        ),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))
    elapsed = _time.monotonic() - start

    assert elapsed < 0.9, "run_backup must give up at the probe timeout"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "Mount check failed" in run.error_output
    assert cat_config_called is False


async def test_run_backup_fails_on_parent_lookup_failure(engine):
    """Verify that if restic_latest_snapshot_id raises a ResticError (such as a timeout
    or network failure), the backup fails cleanly and records the error, preventing
    silent full rescans."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy
    from app.services.restic import ResticError

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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

    backup_called = False

    async def fake_backup(*args, **kwargs):
        nonlocal backup_called
        backup_called = True
        return (0, "{}", "", None)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch(
            "app.services.restic.restic_latest_snapshot_id",
            side_effect=ResticError("snapshots command timed out after 60 seconds"),
        ),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.backup_runner.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "snapshots command timed out" in run.error_output
    assert "Backup runner crashed" not in run.error_output

    # Verify backup was not invoked
    assert backup_called is False


# ── background task tracking ──────────────────────────────────────────────────
# The event loop holds only weak references to tasks; a fire-and-forget
# pipeline task with no strong reference can be garbage-collected mid-backup,
# stranding the run row at status=running and locking the job out of every
# future trigger. The trigger functions must therefore dispatch through
# app.core.tasks.create_tracked_task, which keeps a strong reference until
# the task completes.


async def _assert_dispatch_is_tracked(pipeline_patch_target, trigger):
    """Drive a trigger function whose pipeline is parked on an event and
    assert its task sits in the strong-reference registry until released.

    The pipeline patch must stay active for the whole task lifetime — the
    background task resolves the pipeline function only after the trigger
    call has already returned.
    """
    from app.core import tasks

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_pipeline(*args, **kwargs):
        started.set()
        await release.wait()

    tasks._background_tasks.clear()
    try:
        with patch(
            pipeline_patch_target,
            new=AsyncMock(side_effect=blocking_pipeline),
        ):
            run_id = await trigger()
            assert run_id is not None
            await asyncio.wait_for(started.wait(), timeout=2.0)

            assert len(tasks._background_tasks) == 1
            tracked = next(iter(tasks._background_tasks))

            release.set()
            await asyncio.wait_for(tracked, timeout=2.0)
            await asyncio.sleep(0)

        assert len(tasks._background_tasks) == 0
    finally:
        release.set()
        tasks._background_tasks.clear()


async def test_trigger_run_pipeline_task_is_tracked(engine):
    from app.db.models import TriggeredBy

    await _setup_job(engine)
    await _assert_dispatch_is_tracked(
        "app.services.backup_runner.run_backup",
        lambda: trigger_run(JOB_ID, TriggeredBy.manual),
    )
    assert JOB_ID not in active_jobs


async def test_trigger_prune_pipeline_task_is_tracked(engine):
    from app.db.models import TriggeredBy

    await _setup_job(engine)
    await _assert_dispatch_is_tracked(
        "app.services.backup_runner.run_prune",
        lambda: trigger_prune(JOB_ID, TriggeredBy.manual),
    )
    assert JOB_ID not in active_jobs


async def test_trigger_check_pipeline_task_is_tracked(engine):
    from app.db.models import TriggeredBy

    await _setup_job(engine)
    await _assert_dispatch_is_tracked(
        "app.services.backup_runner.run_check",
        lambda: trigger_check(JOB_ID, TriggeredBy.manual),
    )
    assert JOB_ID not in active_jobs
