"""Tests for app.core.fs.run_probe — event-loop-safe filesystem probes.

A stat()/scandir() against a mounted-but-hung SMB share can block in the
kernel for tens of seconds. Called directly from async code that freezes
the whole event loop (API, scheduler, every pipeline). run_probe pushes
the blocking call onto a worker thread and bounds the wait.
"""

import asyncio
import time

from app.core import fs


async def test_run_probe_returns_result_when_fast():
    def probe(path: str) -> bool:
        return path == "/fast"

    assert await fs.run_probe(probe, "/fast", default=False) is True
    assert await fs.run_probe(probe, "/other", default=True) is False


async def test_run_probe_returns_default_on_timeout():
    """A probe that outlives the timeout yields `default` promptly — the
    caller sees 'mount unavailable' instead of hanging with the kernel."""

    def hung_probe(path: str) -> bool:
        time.sleep(0.5)
        return True

    start = time.monotonic()
    result = await fs.run_probe(hung_probe, "/hung", timeout=0.1, default=False)
    elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 0.4, "run_probe must return at the timeout, not the probe"


async def test_run_probe_does_not_block_event_loop():
    """While the probe thread hangs, other coroutines must keep running —
    that is the whole point of the worker thread."""

    def hung_probe(path: str) -> bool:
        time.sleep(0.4)
        return True

    task = asyncio.create_task(
        fs.run_probe(hung_probe, "/hung", timeout=1.0, default=False)
    )
    start = time.monotonic()
    await asyncio.sleep(0.05)
    loop_delay = time.monotonic() - start

    assert loop_delay < 0.3, "event loop was blocked by the probe"
    assert await task is True


async def test_run_probe_default_timeout_is_module_constant():
    """Callers that don't pass a timeout get FS_PROBE_TIMEOUT_SECONDS,
    resolved at call time so tests (and ops tweaks) can patch it."""
    from unittest.mock import patch

    def hung_probe(path: str) -> bool:
        time.sleep(0.5)
        return True

    with patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.1):
        start = time.monotonic()
        result = await fs.run_probe(hung_probe, "/hung", default=False)
        elapsed = time.monotonic() - start

    assert result is False
    assert elapsed < 0.4
