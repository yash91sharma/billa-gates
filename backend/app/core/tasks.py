"""Strong-reference tracking for fire-and-forget asyncio tasks.

The event loop keeps only weak references to tasks: a task spawned with
``asyncio.create_task`` and not referenced anywhere else may be
garbage-collected mid-execution (documented behavior of ``create_task``).
For this app that failure is catastrophic — a collected backup pipeline
dies silently, no exception handler or ``finally`` runs, the run row is
stranded at ``status=running``, and the overlap check then skips every
future trigger of that job until the container restarts.

Every fire-and-forget task must therefore be spawned through
:func:`create_tracked_task`, which holds a strong reference in a
module-level set until the task completes.
"""

import asyncio
from typing import Any, Coroutine, Set

from app.core.logging import get_logger, log_call

logger = get_logger(__name__)

# Strong references to every in-flight background task. Tasks remove
# themselves on completion via _on_task_done, so the set stays bounded.
_background_tasks: Set["asyncio.Task[Any]"] = set()


@log_call
def _on_task_done(task: "asyncio.Task[Any]") -> None:
    """Drop the strong reference and surface any unhandled exception.

    Retrieving the exception here logs a pipeline crash the moment it
    happens instead of asyncio's deferred 'Task exception was never
    retrieved' warning at garbage-collection time.
    """
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(f"background task {task.get_name()!r} raised {exc!r}")


@log_call
def create_tracked_task(coro: Coroutine[Any, Any, Any]) -> "asyncio.Task[Any]":
    """Spawn a background task that cannot be garbage-collected mid-run."""
    task: "asyncio.Task[Any]" = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task
