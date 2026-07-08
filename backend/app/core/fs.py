"""Event-loop-safe filesystem probes.

A synchronous stat()/scandir() against a mounted-but-hung network share
(SMB server unreachable, NAS mid-reboot) can block in the kernel for tens
of seconds — and OrbStack/Docker virtiofs passes that hang straight into
the container. Called directly from async code, one hung probe freezes the
entire event loop: every API request, the scheduler, and every job
pipeline. `run_probe` pushes the blocking call onto a worker thread and
bounds the wait, so the worst case is one failed probe instead of a
frozen app.
"""

import asyncio
from typing import Any, Callable, Optional, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# How long a filesystem probe may take before the mount is treated as hung.
# A healthy local or network mount answers stat() in milliseconds; 10s is
# generous headroom for a briefly-busy NAS without wedging a backup run (or
# an API request) for minutes on a dead share. Resolved at call time so
# tests can patch it.
FS_PROBE_TIMEOUT_SECONDS: float = 10.0


async def run_probe(
    fn: Callable[..., T],
    *args: Any,
    timeout: Optional[float] = None,
    default: T,
) -> T:
    """Run a blocking filesystem call on a worker thread with a deadline.

    Returns ``default`` when the call does not finish within ``timeout``
    (falls back to FS_PROBE_TIMEOUT_SECONDS). The hung thread itself cannot
    be cancelled — it is left to finish in the pool whenever the kernel
    call returns — but the event loop stays responsive, which is the
    property that matters.

    Deliberately not decorated with @log_call: probes run on every backup
    and every mounts-API request, and the timeout warning below is the only
    event worth a log line.
    """
    if timeout is None:
        timeout = FS_PROBE_TIMEOUT_SECONDS
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "filesystem probe timed out fn=%s args=%s timeout=%ss — "
            "treating target as unavailable (hung network mount?)",
            getattr(fn, "__name__", repr(fn)),
            args,
            timeout,
        )
        return default
