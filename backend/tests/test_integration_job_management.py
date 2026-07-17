"""End-to-end integration tests for the job CRUD lifecycle and constraints.

Exercises the full backend stack — real FastAPI app, real middleware, real
route handlers, real DB session — with only ``os.path.isdir`` mocked so
mount validation passes without a real ``/sources`` and ``/destinations``
filesystem.

Each test walks a distinct critical user journey:

- CRUD + enable/disable lifecycle (job appears → updates → toggled →
  deleted; scheduler is mocked but observed for register/remove calls).
- Destination-label immutability after creation.
- Restic password immutability after the first successful run.
- Source-subpath uniqueness — different subpaths under the same label and
  destination must coexist (design doc §6).
"""

from unittest.mock import MagicMock, patch

from httpx import AsyncClient

from tests.conftest import make_job_payload


async def test_job_full_crud_and_enable_disable_cycle(client: AsyncClient) -> None:
    """Create → fetch → update → disable → enable → delete → 404 on fetch.

    Verifies all six CRUD/state endpoints work in sequence and that scheduler
    register/remove calls are made at the right boundaries (mocked so we
    don't need a live APScheduler).
    """
    mock_sched = MagicMock()
    mock_sched.running = True
    mock_sched.add_job = MagicMock()
    mock_sched.remove_job = MagicMock()
    mock_sched.get_job = MagicMock(return_value=None)
    mock_sched.reschedule_job = MagicMock()

    with (
        patch("os.path.isdir", return_value=True),
        patch("app.core.scheduler.scheduler", mock_sched),
        patch("app.api.routes.jobs.scheduler_module.scheduler", mock_sched),
    ):
        # Create
        create_resp = await client.post(
            "/api/jobs", json=make_job_payload(name="Original")
        )
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]
        # Enabled job → scheduler.add_job was called.
        assert mock_sched.add_job.call_count == 1

        # Fetch
        get_resp = await client.get(f"/api/jobs/{job_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == "Original"

        # Update a mutable field (name/destination/password are immutable —
        # they address the repository; see test_job_name_immutable_*).
        update_payload = make_job_payload(name="Original", schedule_value="12h")
        update_resp = await client.put(f"/api/jobs/{job_id}", json=update_payload)
        assert update_resp.status_code == 200
        assert update_resp.json()["schedule_value"] == "12h"

        # Disable
        mock_sched.remove_job.reset_mock()
        disable_resp = await client.post(f"/api/jobs/{job_id}/disable")
        assert disable_resp.status_code == 200
        assert disable_resp.json()["enabled"] is False
        assert mock_sched.remove_job.call_count == 1

        # Enable
        mock_sched.add_job.reset_mock()
        enable_resp = await client.post(f"/api/jobs/{job_id}/enable")
        assert enable_resp.status_code == 200
        assert enable_resp.json()["enabled"] is True
        assert mock_sched.add_job.call_count == 1

        # Delete
        mock_sched.remove_job.reset_mock()
        delete_resp = await client.delete(f"/api/jobs/{job_id}")
        assert delete_resp.status_code == 204
        assert mock_sched.remove_job.call_count == 1

        # 404 after delete
        gone_resp = await client.get(f"/api/jobs/{job_id}")
        assert gone_resp.status_code == 404


async def test_destination_label_immutable_after_creation(client: AsyncClient) -> None:
    """Per design doc §5 + §6: destination_label is immutable from creation.

    Attempting to change it via PUT returns 422 — the on-disk repo path
    embeds the label, so changing it would orphan the existing repo.
    """
    with patch("os.path.isdir", return_value=True):
        create_resp = await client.post(
            "/api/jobs", json=make_job_payload(destination_label="main")
        )
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]

        # Attempt to change destination_label.
        update_payload = make_job_payload(destination_label="archive")
        update_resp = await client.put(f"/api/jobs/{job_id}", json=update_payload)
        assert update_resp.status_code == 422
        assert "destination" in update_resp.text.lower()


async def test_restic_password_immutable_from_creation(
    client: AsyncClient,
) -> None:
    """restic_password is immutable as soon as the job exists.

    The repository is initialized (and encrypted with this password) during
    POST /api/jobs, so there is no window in which the field can be changed
    safely — changing it could never change the on-disk repo's password, that
    requires `restic key add/remove`.
    """
    with patch("os.path.isdir", return_value=True):
        create_resp = await client.post(
            "/api/jobs",
            json=make_job_payload(restic_password="original-password"),
        )
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]

        locked_payload = make_job_payload(restic_password="should-fail")
        locked_resp = await client.put(f"/api/jobs/{job_id}", json=locked_payload)
        assert locked_resp.status_code == 422
        assert "password" in locked_resp.text.lower()


async def test_job_name_immutable_from_creation(client: AsyncClient) -> None:
    """name addresses the repository directory, so it locks at creation."""
    with patch("os.path.isdir", return_value=True):
        create_resp = await client.post(
            "/api/jobs", json=make_job_payload(name="photos")
        )
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]

        resp = await client.put(
            f"/api/jobs/{job_id}", json=make_job_payload(name="pictures")
        )
        assert resp.status_code == 422
        assert "name" in resp.text.lower()


async def test_two_jobs_with_same_labels_different_subpaths_coexist(
    client: AsyncClient,
) -> None:
    """Per design doc §6: duplicate key is (source_label, source_subpath,
    destination_label). Two jobs with same labels but different subpaths
    must both succeed."""
    with patch("os.path.isdir", return_value=True):
        first = await client.post(
            "/api/jobs",
            json=make_job_payload(
                name="Photos backup",
                source_label="documents",
                source_subpath="photos",
            ),
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/jobs",
            json=make_job_payload(
                name="Videos backup",
                source_label="documents",
                source_subpath="videos",
            ),
        )
        assert second.status_code == 201

        list_resp = await client.get("/api/jobs")
        assert list_resp.status_code == 200
        names = {j["name"] for j in list_resp.json()}
        assert names == {"Photos backup", "Videos backup"}
