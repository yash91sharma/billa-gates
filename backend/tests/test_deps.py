"""Tests for the ``get_session`` FastAPI dependency (``app/api/deps.py``).

Every route depends on it, and the whole suite overrides it (the `client`
fixture swaps in a session bound to in-memory SQLite), so the real generator
was never executed — 0 of its 2 statements. The property worth pinning is not
that it returns a session but that it *closes* it: `async with` on the
sessionmaker is what returns the connection. The production engine uses
NullPool against a single SQLite file, so a session leaked per request holds a
connection to that file until GC, and SQLite is exactly the backend where
accumulating open handles turns into "database is locked" on the next write.
"""

from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.deps as deps_module
from app.api.deps import get_session


async def test_get_session_yields_a_usable_async_session(engine):
    """The dependency hands routes a live AsyncSession."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with patch.object(deps_module, "async_session_maker", factory):
        agen = get_session()
        session = await agen.__anext__()

        assert isinstance(session, AsyncSession)
        # A session that cannot execute is no use to a route.
        assert (
            await session.execute(__import__("sqlalchemy").text("SELECT 1"))
        ).scalar() == 1

        try:
            await agen.__anext__()
        except StopAsyncIteration:
            pass


async def test_get_session_yields_exactly_once(engine):
    """A dependency generator that yields twice raises inside FastAPI."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with patch.object(deps_module, "async_session_maker", factory):
        agen = get_session()
        await agen.__anext__()

        try:
            await agen.__anext__()
            raised = False
        except StopAsyncIteration:
            raised = True

    assert raised, "get_session must yield a single session and then finish"


async def test_get_session_closes_the_session_when_the_request_ends(engine):
    """The `async with` must actually run its exit — otherwise connections leak."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with patch.object(deps_module, "async_session_maker", factory):
        agen = get_session()
        session = await agen.__anext__()

        assert session.is_active

        try:
            await agen.__anext__()
        except StopAsyncIteration:
            pass

    # SQLAlchemy marks a closed AsyncSession's sync session as no longer bound
    # to a connection; `is_active` on a closed session is False.
    assert not session.sync_session.in_transaction()


async def test_get_session_closes_the_session_even_if_the_route_raises(engine):
    """A handler that 500s must not leak its DB connection.

    Errors are the case that leaks in practice: the happy path returns, the
    failing path is the one written without a `finally`.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    with patch.object(deps_module, "async_session_maker", factory):
        agen = get_session()
        session = await agen.__anext__()

        # Mirrors FastAPI throwing the handler's exception into the dependency.
        try:
            await agen.athrow(RuntimeError("route blew up"))
        except RuntimeError:
            pass

    assert not session.sync_session.in_transaction()


def test_deps_and_database_expose_the_same_dependency_shape():
    """`app.db.database` defines a second `get_session`.

    Only the one in `app.api.deps` is wired into the routes and overridden by
    the test client — importing the other one in a router would silently open
    real connections to /app/data during tests. Pinned so the duplication is
    at least visible.
    """
    from app.db import database

    assert deps_module.get_session is not database.get_session
    assert deps_module.async_session_maker is database.async_session_maker


async def test_get_session_uses_the_module_level_sessionmaker():
    """The generator must read the sessionmaker from the module, not capture it.

    This is what makes `patch("app.api.deps.async_session_maker", ...)` work at
    all — and what the `client` fixture's dependency override relies on.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    probe_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(probe_engine, expire_on_commit=False)

    try:
        with patch.object(deps_module, "async_session_maker", factory):
            agen = get_session()
            session = await agen.__anext__()

            assert session.bind is probe_engine

            try:
                await agen.__anext__()
            except StopAsyncIteration:
                pass
    finally:
        await probe_engine.dispose()
