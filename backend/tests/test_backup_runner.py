"""Tests for the full backup run lifecycle (Steps 1–12)."""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import CheckStatus, PruneStatus
from app.services import backup_runner as backup_runner_module
from app.services.backup_runner import (
    active_jobs,
    run_backup,
    run_check,
    run_prune,
    trigger_check,
    trigger_prune,
    trigger_run,
)

# conftest's autouse `_mock_backup_runner_mount_check` fixture swaps
# `check_mount_file_exists` on the module for an always-True mock in every
# test. Capture the real implementation here — the module is imported at
# collection time, before any fixture runs — so the sentinel unit tests below
# probe a real filesystem instead of the mock.
REAL_CHECK_MOUNT_FILE_EXISTS = backup_runner_module.check_mount_file_exists

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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_step4_repo_not_found_fails_and_never_inits(engine):
    """restic ≥0.17 returns exit code 10 when the repository does not exist.

    A run must never initialize: that happens once, at job creation
    (repository.ensure_repository). So rc=10 during a run always means the
    repo went missing — the destination was swapped, wiped, or renamed —
    and initializing would start an empty repo while the user believes their
    history is intact.
    """
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

    init = AsyncMock()

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(10, "", "Fatal: unable to open config file"),
        ),
        patch("app.services.restic.restic_init", new=init),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    init.assert_not_awaited()

    async with factory() as s:
        run_row = await s.get(BackupRun, run_id)
        assert run_row.status == RunStatus.failed
        assert "Repository not found" in run_row.error_output


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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "timed out" in (run.error_output or "").lower()


# ── Step 5: rc=3 partial backup → warning ────────────────────────────────────


async def test_step5_backup_rc3_marks_warning_and_skips_retention(engine):
    """restic exit code 3 (partial backup, snapshot still created) must be
    recorded as `warning` — not `failed` — and must NOT run `restic forget`.

    A partial snapshot is missing exactly the files restic could not read. Let
    it count toward the retention policy and it evicts a complete snapshot in
    its place: with `--keep-last 3` and a source that has started throwing read
    errors (a failing disk, a permission change, a file held open over SMB),
    three runs are enough to leave nothing but partial snapshots, and the last
    copy of those files is gone from the repository. The snapshot is still
    written and still visible — only the deletion is withheld, until a backup
    that reads everything succeeds."""
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert forget_called["v"] is False, (
        "forget must not run on rc=3 — an incomplete snapshot must never be "
        "allowed to push a complete one out of the retention policy"
    )
    from app.db.models import PruneStatus

    assert run.prune_status == PruneStatus.skipped
    assert run.prune_error_output is not None, (
        "a skipped retention has to say why, or the operator reads it as "
        "'no policy configured' and never learns the repo is growing"
    )
    assert "partial" in run.prune_error_output.lower()
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is False, "no retention → no forget"
    assert prune_called["v"] is False, (
        "backup must never call restic_prune (gaps.md H1)"
    )
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.prune_status == PruneStatus.skipped


# ── Step 5: rc=3 must name the files, from the stream restic actually uses ────
#
# Captured from restic 0.18.1 and re-verified byte-for-byte against 0.19.1
# (`restic backup --json` over a source containing one unreadable directory),
# stdout and stderr kept separate:
#
#   EXIT=3
#   stdout: {"message_type":"status",...,"error_count":1}
#           {"message_type":"summary",...}
#   stderr: {"message_type":"error","error":{"message":"openfile for
#            readdirnames failed: ... permission denied"},"during":"scan",
#            "item":"/sources/Docs/locked"}
#           {"message_type":"exit_error","code":3,"message":"Warning: at least
#            one source file could not be read"}
#
# Every per-file error is on *stderr*. Parsing only stdout (as this code did)
# produced `failed_items=0` on every real partial backup, so the run page showed
# a bare "Partial backup: some files could not be read." with nothing after it.

RC3_STDERR = (
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/locked: permission denied"},"during":"scan",'
    '"item":"/sources/Docs/locked"}\n'
    '{"message_type":"exit_error","code":3,"message":"Warning: at least one '
    'source file could not be read"}'
)


# The same capture, verbatim, when the unreadable item is a *directory*:
# restic reports it once from the scanner and once from the archiver. One
# folder, two error lines — the count shown to the user must be 1.
RC3_STDERR_DOUBLE_REPORTED = (
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/locked: permission denied"},"during":'
    '"archival","item":"/sources/Docs/locked"}\n'
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/locked: permission denied"},"during":"scan",'
    '"item":"/sources/Docs/locked"}\n'
    '{"message_type":"exit_error","code":3,"message":"Warning: at least one '
    'source file could not be read"}'
)


async def _run_rc3(engine, *, stdout: str, stderr: str, run_id: str) -> None:
    """Drive a full rc=3 backup with the given restic streams."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
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
            return_value=(3, stdout, stderr, BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))


async def test_step5_rc3_names_the_failing_paths_from_stderr(engine):
    """The whole point of the fix: an operator must be able to see *which*
    item failed without re-running the backup by hand."""
    from app.db.models import RunStatus

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc3(
        engine,
        stdout=json.dumps(BACKUP_SUMMARY),
        stderr=RC3_STDERR,
        run_id=run_id,
    )

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.snapshot_id is not None, "rc=3 still wrote a snapshot"
    assert "/sources/Docs/locked" in run.error_output, (
        "the failing path lives on stderr and must reach error_output"
    )
    assert "permission denied" in run.error_output
    assert "scan" in run.error_output, "the phase tells read-failure from scan-failure"
    assert "1 item" in run.error_output, "the headline must carry the real count"


async def test_step5_rc3_counts_one_item_reported_twice_once(engine):
    """An unreadable directory is reported by both the scanner and the
    archiver. Counting the events instead of the items would tell the user two
    things failed when one did — and inflate every count on a real mount."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc3(
        engine,
        stdout=json.dumps(BACKUP_SUMMARY),
        stderr=RC3_STDERR_DOUBLE_REPORTED,
        run_id=run_id,
    )

    run = await _get_run(engine, run_id)
    assert "1 item" in run.error_output, run.error_output
    listed = [ln for ln in run.error_output.splitlines() if ln.startswith("/sources/")]
    assert len(listed) == 1, "one line per failing item, not per error event"
    assert "scan" in listed[0] and "archival" in listed[0], (
        "both phases are still worth showing — they narrow down the cause"
    )


async def test_step5_rc3_still_reads_error_lines_from_stdout(engine):
    """Back-compat: older restic builds (and anything that merges the streams)
    put the error lines on stdout. Both streams are scanned."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc3(
        engine,
        stdout=RC3_STDERR + "\n" + json.dumps(BACKUP_SUMMARY),
        stderr="",
        run_id=run_id,
    )

    run = await _get_run(engine, run_id)
    assert "/sources/Docs/locked" in run.error_output


async def test_step5_rc3_falls_back_to_raw_stderr_when_unparseable(engine):
    """restic can exit 3 having printed something that is not a JSON error line
    (a plain warning, or output from a version that formats differently). The
    operator still gets the text — never a bare sentence."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc3(
        engine,
        stdout=json.dumps(BACKUP_SUMMARY),
        stderr="Warning: at least one source file could not be read",
        run_id=run_id,
    )

    run = await _get_run(engine, run_id)
    assert "at least one source file could not be read" in run.error_output


async def test_step5_rc3_error_output_is_never_uninformative(engine):
    """Regression guard for the exact bug reported: rc=3 with nothing parseable
    on either stream must still explain what the warning means."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc3(engine, stdout=json.dumps(BACKUP_SUMMARY), stderr="", run_id=run_id)

    run = await _get_run(engine, run_id)
    assert run.error_output
    assert "could not be read" in run.error_output
    assert "snapshot was still saved" in run.error_output, (
        "the user must know the snapshot exists despite the warning"
    )


async def test_step5_rc3_failed_item_list_is_capped(engine):
    """A share that denies a million files must not write a million lines into
    the run row — error_output is read on every run-detail fetch.

    Distinct messages on purpose: identical ones are tallied into a single line
    (the test below), so a same-message flood would never reach the cap and this
    would pass without exercising it.
    """
    from app.services.run_output import MAX_REPORTED_FAILED_ITEMS

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    flood = "\n".join(
        json.dumps(
            {
                "message_type": "error",
                "error": {"message": f"device error 0x{i:04x}"},
                "during": "archival",
                "item": f"/sources/Docs/f{i}",
            }
        )
        for i in range(500)
    )
    await _run_rc3(
        engine, stdout=json.dumps(BACKUP_SUMMARY), stderr=flood, run_id=run_id
    )

    run = await _get_run(engine, run_id)
    listed = [ln for ln in run.error_output.splitlines() if ln.startswith("/sources/")]
    assert len(listed) == MAX_REPORTED_FAILED_ITEMS
    assert "more" in run.error_output, "the user must be told the list was truncated"
    assert len(run.error_output) < 20_000


async def test_step5_rc3_one_cause_across_many_paths_is_tallied(engine):
    """The shape a resource limit makes: one message, thousands of paths. Fifty
    copies of a single sentence is not a record of anything — the count is."""
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    flood = "\n".join(
        json.dumps(
            {
                "message_type": "error",
                "error": {"message": "permission denied"},
                "during": "archival",
                "item": f"/sources/Docs/f{i}",
            }
        )
        for i in range(500)
    )
    await _run_rc3(
        engine, stdout=json.dumps(BACKUP_SUMMARY), stderr=flood, run_id=run_id
    )

    run = await _get_run(engine, run_id)
    tallies = [ln for ln in run.error_output.splitlines() if " × " in ln]
    assert len(tallies) == 1, run.error_output
    assert "permission denied" in tallies[0]
    assert "/sources/Docs/f" in tallies[0], "one path, so it can be checked by hand"
    # The headline still counts items, not causes — the operator needs the scale.
    assert "200+ item(s)" in run.error_output


async def test_step5_rc3_notification_names_the_count(engine):
    """The push should say how bad it is, not just that it happened."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
            settings.notify_on_warning = True
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
            return_value=(3, json.dumps(BACKUP_SUMMARY), RC3_STDERR, BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    body = mock_notify.call_args[0][3]
    assert "1 item" in body
    assert "could not be read" in body


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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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


# ── Step 5: rc=0 does not mean restic read everything ────────────────────────
#
# restic's scan pass hands a directory it cannot list to `ScannerError`
# (internal/ui/backup/progress.go), which prints the line below to stderr,
# returns nil, and never touches `error_count`. The archiver walks the tree
# separately, so it can succeed where the scanner failed — and then the process
# exits **0** with a subtree missing from `total_files`/`total_bytes`. That is
# what turned a 40 GB source into `43% · 72/3,086 files · 1.6 GiB/3.7 GiB` on
# the run page, and this branch used to throw the stderr away.

RC0_STDERR_SCAN_ERRORS = (
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/private: permission denied"},"during":"scan",'
    '"item":"/sources/Docs/private"}'
)


async def _run_rc0(engine, *, stderr: str, run_id: str, forget_called: dict) -> list:
    """Drive a full rc=0 backup with the given stderr, capturing ntfy bodies."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        # `_setup_job` leaves the topic empty, and every push is gated on one —
        # without this the notification assertions would pass vacuously.
        settings = await s.get(AppSettings, 1)
        settings.ntfy_topic = "alerts"
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

    pushes: list = []

    async def fake_notify(*args, **kwargs):
        pushes.append((args, kwargs))
        return True

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), stderr, BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    return pushes


async def test_step5_rc0_with_swallowed_scan_errors_is_a_warning(engine):
    """The run that prompted all this went out green. restic said nothing an
    exit code could carry, so the only signal is the stderr this branch was
    discarding."""
    from app.db.models import RunStatus

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    forget_called = {"v": False}
    pushes = await _run_rc0(
        engine,
        stderr=RC0_STDERR_SCAN_ERRORS,
        run_id=run_id,
        forget_called=forget_called,
    )

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.snapshot_id is not None, "rc=0 wrote a snapshot; nothing is lost"
    assert "/sources/Docs/private" in run.error_output, (
        "the path restic could not list is the one actionable thing here"
    )
    assert "scan" in run.error_output
    # The push has to say which kind of warning this is — an operator who reads
    # "files could not be read" goes looking for a partial snapshot.
    assert any("sizing the source" in str(p) for p in pushes)


async def test_step5_rc0_scan_errors_still_apply_retention(engine):
    """The divergence from rc=3, and the reason this does not reuse
    `backup_warning`. A partial snapshot withholds `restic forget` because it is
    missing files. This snapshot is not: the archiver walks the tree itself and
    would have exited 3 had a read failed. Withholding here would grow the
    repository every time the share hiccups during the estimate pass."""
    from app.db.models import PruneStatus

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    forget_called = {"v": False}
    await _run_rc0(
        engine,
        stderr=RC0_STDERR_SCAN_ERRORS,
        run_id=run_id,
        forget_called=forget_called,
    )

    assert forget_called["v"] is True, "a complete snapshot must count for retention"
    run = await _get_run(engine, run_id)
    assert run.prune_status == PruneStatus.passed


async def test_step5_rc0_with_clean_stderr_is_still_a_plain_success(engine):
    """The guard against the obvious over-reach: parsing stderr on every clean
    exit must not turn ordinary runs yellow."""
    from app.db.models import RunStatus

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc0(engine, stderr="", run_id=run_id, forget_called={"v": False})

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert not run.error_output


async def test_step5_rc0_ignores_stderr_that_names_no_failure(engine):
    """restic writes plenty to stderr that is not a `message_type=error` line.
    Only the parsed items may flip a run, or a chatty backend makes every run a
    warning."""
    from app.db.models import RunStatus

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    await _run_rc0(
        engine,
        stderr="using parent snapshot 1a2b3c4d\nsome unstructured diagnostic\n",
        run_id=run_id,
        forget_called={"v": False},
    )

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_step5_a_failed_run_names_the_items_from_stderr(engine):
    """The rc≠0 twin of the rc=3 lesson, and it was never applied here.

    restic writes `message_type=error` lines to **stderr**; this branch parsed
    `stdout`, where they never appear — so on every failed run `json_errors` came
    back empty and the "Per-file errors" section was never rendered. The operator
    got an undeduplicated stderr dump instead of the capped item list.
    """
    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

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

    stderr = (
        '{"message_type":"error","error":{"message":"too many open files in '
        'system"},"during":"scan","item":"/sources/FamilyMedia/thumbs/ab/cd"}\n'
        '{"message_type":"exit_error","code":1,"message":"Fatal: unable to open '
        'repository"}'
    )

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(1, json.dumps({"message_type": "status"}), stderr, None),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "/sources/FamilyMedia/thumbs/ab/cd" in run.error_output, (
        "the failing path is on stderr, which this branch was not parsing"
    )
    assert "Per-file errors" in run.error_output
    # And the recognised-cause note, which is what makes the run actionable.
    assert "ulimit" in run.error_output


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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert forget_called["v"] is True
    assert prune_called["v"] is False, "backup pipeline must never call restic_prune"


async def test_step8_forget_failure_marks_run_warning(engine):
    """A failed `restic forget` must NOT report the run as success.

    forget *is* the retention policy — the only thing that drops old
    snapshots. Its usual failure causes (stale lock, permissions, disk full)
    persist across runs, so it fails every time while the badge, the run list
    and the ntfy push all said "success"; the repo then grows without bound
    until the destination fills months later. `warning` is the honest status:
    the snapshot was written, but the policy did not apply."""
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning, (
        "a failed retention must not be reported as a successful run"
    )
    assert run.prune_status == PruneStatus.failed
    assert run.prune_error_output is not None
    assert run.snapshot_id is not None, "the snapshot itself was still written"


async def test_step8_forget_failure_notification_names_retention(engine):
    """The warning push must say what actually went wrong. The body used to be
    hardcoded to the rc=3 reason ('some files could not be read'), which would
    be plain wrong for a retention failure."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
            settings.notify_on_warning = True
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
        patch(
            "app.services.restic.restic_forget",
            return_value=(1, "", "repository is already locked"),
        ),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    # call_args is the last call — the completion push (the first one is the
    # notify_on_start "Starting backup" message).
    title, body = mock_notify.call_args[0][2], mock_notify.call_args[0][3]
    assert "warning" in title.lower()
    assert "retention" in body.lower()
    assert "could not be read" not in body.lower(), (
        "must not claim a read failure that did not happen"
    )


async def test_step8_partial_backup_push_names_the_withheld_retention(engine):
    """A partial backup has two consequences and the push must name both: files
    were unreadable, *and* retention did not run this time. Reporting only the
    read failure leaves the operator unaware that the repository has stopped
    shrinking; reporting it as a retention *failure* would send them hunting a
    stale lock that isn't there."""
    from app.db.models import (
        AppSettings,
        BackupRun,
        PruneStatus,
        RunStatus,
        TriggeredBy,
    )

    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
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

    forget_called = {"v": False}

    async def fake_forget(*args, **kwargs):
        forget_called["v"] = True
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.prune_status == PruneStatus.skipped
    assert forget_called["v"] is False
    body = mock_notify.call_args[0][3].lower()
    assert "could not be read" in body
    assert "retention" in body
    assert "failed" not in body, (
        "retention was withheld on purpose, not broken — saying 'failed' sends "
        "the operator looking for a stale lock that does not exist"
    )


async def test_step8_partial_backup_without_retention_reports_only_the_read_failure(
    engine,
):
    """With no retention policy there is nothing to withhold, so the rc=3-only
    message must keep its original wording and say nothing about retention."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
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
            return_value=(3, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    body = mock_notify.call_args[0][3].lower()
    assert "could not be read" in body
    assert "retention" not in body


async def test_step8_clean_run_is_still_success(engine):
    """The happy path must stay `success` — no crying wolf."""
    from app.db.models import BackupRun, PruneStatus, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.prune_status == PruneStatus.passed


async def test_step8_clean_backup_still_applies_retention(engine):
    """The withholding is scoped to partial backups and nothing else. Skipping
    `restic forget` on a clean run would stop retention permanently and let the
    repository grow until the destination fills — the failure this whole step
    exists to prevent."""
    from app.db.models import BackupRun, PruneStatus, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert forget_called["v"] is True
    assert run.prune_status == PruneStatus.passed
    assert run.prune_error_output is None


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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification"),
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
    # Repo path is keyed on the job name, not its UUID.
    assert "/destinations/main/Test Job" in check_called["args"][0]
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
        patch("app.services.run_notifications.send_notification"),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert captured["source_path"] == "/sources/documents"


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
        patch("app.services.run_notifications.send_notification"),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
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
    # Repo path is keyed on the job name, not its UUID.
    assert "/destinations/main/Test Job" in prune_called["args"][0]

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
        patch("app.services.run_notifications.send_notification"),
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
            "app.services.run_notifications.send_notification",
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
        patch("app.services.run_notifications.send_notification"),
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
        patch("app.services.run_notifications.send_notification") as mock_notify,
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
        patch("app.services.run_notifications.send_notification") as mock_notify,
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
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))
    elapsed = _time.monotonic() - start

    assert elapsed < 0.9, "run_backup must give up at the probe timeout"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "Mount check failed" in run.error_output
    assert cat_config_called is False


# ── Sentinel lives at the backup source path ─────────────────────────────────
#
# A job backs up the whole mount, so /sources/<label> is the path whose liveness
# has to be proven — and it is the same path handed to `restic backup`. An
# empty mountpoint left behind by a detached drive is a directory like any
# other: restic turns it into a 0-file snapshot, and then `restic forget`
# prunes the real history against it.


def test_sentinel_is_checked_at_the_mount_root(tmp_path):
    """The mount root is the backup source, so that is where the sentinel has
    to be — a directory that merely exists proves nothing."""
    (tmp_path / "documents").mkdir()

    with patch("app.services.backup_runner.SOURCES_ROOT", str(tmp_path)):
        assert REAL_CHECK_MOUNT_FILE_EXISTS("documents") is False
        (tmp_path / "documents" / ".billa_gates_check").touch()
        assert REAL_CHECK_MOUNT_FILE_EXISTS("documents") is True


def test_sentinel_with_missing_mount_directory_is_false(tmp_path):
    """The mount directory itself is gone (unmounted, renamed): no sentinel,
    no run — and no exception out of the probe."""
    with patch("app.services.backup_runner.SOURCES_ROOT", str(tmp_path)):
        assert REAL_CHECK_MOUNT_FILE_EXISTS("gone") is False


async def test_run_backup_probes_sentinel_at_the_mount_root_and_aborts(engine):
    """run_backup must probe the sentinel at the job's source mount and abort
    before restic when it is missing. The error has to name the path that was
    actually checked."""
    from app.db.models import AppSettings, BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, source_label="documents")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
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

    probe_calls: list[tuple[str, ...]] = []
    restic_called = False

    def sentinel(*args: str) -> bool:
        probe_calls.append(args)
        return False  # drive detached: the mountpoint is there, the file is not

    async def fake_cat_config(*args, **kwargs):
        nonlocal restic_called
        restic_called = True
        return (0, "{}", "")

    async def fake_backup(*args, **kwargs):
        nonlocal restic_called
        restic_called = True
        return (0, "{}", "", None)

    with (
        patch(
            "app.services.backup_runner.check_mount_file_exists",
            side_effect=sentinel,
        ),
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert probe_calls == [("documents",)], (
        "the sentinel must be probed at the source mount, with the label alone"
    )
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert "Mount check failed" in run.error_output
    assert ".billa_gates_check" in run.error_output
    assert "/sources/documents" in run.error_output
    assert restic_called is False
    assert mock_notify.call_count == 1
    assert "Mount check failed" in mock_notify.call_args[0][3]


async def test_run_backup_backs_up_the_path_it_verified(engine):
    """The mirror case: sentinel present → the run goes ahead, and restic backs
    up byte-for-byte the path the probe proved live."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine, source_label="documents")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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

    backed_up_paths: list[str] = []

    def sentinel(*args: str) -> bool:
        return args == ("documents",)

    async def fake_backup(repo_path, password, source_path, *args, **kwargs):
        backed_up_paths.append(source_path)
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch(
            "app.services.backup_runner.check_mount_file_exists",
            side_effect=sentinel,
        ),
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert backed_up_paths == ["/sources/documents"], (
        "restic must back up the same path the sentinel check verified"
    )


async def test_run_backup_persists_progress_snapshots_while_running(engine):
    """The runner must hand restic_backup a sink that writes each bounded
    output snapshot to the run row *during* the backup. Until this existed,
    backup_output was written only after restic exited, so RunDetail's poll had
    nothing to show for the whole (possibly multi-hour) run."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)

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

    mid_run_output: list[str | None] = []

    async def fake_backup(*args, on_output=None, **kwargs):
        assert on_output is not None, "runner must pass an output sink"
        # Simulate restic emitting progress, then read the row back the way
        # the API would while the run is still in flight.
        await on_output("progress: 62% · 41203/68900 files")
        async with factory() as s:
            row = await s.get(BackupRun, run_id)
            mid_run_output.append(row.backup_output if row else None)
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert mid_run_output == ["progress: 62% · 41203/68900 files"], (
        "the snapshot must be visible in the run row before the backup returns"
    )
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.backup_output is not None
    assert "summary" in run.backup_output, "final output still wins at the end"


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
        patch("app.services.run_notifications.send_notification"),
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


# ── Step 4: repo-not-found guard against silent re-initialization ────────────
# rc=10 ("repository does not exist") is only a legitimate first-run signal
# when the job has never written to its repo. Once the job has history (a
# success/warning run, or any run that recorded a snapshot_id), a missing
# repo means the destination was swapped, wiped, or renamed without moving
# the data — re-initializing would silently start an empty repo while the
# user believes their history is intact. The runner must fail loudly instead.


async def _add_finished_run(engine, status, snapshot_id=None):
    """Insert a finished prior run row for JOB_ID."""
    from app.db.models import BackupRun, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(
            BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(JOB_ID),
                status=status,
                triggered_by=TriggeredBy.manual,
                started_at=now,
                finished_at=now,
                snapshot_id=snapshot_id,
            )
        )
        await s.commit()


async def _run_repo_missing_pipeline(engine, run_id):
    """Drive run_backup with cat_config→rc=10 and spies on init/backup.

    Returns (init_called, backup_called) flags."""
    init_called = {"v": False}
    backup_called = {"v": False}

    async def fake_init(*args, **kwargs):
        init_called["v"] = True
        return (0, "created repo", "")

    async def fake_backup(*args, **kwargs):
        backup_called["v"] = True
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(10, "", "Fatal: unable to open config file"),
        ),
        patch("app.services.restic.restic_init", side_effect=fake_init),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    return init_called["v"], backup_called["v"]


async def _make_running_row(engine):
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
    return run_id


async def test_step4_repo_missing_with_success_history_refuses_init(engine):
    """A job with a prior successful run must NOT re-init a missing repo:
    the run fails with an explanatory message and no restic write happens."""
    from app.db.models import RunStatus

    await _setup_job(engine)
    await _add_finished_run(engine, RunStatus.success, snapshot_id="a" * 64)
    run_id = await _make_running_row(engine)

    init_called, backup_called = await _run_repo_missing_pipeline(engine, run_id)

    assert init_called is False
    assert backup_called is False
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.prune_status == PruneStatus.skipped
    assert run.check_status == CheckStatus.skipped
    assert run.error_output is not None
    assert "not found" in run.error_output.lower()
    assert "refus" in run.error_output.lower()  # "refusing to initialize"
    # The message must point at the expected repo path so the operator can act.
    assert "/destinations/main/Test Job" in run.error_output


async def test_step4_repo_missing_without_any_history_also_refuses_init(engine):
    """Run history is no longer an input to this decision.

    A repo is created once, when the job is created, so rc=10 means it went
    missing no matter what the run history looks like. This replaces the old
    "prior failed runs mean this is a genuine first run, so init" branch —
    that branch was what allowed an empty repo to be silently created over a
    destination that had merely gone walkabout.
    """
    from app.db.models import RunStatus

    await _setup_job(engine)
    await _add_finished_run(engine, RunStatus.failed)
    run_id = await _make_running_row(engine)

    init_called, backup_called = await _run_repo_missing_pipeline(engine, run_id)

    assert init_called is False
    assert backup_called is False
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed


async def test_step4_repo_missing_with_no_runs_at_all_refuses_init(engine):
    """Not even a brand-new job may init from a run."""
    from app.db.models import RunStatus

    await _setup_job(engine)
    run_id = await _make_running_row(engine)

    init_called, backup_called = await _run_repo_missing_pipeline(engine, run_id)

    assert init_called is False
    assert backup_called is False
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed


async def test_step4_repo_missing_with_history_sends_failure_notification(engine):
    """The refused re-init must notify the operator when notify_on_failure
    is enabled — this is exactly the 'my backups silently stopped being
    real' scenario notifications exist for."""
    from app.db.models import AppSettings, RunStatus

    await _setup_job(engine)
    await _add_finished_run(engine, RunStatus.success, snapshot_id="c" * 64)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        settings.ntfy_topic = "alerts"
        await s.commit()

    run_id = await _make_running_row(engine)

    notify_called = {"v": False}

    async def fake_notify(url, topic, title, message, **kwargs):
        if "failed" in title.lower() and topic == "alerts":
            notify_called["v"] = True

    with (
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(10, "", "Fatal: unable to open config file"),
        ),
        patch("app.services.restic.restic_init", return_value=(0, "", "")),
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert notify_called["v"] is True
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "refus" in (run.error_output or "").lower()


# ── P0-2: destination sentinel gate on prune / check runs ─────────────────────


async def test_run_prune_destination_sentinel_missing_fails_and_notifies(engine):
    """A prune run must verify the destination `.billa_gates_check` sentinel
    before touching restic. If the backup drive is detached (mountpoint present
    but sentinel gone), the run is failed with a clear error naming the missing
    sentinel, notify_on_failure fires, and `restic prune` is never invoked."""
    from app.db.models import AppSettings, BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine, destination_label="main")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
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

    prune_called = {"v": False}

    async def fake_prune(*args, **kwargs):
        prune_called["v"] = True
        return (0, "", "")

    with (
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.restic.restic_prune", side_effect=fake_prune),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert ".billa_gates_check" in run.error_output
    assert "main" in run.error_output
    assert prune_called["v"] is False

    assert mock_notify.call_count == 1
    assert "failed" in mock_notify.call_args[0][2].lower()
    assert ".billa_gates_check" in mock_notify.call_args[0][3]


async def test_run_prune_ignores_missing_source_sentinel(engine):
    """Prune never reads /sources, so a missing source sentinel must not block
    it — only the destination sentinel gates a prune run."""
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

    with (
        patch(
            "app.services.backup_runner.check_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


async def test_run_check_destination_sentinel_missing_fails_and_notifies(engine):
    """A check run must verify the destination sentinel before running restic
    check. A detached backup drive fails the run with a clear error naming the
    missing sentinel, fires notify_on_failure, and never invokes restic."""
    from app.db.models import AppSettings, BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine, destination_label="main")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())

    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings:
            settings.ntfy_topic = "alerts"
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

    check_called = {"v": False}

    async def fake_check(*args, **kwargs):
        check_called["v"] = True
        return (0, "", "")

    with (
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.restic.restic_check", side_effect=fake_check),
        patch("app.services.run_notifications.send_notification") as mock_notify,
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.error_output is not None
    assert ".billa_gates_check" in run.error_output
    assert "main" in run.error_output
    assert check_called["v"] is False

    assert mock_notify.call_count == 1
    assert "failed" in mock_notify.call_args[0][2].lower()
    assert ".billa_gates_check" in mock_notify.call_args[0][3]


async def test_run_check_ignores_missing_source_sentinel(engine):
    """Check never reads /sources, so a missing source sentinel must not block
    it — only the destination sentinel gates a check run."""
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

    with (
        patch(
            "app.services.backup_runner.check_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.restic.restic_check", return_value=(0, "", "")),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success


# ── Exit-code classification, exhaustively ────────────────────────────────────
#
# The pipeline branches on restic's exit code and nothing else — stderr wording
# is not a contract (gaps.md H5). Two of these codes changed meaning in restic
# 0.19.0, so each one gets an explicit test of the status it produces:
#
#   0    success
#   3    partial backup — snapshot written, so `warning`, and retention still runs
#   10   repo not found      → failed, never re-init (the destination was swapped)
#   11   lock failed         → unlock, retry once
#   12   wrong password      → failed
#   130  killed by signal    → failed (was 1 before 0.19.0; SIGTERM now also 130)
#   -1   wrapper-level launch failure / timeout → failed


async def _run_with_backup_rc(engine, rc, *, stdout="", stderr="", summary=None):
    """Drive one run whose `restic backup` exits with `rc`, returning the row."""
    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

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
            return_value=(rc, stdout, stderr, summary),
        ),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    return await _get_run(engine, run_id)


async def test_backup_rc130_marks_the_run_failed(engine):
    """restic 0.19.0 returns 130 when killed by SIGINT *or* SIGTERM (previously
    1). Both land in the generic failure branch — the important part is that 130
    is never mistaken for a completed backup, because `_terminate_then_kill`
    sends SIGTERM and a killed backup has no valid summary to report stats from.
    """
    run = await _run_with_backup_rc(
        engine, 130, stderr='{"message_type":"exit_error","code":130}'
    )

    from app.db.models import RunStatus

    assert run.status == RunStatus.failed
    assert "130" in (run.error_output or ""), "the exit code must reach the operator"
    assert run.snapshot_id is None
    assert run.files_new is None, "no stats may be written from a killed process"


@pytest.mark.parametrize("rc", (1, 2, 130, 137, -1))
async def test_backup_nonzero_codes_other_than_three_all_fail(engine, rc):
    """Anything that is not 0 or 3 is a failure, and what caused it reaches the
    operator. This is what keeps a future restic exit code from being silently
    absorbed into `success`.

    What "reaches the operator" means differs for -1, and that is the point:
    every real code is a fact about what restic decided, so the number is the
    thing to surface. -1 is this app's marker for restic never having decided
    anything, so the *reason* is — and printing it as an exit code names a code
    restic has never returned."""
    from app.db.models import CheckStatus, PruneStatus, RunStatus
    from app.services.run_output import NO_EXIT_CODE

    run = await _run_with_backup_rc(engine, rc, stderr="something went wrong")

    assert run.status == RunStatus.failed
    if rc == NO_EXIT_CODE:
        assert "something went wrong" in (run.error_output or "")
        assert "exit code" not in (run.error_output or "")
    else:
        assert str(rc) in (run.error_output or "")
    assert run.prune_status == PruneStatus.skipped, "retention must not run"
    assert run.check_status == CheckStatus.skipped


async def test_forget_rc3_marks_retention_failed(engine):
    """restic 0.19.0 returns 3 when `forget` fails to remove one or more
    snapshots — it returned 0 before, which meant a retention failure was
    invisible. The runner treats any non-zero rc as a failed policy, so 3 now
    surfaces as a `warning` run with the Retention column showing failed.
    """
    await _setup_job(engine, retain_keep_last=7)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

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
        patch(
            "app.services.restic.restic_forget",
            return_value=(3, "", "unable to remove snapshot abc123: stale lock"),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.prune_status == PruneStatus.failed
    assert "stale lock" in (run.prune_error_output or "")
    assert run.snapshot_id is not None, "the snapshot itself was still written"


# ── The single-source invariant ───────────────────────────────────────────────


async def test_backup_is_invoked_with_exactly_one_source_path(engine):
    """restic 0.19.0 changed a missing backup source path from exit 0 to exit 3.
    That only affects invocations with *several* sources: with one source, a
    missing path is still a plain fatal (exit 1) and the run fails, rather than
    becoming a `warning` whose retention step then runs against a repo that
    received no new snapshot.

    This test pins the property that makes the change harmless. If a job ever
    grows multiple source paths, rc=3 handling has to be revisited first —
    `backup_success = True` on rc=3 would then be reachable with nothing backed
    up.
    """
    await _setup_job(engine, source_label="reports")
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    from app.db.models import BackupRun, RunStatus, TriggeredBy

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

    captured = {}

    async def fake_backup(repo_path, password, source_path, timeout_seconds, **kwargs):
        captured["source_path"] = source_path
        captured["kwargs"] = kwargs
        return (0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY)

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_forget", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    source_path = captured["source_path"]
    assert isinstance(source_path, str), "one path, not a list of them"
    assert source_path == "/sources/reports"
    # No kwarg smuggles a second path in either.
    assert "source_paths" not in captured["kwargs"]


async def test_partial_backup_still_records_stats_from_the_summary(engine):
    """rc=3 is success-with-warnings: the snapshot exists, so its stats must land
    in the run row. A partial backup showing empty stats columns reads as a
    failed run that somehow warned."""
    partial_summary = dict(BACKUP_SUMMARY)
    run = await _run_with_backup_rc(
        engine,
        3,
        stdout=json.dumps(partial_summary),
        stderr=RC3_STDERR,
        summary=partial_summary,
    )

    from app.db.models import RunStatus

    assert run.status == RunStatus.warning
    assert run.snapshot_id == partial_summary["snapshot_id"]
    assert run.files_new == partial_summary["files_new"]
    assert run.data_added_bytes == partial_summary["data_added"]
    assert run.total_bytes_processed == partial_summary["total_bytes_processed"]
    assert "/sources/Docs/locked" in (run.error_output or "")


# ── trigger_check / run_check: the paths the prune and backup twins already have ──


async def test_trigger_check_returns_skipped_when_backup_is_running(engine):
    """A manual verification must not start while a backup holds the repo. All
    three entry points share one per-job lock and `active_jobs` set precisely so
    two restic processes never write to the same repository — `check` takes a
    lock too, so overlapping it with a backup produces lock conflicts and failed
    runs, not just slowness. trigger_run and trigger_prune have this test; the
    check path did not."""
    from app.db.models import BackupRun, RunKind, RunReason, RunStatus, TriggeredBy

    await _setup_job(engine)

    active_jobs.add(JOB_ID)
    try:
        run_id = await trigger_check(JOB_ID, TriggeredBy.manual)
    finally:
        active_jobs.discard(JOB_ID)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.kind == RunKind.check
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run
    assert run.check_status == CheckStatus.skipped
    assert run.prune_status == PruneStatus.skipped


async def test_trigger_check_returns_skipped_when_a_run_row_is_still_running(engine):
    """The DB row is as authoritative as the in-memory set — after a container
    restart `active_jobs` is empty while a `running` row may still be there."""
    from app.db.models import BackupRun, RunReason, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            BackupRun(
                id=str(uuid.uuid4()),
                job_id=str(JOB_ID),
                status=RunStatus.running,
                triggered_by=TriggeredBy.scheduler,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    run_id = await trigger_check(JOB_ID, TriggeredBy.manual)

    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run.status == RunStatus.skipped
    assert run.reason == RunReason.overlapping_run


async def test_trigger_check_job_not_found_returns_none(engine):
    """Mirrors trigger_run/trigger_prune so the route layer can map it to a 404
    rather than creating an orphan run row."""
    from sqlalchemy import select

    from app.db.models import BackupRun, TriggeredBy

    result = await trigger_check(uuid.uuid4(), TriggeredBy.manual)
    assert result is None

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        rows = (await s.execute(select(BackupRun))).scalars().all()
    assert rows == []


async def _run_check_with(engine, rc, *, notify_on_verification=True):
    """Drive one `run_check` whose restic exits with `rc`; return (row, pushes)."""
    from app.db.models import AppSettings, BackupRun, RunKind, RunStatus, TriggeredBy

    await _setup_job(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        # A push needs a topic: _setup_job leaves it empty, which disables all
        # notifications regardless of the per-event flags.
        settings.ntfy_topic = "alerts"
        settings.notify_on_verification = notify_on_verification
        await s.commit()

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

    pushes: list = []

    async def fake_notify(server_url, topic, title, body, **kwargs):
        pushes.append((title, body))

    with (
        patch("app.services.restic.restic_check", return_value=(rc, "", "boom")),
        patch(
            "app.services.run_notifications.send_notification", side_effect=fake_notify
        ),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    return await _get_run(engine, run_id), pushes


@pytest.mark.parametrize(
    "rc,expected_word", ((0, "passed"), (1, "failed"), (2, "failed"))
)
async def test_run_check_verification_notification_names_the_outcome(
    engine, rc, expected_word
):
    """A verification push that does not say whether the repo passed is useless —
    the operator would have to open the UI to learn what they were told about."""
    _, pushes = await _run_check_with(engine, rc, notify_on_verification=True)

    assert pushes, "notify_on_verification=True must produce a push"
    title, body = pushes[-1]
    assert expected_word in title.lower() or expected_word in body.lower()


async def test_notify_on_verification_false_sends_no_push(engine):
    _, pushes = await _run_check_with(engine, 0, notify_on_verification=False)
    assert pushes == []


async def test_run_check_unhandled_exception_finalizes_run_to_failed(engine):
    """Mirrors the backup and prune crash guards. Without it a crash inside the
    check pipeline leaves the row at `running` forever, which blocks every future
    backup for that job via the overlap check — the job silently stops backing
    up."""
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

    with patch(
        "app.services.restic.restic_check",
        side_effect=RuntimeError("something exploded mid-check"),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed, "a crashed check must not stay running"
    assert run.check_status == CheckStatus.failed
    assert "something exploded mid-check" in (run.check_error_output or "")
    assert run.finished_at is not None
    assert run.duration_seconds is not None
    assert run.prune_status == PruneStatus.skipped


# ── Destination-usage cache invalidation ─────────────────────────────────────
#
# The Backup Destinations page refreshes "after a job completes" without
# polling, which only works if every terminal path drops the cached capacity for
# the drive the run just wrote to. The call therefore lives in each pipeline's
# `finally` — not beside finalize, where the early returns would skip it.


def _seed_usage_cache(*labels: str) -> None:
    from app.services import destination_usage

    destination_usage._clear_cache()
    for label in labels:
        destination_usage._cache[label] = (
            MagicMock(name=f"measurement-{label}"),
            9e9,
        )


async def test_a_completed_backup_invalidates_the_destination_usage_cache(engine):
    """A backup just wrote to the drive, so the cached free-space figure is
    stale the moment the run ends."""
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert "main" not in destination_usage._cache


async def test_a_canceled_backup_still_invalidates_the_destination_usage_cache(engine):
    """The Stop path returns from inside the try, skipping everything beside
    finalize — a canceled run has usually already written data, so the figure
    is stale all the same."""
    from app.db.models import RunStatus
    from app.services import destination_usage, process_registry

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")
    process_registry.mark_canceled(uuid.UUID(run_id))

    try:
        with patch("app.services.run_notifications.send_notification"):
            await run_backup(JOB_ID, uuid.UUID(run_id))
    finally:
        process_registry.clear_canceled(uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.canceled
    assert "main" not in destination_usage._cache


async def test_a_run_that_fails_the_destination_sentinel_still_invalidates(engine):
    """Another early return from inside the try."""
    from app.db.models import RunStatus
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=False,
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "main" not in destination_usage._cache


async def test_a_crashing_backup_still_invalidates_the_destination_usage_cache(engine):
    """The crash handler is in `except`; the invalidation has to be in
    `finally` to survive it."""
    from app.db.models import RunStatus
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch(
            "app.services.restic.restic_cat_config",
            side_effect=RuntimeError("boom"),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert "main" not in destination_usage._cache


async def test_prune_invalidates_the_destination_usage_cache(engine):
    """Freeing space is the entire purpose of prune: a capacity figure that
    doesn't move after the click reads as "prune did nothing"."""
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    assert "main" not in destination_usage._cache


async def test_a_failed_prune_still_invalidates_the_destination_usage_cache(engine):
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch("app.services.restic.restic_prune", return_value=(1, "", "failed")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    assert "main" not in destination_usage._cache


async def test_an_integrity_check_invalidates_the_destination_usage_cache(engine):
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main")

    with (
        patch("app.services.restic.restic_check", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    assert "main" not in destination_usage._cache


async def test_invalidation_is_scoped_to_the_jobs_own_destination(engine):
    """A global clear would throw away a good measurement of a hung share this
    run never touched, making the next page load eat a probe timeout."""
    from app.services import destination_usage

    await _setup_job(engine)
    run_id = await _make_running_row(engine)
    _seed_usage_cache("main", "offsite")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert "main" not in destination_usage._cache
    assert "offsite" in destination_usage._cache, (
        "another drive's measurement must survive a run that never touched it"
    )


# ── The destination sentinel ─────────────────────────────────────────────────
#
# conftest's autouse fixture mocks `check_destination_mount_file_exists` to
# True in every test, so the real implementation was never executed. It is the
# half of the sentinel pair that guards the *write* side: a destination whose
# drive has detached leaves an empty mountpoint behind, and restic would
# happily init a fresh repository into it — an empty history that looks
# healthy, on a job whose real snapshots are on a disk that is not plugged in.
#
# Captured at module import, before any fixture can swap it out.
REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS = (
    backup_runner_module.check_destination_mount_file_exists
)


def test_destination_sentinel_is_checked_at_the_destination_mount_root(tmp_path):
    """`/destinations/<label>/.billa_gates_check` — the root of the drive.

    Deliberately not inside the repository directory: the point is to prove
    the *drive* is attached, and the repo directory is created by the app.
    """
    (tmp_path / "main").mkdir()

    with patch("app.services.backup_runner.DESTINATIONS_ROOT", str(tmp_path)):
        assert REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS("main") is False
        (tmp_path / "main" / ".billa_gates_check").touch()
        assert REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS("main") is True


def test_destination_sentinel_with_a_missing_directory_is_false(tmp_path):
    """A detached drive whose mountpoint is gone: False, not an exception."""
    with patch("app.services.backup_runner.DESTINATIONS_ROOT", str(tmp_path)):
        assert REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS("gone") is False


def test_destination_sentinel_does_not_accept_a_directory_as_the_marker(tmp_path):
    """`os.path.exists` is true for a directory too.

    Pinned because the marker is created by hand by the operator, and a
    `mkdir .billa_gates_check` typo would otherwise silently pass the guard.
    This documents the current behaviour: a directory *does* satisfy it.
    """
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / ".billa_gates_check").mkdir()

    with patch("app.services.backup_runner.DESTINATIONS_ROOT", str(tmp_path)):
        assert REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS("main") is True


def test_the_two_sentinels_probe_different_roots(tmp_path):
    """A source sentinel must never satisfy the destination check.

    They are separate mounts; conflating them would let a job back up to a
    detached drive as long as its *source* was attached.
    """
    (tmp_path / "sources" / "documents").mkdir(parents=True)
    (tmp_path / "destinations" / "documents").mkdir(parents=True)
    (tmp_path / "sources" / "documents" / ".billa_gates_check").touch()

    with (
        patch("app.services.backup_runner.SOURCES_ROOT", str(tmp_path / "sources")),
        patch(
            "app.services.backup_runner.DESTINATIONS_ROOT",
            str(tmp_path / "destinations"),
        ),
    ):
        assert REAL_CHECK_MOUNT_FILE_EXISTS("documents") is True
        assert REAL_CHECK_DESTINATION_MOUNT_FILE_EXISTS("documents") is False


# ── A pipeline whose job row has gone ────────────────────────────────────────
#
# Reachable in normal use: the job is deleted while a trigger is in flight, or
# a scheduler tick fires against a job removed since the last rebuild. Every
# pipeline loads its job row first and returns quietly when it is missing. The
# property that matters is that it returns *before* spawning restic — a
# pipeline that ran on a deleted job would write into a repository nothing
# owns any more, and it has no row to report the outcome on.


@pytest.mark.parametrize(
    "pipeline,args",
    [
        ("run_backup", ()),
        ("run_prune", ()),
        ("run_check", ("full", None, None)),
    ],
)
async def test_a_pipeline_returns_quietly_when_the_job_row_is_missing(
    engine, pipeline, args
):
    missing_job = uuid.uuid4()
    run_id = uuid.uuid4()
    fn = getattr(backup_runner_module, pipeline)

    with patch("asyncio.create_subprocess_exec") as spawn:
        # No exception: the caller is a fire-and-forget task, so anything
        # raised here surfaces only as an unretrieved-exception warning.
        result = await fn(missing_job, run_id, *args)

    assert result is None
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "pipeline,args",
    [
        ("run_backup", ()),
        ("run_prune", ()),
        ("run_check", ("full", None, None)),
    ],
)
async def test_a_missing_job_row_sends_no_notification(engine, pipeline, args):
    """There is nothing to report and nobody to attribute it to."""
    fn = getattr(backup_runner_module, pipeline)

    with (
        patch("asyncio.create_subprocess_exec"),
        patch("app.services.run_notifications.send_notification") as notify,
    ):
        await fn(uuid.uuid4(), uuid.uuid4(), *args)

    notify.assert_not_called()


async def test_a_missing_job_row_creates_no_run_record(engine):
    """The pipeline must not invent a row for a job that no longer exists."""
    from app.db.models import BackupRun

    with patch("asyncio.create_subprocess_exec"):
        await run_backup(uuid.uuid4(), uuid.uuid4())

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(BackupRun))).scalars().all()

    assert rows == []


# ── The auto-unlock attempt is best-effort ───────────────────────────────────


async def test_step4_a_raising_unlock_does_not_abort_the_run(engine):
    """`restic unlock` throwing must not take the run down with it.

    The unlock is an opportunistic recovery step wedged between two
    `cat config` calls, and it is wrapped for a reason: a raise here escapes
    before the run row is finalized, leaving it at `status=running` forever —
    and a row stuck at running locks the job out of *every* future trigger
    (`run_dispatch`'s overlap check refuses on it). So one transient unlock
    failure would silently end all future backups for that job.

    The retry must still happen: the lock may have been released meanwhile,
    which is exactly the case this recovers.
    """
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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

    cat_calls = {"n": 0}

    async def fake_cat_config(*args, **kwargs):
        cat_calls["n"] += 1
        if cat_calls["n"] == 1:
            return (11, "", "Fatal: unable to create lock in backend: already locked")
        return (0, '{"version":2}', "")

    with (
        patch("app.services.restic.restic_cat_config", side_effect=fake_cat_config),
        patch(
            "app.services.restic.restic_unlock",
            side_effect=OSError("transport endpoint is not connected"),
        ),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, json.dumps(BACKUP_SUMMARY), "", BACKUP_SUMMARY),
        ),
        patch("app.services.restic.restic_prune", return_value=(0, "", "")),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    assert cat_calls["n"] == 2, "the retry must still happen after a failed unlock"
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.success
    assert run.finished_at is not None, "the row must never be left at running"


async def test_step4_a_raising_unlock_still_fails_the_run_if_the_lock_persists(engine):
    """When the retry also finds the lock, it is a normal failure — not a crash.

    The distinction matters for what the operator is told: "repository is
    locked and could not be unlocked" points at the Unlock button, whereas an
    unhandled OSError from the unlock attempt would surface as an unrelated
    transport error.
    """
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(11, "", "unable to create lock in backend: already locked"),
        ),
        patch(
            "app.services.restic.restic_unlock",
            side_effect=RuntimeError("unlock blew up"),
        ),
        patch("app.services.restic.restic_backup") as backup,
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    backup.assert_not_called()
    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.finished_at is not None
    assert "locked" in (run.error_output or "").lower()


# ── Saying which kind of failure this was ────────────────────────────────────
#
# Every restic wrapper reports "restic produced no exit code" as rc=-1 — the
# process was stopped at its deadline, or it never started. Both reach the same
# branches as a real non-zero exit, and a pipeline that prints that sentinel as
# "restic exit code -1" describes a code restic does not have, on precisely the
# runs where the operator has nothing else to go on.


async def _make_run_row(engine, kind, run_id):
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(JOB_ID),
                kind=kind,
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()


async def test_a_prune_that_never_started_is_not_given_an_exit_code(engine):
    """`restic could not be started` and `restic exited 1` need different
    actions — check the image versus check the drive — and today both arrive as
    a bare stderr string with a sentinel code in front of it."""
    from app.db.models import RunKind, RunStatus

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    await _make_run_row(engine, RunKind.prune, run_id)

    with patch(
        "app.services.restic.restic_prune",
        return_value=(
            -1,
            "",
            "restic could not be started: FileNotFoundError: No such file",
        ),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    output = run.prune_error_output or ""
    assert "could not be started" in output
    assert "exit code -1" not in output
    assert "Prune" in output, "the operator has to know which step this was"


async def test_a_prune_failure_names_the_code_and_keeps_restics_words(engine):
    """The number is what gets searched for; restic's own line is what says
    what happened. Neither may be dropped."""
    from app.db.models import RunKind

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    await _make_run_row(engine, RunKind.prune, run_id)

    with patch(
        "app.services.restic.restic_prune",
        return_value=(11, "", "unable to create lock: repository is already locked"),
    ):
        await run_prune(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    output = run.prune_error_output or ""
    assert "exit code 11" in output
    assert "locked" in output
    assert "unable to create lock" in output


async def test_a_prune_failure_logs_which_kind_it_was(engine, caplog):
    """The row is for the operator; the log line is for whoever is grepping a
    container log across many runs. `rc=-1` alone is not greppable as a cause."""
    import logging

    from app.db.models import RunKind

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    await _make_run_row(engine, RunKind.prune, run_id)

    with caplog.at_level(logging.WARNING):
        with patch(
            "app.services.restic.restic_prune",
            return_value=(-1, "", "prune timed out after 24h 0m"),
        ):
            await run_prune(JOB_ID, uuid.UUID(run_id))

    assert "reason=no_exit_code" in caplog.text


async def test_a_check_that_timed_out_reports_the_timeout(engine):
    """A verification stopped at its deadline is not a corrupt repository, and
    the run page is the only place that distinction is made."""
    from app.db.models import CheckStatus, RunKind, RunStatus

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    await _make_run_row(engine, RunKind.check, run_id)

    with patch(
        "app.services.restic.restic_check",
        return_value=(-1, "", "check timed out after 24h 0m"),
    ):
        await run_check(JOB_ID, uuid.UUID(run_id), "structural", None, None)

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    assert run.check_status == CheckStatus.failed
    output = run.check_error_output or ""
    assert "timed out after 24h 0m" in output
    assert "exit code -1" not in output


async def test_a_backup_that_never_produced_an_exit_code_is_not_given_one(engine):
    """The production path for a backup timeout: the wrapper contains the
    deadline and reports rc=-1, so the runner's own asyncio.TimeoutError branch
    never sees it and the generic branch is what writes the row."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_latest_snapshot_id", return_value=None),
        patch(
            "app.services.restic.restic_backup",
            return_value=(-1, "", "backup timed out after 24h 0m", None),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    output = run.error_output or ""
    assert "timed out after 24h 0m" in output
    assert "exit code -1" not in output


async def test_the_repository_check_does_not_invent_an_exit_code(engine):
    """rc=-1 from `cat config` means the destination never answered. Reporting
    it as "restic exit code -1" reads as a repository fault."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _setup_job(engine)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
        patch(
            "app.services.restic.restic_cat_config",
            return_value=(-1, "", "cat config timed out after 10m"),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.failed
    output = run.error_output or ""
    assert "cat config timed out after 10m" in output
    assert "exit code -1" not in output


async def test_a_failed_retention_step_names_itself(engine):
    """`restic forget` failing is reported in prune_error_output, the same
    column a prune run uses — so the text has to say which of the two it was."""
    from app.db.models import BackupRun, PruneStatus, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_latest_snapshot_id", return_value=None),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, "", "", {"snapshot_id": "abc123"}),
        ),
        patch(
            "app.services.restic.restic_forget",
            return_value=(1, "", "Fatal: unable to open config file"),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.prune_status == PruneStatus.failed
    output = run.prune_error_output or ""
    assert "Retention" in output
    assert "exit code 1" in output
    assert "unable to open config file" in output


async def test_a_retention_that_could_not_remove_snapshots_says_so(engine):
    """restic 0.19 returns 3 from `forget` when it fails to remove one or more
    snapshots — a different fact from the 3 `backup` returns, which means source
    data could not be read.

    This is the call site that decides which of the two is printed, so it is
    pinned here rather than only over the formatter: the runner passes the
    restic command alongside the display label, and a run whose retention failed
    must never be headlined with a sentence about unreadable files and a saved
    snapshot. That reads as "your data is fine, your source is flaky" while what
    actually happened is that the repository can no longer be pruned.
    """
    from app.db.models import BackupRun, PruneStatus, RunStatus, TriggeredBy

    await _setup_job(engine, retain_keep_last=5)
    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
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
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_latest_snapshot_id", return_value=None),
        patch(
            "app.services.restic.restic_backup",
            return_value=(0, "", "", {"snapshot_id": "abc123"}),
        ),
        patch(
            "app.services.restic.restic_forget",
            # Recorded from restic 0.19.1 with an unwritable snapshots/ dir.
            return_value=(
                3,
                "",
                "unable to remove snapshot/ee5d18d2 from the repository\n"
                "failed to remove one or more snapshots",
            ),
        ),
        patch("app.services.run_notifications.send_notification"),
    ):
        await run_backup(JOB_ID, uuid.UUID(run_id))

    run = await _get_run(engine, run_id)
    assert run.status == RunStatus.warning
    assert run.prune_status == PruneStatus.failed
    output = run.prune_error_output or ""
    assert "exit code 3" in output
    assert "snapshots could not be removed" in output
    assert "failed to remove one or more snapshots" in output
    assert "some files could not be read" not in output, (
        "that is what exit 3 means to `backup`, not to `forget`"
    )
    assert "snapshot was still written" not in output
