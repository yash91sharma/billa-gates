"""Test database migrations.

The schema is defined by a single initial migration. These tests exercise
``alembic upgrade head`` against an empty database — the path a fresh
deployment takes — and verify the final shape matches what the app expects.
"""

import re
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import inspect

import app.db.database as db_module
from alembic import command


@pytest.mark.asyncio
async def test_migration_creates_all_tables():
    """Verify migration creates all tables and columns."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        inspector = inspect(engine)

        # Verify tables exist. The `snapshots` table is intentionally absent
        # — restic is the source of truth (gaps.md C4-Alt).
        tables = set(inspector.get_table_names())
        expected_tables = {"backup_jobs", "backup_runs", "app_settings"}
        assert expected_tables.issubset(tables)
        assert "snapshots" not in tables, (
            "snapshots table must not be created — restic is the source of truth"
        )

        # Verify columns for each table
        job_cols = {col["name"] for col in inspector.get_columns("backup_jobs")}
        expected_job = {
            "id",
            "name",
            "source_label",
            "destination_label",
            "restic_password",
            "schedule_type",
            "schedule_value",
            "enabled",
            "retain_keep_last",
            "retain_keep_hourly",
            "retain_keep_daily",
            "retain_keep_weekly",
            "retain_keep_monthly",
            "retain_keep_yearly",
            "retain_keep_within",
            "retain_keep_within_hourly",
            "retain_keep_within_daily",
            "retain_keep_within_weekly",
            "retain_keep_within_monthly",
            "retain_keep_within_yearly",
            "exclude_patterns",
            "exclude_caches",
            "exclude_if_present",
            "one_file_system",
            "no_scan",
            "tags",
            "compression",
            "pack_size",
            "read_concurrency",
            "timeout_hours",
            "check_enabled",
            "check_mode",
            "check_subset_percent",
            "check_timeout_hours",
            "created_at",
            "updated_at",
        }
        assert expected_job.issubset(job_cols)

        run_cols = {col["name"] for col in inspector.get_columns("backup_runs")}
        expected_run = {
            "id",
            "job_id",
            "kind",
            "status",
            "reason",
            "started_at",
            "finished_at",
            "duration_seconds",
            "snapshot_id",
            "files_new",
            "files_changed",
            "files_unmodified",
            "dirs_new",
            "dirs_changed",
            "dirs_unmodified",
            "data_added_bytes",
            "data_added_packed_bytes",
            "total_bytes_processed",
            "backup_output",
            "error_output",
            "prune_status",
            "prune_error_output",
            "check_status",
            "check_error_output",
            "triggered_by",
        }
        assert expected_run.issubset(run_cols)

        settings_cols = {col["name"] for col in inspector.get_columns("app_settings")}
        expected_settings = {
            "id",
            "ntfy_server_url",
            "ntfy_topic",
            "ntfy_token",
            "notify_on_start",
            "notify_on_success",
            "notify_on_failure",
            "notify_on_warning",
            "notify_on_verification",
            "restic_version",
            "default_job_timeout_hours",
            "keep_last_runs",
            "auto_unlock",
        }
        assert expected_settings.issubset(settings_cols)

        # Verify foreign keys
        assert any(
            fk["constrained_columns"] == ["job_id"]
            for fk in inspector.get_foreign_keys("backup_runs")
        ), "Missing FK: backup_runs.job_id"

        engine.dispose()


@pytest.mark.asyncio
async def test_migration_allows_prune_kind_in_backup_runs():
    """`backup_runs.kind` must accept the value 'prune'. Prune runs reuse
    the BackupRun table with a `kind` discriminator (gaps.md H1) so the UI
    can show them alongside backup runs without a new table."""
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        with engine.begin() as conn:
            job_id = str(_uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO backup_jobs (id, name, source_label, "
                    "destination_label, restic_password, schedule_type, "
                    "schedule_value, enabled, exclude_caches, one_file_system, "
                    "no_scan, check_enabled, created_at, updated_at) "
                    "VALUES (:id, 'j', 'src', 'dst', 'pw', 'interval', '1h', "
                    "1, 0, 0, 0, 0, '2026-01-01', '2026-01-01')"
                ),
                {"id": job_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO backup_runs (id, job_id, kind, status, "
                    "started_at, triggered_by) VALUES "
                    "(:id, :job_id, 'prune', 'success', "
                    "'2026-01-01', 'manual')"
                ),
                {"id": str(_uuid.uuid4()), "job_id": job_id},
            )
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_allows_warning_status_in_backup_runs():
    """`backup_runs.status` must accept the value 'warning' (used for restic's
    exit code 3 / partial-backup case)."""
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        with engine.begin() as conn:
            job_id = str(_uuid.uuid4())
            conn.execute(
                sa.text(
                    "INSERT INTO backup_jobs (id, name, source_label, "
                    "destination_label, restic_password, schedule_type, "
                    "schedule_value, enabled, exclude_caches, one_file_system, "
                    "no_scan, check_enabled, created_at, updated_at) "
                    "VALUES (:id, 'j', 'src', 'dst', 'pw', 'interval', '1h', "
                    "1, 0, 0, 0, 0, '2026-01-01', '2026-01-01')"
                ),
                {"id": job_id},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO backup_runs (id, job_id, status, "
                    "started_at, triggered_by) VALUES "
                    "(:id, :job_id, 'warning', '2026-01-01', 'manual')"
                ),
                {"id": str(_uuid.uuid4()), "job_id": job_id},
            )
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "off", "max", "fastest", "better"])
async def test_migration_allows_every_restic_compression_mode(mode):
    """`backup_jobs.compression` must hold all five zstd modes restic 0.19.1
    accepts. `fastest` and `better` (restic 0.19.0) are 7 and 6 characters,
    where the pre-0.19 enum sized the column at 4 — migration 002 widens it."""
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        job_id = str(_uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO backup_jobs (id, name, source_label, "
                    "destination_label, restic_password, schedule_type, "
                    "schedule_value, enabled, exclude_caches, one_file_system, "
                    "no_scan, check_enabled, compression, created_at, updated_at) "
                    "VALUES (:id, 'j', 'src', 'dst', 'pw', 'interval', '1h', "
                    "1, 0, 0, 0, 0, :mode, '2026-01-01', '2026-01-01')"
                ),
                {"id": job_id, "mode": mode},
            )
        with engine.begin() as conn:
            stored = conn.execute(
                sa.text("SELECT compression FROM backup_jobs WHERE id = :id"),
                {"id": job_id},
            ).scalar_one()
        assert stored == mode, f"compression {mode!r} did not round-trip"
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_002_preserves_jobs_and_unique_constraint():
    """002 rebuilds backup_jobs (SQLite cannot ALTER a column type in place),
    so it must carry the rows and the (destination_label, name) uniqueness
    across the table copy. A silent constraint loss there would let two jobs
    address the same repository directory."""
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        # Stop at 001, seed a pre-existing job, then upgrade across 002.
        command.upgrade(cfg, "001")

        engine = sa.create_engine(db_url, echo=False)
        insert_job = (
            "INSERT INTO backup_jobs (id, name, source_label, destination_label, "
            "restic_password, schedule_type, schedule_value, enabled, "
            "exclude_caches, one_file_system, no_scan, check_enabled, "
            "compression, created_at, updated_at) "
            "VALUES (:id, :name, 'src', 'main', 'pw', 'interval', '1h', "
            "1, 0, 0, 0, 0, 'max', '2026-01-01', '2026-01-01')"
        )
        with engine.begin() as conn:
            conn.execute(
                sa.text(insert_job), {"id": str(_uuid.uuid4()), "name": "Photos"}
            )
        engine.dispose()

        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        with engine.begin() as conn:
            rows = conn.execute(
                sa.text("SELECT name, compression FROM backup_jobs")
            ).all()
        assert rows == [("Photos", "max")], "002 lost pre-existing job rows"

        # The unique constraint must have survived the table rebuild.
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(insert_job), {"id": str(_uuid.uuid4()), "name": "Photos"}
                )
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_003_drops_source_subpath_and_preserves_jobs():
    """003 removes backup_jobs.source_subpath.

    SQLite cannot drop a column in place, so — like 002 — this rebuilds the
    table. The copy must carry the existing rows and the (destination_label,
    name) uniqueness across: a silent constraint loss there would let two jobs
    address the same repository directory.

    The column itself must actually be gone, not merely unused: the ORM/migration
    parity guard below compares the two column sets by equality, and a leftover
    column would also let a stale client's `source_subpath` reach the DB.
    """
    import uuid as _uuid

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        # Stop at 002 — the last revision where the column still exists — seed
        # a job, then upgrade across 003.
        command.upgrade(cfg, "002")

        engine = sa.create_engine(db_url, echo=False)
        insert_job = (
            "INSERT INTO backup_jobs (id, name, source_label, destination_label, "
            "restic_password, schedule_type, schedule_value, enabled, "
            "exclude_caches, one_file_system, no_scan, check_enabled, "
            "compression, created_at, updated_at) "
            "VALUES (:id, :name, 'src', 'main', 'pw', 'interval', '1h', "
            "1, 0, 0, 0, 0, 'max', '2026-01-01', '2026-01-01')"
        )
        with engine.begin() as conn:
            conn.execute(
                sa.text(insert_job), {"id": str(_uuid.uuid4()), "name": "Photos"}
            )
        engine.dispose()

        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        inspector = inspect(engine)
        job_cols = {col["name"] for col in inspector.get_columns("backup_jobs")}
        assert "source_subpath" not in job_cols, "003 did not drop source_subpath"

        with engine.begin() as conn:
            rows = conn.execute(sa.text("SELECT name FROM backup_jobs")).all()
        assert rows == [("Photos",)], "003 lost pre-existing job rows"

        # The unique constraint must have survived the table rebuild.
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    sa.text(insert_job), {"id": str(_uuid.uuid4()), "name": "Photos"}
                )
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_schema_matches_orm_metadata():
    """The migration must produce the same table/column set as the ORM models."""
    from app.db.models import Base

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(db_url, echo=False)
        inspector = inspect(engine)
        migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
        orm_tables = set(Base.metadata.tables.keys())
        assert migrated_tables == orm_tables, (
            f"migration/ORM table mismatch: "
            f"only-in-migration={migrated_tables - orm_tables} "
            f"only-in-orm={orm_tables - migrated_tables}"
        )

        for table_name in orm_tables:
            migrated_cols = {c["name"] for c in inspector.get_columns(table_name)}
            orm_cols = {c.name for c in Base.metadata.tables[table_name].columns}
            assert migrated_cols == orm_cols, (
                f"column mismatch in {table_name}: "
                f"only-in-migration={migrated_cols - orm_cols} "
                f"only-in-orm={orm_cols - migrated_cols}"
            )

        engine.dispose()


@pytest.mark.asyncio
async def test_migration_falls_back_to_app_database_url(monkeypatch):
    """env.py uses app.db.database.DATABASE_URL when no override is set.

    Prevents regression of the alembic/runtime URL split that caused migrations
    to write to /app/billa-gates.db while the app read from /app/data/billa-gates.db.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fallback.db"
        monkeypatch.setattr(db_module, "DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
        monkeypatch.delenv("SQLALCHEMY_URL", raising=False)

        cfg = Config(Path(__file__).parent.parent / "alembic.ini")
        # Deliberately do NOT call cfg.set_main_option — exercises the fallback.
        command.upgrade(cfg, "head")

        assert db_path.exists(), "alembic did not write to the app's DATABASE_URL"

        engine = sa.create_engine(f"sqlite:///{db_path}", echo=False)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"backup_jobs", "backup_runs", "app_settings"}.issubset(tables)
        engine.dispose()


@pytest.mark.asyncio
async def test_migration_enum_values_match_orm():
    """Every enum value the ORM can persist must be declared in the migration.

    Column-name parity (test_migration_schema_matches_orm_metadata) does NOT
    catch *enum value* drift: SQLite stores these enums as plain VARCHAR with
    no CHECK constraint, so a value the migration forgot (e.g.
    RunStatus.canceled / RunReason.user_canceled) still writes fine on SQLite
    while silently diverging from the declared schema — and would be rejected
    the moment a CHECK constraint or a non-SQLite backend is introduced. This
    pins the migration's declared sa.Enum values to the ORM enums so they
    cannot drift apart again.
    """
    from app.db import models

    # Resolve the *effective* declaration across the whole revision chain, not
    # just 001: a later migration that widens an enum (002 adding the restic
    # 0.19 zstd modes) is the authority on that enum's final value set. Files
    # are read in revision order and later declarations win.
    #
    # Only each file's upgrade() body is parsed. A downgrade() necessarily names
    # the *old* value set too, and counting it would make a widening migration
    # look like it still declares the narrow enum.
    versions_dir = Path(__file__).parent.parent / "alembic" / "versions"
    declared: dict[str, set[str]] = {}
    for path in sorted(versions_dir.glob("[0-9]*.py")):
        upgrade_src = path.read_text().split("def downgrade")[0]
        # Extract {enum_name: {values}} from every sa.Enum("a", ..., name="x").
        for body, name in re.findall(
            r"sa\.Enum\((.*?)name=\"(\w+)\"", upgrade_src, re.S
        ):
            declared[name] = set(re.findall(r"\"([a-z_]+)\"", body))

    # SQLAlchemy derives each SAEnum `name` from the lowercased Python enum
    # class name, which is what the migration hard-codes.
    orm_enums = [
        models.ScheduleType,
        models.RunStatus,
        models.RunReason,
        models.RunKind,
        models.PruneStatus,
        models.CheckStatus,
        models.CheckMode,
        models.CompressionMode,
        models.TriggeredBy,
    ]
    for enum_cls in orm_enums:
        name = enum_cls.__name__.lower()
        assert name in declared, f"migration is missing sa.Enum(name={name!r})"
        orm_values = {e.value for e in enum_cls}
        assert declared[name] == orm_values, (
            f"enum value drift for {name!r}: "
            f"only-in-migration={declared[name] - orm_values} "
            f"only-in-orm={orm_values - declared[name]}"
        )
