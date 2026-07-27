"""Tests for /api/mounts/* endpoints."""

import uuid
from unittest.mock import patch


async def _create_job(client, source_label: str, destination_label: str) -> dict:
    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/jobs",
            json={
                "name": f"Job {source_label}",
                "source_label": source_label,
                "destination_label": destination_label,
                "restic_password": "pw",
                "schedule_type": "interval",
                "schedule_value": "6h",
            },
        )
    return resp.json()


# ── GET /api/mounts/sources ───────────────────────────────────────────────────


async def test_list_sources_empty(client):
    with patch("os.scandir") as mock_scandir:
        mock_scandir.return_value.__enter__ = lambda s: iter([])
        mock_scandir.return_value.__exit__ = lambda *a: None
        resp = await client.get("/api/mounts/sources")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_list_sources_returns_directory_names(client, tmp_path):
    sources = tmp_path / "sources"
    (sources / "documents").mkdir(parents=True)
    (sources / "photos").mkdir()

    with patch("app.api.routes.mounts.SOURCES_ROOT", str(sources)):
        resp = await client.get("/api/mounts/sources")
    assert resp.status_code == 200
    labels = resp.json()
    assert "documents" in labels
    assert "photos" in labels


async def test_list_sources_filters_files(client, tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "adir").mkdir()
    (sources / "afile.txt").write_text("not a dir")

    with patch("app.api.routes.mounts.SOURCES_ROOT", str(sources)):
        resp = await client.get("/api/mounts/sources")
    labels = resp.json()
    assert "adir" in labels
    assert "afile.txt" not in labels


# ── GET /api/mounts/destinations ─────────────────────────────────────────────


async def test_list_destinations_returns_directory_names(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "offsite").mkdir()

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.get("/api/mounts/destinations")
    assert resp.status_code == 200
    labels = resp.json()
    assert "main" in labels
    assert "offsite" in labels


# ── POST /api/mounts/destinations/rename ─────────────────────────────────────


async def test_rename_destination_success(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "newlabel").mkdir(parents=True)

    await _create_job(client, "docs", "oldlabel")
    await _create_job(client, "photos", "oldlabel")

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "oldlabel",
                "new_label": "newlabel",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "affected_jobs" in data
    assert len(data["affected_jobs"]) == 2


async def test_rename_destination_updates_all_jobs(client, tmp_path, engine):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import BackupJob

    dests = tmp_path / "destinations"
    (dests / "newlabel").mkdir(parents=True)

    await _create_job(client, "docs", "oldlabel")
    await _create_job(client, "photos", "oldlabel")

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "oldlabel",
                "new_label": "newlabel",
            },
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        result = await s.execute(
            select(BackupJob).where(BackupJob.destination_label == "newlabel")
        )
        jobs = result.scalars().all()
    assert len(jobs) == 2


async def test_rename_destination_new_not_mounted_returns_422(client, tmp_path):
    dests = tmp_path / "destinations"
    dests.mkdir()

    await _create_job(client, "docs", "oldlabel")

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "oldlabel",
                "new_label": "notmounted",
            },
        )
    assert resp.status_code == 422
    assert "not mounted" in resp.json()["detail"].lower()


async def test_rename_destination_no_jobs_returns_404(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "newlabel").mkdir(parents=True)

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "nonexistent",
                "new_label": "newlabel",
            },
        )
    assert resp.status_code == 404


async def test_rename_destination_same_label_returns_422(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    await _create_job(client, "docs", "main")

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "main",
                "new_label": "main",
            },
        )
    assert resp.status_code == 422


async def test_rename_destination_invalid_new_label(client, tmp_path):
    await _create_job(client, "docs", "main")
    resp = await client.post(
        "/api/mounts/destinations/rename",
        json={
            "old_label": "main",
            "new_label": "bad/label",
        },
    )
    assert resp.status_code == 422


async def test_rename_destination_new_label_dot_or_dotdot_rejected(client, tmp_path):
    """new_label feeds destination_label, which is concatenated into the repo
    path — '.' and '..' would resolve outside /destinations/<label>."""
    await _create_job(client, "docs", "main")
    for bad in (".", ".."):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={"old_label": "main", "new_label": bad},
        )
        assert resp.status_code == 422, f"new_label={bad!r} was not rejected"
        # Must be the schema validator, not the "not mounted" route check.
        assert "new_label" in resp.json()["detail"]


async def test_rename_destination_active_run_returns_409(client, tmp_path):
    from app.services import backup_runner

    dests = tmp_path / "destinations"
    (dests / "newlabel").mkdir(parents=True)

    job = await _create_job(client, "docs", "oldlabel")
    job_uuid = uuid.UUID(job["id"])
    backup_runner.active_jobs.add(job_uuid)

    try:
        with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
            resp = await client.post(
                "/api/mounts/destinations/rename",
                json={
                    "old_label": "oldlabel",
                    "new_label": "newlabel",
                },
            )
        assert resp.status_code == 409
        assert "in progress" in resp.json()["detail"].lower()
    finally:
        backup_runner.active_jobs.discard(job_uuid)


async def test_rename_does_not_require_old_label_mounted(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "newlabel").mkdir(parents=True)

    await _create_job(client, "docs", "oldlabel")

    with patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={
                "old_label": "oldlabel",
                "new_label": "newlabel",
            },
        )
    assert resp.status_code == 200


# ── Hung-mount probes (event-loop safety) ─────────────────────────────────────


async def test_list_sources_hung_scandir_returns_empty_promptly(client):
    """A scandir against a hung SMB-backed root must not freeze the event
    loop — the route returns an empty list once the probe timeout expires."""
    import time as _time

    def hung_list(root: str):
        _time.sleep(1.0)  # simulates scandir stuck on a dead mount
        return ["nas"]

    start = _time.monotonic()
    with (
        patch("app.api.routes.mounts._list_dirs", side_effect=hung_list),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        resp = await client.get("/api/mounts/sources")
    elapsed = _time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json() == []
    assert elapsed < 0.9, "route must answer at the probe timeout"
