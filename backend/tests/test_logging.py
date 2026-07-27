"""Tests for app.core.logging — request ID traceability."""

import logging
import re

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.logging import (
    RequestIdFilter,
    RequestLoggingMiddleware,
    _request_id_var,
    get_logger,
    get_request_id,
    setup_logging,
)

# ── contextvar reset between tests ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_request_id():
    token = _request_id_var.set(None)
    yield
    _request_id_var.reset(token)


# ── get_request_id ────────────────────────────────────────────────────────────


def test_get_request_id_returns_none_when_unset():
    assert get_request_id() is None


def test_get_request_id_returns_value_set_via_contextvar():
    _request_id_var.set("abc123def456")
    assert get_request_id() == "abc123def456"


# ── RequestIdFilter ──────────────────────────────────────────────────────────


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hi",
        args=(),
        exc_info=None,
    )


def test_request_id_filter_injects_value_from_contextvar():
    _request_id_var.set("xyz789012abc")
    f = RequestIdFilter()
    record = _make_record()
    assert f.filter(record) is True
    assert getattr(record, "request_id") == "xyz789012abc"


def test_request_id_filter_defaults_to_none_string_when_unset():
    f = RequestIdFilter()
    record = _make_record()
    assert f.filter(record) is True
    assert getattr(record, "request_id") == "none"


# ── setup_logging format and propagation ─────────────────────────────────────


def test_setup_logging_format_contains_request_id_placeholder():
    setup_logging()
    handlers = logging.getLogger().handlers
    assert any(
        "%(request_id)s" in (h.formatter._fmt or "") for h in handlers if h.formatter
    )


def test_setup_logging_makes_request_id_available_on_records(caplog):
    setup_logging()
    _request_id_var.set("propagationid1")
    with caplog.at_level(logging.INFO):
        get_logger("test.propagation").info("hello")
    matching = [r for r in caplog.records if r.message == "hello"]
    assert matching
    assert matching[0].request_id == "propagationid1"


def test_setup_logging_default_request_id_is_none_string(caplog):
    setup_logging()
    with caplog.at_level(logging.INFO):
        get_logger("test.default").info("anonymous")
    matching = [r for r in caplog.records if r.message == "anonymous"]
    assert matching
    assert matching[0].request_id == "none"


# ── RequestLoggingMiddleware end-to-end ──────────────────────────────────────


def _make_app() -> tuple[Starlette, dict]:
    captured: dict = {}

    async def root(request):
        captured["id"] = get_request_id()
        get_logger("test.endpoint").info("endpoint hit")
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", root)])
    app.add_middleware(RequestLoggingMiddleware)
    return app, captured


@pytest.mark.asyncio
async def test_middleware_sets_12_char_hex_request_id():
    app, captured = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/")
    assert resp.status_code == 200
    rid = captured["id"]
    assert rid is not None
    assert re.fullmatch(r"[0-9a-f]{12}", rid)


@pytest.mark.asyncio
async def test_middleware_generates_distinct_ids_across_requests():
    app, captured = _make_app()
    seen: set[str] = set()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        for _ in range(3):
            await ac.get("/")
            seen.add(captured["id"])
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_middleware_clears_request_id_after_response():
    app, _ = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        await ac.get("/")
    assert get_request_id() is None


# ── Request traceability ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_logs_during_one_request_share_same_request_id(caplog):
    setup_logging()
    app, captured = _make_app()

    with caplog.at_level(logging.INFO):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/")

    rid = captured["id"]
    request_records = [
        r for r in caplog.records if getattr(r, "request_id", None) == rid
    ]
    # At minimum: endpoint log + middleware's "GET / → 200" log
    assert len(request_records) >= 2
    for r in request_records:
        assert r.request_id == rid


@pytest.mark.asyncio
async def test_two_sequential_requests_have_isolated_request_ids(caplog):
    setup_logging()
    app, captured = _make_app()

    rids: list[str] = []
    with caplog.at_level(logging.INFO):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/")
            rids.append(captured["id"])
            await ac.get("/")
            rids.append(captured["id"])

    assert rids[0] != rids[1]
    logged_rids = {getattr(r, "request_id", None) for r in caplog.records}
    assert rids[0] in logged_rids
    assert rids[1] in logged_rids


# ── @log_call / sanitize secret redaction ────────────────────────────────────
#
# The restic wrappers receive the repo password as a *positional* argument
# (e.g. restic_cat_config(repo_path, password)), and route handlers receive
# Pydantic bodies whose repr includes restic_password / ntfy_token. Both must
# be redacted before @log_call writes its DEBUG line — a backup tool leaking
# the repo password into logs discloses the key that decrypts every backup.


SECRET = "SUPER_SECRET_PW_zx9"


def test_log_call_redacts_positional_password_argument(caplog):
    from app.core.logging import log_call

    @log_call
    def fake_restic_call(repo_path: str, password: str, timeout_seconds: int = 60):
        return (0, "", "")

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        fake_restic_call("/destinations/main/abc", SECRET)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined
    assert "***" in joined


def test_log_call_redacts_password_keyword_argument(caplog):
    from app.core.logging import log_call

    @log_call
    def fake_restic_call(repo_path: str, password: str):
        return (0, "", "")

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        fake_restic_call("/destinations/main/abc", password=SECRET)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined


def test_log_call_redacts_the_password_inside_the_restic_environment(caplog):
    """The password reaches restic as an environment variable, not an argument.

    `build_restic_env_overrides` returns it under the key `RESTIC_PASSWORD`, and
    that dict is both logged as a return value and passed to
    `restic_process.run_restic` as an argument — so redaction has to match the
    environment variable's own name, not only the `password` parameter it came
    from. Without it, one DEBUG line discloses the key that decrypts every
    backup in the repository.
    """
    from app.services.restic import build_restic_env_overrides

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        overrides = build_restic_env_overrides("/destinations/main/photos", SECRET)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined
    assert "***" in joined
    # Redaction is a logging concern only — the value restic is given is real.
    assert overrides["RESTIC_PASSWORD"] == SECRET


def test_log_call_redacts_token_parameter(caplog):
    """send_notification-style functions take the ntfy token as `token`."""
    from app.core.logging import log_call

    @log_call
    def fake_notify(server_url: str, topic: str, token: str | None = None):
        return None

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        fake_notify("https://ntfy.sh", "topic", token=SECRET)
        fake_notify("https://ntfy.sh", "topic", SECRET)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined


@pytest.mark.asyncio
async def test_log_call_redacts_positional_password_on_async_functions(caplog):
    from app.core.logging import log_call

    @log_call
    async def fake_async_restic_call(repo_path: str, password: str):
        return (0, "", "")

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        await fake_async_restic_call("/destinations/main/abc", SECRET)

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined


def test_sanitize_redacts_pydantic_model_fields():
    from app.api.schemas.jobs import JobCreate
    from app.core.logging import sanitize

    body = JobCreate(
        name="docs",
        source_label="documents",
        destination_label="main",
        restic_password=SECRET,
        schedule_type="interval",
        schedule_value="6h",
    )
    result = sanitize(body)
    assert isinstance(result, dict)
    assert result["restic_password"] == "***"
    assert result["name"] == "docs"
    assert SECRET not in repr(result)


def test_log_call_redacts_pydantic_body_argument(caplog):
    """Route handlers are @log_call-decorated and receive the request body as
    a Pydantic model — its repr must never reach the log with secrets intact."""
    from app.api.schemas.jobs import JobCreate
    from app.core.logging import log_call

    @log_call
    def fake_route(body: JobCreate):
        return None

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        fake_route(
            JobCreate(
                name="docs",
                source_label="documents",
                destination_label="main",
                restic_password=SECRET,
                schedule_type="interval",
                schedule_value="6h",
            )
        )

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert SECRET not in joined


def test_log_call_still_works_on_functions_without_signature_metadata(caplog):
    """Redaction must degrade gracefully for callables whose signature cannot
    be inspected (builtins wrapped in partial, C extensions, *args-only)."""
    from app.core.logging import log_call

    @log_call
    def variadic(*args, **kwargs):
        return len(args)

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        assert variadic("a", "b") == 2

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "variadic called" in joined


# ── @log_call return value truncation ────────────────────────────────────────


def test_log_call_truncates_long_return_values(caplog):
    """Per design doc §16: return values must be truncated at 500 chars."""
    from app.core.logging import log_call

    @log_call
    def big_return() -> str:
        return "X" * 5000

    setup_logging()
    with caplog.at_level(logging.DEBUG):
        big_return()
    returned_lines = [r for r in caplog.records if "returned" in r.getMessage()]
    assert returned_lines
    msg = returned_lines[0].getMessage()
    # truncated representation should contain ellipsis or be at most a bounded size
    assert "..." in msg or len(msg) < 1000


# ── RequestLoggingMiddleware entry/exit + duration ───────────────────────────


@pytest.mark.asyncio
async def test_middleware_logs_request_entry_and_exit_with_duration(caplog):
    """Per design doc §16: middleware emits both an entry line and an exit
    line with duration_ms."""
    setup_logging()
    app, _ = _make_app()
    with caplog.at_level(logging.INFO):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.get("/")

    middleware_msgs = [
        r.getMessage() for r in caplog.records if r.name == "app.core.logging"
    ]
    # one entry line, one exit line
    entry = [m for m in middleware_msgs if "→" in m and "duration_ms" not in m]
    exit_ = [m for m in middleware_msgs if "duration_ms" in m]
    assert entry, f"missing request entry log; got: {middleware_msgs}"
    assert exit_, f"missing response exit log with duration_ms; got: {middleware_msgs}"


@pytest.mark.asyncio
async def test_middleware_does_not_log_sensitive_request_fields(caplog):
    """Per design doc §16: request bodies must be sanitized."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def echo(request: Request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(RequestLoggingMiddleware)

    setup_logging()
    with caplog.at_level(logging.INFO):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            await ac.post(
                "/echo", json={"restic_password": "s3cret", "name": "doc-job"}
            )

    middleware_msgs = " ".join(
        r.getMessage() for r in caplog.records if r.name == "app.core.logging"
    )
    assert "s3cret" not in middleware_msgs


# ── Full-stack traceability via FastAPI ──────────────────────────────────────


@pytest.mark.asyncio
async def test_request_id_propagates_through_route_and_log_call(caplog, client):
    """Verify a real FastAPI route + @log_call helpers all share the same ID.

    Uses GET /api/jobs which exercises the @log_call-decorated route handler
    and helper functions defined in app.api.routes.jobs.
    """
    setup_logging()
    with caplog.at_level(logging.DEBUG):
        resp = await client.get("/api/jobs")
    assert resp.status_code == 200

    # Identify the middleware's request log to extract the ID
    middleware_records = [
        r
        for r in caplog.records
        if r.name == "app.core.logging" and "GET /api/jobs" in r.getMessage()
    ]
    assert middleware_records, "middleware did not log the request"
    rid = middleware_records[0].request_id
    assert re.fullmatch(r"[0-9a-f]{12}", rid)

    # Every record originating from app.api.routes.jobs during this request
    # must carry the same request_id.
    route_records = [r for r in caplog.records if r.name == "app.api.routes.jobs"]
    assert route_records, "no @log_call records from jobs route"
    for r in route_records:
        assert r.request_id == rid, (
            f"record {r.funcName!r} had request_id={r.request_id!r}, expected {rid!r}"
        )


# ── @log_call does no work for log lines nobody will see ─────────────────────


def test_log_call_skips_return_repr_when_debug_is_disabled(caplog):
    """`repr()` of a return value is built eagerly, so at INFO — the default —
    every decorated call paid to materialize a string that was then thrown
    away. For restic_backup, whose return value carried the whole stdout blob,
    that repr alone roughly doubled peak memory (measured: 549 MB → 951 MB on a
    210 MB stream). Nothing expensive may run unless DEBUG is enabled."""
    from unittest.mock import patch as _patch

    from app.core.logging import log_call

    @log_call
    def returns_a_blob() -> str:
        return "x" * 1000

    setup_logging()
    with _patch("app.core.logging._truncate_repr") as mock_repr:
        with caplog.at_level(logging.INFO):
            returns_a_blob()
        assert mock_repr.call_count == 0, (
            "return value must not be repr()'d when DEBUG is off"
        )

        with caplog.at_level(logging.DEBUG):
            returns_a_blob()
        assert mock_repr.call_count == 1, "at DEBUG the value is still logged"


@pytest.mark.asyncio
async def test_log_call_skips_return_repr_when_debug_is_disabled_async(caplog):
    """Same guarantee on the async path — that is where restic_backup lives."""
    from unittest.mock import patch as _patch

    from app.core.logging import log_call

    @log_call
    async def returns_a_blob_async() -> str:
        return "x" * 1000

    setup_logging()
    with _patch("app.core.logging._truncate_repr") as mock_repr:
        with caplog.at_level(logging.INFO):
            await returns_a_blob_async()
        assert mock_repr.call_count == 0

        with caplog.at_level(logging.DEBUG):
            await returns_a_blob_async()
        assert mock_repr.call_count == 1
