import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine():
    from app.db.models import Base

    eng = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
def _patch_backup_runner_engine(engine):
    """Auto-patch backup_runner.engine with test engine for all tests."""
    with patch("app.services.backup_runner.engine", engine):
        yield


@pytest.fixture(autouse=True)
def _mock_backup_runner_mount_check():
    """Autouse fixture to mock Billa-Gates mount check to pass by default."""
    with (
        patch("app.services.backup_runner.check_mount_file_exists", return_value=True),
        patch(
            "app.services.backup_runner.check_destination_mount_file_exists",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_restic_latest_snapshot_id(request):
    """Autouse fixture to mock restic_latest_snapshot_id to return None by default,
    except when running unit tests for restic service itself.
    """
    if "test_restic" in request.module.__name__:
        yield
    else:
        with patch("app.services.restic.restic_latest_snapshot_id", return_value=None):
            yield


@pytest.fixture(autouse=True)
def _reset_backup_runner_concurrency_state():
    """Reset module-level concurrency state between tests.

    job_locks holds asyncio.Lock objects bound to the test's event loop; an
    instance reused across tests raises 'bound to a different event loop'.
    active_jobs is a plain set but can leak from tests that simulate an
    in-flight run without unwinding the state.
    """
    from app.services import backup_runner

    backup_runner.job_locks.clear()
    backup_runner.active_jobs.clear()
    yield
    backup_runner.job_locks.clear()
    backup_runner.active_jobs.clear()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(engine) -> AsyncGenerator[AsyncClient, None]:
    from app.api.deps import get_session
    from app.main import app

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def mock_scheduler():
    m = MagicMock()
    m.running = True
    m.add_job = MagicMock()
    m.remove_job = MagicMock()
    m.get_job = MagicMock(return_value=None)
    m.get_jobs = MagicMock(return_value=[])
    return m


# ── helpers ──────────────────────────────────────────────────────────────────


def make_job_payload(**overrides) -> dict:
    base = {
        "name": "Test Backup",
        "source_label": "documents",
        "destination_label": "main",
        "restic_password": "secret123",
        "schedule_type": "interval",
        "schedule_value": "6h",
        "enabled": True,
    }
    base.update(overrides)
    return base


def make_run_row(db_session, job_id: str, **overrides):
    """Insert a BackupRun row directly into the test DB."""
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    now = datetime.now(timezone.utc)
    run = BackupRun(
        id=str(uuid.uuid4()),
        job_id=job_id,
        status=overrides.pop("status", RunStatus.success),
        triggered_by=overrides.pop("triggered_by", TriggeredBy.manual),
        started_at=overrides.pop("started_at", now),
        finished_at=overrides.pop("finished_at", now),
        **overrides,
    )
    db_session.add(run)
    return run
