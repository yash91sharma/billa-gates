"""How a restic subprocess is run — the one place in the app that spawns one.

:func:`run_restic` owns a restic process for its whole lifetime, and it owns it
for every command: `cat config`, `init`, `snapshots`, `backup`, `forget`,
`prune`, `check`, `unlock` and `version`. Four properties travel with it, and
the app depends on all four at every call site:

* **The process is registered while it runs.** The cancel endpoint reaches the
  live process through :mod:`app.services.process_registry`. A command that
  does not register cannot be stopped, so the Stop click sets the flag, kills
  nothing, and is only noticed in the gap after the command finishes on its own
  — which on a multi-hour backup is exactly the wait the click was meant to end.
* **The wait is bounded.** A mounted-but-hung SMB share makes restic block
  indefinitely. Without the timeout the run row stays at `status=running`, and
  a row stuck there locks the job out of every future trigger, manual or
  scheduled, through the overlap check.
* **Giving up means SIGTERM first, SIGKILL only as a fallback.** restic catches
  SIGTERM and removes its lock file; SIGKILL leaves that lock behind and breaks
  the job's next backup.
* **A failure to start is contained.** No restic on PATH, a failed fork, a lost
  executable bit. An OSError raised into a pipeline aborts it before the run
  row is closed out, which strands the row at `running` — the same lock-out as
  above. Not hypothetical: the dev container carries no restic binary, and an
  image built without the fetcher stage hits this on every run.

These were transcribed at nine call sites — the eight wrappers in
:mod:`app.services.restic` plus `snapshot_listing.list_snapshots` — and
asserted at one. Duplicated safety code is duplicated in name only: when
something is found, one copy gets fixed. They are now asserted over this
function for all ten commands (tests/test_restic_process.py), and the same
module pins the reason it stays that way: nothing else in `app/` may call
`create_subprocess_exec`.

Output stays as bytes here on purpose. Decoding belongs to the caller, so a
stray non-UTF-8 byte in restic's stderr surfaces where it happened instead of
being reclassified as "restic failed to launch".
"""

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterator, Mapping, Optional, Sequence, Tuple

from app.core.logging import get_logger, log_call
from app.services import process_registry
from app.services.process_registry import _terminate_then_kill

logger = get_logger(__name__)


@contextlib.contextmanager
def _tracked(
    run_id: Optional[uuid.UUID], proc: asyncio.subprocess.Process
) -> Iterator[None]:
    """Register the subprocess in the registry for the lifetime of the call.

    No-op when run_id is None — commands that belong to no run (`restic
    version` at startup, repository provisioning at job creation) must not
    leave an entry behind, because a stale handle would let a later cancel
    signal a process that no longer belongs to that run. Cleanup in the finally
    keeps the registry from leaking even when the call times out or raises.
    """
    if run_id is None:
        yield
        return
    process_registry.register(run_id, proc)
    try:
        yield
    finally:
        process_registry.unregister(run_id)


@dataclass(frozen=True)
class ResticOutcome:
    """What running a restic command produced. Exactly one of three shapes:

    * **completed** — `error` is None and `returncode` is restic's exit code.
    * **timed out** — `timed_out` is True and `error` is the `TimeoutError`.
      The process has already been terminated.
    * **never produced an exit code** — `error` is the exception that stopped
      it: a failed spawn, or a failure while its output was being consumed.

    Callers test `timed_out` before `error`, because a timeout sets both. The
    two need different words: a timeout names the limit the operator
    configured, a launch failure names the exception.
    """

    returncode: int = -1
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    error: Optional[BaseException] = None


# Consumes both pipes of a running restic process. The default reads them to
# the end with `communicate()`; `restic backup` replaces it because its output
# is a multi-hour firehose that must be digested as it arrives.
OutputConsumer = Callable[[asyncio.subprocess.Process], Awaitable[None]]


@log_call
async def run_restic(
    argv: Sequence[str],
    *,
    env_overrides: Optional[Mapping[str, str]] = None,
    timeout_seconds: float,
    run_id: Optional[uuid.UUID] = None,
    consume: Optional[OutputConsumer] = None,
) -> ResticOutcome:
    """Run one restic command to completion, a timeout, or a contained failure.

    `argv` comes from a `restic.build_*_args()` builder and is never assembled
    here or at a call site: the Job detail page renders those same builders as
    the commands a run issues, so an inlined argv is a command line the preview
    cannot show (app/services/job_commands.py).

    `env_overrides` is layered over this process's own environment — the
    inherited one carries PATH and the container's locale, the overrides carry
    the repository, its password and the cache directory. Passing None inherits
    unchanged, which only `restic version` wants; every repository command
    passes `restic.build_restic_env_overrides()`, the single description of
    what a restic subprocess is told about the repo.

    `consume` replaces the default `communicate()` capture for `restic backup`,
    which streams its output instead of buffering it (app/services/
    restic_stream.py). It is awaited inside the timeout with the process
    registered, exactly like the default, and the outcome then carries no bytes
    because the caller's collector holds them.
    """
    env: Optional[Mapping[str, str]] = (
        None if env_overrides is None else {**os.environ, **env_overrides}
    )
    captured: Tuple[bytes, bytes] = (b"", b"")

    async def _capture(proc: asyncio.subprocess.Process) -> None:
        nonlocal captured
        captured = await proc.communicate()

    consumer: OutputConsumer = _capture if consume is None else consume

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                await asyncio.wait_for(consumer(proc), timeout=timeout_seconds)
            except asyncio.TimeoutError as exc:
                await _terminate_then_kill(proc)
                logger.warning(
                    "restic %s timed out after %ss",
                    argv[1] if len(argv) > 1 else argv[0],
                    timeout_seconds,
                )
                return ResticOutcome(timed_out=True, error=exc)
    except Exception as exc:
        logger.error("restic command failed to run argv=%s error=%r", list(argv), exc)
        return ResticOutcome(error=exc)

    assert proc.returncode is not None
    return ResticOutcome(proc.returncode, captured[0], captured[1])
