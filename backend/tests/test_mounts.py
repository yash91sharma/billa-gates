"""Tests for /api/mounts/* endpoints."""

import os
import shutil
import uuid
from unittest.mock import patch

import pytest

from app.services import destination_usage


@pytest.fixture(autouse=True)
def _clear_destination_usage_cache():
    """The usage endpoint caches per label; keep one test out of the next."""
    destination_usage._clear_cache()
    yield
    destination_usage._clear_cache()


def _usage(total: int, used: int, free: int):
    return shutil._ntuple_diskusage(total=total, used=used, free=free)


def _usage_only_for_real_dirs(total: int = 1000, used: int = 600, free: int = 300):
    """A disk_usage stand-in that fails on a path that isn't there, the way the
    real one does — so a test about a missing destination measures nothing."""

    def fake(path, *args, **kwargs):
        if not os.path.isdir(str(path)):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return _usage(total, used, free)

    return fake


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


# ── GET /api/mounts/destinations/usage ────────────────────────────────────────


async def test_destination_usage_lists_every_directory_under_destinations(
    client, tmp_path
):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "offsite").mkdir()
    (dests / "notadrive.txt").write_text("not a dir")

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    assert resp.status_code == 200
    body = resp.json()
    labels = [d["label"] for d in body["destinations"]]
    assert labels == ["main", "offsite"]
    assert "notadrive.txt" not in labels


async def test_destination_usage_exposes_exactly_the_documented_keys(client, tmp_path):
    """Pins the response contract the frontend types mirror."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    body = resp.json()
    assert set(body) == {"measured_at", "destinations"}
    assert set(body["destinations"][0]) == {
        "label",
        "path",
        "available",
        "unavailable_reason",
        "total_bytes",
        "used_bytes",
        "free_bytes",
        "reserved_bytes",
        "percent_used",
        "filesystem_id",
        "is_separate_mount",
        "shares_filesystem_with",
        "sentinel_present",
        "job_count",
        "job_names",
        "measured_at",
    }


async def test_destination_usage_reports_the_measured_bytes_and_percent(
    client, tmp_path
):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    row = resp.json()["destinations"][0]
    assert row["available"] is True
    assert (row["total_bytes"], row["used_bytes"], row["free_bytes"]) == (
        1000,
        600,
        300,
    )
    assert row["reserved_bytes"] == 100
    assert row["percent_used"] == 60.0


async def test_destination_usage_includes_job_count_and_names(client, tmp_path):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "offsite").mkdir()
    (dests / "spare").mkdir()

    await _create_job(client, "docs", "main")
    await _create_job(client, "photos", "main")
    await _create_job(client, "music", "offsite")

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    rows = {d["label"]: d for d in resp.json()["destinations"]}
    assert rows["main"]["job_count"] == 2
    assert sorted(rows["main"]["job_names"]) == ["Job docs", "Job photos"]
    assert rows["offsite"]["job_count"] == 1
    assert rows["spare"]["job_count"] == 0
    assert rows["spare"]["job_names"] == []


async def test_destination_usage_lists_a_referenced_label_whose_directory_is_gone(
    client, tmp_path
):
    """A drive a job still points at must not vanish from the page that exists
    to tell you about drives — it is listed as unavailable instead."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    await _create_job(client, "docs", "detached")

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", side_effect=_usage_only_for_real_dirs()),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    rows = {d["label"]: d for d in resp.json()["destinations"]}
    assert set(rows) == {"main", "detached"}
    assert rows["detached"]["available"] is False
    assert rows["detached"]["unavailable_reason"]
    assert rows["detached"]["total_bytes"] is None
    assert rows["detached"]["job_count"] == 1


async def test_destination_usage_measured_at_is_the_oldest_row(client, tmp_path):
    """The envelope stamp drives the page's "as of …", so it must be the
    stalest row's — never fresher than the oldest number on screen."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "offsite").mkdir()

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    body = resp.json()
    stamps = [d["measured_at"] for d in body["destinations"]]
    assert body["measured_at"] == min(stamps)
    assert body["measured_at"].endswith("Z"), "the browser needs the UTC designator"


async def test_destination_usage_one_hung_destination_still_returns_the_others(
    client, tmp_path
):
    """One dead drive costs one row, not the page."""
    import time as _time

    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "nas").mkdir()

    def slow_or_fast(path, *args, **kwargs):
        if str(path).endswith("nas"):
            _time.sleep(1.0)
        return _usage(1000, 600, 300)

    start = _time.monotonic()
    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", side_effect=slow_or_fast),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        resp = await client.get("/api/mounts/destinations/usage")
    elapsed = _time.monotonic() - start

    assert resp.status_code == 200
    rows = {d["label"]: d for d in resp.json()["destinations"]}
    assert rows["main"]["available"] is True
    assert rows["nas"]["available"] is False
    assert elapsed < 0.9


async def test_destination_usage_hung_root_returns_no_destinations_promptly(client):
    import time as _time

    def hung_list(root: str):
        _time.sleep(1.0)
        return ["nas"]

    start = _time.monotonic()
    with (
        patch("app.api.routes.mounts._list_dirs", side_effect=hung_list),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        resp = await client.get("/api/mounts/destinations/usage")
    elapsed = _time.monotonic() - start

    assert resp.status_code == 200
    assert resp.json()["destinations"] == []
    assert elapsed < 0.9


async def test_destination_usage_missing_root_returns_no_destinations(client, tmp_path):
    missing = tmp_path / "nope"

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(missing)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(missing)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    assert resp.status_code == 200
    assert resp.json()["destinations"] == []


async def test_destination_usage_refresh_bypasses_the_cache(client, tmp_path):
    """A Refresh button that hands back a five-minute-old number reads as
    broken, so ?refresh=true re-probes."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    calls: list[str] = []

    def counting(path, *args, **kwargs):
        calls.append(str(path))
        return _usage(1000, 600, 300)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", side_effect=counting),
    ):
        await client.get("/api/mounts/destinations/usage")
        await client.get("/api/mounts/destinations/usage")
        assert len(calls) == 1, "the second read must be served from cache"
        await client.get("/api/mounts/destinations/usage?refresh=true")

    assert len(calls) == 2


async def test_destination_usage_flags_a_label_that_is_not_its_own_mount(
    client, tmp_path
):
    """A plain folder under /destinations reports the container's filesystem;
    the row has to say so rather than imply a dedicated drive."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    row = resp.json()["destinations"][0]
    assert row["is_separate_mount"] is False
    assert row["total_bytes"] == 1000, "the numbers must not be blanked"


async def test_destination_usage_cross_references_labels_on_one_filesystem(
    client, tmp_path
):
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)
    (dests / "spare").mkdir()

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    rows = {d["label"]: d for d in resp.json()["destinations"]}
    assert rows["main"]["shares_filesystem_with"] == ["spare"]
    assert rows["spare"]["shares_filesystem_with"] == ["main"]


async def test_destination_usage_reports_the_sentinel_from_the_shared_checker(
    client, tmp_path
):
    """The marker file is the app's definition of "really attached", and the row
    must use the same check a run does — not its own copy of the path."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=False,
        ),
    ):
        resp = await client.get("/api/mounts/destinations/usage")

    assert resp.json()["destinations"][0]["sentinel_present"] is False


async def test_the_plain_destinations_list_still_returns_bare_labels(client, tmp_path):
    """Regression guard: the new sub-path must not shadow GET /destinations,
    and that endpoint's List[str] shape is unchanged."""
    dests = tmp_path / "destinations"
    (dests / "main").mkdir(parents=True)

    with (
        patch("app.api.routes.mounts.DESTINATIONS_ROOT", str(dests)),
        patch("app.services.destination_usage.DESTINATIONS_ROOT", str(dests)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        plain = await client.get("/api/mounts/destinations")
        usage = await client.get("/api/mounts/destinations/usage")

    assert plain.status_code == 200
    assert plain.json() == ["main"]
    assert usage.status_code == 200
    assert usage.json()["destinations"][0]["label"] == "main"
