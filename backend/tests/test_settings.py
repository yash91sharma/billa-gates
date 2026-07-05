"""Tests for GET/PUT /api/settings, test-ntfy, restic-update-check, and /api/health."""

from unittest.mock import AsyncMock, MagicMock, patch

# ── GET /api/settings ─────────────────────────────────────────────────────────


async def test_get_settings_default_values(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ntfy_server_url"] == "https://ntfy.sh"
    assert data["ntfy_topic"] == ""
    assert data["notify_on_start"] is True
    assert data["notify_on_success"] is True
    assert data["notify_on_failure"] is True
    assert data["notify_on_warning"] is True
    assert data["notify_on_verification"] is True
    assert data["default_job_timeout_hours"] == 24


async def test_get_settings_keep_last_runs_default(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["keep_last_runs"] == 100


async def test_get_settings_auto_unlock_default(client):
    """auto_unlock must default to True so stale restic lock files (e.g. left
    after a container kill) are cleared automatically before the next backup.
    Operators who want the legacy fail-loud behavior can flip it off."""
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["auto_unlock"] is True


async def test_update_settings_auto_unlock_false(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "x",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_warning": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "keep_last_runs": 100,
            "auto_unlock": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["auto_unlock"] is False


async def test_update_settings_keep_last_runs(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "x",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_warning": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "keep_last_runs": 50,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["keep_last_runs"] == 50


async def test_update_settings_keep_last_runs_rejects_below_minimum(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "x",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_warning": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "keep_last_runs": 0,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_notify_on_warning(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "x",
            "ntfy_token": None,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_warning": False,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["notify_on_warning"] is False


async def test_get_settings_ntfy_token_masked_or_null(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "ntfy_token" in data


# ── PUT /api/settings ─────────────────────────────────────────────────────────


async def test_update_settings_valid(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "my-backups",
            "ntfy_token": None,
            "notify_on_start": False,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": False,
            "default_job_timeout_hours": 12,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ntfy_topic"] == "my-backups"
    assert data["notify_on_start"] is False
    assert data["default_job_timeout_hours"] == 12


async def test_update_settings_ntfy_url_must_be_http(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "file:///etc/passwd",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 422
    assert "http" in resp.json()["detail"].lower()


async def test_update_settings_ntfy_url_javascript_scheme_rejected(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "javascript:alert(1)",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_ntfy_url_too_long(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://" + "x" * 600,
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_ntfy_topic_invalid_chars(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "invalid topic!",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_ntfy_topic_too_long(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "x" * 65,
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_empty_topic_valid(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
        },
    )
    assert resp.status_code == 200


async def test_update_settings_timeout_zero_invalid(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 0,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_timeout_too_large(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 169,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_timeout_max_valid(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 168,
        },
    )
    assert resp.status_code == 200


async def test_get_settings_metadata_timeout_default(client):
    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["metadata_timeout_seconds"] == 600


async def test_update_settings_metadata_timeout_valid(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "metadata_timeout_seconds": 300,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["metadata_timeout_seconds"] == 300


async def test_update_settings_metadata_timeout_invalid_too_small(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "metadata_timeout_seconds": 9,
        },
    )
    assert resp.status_code == 422


async def test_update_settings_metadata_timeout_invalid_too_large(client):
    resp = await client.put(
        "/api/settings",
        json={
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "notify_on_start": True,
            "notify_on_success": True,
            "notify_on_failure": True,
            "notify_on_verification": True,
            "default_job_timeout_hours": 24,
            "metadata_timeout_seconds": 86401,
        },
    )
    assert resp.status_code == 422


# ── POST /api/settings/test-ntfy ─────────────────────────────────────────────


async def test_test_ntfy_empty_topic_returns_422(client):
    resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 422


async def test_test_ntfy_success(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="test",
                default_job_timeout_hours=24,
            )
            s.add(settings)
        else:
            settings.ntfy_topic = "test"
        await s.commit()

    mock_response = AsyncMock()
    mock_response.status_code = 200

    # Patch the AsyncClient CLASS (not the instance method) so the
    # TestClient (an already-created instance) is unaffected by the mock.
    # __aenter__.return_value = mock_http makes `async with client as c` yield
    # mock_http itself rather than a new child AsyncMock.
    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.post.return_value = mock_response
    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


async def test_test_ntfy_failure_returns_ok_false(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="test",
                default_job_timeout_hours=24,
            )
            s.add(settings)
        else:
            settings.ntfy_topic = "test"
        await s.commit()

    mock_response = AsyncMock()
    mock_response.status_code = 403
    mock_response.text = "Unauthorized"

    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.post.return_value = mock_response
    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.post("/api/settings/test-ntfy")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "error" in resp.json()


# ── GET /api/settings/restic-update-check ────────────────────────────────────


async def test_restic_update_check_up_to_date(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="",
                restic_version="0.17.3",
                default_job_timeout_hours=24,
            )
            s.add(settings)
        else:
            settings.restic_version = "0.17.3"
        await s.commit()

    github_response = {"tag_name": "v0.17.3"}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=github_response)

    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.get.return_value = mock_resp
    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "0.17.3"
    assert data["latest"] == "0.17.3"
    assert data["update_available"] is False


async def test_restic_update_check_update_available(client, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="",
                restic_version="0.16.0",
                default_job_timeout_hours=24,
            )
            s.add(settings)
        else:
            settings.restic_version = "0.16.0"
        await s.commit()

    github_response = {"tag_name": "v0.17.3"}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=github_response)

    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.get.return_value = mock_resp
    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["update_available"] is True
    assert data["latest"] == "0.17.3"


async def test_restic_update_check_github_unreachable(client, engine):
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        if settings is None:
            settings = AppSettings(
                id=1,
                ntfy_server_url="https://ntfy.sh",
                ntfy_topic="",
                restic_version="0.17.3",
                default_job_timeout_hours=24,
            )
            s.add(settings)
        await s.commit()

    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    mock_http.get.side_effect = httpx.TimeoutException("timeout")
    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["latest"] is None
    assert data["update_available"] is None


async def test_restic_update_check_restic_not_detected(client):
    resp = await client.get("/api/settings/restic-update-check")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] is None
    assert data["update_available"] is None


# ── GET /api/health ───────────────────────────────────────────────────────────


async def test_health_returns_200_always(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200


async def test_health_response_shape(client):
    resp = await client.get("/api/health")
    data = resp.json()
    assert "scheduler_running" in data
    assert "restic_version" in data
    assert "db_ok" in data


async def test_health_db_ok_true_when_db_works(client):
    resp = await client.get("/api/health")
    assert resp.json()["db_ok"] is True


async def test_health_scheduler_running_reflects_state(client):
    from unittest.mock import patch as mpatch

    with mpatch("app.core.scheduler.scheduler") as mock_sched:
        mock_sched.running = False
        resp = await client.get("/api/health")
    assert resp.json()["scheduler_running"] is False


async def test_health_returns_200_even_when_scheduler_not_running(client):
    from unittest.mock import patch as mpatch

    with mpatch("app.core.scheduler.scheduler") as mock_sched:
        mock_sched.running = False
        resp = await client.get("/api/health")
    assert resp.status_code == 200


# ── Notifications service unit tests ─────────────────────────────────────────


async def test_send_notification_skips_when_topic_empty():

    from app.services.notifications import send_notification

    with patch("httpx.AsyncClient.post") as mock_post:
        await send_notification(
            server_url="https://ntfy.sh",
            topic="",
            title="Test",
            message="msg",
        )
    mock_post.assert_not_called()


async def test_send_notification_posts_when_topic_set():
    from app.services.notifications import send_notification

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        await send_notification(
            server_url="https://ntfy.sh",
            topic="my-topic",
            title="Backup started",
            message="Job is running",
        )
    mock_post.assert_called_once()


async def test_send_notification_includes_auth_header_when_token_set():
    from app.services.notifications import send_notification

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    captured = {}

    async def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return mock_response

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        await send_notification(
            server_url="https://ntfy.sh",
            topic="my-topic",
            title="Test",
            message="msg",
            token="mytoken",
        )
    assert "Authorization" in captured["headers"]
    assert "mytoken" in captured["headers"]["Authorization"]


async def test_send_notification_no_auth_header_without_token():
    from app.services.notifications import send_notification

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    captured = {}

    async def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return mock_response

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        await send_notification(
            server_url="https://ntfy.sh",
            topic="my-topic",
            title="Test",
            message="msg",
            token=None,
        )
    headers = captured.get("headers", {})
    assert "Authorization" not in headers


async def test_send_notification_logs_error_on_http_failure():
    import httpx

    from app.services.notifications import send_notification

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Internal Server Error", request=MagicMock(), response=mock_response
    )

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        with patch("app.services.notifications.logger.error") as mock_logger_error:
            await send_notification(
                server_url="https://ntfy.sh",
                topic="my-topic",
                title="Test",
                message="msg",
            )
    mock_post.assert_called_once()
    mock_logger_error.assert_called_once()
    assert "notification failed" in mock_logger_error.call_args[0][0]
