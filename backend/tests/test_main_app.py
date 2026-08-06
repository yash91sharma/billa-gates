"""Tests for the FastAPI application shell in ``app/main.py``.

Route *registration order* is the load-bearing property here: FastAPI matches
first-registered-first, and the catch-all `GET /{full_path:path}` swallows
anything still unmatched when it is reached. That has two consequences the
suite never pinned:

  * an API router registered after the catch-all would be permanently dead —
    every one of its routes would answer with the SPA shell and a 200;
  * a bare ``/health`` is *not* the health endpoint. It falls through to the
    catch-all and returns ``index.html`` with a 200, so a container HEALTHCHECK
    pointed at it reports healthy no matter what the app is doing. The real
    endpoint is ``/api/health``.

The lifespan handler and the 422 flattening handler were likewise unexercised.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.routing import APIRoute

import app.main as main_module
from app.main import app

# ── The SPA catch-all ────────────────────────────────────────────────────────


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    """Point main at a throwaway static dir holding a recognisable index.html."""
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Billa-Gates SPA</title>"
    )
    monkeypatch.setattr(main_module, "_STATIC_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/jobs",
        "/jobs/8b2f1c4e-0000-4000-8000-000000000000",
        "/runs/8b2f1c4e-0000-4000-8000-000000000001",
        "/destinations",
        "/settings",
        "/some/deeply/nested/client/route",
    ],
)
async def test_client_side_routes_are_served_the_spa_shell(client, static_dir, path):
    """Every client-side route must return index.html so React Router can boot.

    A 404 here is the classic "works until you refresh the page" SPA bug.
    """
    resp = await client.get(path)

    assert resp.status_code == 200
    assert "Billa-Gates SPA" in resp.text


async def test_catch_all_returns_404_when_the_bundle_is_missing(
    client, tmp_path, monkeypatch
):
    """With no built bundle, the catch-all 404s rather than 500ing."""
    monkeypatch.setattr(main_module, "_STATIC_DIR", tmp_path)

    resp = await client.get("/jobs")

    assert resp.status_code == 404


async def test_the_catch_all_does_not_shadow_any_api_router(client, static_dir):
    """Each registered API router still answers with JSON, not the SPA shell.

    This is the regression guard for the ordering rule: if the catch-all were
    moved above the routers, every one of these would return index.html/200.
    """
    for path in (
        "/api/jobs",
        "/api/runs/recent",
        "/api/mounts/sources",
        "/api/settings",
    ):
        resp = await client.get(path)

        assert resp.status_code == 200, path
        assert "application/json" in resp.headers["content-type"], path
        assert "Billa-Gates SPA" not in resp.text, path


async def test_an_unknown_api_path_falls_through_to_the_spa_shell(client, static_dir):
    """An unmatched `/api/*` path returns index.html with a 200, not a 404.

    The catch-all is mounted at the root (`/{full_path:path}`) with no prefix,
    so it swallows unmatched `/api/*` paths too — the same trap as `/health`
    one test down. Pinned because of what it does to the client: `lib/api.ts`
    calls `resp.json()` on any ok response, so a mistyped or removed endpoint
    surfaces as a JSON parse error ("Unexpected token <") rather than as the
    404 the caller's `err.status` branches are written for.

    This is behaviour, not a recommendation — if the catch-all ever grows an
    `/api/` guard, this test is the one to update.
    """
    resp = await client.get("/api/does-not-exist")

    assert resp.status_code == 200
    assert "Billa-Gates SPA" in resp.text


async def test_bare_health_is_the_spa_shell_not_the_health_endpoint(client, static_dir):
    """`/health` has no route — it is swallowed by the catch-all.

    Pinned deliberately: a container HEALTHCHECK aimed at `/health` gets a 200
    from the SPA shell whether or not the app is working. The real check is
    `/api/health`, which reports scheduler and DB state.
    """
    spa = await client.get("/health")
    assert spa.status_code == 200
    assert "Billa-Gates SPA" in spa.text

    real = await client.get("/api/health")
    assert real.status_code == 200
    body = real.json()
    assert set(body) >= {"scheduler_running", "db_ok", "restic_version"}


def test_the_catch_all_is_the_last_registered_route():
    """The structural version of the rule, independent of any single path."""
    paths = [r.path for r in app.routes if isinstance(r, APIRoute)]
    catch_all = "/{full_path:path}"

    assert catch_all in paths
    assert paths[-1] == catch_all, (
        "the SPA catch-all must be registered last; anything after it is "
        f"unreachable. Routes after it: {paths[paths.index(catch_all) + 1 :]}"
    )


def test_the_service_worker_route_is_registered_before_the_catch_all():
    """`/sw.js` must out-rank the catch-all or it would serve index.html.

    A service worker registration whose script is HTML fails outright, which
    silently un-installs the PWA.
    """
    paths = [r.path for r in app.routes if isinstance(r, APIRoute)]

    assert paths.index("/sw.js") < paths.index("/{full_path:path}")


# ── The 422 flattening handler ───────────────────────────────────────────────


async def test_validation_errors_are_flattened_to_a_single_string(client):
    """FastAPI's default 422 detail is a list; the UI renders a string.

    `lib/api.ts` throws `new Error(err.detail)` and pages render it inline, so
    a list would reach the operator as "[object Object]".
    """
    resp = await client.put("/api/settings", json={"ntfy_server_url": "not-a-url"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    # The field name must survive the flattening — it is what tells the
    # operator which input to fix.
    assert "ntfy_server_url" in detail


async def test_validation_error_detail_names_every_bad_field(client):
    """Multiple failures are joined, not truncated to the first."""
    resp = await client.post(
        "/api/jobs",
        json={"name": "", "source_label": "docs", "destination_label": "main"},
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "restic_password" in detail
    assert ";" in detail or "name" in detail


async def test_the_body_prefix_is_stripped_from_the_field_path(client):
    """`('body', 'field')` renders as `field:`, not `body.field:`."""
    resp = await client.put("/api/settings", json={"ntfy_server_url": "not-a-url"})

    assert not resp.json()["detail"].startswith("body")


# ── Lifespan ─────────────────────────────────────────────────────────────────


async def test_lifespan_starts_and_stops_the_scheduler():
    """Startup boots the scheduler; shutdown stops it.

    The scheduler is rebuilt from BackupJob rows on every startup (there is no
    persistent APScheduler state), so a lifespan that skipped it would leave an
    app that serves the UI perfectly and never runs a scheduled backup.
    """
    with (
        patch.object(main_module, "start_scheduler", new=AsyncMock()) as start,
        patch.object(main_module, "shutdown_scheduler", new=AsyncMock()) as stop,
        patch.object(main_module, "setup_logging") as setup,
    ):
        async with main_module.lifespan(app):
            setup.assert_called_once()
            start.assert_awaited_once()
            stop.assert_not_awaited()

        stop.assert_awaited_once()


async def test_lifespan_configures_logging_before_starting_the_scheduler():
    """Ordering matters: scheduler startup logs, and must not log unconfigured."""
    calls: list[str] = []

    async def _start() -> None:
        calls.append("start_scheduler")

    with (
        patch.object(
            main_module,
            "setup_logging",
            side_effect=lambda: calls.append("setup_logging"),
        ),
        patch.object(main_module, "start_scheduler", new=_start),
        patch.object(main_module, "shutdown_scheduler", new=AsyncMock()),
    ):
        async with main_module.lifespan(app):
            pass

    assert calls == ["setup_logging", "start_scheduler"]


async def test_the_app_is_wired_to_the_lifespan_handler():
    """A lifespan that is written but not attached starts no scheduler."""
    assert app.router.lifespan_context is not None
    # FastAPI wraps the handler, so identity is checked by name.
    assert "lifespan" in repr(app.router.lifespan_context)


# ── Middleware and CORS ──────────────────────────────────────────────────────


def test_request_logging_middleware_is_installed():
    """Every request/response envelope is logged by the middleware, not by
    individual handlers — so it has to actually be registered."""
    from app.core.logging import RequestLoggingMiddleware

    assert any(m.cls is RequestLoggingMiddleware for m in app.user_middleware)


async def test_cors_allows_the_vite_dev_server_origin(client):
    """`npm run dev` serves the UI from :5173 and proxies /api."""
    resp = await client.options(
        "/api/jobs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_cors_does_not_echo_an_arbitrary_origin(client):
    """The allow-list is explicit; a wildcard would let any page call the API."""
    resp = await client.options(
        "/api/jobs",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert resp.headers.get("access-control-allow-origin") != "https://evil.example"
    assert resp.headers.get("access-control-allow-origin") != "*"


# ── Docs / OpenAPI live under /api so the catch-all cannot shadow them ───────


async def test_openapi_and_docs_are_served_under_the_api_prefix(client, static_dir):
    """Both are configured off the default root paths; at the root they would
    be indistinguishable from a client-side route."""
    schema = await client.get("/api/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["openapi"].startswith("3.")

    # The default locations are just SPA routes.
    assert "Billa-Gates SPA" in (await client.get("/docs")).text
