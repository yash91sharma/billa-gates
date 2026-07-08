"""Tests for scheduler startup sequence, job registration, and trigger construction."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker


async def _insert_settings(engine, **overrides):
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        existing = await s.get(AppSettings, 1)
        if existing is None:
            # Build base defaults then apply overrides to avoid duplicate kwargs.
            base = {
                "id": 1,
                "ntfy_server_url": "https://ntfy.sh",
                "ntfy_topic": "",
                "default_job_timeout_hours": 24,
            }
            base.update(overrides)
            settings = AppSettings(**base)
            s.add(settings)
        else:
            for k, v in overrides.items():
                setattr(existing, k, v)
        await s.commit()


async def _insert_job(engine, **overrides) -> str:
    from app.db.models import BackupJob, ScheduleType

    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = str(uuid.uuid4())
    async with factory() as s:
        job = BackupJob(
            id=job_id,
            name=overrides.pop("name", "Test Job"),
            source_label=overrides.pop("source_label", "docs"),
            destination_label=overrides.pop("destination_label", "main"),
            restic_password=overrides.pop("restic_password", "s3cret"),
            schedule_type=overrides.pop("schedule_type", ScheduleType.interval),
            schedule_value=overrides.pop("schedule_value", "6h"),
            enabled=overrides.pop("enabled", True),
            **overrides,
        )
        s.add(job)
        await s.commit()
    return job_id


# ── Startup: seed AppSettings ─────────────────────────────────────────────────


async def test_startup_seeds_app_settings_on_first_boot(engine):
    from app.core.scheduler import start_scheduler
    from app.db.models import AppSettings

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        await start_scheduler()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
    assert settings is not None
    assert settings.ntfy_server_url == "https://ntfy.sh"
    assert settings.default_job_timeout_hours == 24


async def test_startup_seed_is_noop_if_settings_exist(engine):
    from app.core.scheduler import start_scheduler
    from app.db.models import AppSettings

    await _insert_settings(engine, ntfy_topic="existing-topic")

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        await start_scheduler()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
    assert settings is not None
    assert settings.ntfy_topic == "existing-topic"


# ── Startup: restic version detection ────────────────────────────────────────


async def test_startup_detects_restic_version(engine):
    from app.core.scheduler import start_scheduler

    await _insert_settings(engine)

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value="0.17.3"),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        await start_scheduler()

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
    assert settings is not None
    assert settings.restic_version == "0.17.3"


async def test_startup_handles_restic_not_found(engine):
    from app.core.scheduler import start_scheduler

    await _insert_settings(engine)

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        await start_scheduler()

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
    assert settings is not None
    assert settings.restic_version is None


# ── Startup: stale run cleanup ────────────────────────────────────────────────


async def test_startup_stale_cleanup_running_rows(engine):
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _insert_settings(engine)
    job_id = await _insert_job(engine)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=job_id,
            status=RunStatus.running,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    from app.core.scheduler import start_scheduler

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        mock_sched.add_job = MagicMock()
        await start_scheduler()

    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.status == RunStatus.failed
    assert run.reason is not None and run.reason.value == "container_restart"
    assert run.prune_status is not None and run.prune_status.value == "skipped"
    assert run.check_status is not None and run.check_status.value == "skipped"


async def test_startup_stale_cleanup_null_check_status_rows(engine):
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    await _insert_settings(engine)
    job_id = await _insert_job(engine)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=job_id,
            status=RunStatus.success,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            check_status=None,
        )
        s.add(run)
        await s.commit()

    from app.core.scheduler import start_scheduler

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        mock_sched.add_job = MagicMock()
        await start_scheduler()

    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.check_status is not None and run.check_status.value == "skipped"


# ── Startup: job registration ─────────────────────────────────────────────────


async def test_startup_registers_enabled_jobs(engine):
    await _insert_settings(engine)
    job_id = await _insert_job(engine, enabled=True)

    from app.core.scheduler import start_scheduler

    add_job_calls = []

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        mock_sched.add_job = MagicMock(
            side_effect=lambda *a, **kw: add_job_calls.append(kw)
        )
        await start_scheduler()

    assert any(kw.get("id") == job_id for kw in add_job_calls)


async def test_startup_skips_disabled_jobs(engine):
    await _insert_settings(engine)
    job_id = await _insert_job(engine, enabled=False)

    from app.core.scheduler import start_scheduler

    add_job_calls = []

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        mock_sched.start = MagicMock()
        mock_sched.add_job = MagicMock(
            side_effect=lambda *a, **kw: add_job_calls.append(kw)
        )
        await start_scheduler()

    assert not any(kw.get("id") == job_id for kw in add_job_calls)


# ── Trigger construction ──────────────────────────────────────────────────────


def test_interval_trigger_hours():
    from app.core.scheduler import build_trigger

    trigger = build_trigger("interval", "6h")
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.seconds == 6 * 3600


def test_interval_trigger_days():
    from app.core.scheduler import build_trigger

    trigger = build_trigger("interval", "1d")
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.days == 1


def test_interval_trigger_minutes():
    from app.core.scheduler import build_trigger

    trigger = build_trigger("interval", "30m")
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.seconds == 30 * 60


def test_cron_trigger_construction():
    from app.core.scheduler import build_trigger

    trigger = build_trigger("cron", "0 2 * * *")
    assert isinstance(trigger, CronTrigger)


def test_interval_trigger_invalid_raises():
    from app.core.scheduler import build_trigger

    with pytest.raises(ValueError):
        build_trigger("interval", "invalid")


# ── Lifecycle: add/remove/update ──────────────────────────────────────────────


async def test_job_added_to_scheduler_on_enable(client, engine):
    from unittest.mock import patch as mpatch

    with mpatch("os.path.isdir", return_value=True):
        created = (
            await client.post(
                "/api/jobs",
                json={
                    "name": "J",
                    "source_label": "docs",
                    "destination_label": "main",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "6h",
                    "enabled": False,
                },
            )
        ).json()

    add_calls = []
    with mpatch("app.core.scheduler.scheduler") as mock_sched:
        mock_sched.running = True
        mock_sched.add_job = MagicMock(
            side_effect=lambda *a, **kw: add_calls.append(kw)
        )
        mock_sched.remove_job = MagicMock()
        resp = await client.post(f"/api/jobs/{created['id']}/enable")

    assert resp.status_code == 200
    assert any(kw.get("id") == created["id"] for kw in add_calls)


async def test_job_removed_from_scheduler_on_delete(client, engine):
    from unittest.mock import patch as mpatch

    with mpatch("os.path.isdir", return_value=True):
        created = (
            await client.post(
                "/api/jobs",
                json={
                    "name": "J",
                    "source_label": "docs",
                    "destination_label": "main",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "6h",
                },
            )
        ).json()

    remove_calls = []
    with mpatch("app.core.scheduler.scheduler") as mock_sched:
        mock_sched.running = True
        mock_sched.remove_job = MagicMock(
            side_effect=lambda jid, **kw: remove_calls.append(jid)
        )
        resp = await client.delete(f"/api/jobs/{created['id']}")

    assert resp.status_code == 204
    assert created["id"] in remove_calls


# ── Trigger construction: error cases ─────────────────────────────────────────


def test_build_trigger_unknown_schedule_type_raises():
    import pytest

    from app.core.scheduler import build_trigger

    with pytest.raises(ValueError, match="Unknown schedule_type"):
        build_trigger("weekly", "1")


def test_build_trigger_invalid_interval_zero_raises():
    import pytest

    from app.core.scheduler import build_trigger

    with pytest.raises(ValueError):
        build_trigger("interval", "0h")


def test_build_trigger_invalid_interval_negative_raises():
    import pytest

    from app.core.scheduler import build_trigger

    with pytest.raises(ValueError):
        build_trigger("interval", "-6h")


def test_build_trigger_invalid_interval_letters_only_raises():
    import pytest

    from app.core.scheduler import build_trigger

    with pytest.raises(ValueError):
        build_trigger("interval", "h")


def test_cron_trigger_invalid_expression_raises():
    import pytest

    from app.core.scheduler import build_trigger

    with pytest.raises(Exception):
        build_trigger("cron", "not a cron expression")


# ── Startup: scheduler timezone ───────────────────────────────────────────────


async def test_startup_scheduler_started(engine):
    from app.core.scheduler import start_scheduler

    await _insert_settings(engine)

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = False
        start_calls = []
        mock_sched.start = MagicMock(side_effect=lambda **kw: start_calls.append(kw))
        mock_sched.add_job = MagicMock()
        await start_scheduler()

    assert len(start_calls) == 1


async def test_startup_does_not_double_start_if_already_running(engine):
    from app.core.scheduler import start_scheduler

    await _insert_settings(engine)

    with (
        patch("app.core.scheduler.engine", engine),
        patch("app.core.scheduler.scheduler") as mock_sched,
        patch("app.services.restic.restic_version", return_value=None),
    ):
        mock_sched.running = True
        start_calls = []
        mock_sched.start = MagicMock(side_effect=lambda **kw: start_calls.append(kw))
        mock_sched.add_job = MagicMock()
        await start_scheduler()

    assert len(start_calls) == 0


# ── Lifecycle: update triggers reschedule ────────────────────────────────────


async def test_job_rescheduled_when_schedule_changes(client, engine):
    from unittest.mock import patch as mpatch

    with mpatch("os.path.isdir", return_value=True):
        created = (
            await client.post(
                "/api/jobs",
                json={
                    "name": "J",
                    "source_label": "docs",
                    "destination_label": "main",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "6h",
                },
            )
        ).json()

    # PUT now syncs via add_job(replace_existing=True) — it both replaces the
    # trigger of a registered job and registers a job the scheduler doesn't
    # know yet (e.g. one enabled through PUT).
    add_calls = []
    with mpatch("os.path.isdir", return_value=True):
        with mpatch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            await client.put(
                f"/api/jobs/{created['id']}",
                json={
                    "name": "J",
                    "source_label": "docs",
                    "destination_label": "main",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "12h",
                },
            )

    assert any(
        kw.get("id") == created["id"] and kw.get("replace_existing") for kw in add_calls
    )
