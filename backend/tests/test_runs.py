"""Tests for GET /api/runs/recent and GET /api/runs/{id}."""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch


async def _create_job(client) -> str:
    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": "Test Job",
                "source_label": "docs",
                "destination_label": "main",
                "restic_password": "s3cret",
                "schedule_type": "interval",
                "schedule_value": "6h",
                "enabled": True,
            },
        )
    return resp.json()["id"]


async def _insert_run(engine, job_id: str, **kwargs):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=job_id,
            status=kwargs.pop("status", RunStatus.success),
            triggered_by=kwargs.pop("triggered_by", TriggeredBy.manual),
            started_at=kwargs.pop("started_at", now),
            finished_at=kwargs.pop("finished_at", now),
            backup_output=kwargs.pop("backup_output", "some output"),
            error_output=kwargs.pop("error_output", None),
            **kwargs,
        )
        s.add(run)
        await s.commit()
        return run.id


# ── GET /api/runs/recent ──────────────────────────────────────────────────────


async def test_recent_runs_empty(client):
    resp = await client.get("/api/runs/recent")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_recent_runs_default_limit_10(client, engine):
    job_id = await _create_job(client)
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    for i in range(15):
        await _insert_run(engine, job_id, started_at=now - timedelta(minutes=i))

    resp = await client.get("/api/runs/recent")
    assert resp.status_code == 200
    assert len(resp.json()) == 10


async def test_recent_runs_custom_limit(client, engine):
    job_id = await _create_job(client)
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    for i in range(10):
        await _insert_run(engine, job_id, started_at=now - timedelta(minutes=i))

    resp = await client.get("/api/runs/recent?limit=5")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


async def test_recent_runs_limit_zero_returns_422(client):
    resp = await client.get("/api/runs/recent?limit=0")
    assert resp.status_code == 422


async def test_recent_runs_limit_101_returns_422(client):
    resp = await client.get("/api/runs/recent?limit=101")
    assert resp.status_code == 422


async def test_recent_runs_limit_100_is_valid(client, engine):
    await _create_job(client)
    resp = await client.get("/api/runs/recent?limit=100")
    assert resp.status_code == 200


async def test_recent_runs_ordered_newest_first(client, engine):
    from datetime import timedelta

    job_id = await _create_job(client)
    now = datetime.now(timezone.utc)
    for i in range(5):
        await _insert_run(engine, job_id, started_at=now - timedelta(hours=i))

    resp = await client.get("/api/runs/recent")
    runs = resp.json()
    started_ats = [r["started_at"] for r in runs]
    assert started_ats == sorted(started_ats, reverse=True)


async def test_recent_runs_includes_job_name(client, engine):
    job_id = await _create_job(client)
    await _insert_run(engine, job_id)

    resp = await client.get("/api/runs/recent")
    run = resp.json()[0]
    assert "job_name" in run
    assert run["job_name"] == "Test Job"


async def test_recent_runs_includes_check_status(client, engine):
    from app.db.models import CheckStatus

    job_id = await _create_job(client)
    await _insert_run(engine, job_id, check_status=CheckStatus.passed)

    resp = await client.get("/api/runs/recent")
    run = resp.json()[0]
    assert "check_status" in run


async def test_recent_runs_excludes_output_fields(client, engine):
    job_id = await _create_job(client)
    await _insert_run(engine, job_id, backup_output="large output")

    resp = await client.get("/api/runs/recent")
    run = resp.json()[0]
    assert "backup_output" not in run
    assert "error_output" not in run
    assert "prune_error_output" not in run
    assert "check_error_output" not in run


async def test_recent_runs_across_multiple_jobs(client, engine):
    with patch("os.path.isdir", return_value=True):
        job1 = (
            await client.post(
                "/api/jobs",
                json={
                    "name": "Job 1",
                    "source_label": "docs",
                    "destination_label": "main",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "6h",
                },
            )
        ).json()["id"]
        job2 = (
            await client.post(
                "/api/jobs",
                json={
                    "name": "Job 2",
                    "source_label": "photos",
                    "destination_label": "backup",
                    "restic_password": "pw",
                    "schedule_type": "interval",
                    "schedule_value": "6h",
                },
            )
        ).json()["id"]

    await _insert_run(engine, job1)
    await _insert_run(engine, job2)

    resp = await client.get("/api/runs/recent")
    assert len(resp.json()) == 2
    job_ids = {r["job_id"] for r in resp.json()}
    assert job_ids == {job1, job2}


# ── GET /api/runs/{id} ────────────────────────────────────────────────────────


async def test_get_run_detail_success(client, engine):
    job_id = await _create_job(client)
    run_id = await _insert_run(engine, job_id, backup_output="file-level output")

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert data["job_id"] == job_id


async def test_get_run_detail_includes_backup_output(client, engine):
    job_id = await _create_job(client)
    run_id = await _insert_run(engine, job_id, backup_output="verbose output here")

    resp = await client.get(f"/api/runs/{run_id}")
    assert "backup_output" in resp.json()
    assert resp.json()["backup_output"] == "verbose output here"


async def test_get_run_detail_includes_error_output(client, engine):
    from app.db.models import RunStatus

    job_id = await _create_job(client)
    run_id = await _insert_run(
        engine,
        job_id,
        status=RunStatus.failed,
        error_output="fatal: permission denied",
        backup_output=None,
    )

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.json()["error_output"] == "fatal: permission denied"


async def test_get_run_detail_includes_prune_error_output(client, engine):
    from app.db.models import PruneStatus

    job_id = await _create_job(client)
    run_id = await _insert_run(
        engine,
        job_id,
        prune_status=PruneStatus.failed,
        prune_error_output="prune failed: disk full",
    )

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.json()["prune_error_output"] == "prune failed: disk full"


async def test_get_run_detail_includes_check_error_output(client, engine):
    from app.db.models import CheckStatus

    job_id = await _create_job(client)
    run_id = await _insert_run(
        engine,
        job_id,
        check_status=CheckStatus.failed,
        check_error_output="pack file corrupted",
    )

    resp = await client.get(f"/api/runs/{run_id}")
    assert resp.json()["check_error_output"] == "pack file corrupted"


async def test_get_run_not_found(client):
    resp = await client.get(f"/api/runs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


async def test_get_run_detail_all_fields_present(client, engine):
    from app.db.models import CheckStatus, PruneStatus, RunStatus

    job_id = await _create_job(client)
    run_id = await _insert_run(
        engine,
        job_id,
        status=RunStatus.success,
        files_new=10,
        files_changed=5,
        files_unmodified=1000,
        dirs_new=2,
        dirs_changed=1,
        dirs_unmodified=50,
        data_added_bytes=1024000,
        total_bytes_processed=50000000,
        duration_seconds=120,
        prune_status=PruneStatus.passed,
        check_status=CheckStatus.passed,
    )

    resp = await client.get(f"/api/runs/{run_id}")
    data = resp.json()
    assert data["files_new"] == 10
    assert data["files_changed"] == 5
    assert data["duration_seconds"] == 120
    assert data["prune_status"] == "passed"
    assert data["check_status"] == "passed"


# ── POST /api/runs/{id}/cancel: the id that is not a UUID ────────────────────
#
# The cancel route looks the row up by string id, then parses that id into a
# UUID to reach the process registry (which is keyed by UUID). Rows are always
# created with UUIDs, so the two always agree — except for a row that got into
# the DB another way: a hand-written fixture, a manual INSERT during recovery,
# or a future migration that changes the id format. The route treats an
# unparseable id as not-found rather than letting the ValueError become a 500,
# because a 500 on Stop tells the operator nothing about what to do next.


async def _insert_run_with_id(engine, job_id: str, run_id: str, status):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, TriggeredBy

    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=job_id,
                status=status,
                triggered_by=TriggeredBy.manual,
                started_at=now,
            )
        )
        await s.commit()
    return run_id


async def test_cancel_a_running_row_whose_id_is_not_a_uuid_is_a_404(client, engine):
    """The row exists and is running, but its id cannot address the registry."""
    from app.db.models import RunStatus

    job_id = await _create_job(client)
    await _insert_run_with_id(engine, job_id, "not-a-uuid", RunStatus.running)

    resp = await client.post("/api/runs/not-a-uuid/cancel")

    # 404, not a 500 — the ValueError from uuid.UUID() is contained.
    assert resp.status_code == 404


async def test_cancel_with_a_non_uuid_id_does_not_touch_the_process_registry(
    client, engine
):
    """Nothing may be marked canceled or terminated on an id we cannot parse.

    The registry is keyed by UUID; a stray call with an unparseable id would
    either raise or, worse, be coerced into matching some other live run and
    SIGTERM a backup the operator never asked to stop.
    """
    from app.db.models import RunStatus
    from app.services import process_registry

    job_id = await _create_job(client)
    await _insert_run_with_id(engine, job_id, "still-not-a-uuid", RunStatus.running)

    with (
        patch.object(process_registry, "mark_canceled") as mark,
        patch.object(process_registry, "terminate") as terminate,
    ):
        resp = await client.post("/api/runs/still-not-a-uuid/cancel")

    assert resp.status_code == 404
    mark.assert_not_called()
    terminate.assert_not_called()


async def test_cancel_checks_the_status_before_the_id_format(client, engine):
    """A finished run answers 409 even when its id is unparseable.

    Ordering matters for the message the UI shows: "already finished" is
    actionable (refresh the page), "not found" is not.
    """
    from app.db.models import RunStatus

    job_id = await _create_job(client)
    await _insert_run_with_id(engine, job_id, "bad-id-finished", RunStatus.success)

    resp = await client.post("/api/runs/bad-id-finished/cancel")

    assert resp.status_code == 409
    assert "cannot cancel" in resp.json()["detail"].lower()


async def test_cancel_a_missing_run_is_a_404(client):
    resp = await client.post(f"/api/runs/{uuid.uuid4()}/cancel")

    assert resp.status_code == 404
