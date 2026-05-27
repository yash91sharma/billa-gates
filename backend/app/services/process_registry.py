"""Per-run registry of active restic subprocess handles + cancel flags.

Exists so the cancel API endpoint can reach into the backup pipeline and
terminate the in-flight restic process without re-architecting the pipeline
around message-passing. The pipeline is sequential — at most one restic
subprocess is alive per run at any moment — so a single-handle-per-run map
is sufficient.

The cancel flag is separate from the handle: the user may click Stop in the
gap between two restic subprocesses (e.g. between `restic backup` and
`restic forget`). The flag survives that gap so the pipeline notices the
intent on its next poll and short-circuits.
"""

import asyncio
import uuid
from typing import Dict, Optional, Set

from app.core.logging import get_logger

logger = get_logger(__name__)


async def _terminate_then_kill(
    proc: asyncio.subprocess.Process, grace_seconds: float = 10.0
) -> None:
    """SIGTERM the process, give it a grace window to clean up, SIGKILL only
    if it's still alive. Restic catches SIGTERM and removes its lock file —
    SIGKILL leaves the lock behind and breaks every subsequent backup.

    Lives here (and not in restic.py) so the process registry can terminate
    handles without restic.py needing to import the registry — that direction
    of dependency makes the registry self-contained and lets restic.py import
    from us instead, avoiding a circular import.
    """
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


# Module-level state. Reset between tests in conftest is not currently wired
# (the cancel tests reset these dicts themselves) — if more tests touch the
# registry, consider adding an autouse fixture that clears them.
_processes: Dict[uuid.UUID, asyncio.subprocess.Process] = {}
_canceled: Set[uuid.UUID] = set()


def register(run_id: uuid.UUID, proc: asyncio.subprocess.Process) -> None:
    """Record the subprocess currently driving this run."""
    _processes[run_id] = proc


def unregister(run_id: uuid.UUID) -> None:
    """Drop the subprocess handle. No-op when nothing was registered."""
    _processes.pop(run_id, None)


def get(run_id: uuid.UUID) -> Optional[asyncio.subprocess.Process]:
    return _processes.get(run_id)


def mark_canceled(run_id: uuid.UUID) -> None:
    """Set the cancel flag. The pipeline polls is_canceled() between steps."""
    _canceled.add(run_id)


def is_canceled(run_id: uuid.UUID) -> bool:
    return run_id in _canceled


def clear_canceled(run_id: uuid.UUID) -> None:
    """Drop the cancel flag once the pipeline has finalized the run row.
    Keeps the set bounded across long-lived processes."""
    _canceled.discard(run_id)


async def terminate(run_id: uuid.UUID) -> None:
    """SIGTERM (then SIGKILL) the registered subprocess for this run.

    No-op when no process is registered — common case when the user clicks
    Stop between two restic subprocess invocations. The pipeline will still
    notice is_canceled() on its next poll and short-circuit.
    """
    proc = _processes.get(run_id)
    if proc is None:
        logger.info(f"terminate run_id={run_id} no_process_registered")
        return
    logger.info(f"terminate run_id={run_id} sending_sigterm")
    await _terminate_then_kill(proc)
