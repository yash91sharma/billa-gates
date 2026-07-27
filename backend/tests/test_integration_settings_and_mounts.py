"""End-to-end integration tests for settings, mounts, and health endpoints.

Exercises the full backend stack with only external boundaries mocked:

- ``os.path.isdir`` and ``os.scandir`` for mount listing without a real
  filesystem.
- ``httpx.AsyncClient`` for the ntfy POST and GitHub Releases GET calls.

Covered critical user journeys:

- AppSettings get → update → get reflects changes.
- Test ntfy notification (success + failure response paths).
- Destination rename propagates to all jobs that reference the old label.
- Health endpoint reflects scheduler running state, DB liveness, and
  detected restic version.
- Restic update-check returns current vs latest from the GitHub API.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AppSettings
from tests.conftest import make_job_payload

# ── AppSettings lifecycle ────────────────────────────────────────────────────


async def test_settings_get_update_roundtrip(client: AsyncClient) -> None:
    """GET defaults → PUT updates → GET reflects new values; ntfy_token is
    accepted on input but never returned (security invariant)."""
    # GET on a fresh DB creates the singleton row with defaults.
    initial = await client.get("/api/settings")
    assert initial.status_code == 200
    assert initial.json()["ntfy_token"] is None  # never returned

    # PUT with new values, including a token.
    update_payload = {
        "ntfy_server_url": "https://ntfy.example.com",
        "ntfy_topic": "home-backup",
        "ntfy_token": "secret-token-do-not-leak",
        "notify_on_start": False,
        "notify_on_success": True,
        "notify_on_failure": True,
        "notify_on_verification": False,
        "default_job_timeout_hours": 12,
    }
    update_resp = await client.put("/api/settings", json=update_payload)
    assert update_resp.status_code == 200
    updated = update_resp.json()
    assert updated["ntfy_server_url"] == "https://ntfy.example.com"
    assert updated["ntfy_topic"] == "home-backup"
    assert updated["default_job_timeout_hours"] == 12
    assert updated["notify_on_start"] is False
    # Token never returned, even on the response that just accepted it.
    assert updated["ntfy_token"] is None

    # Re-GET shows persisted values.
    final = await client.get("/api/settings")
    assert final.json()["ntfy_topic"] == "home-backup"
    assert final.json()["ntfy_token"] is None


# ── Test ntfy notification ───────────────────────────────────────────────────


async def test_test_ntfy_success(client: AsyncClient) -> None:
    """POST /settings/test-ntfy → 200 from the mocked ntfy server → ok=True."""
    # Configure a topic first.
    await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.example.com",
            "ntfy_topic": "alerts",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = "ok"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "error": None}


async def test_test_ntfy_failure_returns_error_field(client: AsyncClient) -> None:
    """When the ntfy server returns non-200, the route returns ok=False with
    an error string — it never raises an HTTPException so the UI can show
    the user a useful message."""
    await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.example.com",
            "ntfy_topic": "alerts",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "unauthorized"

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 200  # never raises
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] is not None
    assert "401" in body["error"]


async def test_test_ntfy_empty_topic_returns_422(client: AsyncClient) -> None:
    """When ntfy_topic is empty, the route returns 422 (can't notify nowhere)."""
    # Default settings have empty topic.
    resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 422
    assert "topic" in resp.text.lower()


# ── Mount listing ────────────────────────────────────────────────────────────


async def test_list_sources_and_destinations(client: AsyncClient) -> None:
    """GET /mounts/sources and /mounts/destinations return the scanned mount
    directory names."""

    def fake_scandir(path: str):
        if path == "/sources":
            return _fake_dir_iter(["documents", "photos"])
        if path == "/destinations":
            return _fake_dir_iter(["main", "archive"])
        return _fake_dir_iter([])

    with patch("os.scandir", side_effect=fake_scandir):
        srcs = await client.get("/api/mounts/sources")
        assert srcs.status_code == 200
        assert set(srcs.json()) == {"documents", "photos"}

        dsts = await client.get("/api/mounts/destinations")
        assert dsts.status_code == 200
        assert set(dsts.json()) == {"main", "archive"}


# ── Destination rename ───────────────────────────────────────────────────────


async def test_rename_destination_updates_all_referencing_jobs(
    client: AsyncClient,
) -> None:
    """Per design doc §6: renaming a destination updates destination_label on
    every job that referenced the old label. Only a DB update — no on-disk
    rename."""
    with patch("os.path.isdir", return_value=True):
        # Two jobs share destination=main.
        r1 = await client.post(
            "/api/jobs",
            json=make_job_payload(name="docs-job", source_label="documents"),
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/jobs",
            json=make_job_payload(name="photos-job", source_label="photos"),
        )
        assert r2.status_code == 201
        job1_id = r1.json()["id"]
        job2_id = r2.json()["id"]

        rename_resp = await client.post(
            "/api/mounts/destinations/rename",
            json={"old_label": "main", "new_label": "archive"},
        )
        assert rename_resp.status_code == 200
        affected = rename_resp.json()["affected_jobs"]
        assert len(affected) == 2
        assert {j["id"] for j in affected} == {job1_id, job2_id}

        # Both jobs now reference the new label.
        for job_id in (job1_id, job2_id):
            got = await client.get(f"/api/jobs/{job_id}")
            assert got.json()["destination_label"] == "archive"


async def test_rename_destination_404_when_no_jobs_use_old_label(
    client: AsyncClient,
) -> None:
    with patch("os.path.isdir", return_value=True):
        resp = await client.post(
            "/api/mounts/destinations/rename",
            json={"old_label": "never-used", "new_label": "archive"},
        )
    assert resp.status_code == 404


# ── Health + restic update check ─────────────────────────────────────────────


async def test_health_reports_scheduler_db_and_restic_version(
    client: AsyncClient, engine
) -> None:
    """GET /health is the dashboard's truth source for scheduler/DB liveness
    and the installed restic version."""
    # Seed the AppSettings row with a restic_version so health reports it.
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(AppSettings(id=1, restic_version="0.17.3"))
        await s.commit()

    mock_sched = MagicMock()
    mock_sched.running = True
    with patch("app.api.routes.settings.scheduler_module.scheduler", mock_sched):
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scheduler_running"] is True
    assert body["db_ok"] is True
    assert body["restic_version"] == "0.17.3"


async def test_restic_update_check_reports_update_available(
    client: AsyncClient, engine
) -> None:
    """GET /settings/restic-update-check compares the installed version to
    the latest GitHub release. Returns update_available=True when they
    differ."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(AppSettings(id=1, restic_version="0.16.0"))
        await s.commit()

    mock_response = MagicMock()
    mock_response.json.return_value = {"tag_name": "v0.17.3"}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
    mock_client.__aexit__.return_value = None

    with patch("httpx.AsyncClient", return_value=mock_client):
        resp = await client.get("/api/settings/restic-update-check")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current"] == "0.16.0"
    assert body["latest"] == "0.17.3"
    assert body["update_available"] is True


# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeDirEntry:
    """Minimal os.DirEntry stand-in for os.scandir mocking."""

    def __init__(self, name: str) -> None:
        self.name = name

    def is_dir(self) -> bool:
        return True


class _FakeScandirContext:
    """Context manager mirroring os.scandir()'s API."""

    def __init__(self, entries: list[_FakeDirEntry]) -> None:
        self._entries = entries

    def __enter__(self) -> list[_FakeDirEntry]:
        return self._entries

    def __exit__(self, *exc_info: object) -> None:
        return None

    def __iter__(self):
        return iter(self._entries)


def _fake_dir_iter(names: list[str]) -> _FakeScandirContext:
    return _FakeScandirContext([_FakeDirEntry(n) for n in names])
