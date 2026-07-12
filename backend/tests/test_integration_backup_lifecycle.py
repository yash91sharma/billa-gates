"""End-to-end integration test for the backup lifecycle.

Exercises the full backend stack — real FastAPI app, real middleware, real
route handlers, real DB session, real ``backup_runner`` orchestration — with
only the external boundaries mocked:

- ``restic`` subprocess wrappers (``restic`` isn't installed in the dev
  container).
- ``app.services.snapshot_listing.list_snapshots`` (would otherwise shell
  out to restic).
- ``os.path.isdir`` (so mount validation passes without real ``/sources``
  and ``/destinations`` directories).
- ``send_notification`` (no outbound network).

This catches bugs at the seams that unit tests miss: route ↔ DB,
middleware ↔ handler, backup_runner ↔ DB, schema serialization, request-ID
propagation, BackgroundTasks wiring.
"""

import asyncio
import json
import uuid
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.services import backup_runner
from tests.conftest import make_job_payload

# ── Restic mock payloads ──────────────────────────────────────────────────────

# 64-char hex snapshot ID matching restic's real format.
_SNAPSHOT_ID: str = "a" * 64

# Realistic restic JSON summary emitted as the last line of `restic backup --json`.
_BACKUP_SUMMARY: Dict[str, Any] = {
    "message_type": "summary",
    "files_new": 10,
    "files_changed": 5,
    "files_unmodified": 1000,
    "dirs_new": 2,
    "dirs_changed": 1,
    "dirs_unmodified": 50,
    "data_added": 1024 * 1024 * 50,
    "data_added_packed": 1024 * 1024 * 45,
    "total_bytes_processed": 1024 * 1024 * 500,
    "snapshot_id": _SNAPSHOT_ID,
}

# Normalized shape returned by `list_snapshots` (the snapshot-listing service
# translates restic's raw keys `id`/`time`/`total_size` into the API's stable
# `snapshot_id`/`snapshot_time`/`size_bytes` names).
_SNAPSHOT_FROM_RESTIC: List[Dict[str, Any]] = [
    {
        "snapshot_id": _SNAPSHOT_ID,
        "snapshot_time": "2026-05-19T12:00:00Z",
        "hostname": "integration-host",
        "paths": ["/sources/documents"],
        "tags": ["integration"],
        "size_bytes": 1024 * 1024 * 500,
    }
]


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _wait_for_terminal_status(
    client: AsyncClient, run_id: str, *, timeout_s: float = 5.0
) -> Dict[str, Any]:
    """Poll GET /api/runs/{id} until status is no longer 'running' or timeout."""
    deadline_iterations: int = int(timeout_s * 100)
    for _ in range(deadline_iterations):
        resp = await client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        data: Dict[str, Any] = resp.json()
        if data["status"] != "running":
            return data
        await asyncio.sleep(0.01)
    pytest.fail(f"Run {run_id} did not reach terminal status within {timeout_s}s")


# ── Test ──────────────────────────────────────────────────────────────────────


async def test_full_backup_lifecycle_success(client: AsyncClient) -> None:
    """End-to-end success path: create → trigger → finish → fetch detail/snapshots.

    Verifies the full chain works: the request-logging middleware does not
    swallow exceptions, route validation accepts the payload, the job persists
    with computed fields populated, the run row is created, the orchestrator
    runs all 12 steps, restic JSON stats land on the run row, the snapshot
    table is reconciled, and the response schemas match the frontend's expected
    shapes.
    """
    with (
        patch("os.path.isdir", return_value=True),
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(0, "{}", "")),
        ),
        patch(
            "app.services.restic.restic_backup",
            new=AsyncMock(
                return_value=(0, json.dumps(_BACKUP_SUMMARY), "", _BACKUP_SUMMARY)
            ),
        ),
        patch(
            "app.services.snapshot_listing.list_snapshots",
            new=AsyncMock(return_value=_SNAPSHOT_FROM_RESTIC),
        ),
        patch(
            "app.services.restic.restic_forget",
            new=AsyncMock(return_value=(0, "ok", "")),
        ),
        patch(
            "app.services.restic.restic_prune",
            new=AsyncMock(return_value=(0, "ok", "")),
        ),
        patch(
            "app.services.backup_runner.send_notification",
            new=AsyncMock(return_value=None),
        ),
    ):
        # 1. Create job ──────────────────────────────────────────────────────
        create_resp = await client.post("/api/jobs", json=make_job_payload())
        assert create_resp.status_code == 201, create_resp.text
        job = create_resp.json()
        job_id: str = job["id"]
        # restic_password must never leak in responses (security invariant).
        assert job["restic_password"] is None
        # Computed fields present.
        assert job["has_successful_run"] is False
        assert job["last_run"] is None

        # 2. Trigger manual run ─────────────────────────────────────────────
        run_resp = await client.post(f"/api/jobs/{job_id}/run")
        assert run_resp.status_code == 200, run_resp.text
        run_id: str = run_resp.json()["run_id"]

        # 3. Wait for orchestrator to finish (BackgroundTasks timing varies
        #    under ASGITransport, so polling is the robust pattern).
        detail = await _wait_for_terminal_status(client, run_id)

        # 4. Run reached success terminal state ─────────────────────────────
        assert detail["status"] == "success", detail
        assert detail["files_new"] == 10
        assert detail["files_changed"] == 5
        assert detail["data_added_bytes"] == 50 * 1024 * 1024
        assert detail["snapshot_id"] == _SNAPSHOT_ID
        assert detail["duration_seconds"] is not None
        assert detail["finished_at"] is not None
        # No retention configured on this job → forget+prune are skipped
        # entirely (gaps.md H1). check is skipped because check_enabled=False.
        assert detail["prune_status"] == "skipped"
        assert detail["check_status"] == "skipped"
        # error_output stays null on success.
        assert detail["error_output"] is None
        # backup_output captures the full stdout.
        assert detail["backup_output"] is not None
        assert _SNAPSHOT_ID in detail["backup_output"]

        # 5. Snapshot listing route queries restic live ─────────────────────
        snaps_resp = await client.get(f"/api/jobs/{job_id}/snapshots")
        assert snaps_resp.status_code == 200
        snapshots = snaps_resp.json()
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["snapshot_id"] == _SNAPSHOT_ID
        assert snap["hostname"] == "integration-host"
        assert snap["paths"] == ["/sources/documents"]
        assert snap["size_bytes"] == 500 * 1024 * 1024
        # Run-to-snapshot linkage lives on BackupRun (not the listing payload,
        # which is the unfiltered restic output).
        run_detail = await client.get(f"/api/runs/{run_id}")
        assert run_detail.json()["snapshot_id"] == _SNAPSHOT_ID

        # 6. Job reports has_successful_run=True after the run ──────────────
        job_resp = await client.get(f"/api/jobs/{job_id}")
        assert job_resp.status_code == 200
        job_after = job_resp.json()
        assert job_after["has_successful_run"] is True
        assert job_after["last_run"] is not None
        assert job_after["last_run"]["status"] == "success"

        # 7. Run shows up in the recent-runs feed (used by Dashboard) ──────
        recent_resp = await client.get("/api/runs/recent")
        assert recent_resp.status_code == 200
        recent = recent_resp.json()
        assert any(r["id"] == run_id for r in recent)

    # Clean up the in-memory active-jobs registry so a re-run in the same
    # session starts from a clean slate (run_backup adds the job uuid then
    # removes it in finally; this is a safety net against any leak).
    backup_runner.active_jobs.discard(uuid.UUID(job_id))


async def test_full_backup_lifecycle_failure(client: AsyncClient) -> None:
    """End-to-end failure path: restic_backup exits non-zero.

    Verifies the failure branch of backup_runner: run finalized as failed,
    error_output populated, no snapshot reconciled, prune and check both
    marked skipped (steps 8-9 and 12 are not reached on failure per design
    doc §8 step 10).
    """
    with (
        patch("os.path.isdir", return_value=True),
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(0, "{}", "")),
        ),
        patch(
            "app.services.restic.restic_backup",
            new=AsyncMock(
                return_value=(1, "", "Fatal: permission denied: /sources/foo", None)
            ),
        ),
        patch(
            "app.services.snapshot_listing.list_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.backup_runner.send_notification",
            new=AsyncMock(return_value=None),
        ),
    ):
        create_resp = await client.post("/api/jobs", json=make_job_payload())
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]

        run_resp = await client.post(f"/api/jobs/{job_id}/run")
        assert run_resp.status_code == 200
        run_id: str = run_resp.json()["run_id"]

        detail = await _wait_for_terminal_status(client, run_id)

        assert detail["status"] == "failed"
        assert detail["error_output"] is not None
        assert "permission denied" in detail["error_output"]
        assert detail["snapshot_id"] is None
        assert detail["files_new"] is None  # restic stats never populated
        assert detail["prune_status"] == "skipped"
        assert detail["check_status"] == "skipped"

        # Restic is the source of truth for snapshots — the failed run never
        # created one, so the listing route returns the empty list.
        snaps_resp = await client.get(f"/api/jobs/{job_id}/snapshots")
        assert snaps_resp.status_code == 200
        assert snaps_resp.json() == []

        # Job's has_successful_run stays False after a failed run.
        job_resp = await client.get(f"/api/jobs/{job_id}")
        assert job_resp.json()["has_successful_run"] is False

    backup_runner.active_jobs.discard(uuid.UUID(job_id))


async def test_full_backup_lifecycle_with_verification(client: AsyncClient) -> None:
    """End-to-end manually triggered check: check triggered via POST /check, runs,
    sets kind=check, and check_status=passed.
    """
    payload = make_job_payload()
    with (
        patch("os.path.isdir", return_value=True),
        patch(
            "app.services.restic.restic_check",
            new=AsyncMock(return_value=(0, "no errors found", "")),
        ),
        patch(
            "app.services.backup_runner.send_notification",
            new=AsyncMock(return_value=None),
        ),
    ):
        create_resp = await client.post("/api/jobs", json=payload)
        assert create_resp.status_code == 201, create_resp.text
        job_id: str = create_resp.json()["id"]

        run_resp = await client.post(
            f"/api/jobs/{job_id}/check", json={"check_mode": "structural"}
        )
        assert run_resp.status_code == 200, run_resp.text
        run_id: str = run_resp.json()["run_id"]

        detail = await _wait_for_terminal_status(client, run_id)

        assert detail["status"] == "success"
        assert detail["kind"] == "check"
        assert detail["check_status"] == "passed"
        assert detail["check_error_output"] is None

    backup_runner.active_jobs.discard(uuid.UUID(job_id))


async def test_overlapping_manual_run_returns_409_and_records_skipped_row(
    client: AsyncClient,
) -> None:
    """End-to-end overlap: when a run is in progress, a second manual trigger
    returns 409 (the contract the UI pattern-matches on) while a skipped
    audit row is still recorded (per design doc §7 concurrent run guard)."""
    with patch("os.path.isdir", return_value=True):
        create_resp = await client.post("/api/jobs", json=make_job_payload())
        assert create_resp.status_code == 201
        job_id: str = create_resp.json()["id"]
        job_uuid = uuid.UUID(job_id)

    # Simulate an in-flight run by directly populating the active-jobs set —
    # this matches the runtime state the trigger_run handler checks.
    backup_runner.active_jobs.add(job_uuid)
    try:
        run_resp = await client.post(f"/api/jobs/{job_id}/run")
        assert run_resp.status_code == 409
        assert "in progress" in run_resp.json()["detail"].lower()
    finally:
        backup_runner.active_jobs.discard(job_uuid)

    # The skipped row is recorded immediately (no backup_runner involved).
    runs_resp = await client.get(f"/api/jobs/{job_id}/runs")
    assert runs_resp.status_code == 200
    skipped = [r for r in runs_resp.json() if r["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "overlapping_run"
    assert skipped[0]["triggered_by"] == "manual"
