"""Alembic migration environment."""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: the default (True) silences every
    # already-created logger. When migrations run inside the app process
    # (tests invoke alembic programmatically), that would permanently
    # disable all app.* loggers created before this point.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _resolve_url(ini_url: str | None) -> str:
    """Resolve the DB URL alembic should use.

    Priority: SQLALCHEMY_URL env var > value in alembic.ini / set_main_option >
    app.db.database.DATABASE_URL. The app uses an async driver (`+aiosqlite`)
    that alembic's sync engine cannot load, so the suffix is stripped from the
    fallback URL.
    """
    if "SQLALCHEMY_URL" in os.environ:
        return os.environ["SQLALCHEMY_URL"]
    if ini_url:
        return ini_url
    # Imported lazily so tests can monkeypatch DATABASE_URL before resolution.
    from app.db.database import DATABASE_URL

    return DATABASE_URL.replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    """Generate SQL migration script without running against database."""
    url = _resolve_url(config.get_main_option("sqlalchemy.url"))
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against live database."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url(configuration.get("sqlalchemy.url"))
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
