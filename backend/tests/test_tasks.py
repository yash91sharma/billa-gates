"""Tests for app.core.tasks — strong-reference tracking of background tasks.

The event loop keeps only weak references to tasks: a task spawned with
asyncio.create_task and not referenced anywhere else may be garbage-collected
mid-execution (documented behavior of create_task). create_tracked_task must
hold a strong reference for the task's whole lifetime and drop it on
completion so the set stays bounded.
"""

import asyncio
import logging

import pytest

from app.core import tasks


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep leaked tasks from one test out of the next test's assertions."""
    tasks._background_tasks.clear()
    yield
    tasks._background_tasks.clear()


async def test_pending_task_is_strongly_referenced():
    started = asyncio.Event()
    release = asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    task = tasks.create_tracked_task(work())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    assert task in tasks._background_tasks

    release.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_completed_task_is_discarded():
    async def work():
        return 42

    task = tasks.create_tracked_task(work())
    await asyncio.wait_for(task, timeout=2.0)
    # add_done_callback runs via call_soon; yield once so it fires.
    await asyncio.sleep(0)

    assert task not in tasks._background_tasks
    assert len(tasks._background_tasks) == 0


async def test_returns_the_task_for_optional_awaiting():
    async def work():
        return "done"

    task = tasks.create_tracked_task(work())
    assert isinstance(task, asyncio.Task)
    assert await asyncio.wait_for(task, timeout=2.0) == "done"


async def test_failed_task_is_discarded_and_exception_logged(caplog):
    async def boom():
        raise RuntimeError("pipeline exploded")

    with caplog.at_level(logging.ERROR, logger="app.core.tasks"):
        task = tasks.create_tracked_task(boom())
        # The task raises immediately; retrieving it here would swallow the
        # exception path under test, so wait for the done callback instead.
        for _ in range(5):
            await asyncio.sleep(0)

    assert task.done()
    assert task not in tasks._background_tasks
    assert any("pipeline exploded" in r.getMessage() for r in caplog.records)


async def test_canceled_task_is_discarded_without_error_log(caplog):
    release = asyncio.Event()

    async def work():
        await release.wait()

    with caplog.at_level(logging.ERROR, logger="app.core.tasks"):
        task = tasks.create_tracked_task(work())
        await asyncio.sleep(0)
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)

    assert task.cancelled()
    assert task not in tasks._background_tasks
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
