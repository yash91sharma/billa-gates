"""Tests for the user-initiated cancel-a-running-backup feature.

Covers four areas, end-to-end:

1. The ``canceled`` / ``user_canceled`` enum values are accepted by the ORM.
2. The ``process_registry`` module tracks subprocess handles per run, can
   mark a run as canceled, and terminates the underlying process via
   ``_terminate_then_kill``.
3. ``backup_runner.run_backup`` honors the canceled flag — short-circuits
   the rest of the pipeline, writes ``status=canceled, reason=user_canceled``,
   and sends a canceled notification.
4. ``POST /api/runs/{id}/cancel`` returns the right status codes for the
   404 / 409 / 202 paths and invokes the registry.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

# ── 1. Enum values are accepted by the ORM ────────────────────────────────────


async def test_run_status_canceled_value_exists():
    from app.db.models import RunStatus

    assert RunStatus.canceled.value == "canceled"


async def test_run_reason_user_canceled_value_exists():
    from app.db.models import RunReason

    assert RunReason.user_canceled.value == "user_canceled"


async def test_backup_run_row_accepts_canceled_status(client, engine):
    """A BackupRun with status=canceled / reason=user_canceled persists."""
    from app.db.models import BackupRun, RunReason, RunStatus, TriggeredBy

    # Create a job via the API so foreign keys line up.
    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Cancel Test",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    job_id = resp.json()["id"]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=job_id,
            status=RunStatus.canceled,
            reason=RunReason.user_canceled,
            started_at=now,
            finished_at=now,
            triggered_by=TriggeredBy.manual,
        )
        s.add(run)
        await s.commit()

    async with factory() as s:
        loaded = await s.get(BackupRun, run_id)
        assert loaded is not None
        assert loaded.status == RunStatus.canceled
        assert loaded.reason == RunReason.user_canceled


# ── 2. Process registry ───────────────────────────────────────────────────────


def _make_proc(returncode: int = 0) -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


def _fresh_registry():
    """Import a fresh registry module reference and clear its state.

    The registry is module-level state; tests must reset it to stay isolated.
    """
    from app.services import process_registry

    process_registry._processes.clear()
    process_registry._canceled.clear()
    return process_registry


async def test_registry_register_and_get():
    registry = _fresh_registry()
    run_id = uuid.uuid4()
    proc = _make_proc()
    registry.register(run_id, proc)
    assert registry.get(run_id) is proc


async def test_registry_unregister_removes_handle():
    registry = _fresh_registry()
    run_id = uuid.uuid4()
    proc = _make_proc()
    registry.register(run_id, proc)
    registry.unregister(run_id)
    assert registry.get(run_id) is None


async def test_registry_unregister_unknown_run_is_noop():
    registry = _fresh_registry()
    # Should not raise.
    registry.unregister(uuid.uuid4())


async def test_registry_mark_and_is_canceled():
    registry = _fresh_registry()
    run_id = uuid.uuid4()
    assert registry.is_canceled(run_id) is False
    registry.mark_canceled(run_id)
    assert registry.is_canceled(run_id) is True


async def test_registry_clear_canceled_removes_flag():
    registry = _fresh_registry()
    run_id = uuid.uuid4()
    registry.mark_canceled(run_id)
    registry.clear_canceled(run_id)
    assert registry.is_canceled(run_id) is False


async def test_registry_terminate_sends_sigterm_then_returns():
    registry = _fresh_registry()
    run_id = uuid.uuid4()
    proc = _make_proc(returncode=-15)
    registry.register(run_id, proc)
    await registry.terminate(run_id)
    proc.terminate.assert_called_once()


async def test_registry_terminate_unknown_run_is_noop():
    registry = _fresh_registry()
    # Must not raise when no process is registered for the run.
    await registry.terminate(uuid.uuid4())


# ── 3. Cancel flow in run_backup ──────────────────────────────────────────────


async def _create_job_row(engine, **overrides: Any) -> str:
    """Insert a BackupJob directly so we can drive run_backup without going
    through the full API surface (mounts validation, etc)."""
    from app.db.models import BackupJob, ScheduleType

    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = str(uuid.uuid4())
    async with factory() as s:
        job = BackupJob(
            id=job_id,
            name=overrides.pop("name", "Cancel Flow Job"),
            source_label=overrides.pop("source_label", "docs"),
            destination_label=overrides.pop("destination_label", "main"),
            restic_password=overrides.pop("restic_password", "pw"),
            schedule_type=ScheduleType.interval,
            schedule_value="6h",
            enabled=True,
            **overrides,
        )
        s.add(job)
        await s.commit()
    return job_id


async def _create_running_run(engine, job_id: str) -> str:
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=job_id,
            status=RunStatus.running,
            started_at=datetime.now(timezone.utc),
            triggered_by=TriggeredBy.manual,
        )
        s.add(run)
        await s.commit()
    return run_id


async def test_run_backup_finalizes_as_canceled_when_flag_set(engine):
    """If the cancel flag is set before run_backup starts, the pipeline must
    short-circuit at the first restic call and finalize the row as canceled.

    Strategy: mark the run canceled in the registry, mock the restic backup to
    return as if SIGTERM was received (rc=-15 / synthetic 'canceled' output),
    and confirm the row ends up status=canceled, reason=user_canceled.
    """
    from app.db.models import BackupRun, RunReason, RunStatus
    from app.services import backup_runner, process_registry

    job_id = await _create_job_row(engine)
    run_id = await _create_running_run(engine, job_id)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    # Stub every restic step. cat_config returns rc=0 (repo OK), backup returns
    # rc=-15 (terminated). The runner should detect the canceled flag and stop.
    async def fake_cat_config(*a, **kw):
        return (0, "", "")

    async def fake_unlock(*a, **kw):
        return (0, "", "")

    async def fake_latest_snap(*a, **kw):
        return None

    async def fake_backup(*a, **kw):
        return (-15, "", "terminated", None)

    with (
        patch.object(
            backup_runner.restic, "restic_cat_config", side_effect=fake_cat_config
        ),
        patch.object(backup_runner.restic, "restic_unlock", side_effect=fake_unlock),
        patch.object(
            backup_runner.restic,
            "restic_latest_snapshot_id",
            side_effect=fake_latest_snap,
        ),
        patch.object(backup_runner.restic, "restic_backup", side_effect=fake_backup),
        patch.object(backup_runner, "_try_notify", new=AsyncMock()),
    ):
        await backup_runner.run_backup(uuid.UUID(job_id), uuid.UUID(run_id))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
        assert run is not None
        assert run.status == RunStatus.canceled
        assert run.reason == RunReason.user_canceled
        assert run.finished_at is not None
        assert run.duration_seconds is not None


async def test_run_backup_canceled_skips_forget_and_check(engine):
    """When cancel arrives during backup, the runner must not attempt forget,
    prune, or check afterwards (otherwise we'd corrupt half-baked state)."""
    from app.services import backup_runner, process_registry

    job_id = await _create_job_row(engine, check_enabled=False)
    run_id = await _create_running_run(engine, job_id)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    async def fake_ok(*a, **kw):
        return (0, "", "")

    async def fake_latest_snap(*a, **kw):
        return None

    async def fake_backup(*a, **kw):
        return (-15, "", "terminated", None)

    forget_mock = AsyncMock(return_value=(0, "", ""))
    check_mock = AsyncMock(return_value=(0, "", ""))

    with (
        patch.object(backup_runner.restic, "restic_cat_config", side_effect=fake_ok),
        patch.object(backup_runner.restic, "restic_unlock", side_effect=fake_ok),
        patch.object(
            backup_runner.restic,
            "restic_latest_snapshot_id",
            side_effect=fake_latest_snap,
        ),
        patch.object(backup_runner.restic, "restic_backup", side_effect=fake_backup),
        patch.object(backup_runner.restic, "restic_forget", forget_mock),
        patch.object(backup_runner.restic, "restic_check", check_mock),
        patch.object(backup_runner, "_try_notify", new=AsyncMock()),
    ):
        await backup_runner.run_backup(uuid.UUID(job_id), uuid.UUID(run_id))

    forget_mock.assert_not_called()
    check_mock.assert_not_called()


async def test_run_backup_canceled_sends_canceled_notification(engine):
    """Canceling a run should produce an ntfy notification with a clear title."""
    from app.db.models import AppSettings
    from app.services import backup_runner, process_registry

    # Configure ntfy on the settings row so notifications actually fire.
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.test",
                ntfy_topic="t",
                notify_on_start=False,
                notify_on_success=True,
                notify_on_failure=True,
                notify_on_warning=True,
                notify_on_verification=True,
            )
        )
        await s.commit()

    job_id = await _create_job_row(engine)
    run_id = await _create_running_run(engine, job_id)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    async def fake_ok(*a, **kw):
        return (0, "", "")

    async def fake_latest_snap(*a, **kw):
        return None

    async def fake_backup(*a, **kw):
        return (-15, "", "terminated", None)

    notify_mock = AsyncMock()
    with (
        patch.object(backup_runner.restic, "restic_cat_config", side_effect=fake_ok),
        patch.object(backup_runner.restic, "restic_unlock", side_effect=fake_ok),
        patch.object(
            backup_runner.restic,
            "restic_latest_snapshot_id",
            side_effect=fake_latest_snap,
        ),
        patch.object(backup_runner.restic, "restic_backup", side_effect=fake_backup),
        patch.object(backup_runner, "_try_notify", notify_mock),
    ):
        await backup_runner.run_backup(uuid.UUID(job_id), uuid.UUID(run_id))

    # At least one notification call should mention "canceled" in the title.
    titles = [
        call.args[2] for call in notify_mock.call_args_list if len(call.args) >= 3
    ]
    assert any("canceled" in t.lower() for t in titles), (
        f"No canceled notification fired. Titles: {titles}"
    )


# ── 4. POST /api/runs/{id}/cancel ─────────────────────────────────────────────


async def test_cancel_endpoint_404_when_run_missing(client):
    resp = await client.post(f"/api/runs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404


async def test_cancel_endpoint_409_when_run_not_running(client, engine):
    """A terminal run (success/failed/canceled/skipped) cannot be canceled."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Done Job",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    job_id = resp.json()["id"]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=job_id,
                status=RunStatus.success,
                started_at=now,
                finished_at=now,
                triggered_by=TriggeredBy.manual,
            )
        )
        await s.commit()

    resp = await client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 409


async def test_cancel_endpoint_202_marks_canceled_and_terminates(client, engine):
    """A running run can be canceled: registry marks the flag and terminates
    the subprocess if a handle is registered."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy
    from app.services import process_registry

    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Running Job",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    job_id = resp.json()["id"]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=job_id,
                status=RunStatus.running,
                started_at=datetime.now(timezone.utc),
                triggered_by=TriggeredBy.manual,
            )
        )
        await s.commit()

    # Register a fake subprocess so terminate() has something to act on.
    process_registry._processes.clear()
    process_registry._canceled.clear()
    proc = _make_proc(returncode=-15)
    process_registry.register(uuid.UUID(run_id), proc)

    resp = await client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 202
    # Registry flag set, and terminate fired on the registered handle.
    assert process_registry.is_canceled(uuid.UUID(run_id)) is True
    # Allow the spawned terminate() task to settle.
    await asyncio.sleep(0)
    proc.terminate.assert_called_once()


async def test_cancel_endpoint_202_without_registered_subprocess(client, engine):
    """A running row whose subprocess hasn't been registered yet still gets
    its cancel flag set; the next restic call's exit will trigger the
    short-circuit branch."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy
    from app.services import process_registry

    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Pre-spawn Job",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    job_id = resp.json()["id"]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=job_id,
                status=RunStatus.running,
                started_at=datetime.now(timezone.utc),
                triggered_by=TriggeredBy.manual,
            )
        )
        await s.commit()

    process_registry._processes.clear()
    process_registry._canceled.clear()

    resp = await client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 202
    assert process_registry.is_canceled(uuid.UUID(run_id)) is True


# ── 5. Canceled-branch notification ───────────────────────────────────────────


async def test_send_notification_canceled_branch_invokes_httpx():
    """notifications.send_notification works for canceled titles like any
    other ntfy push — the branch difference is in backup_runner, but the
    sanity check here ensures titles containing 'canceled' make it through."""
    from app.services import notifications

    sent: list[dict] = []

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a, **kw):
            return False

        async def post(self, url: str, headers=None, json=None):
            sent.append({"url": url, "json": json})
            return _FakeResp()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _FakeClient

    import sys

    with patch.dict(sys.modules, {"httpx": fake_httpx}):
        await notifications.send_notification(
            "https://ntfy.test",
            "topic",
            "Backup canceled: My Job",
            "Duration: 12s",
        )

    assert sent, "Expected an HTTP POST to be issued for the canceled notification"
    assert "canceled" in sent[0]["json"]["title"].lower()


# ── 5. run_prune / run_check honor the cancel flag ────────────────────────────
#
# The cancel endpoint sets the flag and SIGTERMs the subprocess for ANY
# running run. Prune and check pipelines must observe the flag and finalize
# the row as canceled/user_canceled (not failed), then clear the flag so the
# registry set stays bounded.


async def _create_running_kind_run(engine, job_id: str, kind) -> str:
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        run = BackupRun(
            id=run_id,
            job_id=job_id,
            kind=kind,
            status=RunStatus.running,
            started_at=datetime.now(timezone.utc),
            triggered_by=TriggeredBy.manual,
        )
        s.add(run)
        await s.commit()
    return run_id


async def test_run_prune_finalizes_as_canceled_when_flag_set(engine):
    """A canceled prune must be recorded as canceled/user_canceled — not as a
    failed run with the SIGTERM'd process's stderr as the error."""
    from app.db.models import BackupRun, RunKind, RunReason, RunStatus
    from app.services import backup_runner, process_registry

    job_id = await _create_job_row(engine)
    run_id = await _create_running_kind_run(engine, job_id, RunKind.prune)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    # Simulate the SIGTERM'd restic prune returning non-zero.
    async def fake_prune(*a, **kw):
        return (-15, "", "terminated")

    with (
        patch.object(backup_runner.restic, "restic_prune", side_effect=fake_prune),
        patch.object(backup_runner, "_try_notify", new=AsyncMock()),
    ):
        await backup_runner.run_prune(uuid.UUID(job_id), uuid.UUID(run_id))

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
        assert run is not None
        assert run.status == RunStatus.canceled
        assert run.reason == RunReason.user_canceled
        assert run.finished_at is not None
        assert run.duration_seconds is not None
    # Flag must be cleared so the module-level set stays bounded.
    assert not process_registry.is_canceled(uuid.UUID(run_id))


async def test_run_prune_canceled_before_start_skips_restic(engine):
    """Cancel arriving before the subprocess spawns must prevent the (possibly
    hours-long) prune from running at all."""
    from app.db.models import BackupRun, RunKind, RunReason, RunStatus
    from app.services import backup_runner, process_registry

    job_id = await _create_job_row(engine)
    run_id = await _create_running_kind_run(engine, job_id, RunKind.prune)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    prune_mock = AsyncMock(return_value=(0, "", ""))
    with (
        patch.object(backup_runner.restic, "restic_prune", prune_mock),
        patch.object(backup_runner, "_try_notify", new=AsyncMock()),
    ):
        await backup_runner.run_prune(uuid.UUID(job_id), uuid.UUID(run_id))

    prune_mock.assert_not_called()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
        assert run is not None
        assert run.status == RunStatus.canceled
        assert run.reason == RunReason.user_canceled


async def test_run_check_finalizes_as_canceled_when_flag_set(engine):
    """A canceled integrity check must be recorded as canceled/user_canceled,
    with check_status left as skipped rather than failed."""
    from app.db.models import BackupRun, CheckStatus, RunKind, RunReason, RunStatus
    from app.services import backup_runner, process_registry

    job_id = await _create_job_row(engine)
    run_id = await _create_running_kind_run(engine, job_id, RunKind.check)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    async def fake_check(*a, **kw):
        return (-15, "", "terminated")

    with (
        patch.object(backup_runner.restic, "restic_check", side_effect=fake_check),
        patch.object(backup_runner, "_try_notify", new=AsyncMock()),
    ):
        await backup_runner.run_check(
            uuid.UUID(job_id), uuid.UUID(run_id), "structural", None, None
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
        assert run is not None
        assert run.status == RunStatus.canceled
        assert run.reason == RunReason.user_canceled
        assert run.check_status == CheckStatus.skipped
        assert run.finished_at is not None
    assert not process_registry.is_canceled(uuid.UUID(run_id))


async def test_run_check_canceled_sends_warning_notification(engine):
    """Cancel of a check mirrors the backup-cancel notification behavior:
    a notify_on_warning message naming the cancellation."""
    from app.db.models import AppSettings, RunKind
    from app.services import backup_runner, process_registry

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.test",
                ntfy_topic="t",
                notify_on_warning=True,
            )
        )
        await s.commit()

    job_id = await _create_job_row(engine)
    run_id = await _create_running_kind_run(engine, job_id, RunKind.check)
    process_registry._processes.clear()
    process_registry._canceled.clear()
    process_registry.mark_canceled(uuid.UUID(run_id))

    async def fake_check(*a, **kw):
        return (-15, "", "terminated")

    notify_mock = AsyncMock()
    with (
        patch.object(backup_runner.restic, "restic_check", side_effect=fake_check),
        patch.object(backup_runner, "_try_notify", notify_mock),
    ):
        await backup_runner.run_check(
            uuid.UUID(job_id), uuid.UUID(run_id), "structural", None, None
        )

    titles = [
        call.args[2] for call in notify_mock.call_args_list if len(call.args) >= 3
    ]
    assert any("canceled" in t.lower() for t in titles), (
        f"No canceled notification fired. Titles: {titles}"
    )


async def test_cancel_endpoint_terminate_task_is_tracked(client, engine):
    """The fire-and-forget SIGTERM task must be spawned through
    app.core.tasks.create_tracked_task — an untracked task holds only a weak
    reference in the event loop and can be garbage-collected before it ever
    sends the signal, turning the user's Stop click into a silent no-op."""
    from app.core import tasks
    from app.db.models import BackupRun, RunStatus, TriggeredBy
    from app.services import process_registry

    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Tracked Cancel Job",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    job_id = resp.json()["id"]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=job_id,
                status=RunStatus.running,
                started_at=datetime.now(timezone.utc),
                triggered_by=TriggeredBy.manual,
            )
        )
        await s.commit()

    process_registry._processes.clear()
    process_registry._canceled.clear()
    proc = _make_proc(returncode=-15)
    process_registry.register(uuid.UUID(run_id), proc)

    with patch(
        "app.api.routes.runs.create_tracked_task",
        side_effect=tasks.create_tracked_task,
    ) as spawn:
        resp = await client.post(f"/api/runs/{run_id}/cancel")

    assert resp.status_code == 202
    spawn.assert_called_once()
    # Allow the tracked terminate() task to settle.
    for _ in range(5):
        await asyncio.sleep(0)
    proc.terminate.assert_called_once()

    process_registry._processes.clear()
    process_registry._canceled.clear()
