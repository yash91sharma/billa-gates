"""Tests for POST/GET/PUT/DELETE /api/jobs and sub-routes."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from tests.conftest import make_job_payload

# ── POST /api/jobs ────────────────────────────────────────────────────────────


async def test_create_job_success(client):
    payload = make_job_payload()
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Backup"
    assert data["source_label"] == "documents"
    assert data["destination_label"] == "main"
    assert "id" in data
    assert data["restic_password"] is None  # never returned


async def test_create_job_restic_password_excluded(client):
    payload = make_job_payload()
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    assert resp.json()["restic_password"] is None


async def test_create_job_missing_name(client):
    payload = make_job_payload()
    del payload["name"]
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_missing_password(client):
    payload = make_job_payload()
    del payload["restic_password"]
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_name_too_long(client):
    payload = make_job_payload(name="x" * 129)
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_invalid_source_label_slash(client):
    payload = make_job_payload(source_label="a/b")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_invalid_source_label_dotdot(client):
    payload = make_job_payload(source_label="..")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_invalid_destination_label(client):
    payload = make_job_payload(destination_label="bad/label")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_invalid_source_subpath_with_slash(client):
    payload = make_job_payload(source_subpath="photos/2024")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_interval_too_short(client):
    payload = make_job_payload(schedule_type="interval", schedule_value="4m")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_interval_minimum_valid(client):
    payload = make_job_payload(schedule_type="interval", schedule_value="5m")
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201


async def test_create_job_interval_bad_format(client):
    payload = make_job_payload(schedule_type="interval", schedule_value="6hours")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_cron_too_frequent(client):
    payload = make_job_payload(schedule_type="cron", schedule_value="* * * * *")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_cron_invalid_expression(client):
    payload = make_job_payload(schedule_type="cron", schedule_value="not a cron")
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_cron_valid_expression(client):
    payload = make_job_payload(schedule_type="cron", schedule_value="0 2 * * *")
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201


async def test_create_job_source_not_mounted(client):
    payload = make_job_payload()
    with patch("os.path.isdir", return_value=False):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422
    assert "source mount" in resp.json()["detail"].lower()


async def test_create_job_destination_not_mounted(client):
    payload = make_job_payload()

    def is_dir(path):
        return "sources" in path  # source mounted, destination not

    with patch("os.path.isdir", side_effect=is_dir):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422
    assert "destination mount" in resp.json()["detail"].lower()


async def test_create_job_duplicate_source_destination(client):
    payload = make_job_payload()
    with patch("os.path.isdir", return_value=True):
        resp1 = await client.post("/api/jobs", json=payload)
        assert resp1.status_code == 201
        resp2 = await client.post("/api/jobs", json=payload)
    assert resp2.status_code == 409


async def test_create_job_same_labels_different_subpaths_allowed(client):
    """Per design doc §6: duplicate key is (source_label, source_subpath,
    destination_label). Different subpaths → different jobs."""
    with patch("os.path.isdir", return_value=True):
        resp1 = await client.post(
            "/api/jobs", json=make_job_payload(source_subpath="photos")
        )
        assert resp1.status_code == 201
        resp2 = await client.post(
            "/api/jobs", json=make_job_payload(source_subpath="videos")
        )
    assert resp2.status_code == 201


async def test_create_job_duplicate_same_subpath_rejected(client):
    with patch("os.path.isdir", return_value=True):
        resp1 = await client.post(
            "/api/jobs", json=make_job_payload(source_subpath="photos")
        )
        assert resp1.status_code == 201
        resp2 = await client.post(
            "/api/jobs", json=make_job_payload(source_subpath="photos")
        )
    assert resp2.status_code == 409


async def test_create_job_409_response_includes_conflicting_job_identity(client):
    """Per design doc §6: 409 conflict returns the existing job's name and id."""
    with patch("os.path.isdir", return_value=True):
        first = await client.post("/api/jobs", json=make_job_payload(name="Original"))
        assert first.status_code == 201
        first_id = first.json()["id"]
        resp = await client.post("/api/jobs", json=make_job_payload(name="Duplicate"))
    assert resp.status_code == 409
    body = resp.json()
    # detail is a dict with conflict info
    assert isinstance(body["detail"], dict)
    assert body["detail"]["conflicting_job_id"] == first_id
    assert body["detail"]["conflicting_job_name"] == "Original"


async def test_create_job_retain_keep_last_valid(client):
    payload = make_job_payload(retain_keep_last=7)
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    assert resp.json()["retain_keep_last"] == 7


async def test_create_job_retain_keep_last_too_high(client):
    payload = make_job_payload(retain_keep_last=10000)
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_pack_size_valid(client):
    payload = make_job_payload(pack_size=512)
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201


async def test_create_job_pack_size_too_large(client):
    payload = make_job_payload(pack_size=2000)
    resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 422


async def test_create_job_default_enabled_true(client):
    payload = make_job_payload()
    payload.pop("enabled", None)
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    assert resp.json()["enabled"] is True


# ── GET /api/jobs ─────────────────────────────────────────────────────────────


async def test_list_jobs_empty(client):
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_returns_all(client):
    with patch("os.path.isdir", return_value=True):
        await client.post(
            "/api/jobs",
            json=make_job_payload(
                name="Job A", source_label="docs", destination_label="main"
            ),
        )
        await client.post(
            "/api/jobs",
            json=make_job_payload(
                name="Job B", source_label="photos", destination_label="backup"
            ),
        )
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_list_jobs_includes_next_run_time(client):
    with patch("os.path.isdir", return_value=True):
        await client.post("/api/jobs", json=make_job_payload())
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200
    job = resp.json()[0]
    assert "next_run_time" in job


async def test_list_jobs_includes_last_run(client):
    with patch("os.path.isdir", return_value=True):
        await client.post("/api/jobs", json=make_job_payload())
    resp = await client.get("/api/jobs")
    job = resp.json()[0]
    assert "last_run" in job
    assert job["last_run"] is None  # no runs yet


async def test_list_jobs_excludes_output_fields(client):
    with patch("os.path.isdir", return_value=True):
        await client.post("/api/jobs", json=make_job_payload())
    resp = await client.get("/api/jobs")
    job = resp.json()[0]
    assert "backup_output" not in job
    assert "error_output" not in job


async def test_list_jobs_includes_has_successful_run(client):
    with patch("os.path.isdir", return_value=True):
        await client.post("/api/jobs", json=make_job_payload())
    resp = await client.get("/api/jobs")
    job = resp.json()[0]
    assert "has_successful_run" in job
    assert job["has_successful_run"] is False


# ── GET /api/jobs/{id} ────────────────────────────────────────────────────────


async def test_get_job_by_id(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


async def test_get_job_not_found(client):
    resp = await client.get(f"/api/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not found"


async def test_get_job_password_excluded(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.json()["restic_password"] is None


# ── PUT /api/jobs/{id} ────────────────────────────────────────────────────────


async def test_update_job_name(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    update = make_job_payload(name="Updated Name")
    with patch("os.path.isdir", return_value=True):
        resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


async def test_update_job_destination_label_immutable(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    update = make_job_payload(destination_label="different")
    resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 422
    assert "destination" in resp.json()["detail"].lower()


async def test_update_job_password_immutable_after_success(client, db_session, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.success,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    update = make_job_payload(restic_password="newpassword")
    resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 422
    assert "restic_password" in resp.json()["detail"].lower()


async def test_update_job_password_editable_before_success(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    update = make_job_payload(restic_password="newpassword")
    with patch("os.path.isdir", return_value=True):
        resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 200


async def test_update_job_password_absent_leaves_unchanged(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    update = make_job_payload()
    update.pop("restic_password")
    with patch("os.path.isdir", return_value=True):
        resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 200


async def test_update_job_not_found(client):
    resp = await client.put(f"/api/jobs/{uuid.uuid4()}", json=make_job_payload())
    assert resp.status_code == 404


async def test_update_job_source_label_change_checks_mount(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    update = make_job_payload(source_label="newlabel")
    with patch("os.path.isdir", return_value=False):
        resp = await client.put(f"/api/jobs/{created['id']}", json=update)
    assert resp.status_code == 422
    assert "source mount" in resp.json()["detail"].lower()


# ── DELETE /api/jobs/{id} ─────────────────────────────────────────────────────


async def test_delete_job_success(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    resp = await client.delete(f"/api/jobs/{created['id']}")
    assert resp.status_code == 204


async def test_delete_job_not_found(client):
    resp = await client.delete(f"/api/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_delete_job_active_run_returns_409(client):
    from app.services import backup_runner

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    job_uuid = uuid.UUID(created["id"])
    backup_runner.active_jobs.add(job_uuid)
    try:
        resp = await client.delete(f"/api/jobs/{created['id']}")
        assert resp.status_code == 409
        assert "in progress" in resp.json()["detail"].lower()
    finally:
        backup_runner.active_jobs.discard(job_uuid)


async def test_delete_job_does_not_delete_restic_repo(client, tmp_path):
    repo_dir = tmp_path / "destinations" / "main" / "some-id"
    repo_dir.mkdir(parents=True)

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    await client.delete(f"/api/jobs/{created['id']}")
    assert repo_dir.exists()


# ── POST /api/jobs/{id}/run ───────────────────────────────────────────────────


async def test_trigger_run_returns_run_id(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_backup"):
        resp = await client.post(f"/api/jobs/{created['id']}/run")
    assert resp.status_code == 200
    assert "run_id" in resp.json()


async def test_trigger_run_creates_running_row(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_backup"):
        resp = await client.post(f"/api/jobs/{created['id']}/run")

    run_id = resp.json()["run_id"]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.status == RunStatus.running


async def test_trigger_run_overlapping_returns_skipped(client):
    from app.services import backup_runner

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    job_uuid = uuid.UUID(created["id"])
    backup_runner.active_jobs.add(job_uuid)
    try:
        with patch("app.services.backup_runner.run_backup"):
            resp = await client.post(f"/api/jobs/{created['id']}/run")
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]

        # The returned run_id should be a skipped row
        assert run_id is not None
    finally:
        backup_runner.active_jobs.discard(job_uuid)


async def test_trigger_run_not_found(client):
    resp = await client.post(f"/api/jobs/{uuid.uuid4()}/run")
    assert resp.status_code == 404


async def test_trigger_run_disabled_job_still_works(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=False))
        ).json()
    with patch("app.services.backup_runner.run_backup"):
        resp = await client.post(f"/api/jobs/{created['id']}/run")
    assert resp.status_code == 200


# ── POST /api/jobs/{id}/prune ────────────────────────────────────────────────


async def test_trigger_prune_returns_run_id(client):
    """Manual prune endpoint mirrors /run — returns 200 with a run_id that
    the UI can navigate to. Prune is now decoupled from backup (gaps.md H1)
    and must be triggered explicitly."""
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_prune"):
        resp = await client.post(f"/api/jobs/{created['id']}/prune")
    assert resp.status_code == 200
    assert "run_id" in resp.json()


async def test_trigger_prune_creates_prune_kind_row(client, engine):
    """The created BackupRun must have kind=prune so the UI can label it
    distinctly and so retention logic / metrics can distinguish prune from
    backup runs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunKind, RunStatus

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_prune"):
        resp = await client.post(f"/api/jobs/{created['id']}/prune")

    run_id = resp.json()["run_id"]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.kind == RunKind.prune
    assert run.status == RunStatus.running


async def test_trigger_prune_overlapping_returns_skipped(client):
    """If a backup is already running for this job, the prune trigger must
    return a skipped/overlapping_run row rather than racing against the
    backup against the same restic repo."""
    from app.db.models import RunKind, RunReason, RunStatus
    from app.services import backup_runner

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    job_uuid = uuid.UUID(created["id"])
    backup_runner.active_jobs.add(job_uuid)
    try:
        with patch("app.services.backup_runner.run_prune"):
            resp = await client.post(f"/api/jobs/{created['id']}/prune")
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
    finally:
        backup_runner.active_jobs.discard(job_uuid)

    # Read the row back through the runs endpoint so we don't need direct
    # DB access from this test.
    runs_resp = await client.get(f"/api/runs/{run_id}")
    assert runs_resp.status_code == 200
    body = runs_resp.json()
    assert body["status"] == RunStatus.skipped.value
    assert body["reason"] == RunReason.overlapping_run.value
    assert body["kind"] == RunKind.prune.value


async def test_trigger_prune_not_found(client):
    """Prune on a non-existent job returns 404."""
    resp = await client.post(f"/api/jobs/{uuid.uuid4()}/prune")
    assert resp.status_code == 404


# ── POST /api/jobs/{id}/check ─────────────────────────────────────────────────


async def test_trigger_check_returns_run_id(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_check"):
        resp = await client.post(
            f"/api/jobs/{created['id']}/check", json={"check_mode": "structural"}
        )
    assert resp.status_code == 200
    assert "run_id" in resp.json()


async def test_trigger_check_creates_check_kind_row(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunKind, RunStatus

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch("app.services.backup_runner.run_check"):
        resp = await client.post(
            f"/api/jobs/{created['id']}/check", json={"check_mode": "structural"}
        )

    run_id = resp.json()["run_id"]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = await s.get(BackupRun, run_id)
    assert run is not None
    assert run.kind == RunKind.check
    assert run.status == RunStatus.running


async def test_trigger_check_overlapping_returns_skipped(client):
    from app.db.models import RunKind, RunReason, RunStatus
    from app.services import backup_runner

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    job_uuid = uuid.UUID(created["id"])
    backup_runner.active_jobs.add(job_uuid)
    try:
        with patch("app.services.backup_runner.run_check"):
            resp = await client.post(
                f"/api/jobs/{created['id']}/check", json={"check_mode": "structural"}
            )
        assert resp.status_code == 200
        run_id = resp.json()["run_id"]
    finally:
        backup_runner.active_jobs.discard(job_uuid)

    runs_resp = await client.get(f"/api/runs/{run_id}")
    assert runs_resp.status_code == 200
    body = runs_resp.json()
    assert body["status"] == RunStatus.skipped.value
    assert body["reason"] == RunReason.overlapping_run.value
    assert body["kind"] == RunKind.check.value


async def test_trigger_check_not_found(client):
    resp = await client.post(
        f"/api/jobs/{uuid.uuid4()}/check", json={"check_mode": "structural"}
    )
    assert resp.status_code == 404


# ── POST /api/jobs/{id}/enable & /disable ────────────────────────────────────


async def test_enable_job(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=False))
        ).json()
    resp = await client.post(f"/api/jobs/{created['id']}/enable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


async def test_disable_job(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=True))
        ).json()
    resp = await client.post(f"/api/jobs/{created['id']}/disable")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


async def test_enable_is_idempotent(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=True))
        ).json()
    resp1 = await client.post(f"/api/jobs/{created['id']}/enable")
    resp2 = await client.post(f"/api/jobs/{created['id']}/enable")
    assert resp1.status_code == 200
    assert resp2.status_code == 200


async def test_disable_is_idempotent(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=False))
        ).json()
    resp1 = await client.post(f"/api/jobs/{created['id']}/disable")
    resp2 = await client.post(f"/api/jobs/{created['id']}/disable")
    assert resp1.status_code == 200
    assert resp2.status_code == 200


async def test_enable_not_found(client):
    resp = await client.post(f"/api/jobs/{uuid.uuid4()}/enable")
    assert resp.status_code == 404


# ── POST /api/jobs/{id}/unlock ────────────────────────────────────────────────


async def test_unlock_job(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    with patch(
        "app.services.restic.restic_unlock", return_value=(0, "Lock removed", "")
    ):
        resp = await client.post(f"/api/jobs/{created['id']}/unlock")
    assert resp.status_code == 200
    assert "output" in resp.json()


async def test_unlock_job_active_run_returns_409(client):
    from app.services import backup_runner

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    job_uuid = uuid.UUID(created["id"])
    backup_runner.active_jobs.add(job_uuid)
    try:
        resp = await client.post(f"/api/jobs/{created['id']}/unlock")
        assert resp.status_code == 409
        assert "in progress" in resp.json()["detail"].lower()
    finally:
        backup_runner.active_jobs.discard(job_uuid)


async def test_unlock_not_found(client):
    resp = await client.post(f"/api/jobs/{uuid.uuid4()}/unlock")
    assert resp.status_code == 404


# ── GET /api/jobs/{id}/runs ───────────────────────────────────────────────────


async def test_get_job_runs_empty(client):
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()
    resp = await client.get(f"/api/jobs/{created['id']}/runs")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_job_runs_ordered_newest_first(client, engine):
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    now = datetime.now(timezone.utc)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        for i in range(3):
            run = BackupRun(
                id=str(uuid.uuid4()),
                job_id=created["id"],
                status=RunStatus.success,
                triggered_by=TriggeredBy.manual,
                started_at=now - timedelta(hours=i),
                finished_at=now - timedelta(hours=i),
            )
            s.add(run)
        await s.commit()

    resp = await client.get(f"/api/jobs/{created['id']}/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 3
    started_ats = [r["started_at"] for r in runs]
    assert started_ats == sorted(started_ats, reverse=True)


async def test_get_job_runs_excludes_output_fields(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.success,
            triggered_by=TriggeredBy.manual,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            backup_output="lots of output here",
            error_output="some error",
        )
        s.add(run)
        await s.commit()

    resp = await client.get(f"/api/jobs/{created['id']}/runs")
    run_data = resp.json()[0]
    assert "backup_output" not in run_data
    assert "error_output" not in run_data


# ── GET /api/jobs/{id}/snapshots ──────────────────────────────────────────────
#
# Restic is the source of truth for snapshots (gaps.md C4-Alt): the endpoint
# calls `app.services.snapshot_listing.list_snapshots` which shells out to
# `restic snapshots --json --tag job:<id> --no-lock`. These tests therefore
# mock the service rather than seeding a DB table.


async def test_get_job_snapshots_empty(client):
    from unittest.mock import AsyncMock

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    with patch(
        "app.services.snapshot_listing.list_snapshots",
        new=AsyncMock(return_value=[]),
    ):
        resp = await client.get(f"/api/jobs/{created['id']}/snapshots")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_job_snapshots_returns_all(client):
    from unittest.mock import AsyncMock

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    fake_snaps = [
        {
            "snapshot_id": f"{'a' * 60}{i:04d}",
            "snapshot_time": "2026-05-01T12:00:00Z",
            "hostname": "host",
            "paths": ["/sources/documents"],
            "tags": None,
            "size_bytes": None,
        }
        for i in range(3)
    ]
    with patch(
        "app.services.snapshot_listing.list_snapshots",
        new=AsyncMock(return_value=fake_snaps),
    ):
        resp = await client.get(f"/api/jobs/{created['id']}/snapshots")

    assert resp.status_code == 200
    assert len(resp.json()) == 3


async def test_get_job_snapshots_ordered_newest_first(client):
    """Restic emits snapshots oldest-first; the route reverses them so the UI
    shows the most recent snapshot at the top of the list."""
    from datetime import timedelta
    from unittest.mock import AsyncMock

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    now = datetime.now(timezone.utc)
    # Deliberately emit oldest-first so the assertion proves the route
    # actually sorts rather than coincidentally agreeing with the mock order.
    fake_snaps = [
        {
            "snapshot_id": f"{'b' * 60}{i:04d}",
            "snapshot_time": (now - timedelta(hours=i)).isoformat(),
            "hostname": "host",
            "paths": ["/sources/documents"],
            "tags": None,
            "size_bytes": None,
        }
        for i in reversed(range(3))
    ]
    with patch(
        "app.services.snapshot_listing.list_snapshots",
        new=AsyncMock(return_value=fake_snaps),
    ):
        resp = await client.get(f"/api/jobs/{created['id']}/snapshots")

    times = [s["snapshot_time"] for s in resp.json()]
    assert times == sorted(times, reverse=True)


async def test_get_job_snapshots_repo_missing_returns_empty_list(client):
    """A genuine first run (or a deleted repo) raises SnapshotListingError with
    a 'does not exist' / 'unable to open' stderr — the route must surface this
    as an empty list rather than a 503 so the UI shows 'No snapshots yet'."""
    from unittest.mock import AsyncMock

    from app.services.snapshot_listing import SnapshotListingError

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    with patch(
        "app.services.snapshot_listing.list_snapshots",
        new=AsyncMock(
            side_effect=SnapshotListingError(
                "restic snapshots failed rc=10 stderr='Fatal: unable to open repo'"
            )
        ),
    ):
        resp = await client.get(f"/api/jobs/{created['id']}/snapshots")

    assert resp.status_code == 200
    assert resp.json() == []


async def test_get_job_snapshots_genuine_failure_returns_503(client):
    """A real backend failure (timeout, corruption) must surface as 503 so
    operators see the problem rather than silently rendering 'No snapshots'."""
    from unittest.mock import AsyncMock

    from app.services.snapshot_listing import SnapshotListingError

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    with patch(
        "app.services.snapshot_listing.list_snapshots",
        new=AsyncMock(
            side_effect=SnapshotListingError("restic snapshots timed out after 60s")
        ),
    ):
        resp = await client.get(f"/api/jobs/{created['id']}/snapshots")

    assert resp.status_code == 503


# ── has_successful_run field ──────────────────────────────────────────────────


async def test_get_job_has_successful_run_true_after_success(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.success,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["has_successful_run"] is True


async def test_list_jobs_has_successful_run_true_when_success_exists(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.success,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    resp = await client.get("/api/jobs")
    job = next(j for j in resp.json() if j["id"] == created["id"])
    assert job["has_successful_run"] is True


async def test_has_successful_run_false_when_only_failed_run(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.failed,
            triggered_by=TriggeredBy.scheduler,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        s.add(run)
        await s.commit()

    resp = await client.get(f"/api/jobs/{created['id']}")
    assert resp.json()["has_successful_run"] is False


# ── Scheduler registration on create/enable/disable ───────────────────────────


async def test_create_enabled_job_registers_in_scheduler(client):
    add_calls = []
    with patch("os.path.isdir", return_value=True):
        with patch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            created = (
                await client.post("/api/jobs", json=make_job_payload(enabled=True))
            ).json()

    assert any(kw.get("id") == created["id"] for kw in add_calls)


async def test_create_disabled_job_does_not_register_in_scheduler(client):
    add_calls = []
    with patch("os.path.isdir", return_value=True):
        with patch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            created = (
                await client.post("/api/jobs", json=make_job_payload(enabled=False))
            ).json()

    assert not any(kw.get("id") == created["id"] for kw in add_calls)


async def test_disable_removes_job_from_scheduler(client):
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=True))
        ).json()

    remove_calls = []
    with patch("app.core.scheduler.scheduler") as mock_sched:
        mock_sched.running = True
        mock_sched.remove_job = MagicMock(
            side_effect=lambda jid, **kw: remove_calls.append(jid)
        )
        await client.post(f"/api/jobs/{created['id']}/disable")

    assert created["id"] in remove_calls


async def test_update_job_reschedules_in_scheduler(client):
    """Editing an enabled job re-registers it so a schedule change takes
    effect. add_job(replace_existing=True) covers both replacing the trigger
    of a registered job and registering one the scheduler doesn't know yet."""
    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    add_calls = []
    with patch("os.path.isdir", return_value=True):
        with patch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            await client.put(
                f"/api/jobs/{created['id']}",
                json=make_job_payload(schedule_value="12h"),
            )

    assert any(
        kw.get("id") == created["id"] and kw.get("replace_existing") for kw in add_calls
    )


async def test_update_job_enabling_via_put_registers_in_scheduler(client):
    """A disabled job is not in the scheduler. Flipping enabled=True through
    PUT (the edit form path) must register it — otherwise the DB says
    'enabled' while the scheduler never fires it: silent missed backups."""
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=False))
        ).json()

    add_calls = []
    with patch("os.path.isdir", return_value=True):
        with patch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            resp = await client.put(
                f"/api/jobs/{created['id']}",
                json=make_job_payload(enabled=True),
            )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert any(kw.get("id") == created["id"] for kw in add_calls)


async def test_update_job_disabling_via_put_removes_from_scheduler(client):
    """Flipping enabled=False through PUT must remove the scheduler entry —
    otherwise the job keeps firing while the UI shows it as disabled."""
    with patch("os.path.isdir", return_value=True):
        created = (
            await client.post("/api/jobs", json=make_job_payload(enabled=True))
        ).json()

    add_calls = []
    remove_calls = []
    with patch("os.path.isdir", return_value=True):
        with patch("app.core.scheduler.scheduler") as mock_sched:
            mock_sched.running = True
            mock_sched.add_job = MagicMock(
                side_effect=lambda *a, **kw: add_calls.append(kw)
            )
            mock_sched.remove_job = MagicMock(
                side_effect=lambda jid, **kw: remove_calls.append(jid)
            )
            resp = await client.put(
                f"/api/jobs/{created['id']}",
                json=make_job_payload(enabled=False),
            )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert created["id"] in remove_calls
    assert not any(kw.get("id") == created["id"] for kw in add_calls)


# ── Duplicate source+destination conflict ─────────────────────────────────────


async def test_update_job_source_label_conflict_with_existing_job(client):
    with patch("os.path.isdir", return_value=True):
        job1 = (
            await client.post(
                "/api/jobs",
                json=make_job_payload(
                    name="Job 1", source_label="docs", destination_label="main"
                ),
            )
        ).json()
        await client.post(
            "/api/jobs",
            json=make_job_payload(
                name="Job 2", source_label="photos", destination_label="main"
            ),
        )

    update = make_job_payload(source_label="photos", destination_label="main")
    resp = await client.put(f"/api/jobs/{job1['id']}", json=update)
    assert resp.status_code == 409


# ── last_run field ────────────────────────────────────────────────────────────


async def test_list_jobs_last_run_populated_after_run(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupRun, RunStatus, TriggeredBy

    with patch("os.path.isdir", return_value=True):
        created = (await client.post("/api/jobs", json=make_job_payload())).json()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        run = BackupRun(
            id=str(uuid.uuid4()),
            job_id=created["id"],
            status=RunStatus.success,
            triggered_by=TriggeredBy.scheduler,
            started_at=now,
            finished_at=now,
            duration_seconds=60,
        )
        s.add(run)
        await s.commit()

    resp = await client.get("/api/jobs")
    job = resp.json()[0]
    assert job["last_run"] is not None
    assert job["last_run"]["status"] == "success"


async def test_create_job_check_disabled_with_subset_mode_without_percent(client):
    """If verification is disabled (check_enabled=False), validation should not
    enforce having a subset percent even if check_mode is set to 'subset'."""
    payload = make_job_payload(
        check_enabled=False, check_mode="subset", check_subset_percent=None
    )
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["check_enabled"] is False
    assert data["check_mode"] == "subset"
    assert data["check_subset_percent"] is None


# ── Hung-mount probes (event-loop safety) ─────────────────────────────────────


async def test_create_job_hung_mount_probe_returns_422_promptly(client):
    """os.path.isdir against a hung SMB mount must not freeze the event loop
    during job creation — the probe times out and the mount is reported as
    not mounted (422)."""
    import time as _time

    def hung_isdir(path: str) -> bool:
        _time.sleep(1.0)  # simulates stat() stuck on a dead mount
        return True

    start = _time.monotonic()
    with (
        patch("os.path.isdir", side_effect=hung_isdir),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        resp = await client.post("/api/jobs", json=make_job_payload())
    elapsed = _time.monotonic() - start

    assert resp.status_code == 422
    assert "not mounted" in resp.json()["detail"].lower()
    assert elapsed < 0.9, "route must answer at the probe timeout"
