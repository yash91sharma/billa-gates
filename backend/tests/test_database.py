"""Tests for database engine configuration."""

from sqlalchemy.pool import NullPool


def test_engine_uses_null_pool():
    """Production engine must use NullPool.

    SQLite + aiosqlite serializes all I/O through a single thread.  A
    QueuePool (the default) causes pool exhaustion under concurrent load
    because waiters pile up behind the SQLite write lock.  NullPool gives
    each async operation its own connection, eliminating the contention.
    """
    from app.db.database import engine

    assert isinstance(engine.pool, NullPool)
