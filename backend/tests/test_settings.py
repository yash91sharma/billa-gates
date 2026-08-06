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


def _settings_put_payload(**overrides) -> dict:
    base = {
        "ntfy_server_url": "https://ntfy.sh",
        "ntfy_topic": "x",
        "notify_on_start": True,
        "notify_on_success": True,
        "notify_on_failure": True,
        "notify_on_warning": True,
        "notify_on_verification": True,
        "default_job_timeout_hours": 24,
    }
    base.update(overrides)
    return base


async def _stored_ntfy_token(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        settings = await s.get(AppSettings, 1)
        assert settings is not None
        return settings.ntfy_token


async def test_update_settings_ntfy_token_null_preserves_stored_token(client, engine):
    """null / omitted token means 'keep the stored one' — the UI never gets
    the token back, so it sends null on every ordinary save."""
    resp = await client.put(
        "/api/settings", json=_settings_put_payload(ntfy_token="tok-123")
    )
    assert resp.status_code == 200
    assert await _stored_ntfy_token(engine) == "tok-123"

    resp = await client.put(
        "/api/settings", json=_settings_put_payload(ntfy_token=None)
    )
    assert resp.status_code == 200
    assert await _stored_ntfy_token(engine) == "tok-123"


async def test_update_settings_ntfy_token_empty_string_clears(client, engine):
    """An explicit empty string means 'remove the stored token' — without
    this, a token can be replaced but never removed."""
    resp = await client.put(
        "/api/settings", json=_settings_put_payload(ntfy_token="tok-123")
    )
    assert resp.status_code == 200
    assert await _stored_ntfy_token(engine) == "tok-123"

    resp = await client.put("/api/settings", json=_settings_put_payload(ntfy_token=""))
    assert resp.status_code == 200
    assert await _stored_ntfy_token(engine) is None


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

    # ntfy JSON publishing must target the server ROOT with the topic inside
    # the payload — posting JSON to {server}/{topic} would deliver the raw
    # JSON string as the message body with no title.
    post_args = mock_http.post.call_args
    assert post_args.args[0] == "https://ntfy.sh"
    assert post_args.kwargs["json"]["topic"] == "test"
    assert "title" in post_args.kwargs["json"]


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


# ── test-ntfy / restic-update-check / health: the remaining failure paths ────
#
# The sections above cover the happy paths and a non-200 from ntfy. What was
# left unexercised is everything that goes wrong *around* the request — an
# unreachable host, an auth token, a response that is not the payload we
# expect, a dead database. Each one is a case where the endpoint's contract is
# to degrade into a readable answer rather than raise, because all three are
# rendered inline on the Settings page.
#
# All of these patch the AsyncClient CLASS, never its methods: the test
# `client` fixture is itself an httpx.AsyncClient, so patching
# `httpx.AsyncClient.post` would intercept the request under test on its way
# into the app. Replacing the class only affects instances built afterwards —
# i.e. the one the route constructs.


async def _set_ntfy(engine, *, topic="backups", token=None, server="https://ntfy.sh"):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        row = await s.get(AppSettings, 1)
        if row is None:
            row = AppSettings(id=1, default_job_timeout_hours=24)
            s.add(row)
        row.ntfy_server_url = server
        row.ntfy_topic = topic
        row.ntfy_token = token
        await s.commit()


def _fake_http(**attrs):
    """An AsyncClient stand-in that yields itself from `async with`."""
    mock_http = AsyncMock()
    mock_http.__aenter__.return_value = mock_http
    for key, value in attrs.items():
        setattr(mock_http, key, value)
    return mock_http


async def test_test_ntfy_sends_the_bearer_token_when_one_is_stored(client, engine):
    """A protected topic 403s without it, which reads to the operator as
    "ntfy is broken" rather than "your token is not being sent"."""
    await _set_ntfy(engine, token="tok-abc")

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_http = _fake_http(post=AsyncMock(return_value=mock_response))

    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.post("/api/settings/test-ntfy")

    assert resp.status_code == 200
    assert (
        mock_http.post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-abc"
    )


async def test_test_ntfy_sends_no_auth_header_without_a_token(client, engine):
    """Public ntfy.sh topics take no auth; an empty Bearer header can 401."""
    await _set_ntfy(engine, token=None)

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_http = _fake_http(post=AsyncMock(return_value=mock_response))

    with patch("httpx.AsyncClient", return_value=mock_http):
        await client.post("/api/settings/test-ntfy")

    assert "Authorization" not in mock_http.post.call_args.kwargs["headers"]


async def test_test_ntfy_strips_a_trailing_slash_from_the_server_url(client, engine):
    """`https://ntfy.sh/` must not become a POST to `https://ntfy.sh//`."""
    await _set_ntfy(engine, server="https://ntfy.example/")

    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_http = _fake_http(post=AsyncMock(return_value=mock_response))

    with patch("httpx.AsyncClient", return_value=mock_http):
        await client.post("/api/settings/test-ntfy")

    assert mock_http.post.call_args.args[0] == "https://ntfy.example"


async def test_test_ntfy_reports_a_network_error_instead_of_raising(client, engine):
    """An unreachable host comes back as ok=False with the reason.

    This endpoint exists to diagnose exactly this failure, so it must not be
    the thing that 500s when the server is down — the page would then show a
    generic error instead of "Name or service not known".
    """
    await _set_ntfy(engine)

    mock_http = _fake_http(
        post=AsyncMock(side_effect=OSError("Name or service not known"))
    )

    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.post("/api/settings/test-ntfy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Name or service not known" in body["error"]


async def test_restic_update_check_reports_unknown_when_the_tag_is_missing(
    client, engine
):
    """An absent `tag_name` must not render as "you are out of date".

    `"".lstrip("v")` is falsy, so `latest` is None and the comparison is
    skipped — without that, an unexpected GitHub payload would tell every
    operator an update exists, permanently.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        row = await s.get(AppSettings, 1)
        if row is None:
            row = AppSettings(id=1, ntfy_server_url="https://ntfy.sh", ntfy_topic="")
            s.add(row)
        row.restic_version = "0.19.1"
        await s.commit()

    mock_response = MagicMock()
    mock_response.json = MagicMock(return_value={"name": "0.19.1"})
    mock_http = _fake_http(get=AsyncMock(return_value=mock_response))

    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")

    assert resp.status_code == 200
    assert resp.json() == {
        "current": "0.19.1",
        "latest": None,
        "update_available": None,
    }


async def test_restic_update_check_degrades_when_the_body_is_not_json(client, engine):
    """GitHub's rate-limit and error pages are not the release payload."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        row = await s.get(AppSettings, 1)
        if row is None:
            row = AppSettings(id=1, ntfy_server_url="https://ntfy.sh", ntfy_topic="")
            s.add(row)
        row.restic_version = "0.19.1"
        await s.commit()

    mock_response = MagicMock()
    mock_response.json = MagicMock(side_effect=ValueError("not json"))
    mock_http = _fake_http(get=AsyncMock(return_value=mock_response))

    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")

    assert resp.json() == {
        "current": "0.19.1",
        "latest": None,
        "update_available": None,
    }


async def test_restic_update_check_does_not_call_github_without_a_known_version(client):
    """With nothing to compare against there is no question to ask.

    GitHub rate-limits unauthenticated callers, so a request that cannot
    produce an answer must not be spent.
    """
    mock_http = _fake_http(get=AsyncMock())

    with patch("httpx.AsyncClient", return_value=mock_http):
        resp = await client.get("/api/settings/restic-update-check")

    assert resp.json() == {"current": None, "latest": None, "update_available": None}
    mock_http.get.assert_not_called()


async def test_health_reports_a_db_failure_without_erroring(client):
    """A dead database must still answer 200 with db_ok=False.

    `/api/health` is what a container HEALTHCHECK and the UI banner read. If
    it raised, both would see a connection error and could not tell "the
    database is unreachable" from "the app is not running at all" — and the
    field that exists to report exactly that distinction would never be seen.
    """
    from app.api.deps import get_session
    from app.main import app

    class _BrokenSession:
        async def execute(self, *args, **kwargs):
            raise OSError("database is locked")

        async def get(self, *args, **kwargs):
            return None

    async def _broken():
        yield _BrokenSession()

    app.dependency_overrides[get_session] = _broken
    try:
        resp = await client.get("/api/health")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["db_ok"] is False
    assert body["restic_version"] is None
