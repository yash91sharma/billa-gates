"""The contract every restic subprocess is run under — one copy, all commands.

Nine call sites used to hand-roll it: the eight wrappers in `restic.py` plus
`snapshot_listing.list_snapshots`, each carrying its own transcription of
spawn → register-for-cancel → wait_for(timeout) → terminate-then-kill →
contain-the-launch-failure. Two had already drifted off the builders — `restic
version` and `restic snapshots` assembled their own argv, and the snapshot
listing assembled its own environment dict — which is exactly the drift
CLAUDE.md warns about for the command preview: a second copy starts lying the
first time a flag or an environment variable is added to the first.

The tests below are parametrized over *every* command in the app on purpose.
Each property they pin is load-bearing, and each was previously asserted for
`restic_cat_config` alone:

* **registered while it runs** — the Stop button reaches the live process
  through `process_registry`. A command that forgets to register cannot be
  canceled, and the click is silently ignored for however many hours the
  command runs.
* **released afterwards, timeouts included** — a stale handle lets a later
  cancel signal a process that no longer belongs to that run.
* **SIGTERM before SIGKILL** — restic removes its lock file on SIGTERM; a
  SIGKILL leaves a stale lock behind that breaks the job's next backup.
* **a launch failure never escapes** — no restic on PATH, a failed fork, a
  missing executable bit. Raising into a pipeline would abort it before the run
  row is finalized, stranding the row at `status=running`, which locks the job
  out of every future trigger via the overlap check.
"""

import ast
import asyncio
import contextlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import process_registry, restic, restic_process, snapshot_listing
from app.services.process_registry import _terminate_then_kill
from app.services.restic_process import ResticOutcome, run_restic
from tests.conftest import make_fake_process

REPO = "/destinations/main/photos"
PASSWORD = "s3cr3t"
SOURCE = "/sources/documents"
LOCK_ID = "a" * 64

# Commands issued as part of a run, and therefore cancelable: each takes a
# `run_id` and must be reachable through the process registry while it runs.
RUN_SCOPED = (
    "cat_config",
    "init",
    "latest_snapshot",
    "backup",
    "forget",
    "prune",
    "check",
    "unlock",
)
# `version` runs at startup, and `snapshots`, `list_locks` and `cat_lock`
# inside an API request — none belongs to a run, so none takes a run_id. They
# are still restic subprocesses and still need the timeout, the SIGTERM and the
# containment.
ALL_COMMANDS = RUN_SCOPED + ("version", "snapshots", "list_locks", "cat_lock")
# Everything except `restic version`, which is the one command that needs no
# repository and is given no repository environment.
REPO_SCOPED = RUN_SCOPED + ("snapshots", "list_locks", "cat_lock")

# The builder each command's argv must come from. Named here rather than
# inlined so a command added without a builder has nowhere to be listed.
BUILDERS: Dict[str, str] = {
    "cat_config": "build_cat_config_args",
    "init": "build_init_args",
    "latest_snapshot": "build_latest_snapshot_args",
    "backup": "build_backup_args",
    "forget": "build_forget_args",
    "prune": "build_prune_args",
    "check": "build_check_args",
    "unlock": "build_unlock_args",
    "version": "build_version_args",
    "snapshots": "build_snapshots_args",
    "list_locks": "build_list_locks_args",
    "cat_lock": "build_cat_lock_args",
}


async def _invoke(
    name: str,
    *,
    run_id: Optional[uuid.UUID] = None,
    timeout_seconds: int = 60,
) -> Any:
    """Call one restic command, holding everything else constant.

    `[]` is a valid response for every command that parses its output, so the
    same fake process serves all ten.
    """
    if name == "cat_config":
        return await restic.restic_cat_config(
            REPO, PASSWORD, timeout_seconds, run_id=run_id
        )
    if name == "init":
        return await restic.restic_init(REPO, PASSWORD, timeout_seconds, run_id=run_id)
    if name == "latest_snapshot":
        return await restic.restic_latest_snapshot_id(
            REPO, PASSWORD, timeout_seconds=timeout_seconds, run_id=run_id
        )
    if name == "backup":
        return await restic.restic_backup(
            REPO, PASSWORD, SOURCE, timeout_seconds, run_id=run_id
        )
    if name == "forget":
        return await restic.restic_forget(
            REPO, PASSWORD, timeout_seconds, run_id=run_id, retain_keep_last=3
        )
    if name == "prune":
        return await restic.restic_prune(REPO, PASSWORD, timeout_seconds, run_id=run_id)
    if name == "check":
        return await restic.restic_check(
            REPO, PASSWORD, "structural", None, timeout_seconds, run_id=run_id
        )
    if name == "unlock":
        return await restic.restic_unlock(
            REPO, PASSWORD, timeout_seconds, run_id=run_id
        )
    if name == "version":
        return await restic.restic_version()
    if name == "list_locks":
        return await restic.restic_list_locks(REPO, PASSWORD, timeout_seconds)
    if name == "cat_lock":
        return await restic.restic_cat_lock(REPO, PASSWORD, LOCK_ID, timeout_seconds)
    if name == "snapshots":
        return await snapshot_listing.list_snapshots(
            REPO, PASSWORD, timeout_seconds=timeout_seconds, use_cache=False
        )
    raise AssertionError(f"unknown command {name!r} — add it to _invoke")


def _hanging_process() -> AsyncMock:
    """A process that never finishes, for the timeout paths."""
    proc = AsyncMock()
    proc.returncode = None
    proc.communicate = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    return proc


async def _always_timeout(coro: Any, timeout: Any) -> None:
    """Stand-in for asyncio.wait_for that always gives up.

    Closing the coroutine keeps "never awaited" warnings out of the run.
    """
    if hasattr(coro, "close"):
        coro.close()
    raise asyncio.TimeoutError()


# Errors the two parsing commands raise instead of returning a failure tuple.
RAISED_FAILURES = (restic.ResticError, snapshot_listing.SnapshotListingError)


def _assert_reports_failure(name: str, result: Any, fragment: str) -> None:
    """Every command reports a failure in its own documented shape."""
    if name == "version":
        # A version that cannot be read is None; callers store NULL and the
        # health endpoint reports it as unknown.
        assert result is None
        return
    if name == "backup":
        rc, _stdout, stderr, summary = result
        assert rc == -1
        assert summary is None, "a failed launch must never look like a snapshot"
        assert fragment in stderr
        return
    rc, stdout, stderr = result
    assert rc == -1
    assert stdout == ""
    assert fragment in stderr


# ── The contract, over every command ─────────────────────────────────────────


@pytest.mark.parametrize("name", RUN_SCOPED)
async def test_every_run_scoped_command_is_reachable_for_cancel_while_it_runs(name):
    """Stop terminates whatever restic process the run has open right now.

    It finds it in the registry, so a command that does not register itself is
    a hole in cancellation: the click sets the flag, kills nothing, and the
    pipeline only notices between steps — after the command has finished.
    """
    run_id = uuid.uuid4()
    proc = make_fake_process(0, stdout="[]")
    seen: Dict[str, Any] = {}
    register = process_registry.register

    def spy(rid: uuid.UUID, p: Any) -> None:
        seen["registered"] = (rid, p)
        register(rid, p)

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(process_registry, "register", spy),
    ):
        await _invoke(name, run_id=run_id)

    assert seen.get("registered") == (run_id, proc), (
        f"{name} never registered its process — Stop cannot reach it"
    )
    assert process_registry.get(run_id) is None, f"{name} leaked its process handle"


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_no_command_touches_the_registry_outside_a_run(name):
    """`version` at startup and `snapshots` in a request belong to no run, and
    the run-scoped commands are also called without a run_id (repository
    provisioning at job creation). None of them may leave an entry behind: a
    stale handle would let a later cancel signal an unrelated process."""
    proc = make_fake_process(0, stdout="[]")
    before = dict(process_registry._processes)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await _invoke(name)

    assert process_registry._processes == before


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_every_command_sigterms_the_process_it_gave_up_on(name):
    """SIGTERM first, always. restic catches it and removes its lock file;
    SIGKILL leaves the lock behind and breaks the job's next backup."""
    proc = _hanging_process()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=_always_timeout),
        contextlib.suppress(*RAISED_FAILURES),
    ):
        await _invoke(name, timeout_seconds=1)

    proc.terminate.assert_called_once()


@pytest.mark.parametrize("name", RUN_SCOPED)
async def test_every_run_scoped_command_releases_its_handle_when_it_times_out(name):
    """The release lives in a finally block precisely so a hung backend does
    not leak the handle into the next run's cancel."""
    run_id = uuid.uuid4()
    proc = _hanging_process()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=_always_timeout),
        contextlib.suppress(*RAISED_FAILURES),
    ):
        await _invoke(name, run_id=run_id, timeout_seconds=1)

    assert process_registry.get(run_id) is None


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_every_command_reports_a_timeout_instead_of_hanging(name):
    """A hung mount must end the command, not the app. Whatever shape the
    command reports in, the caller has to be able to tell it timed out."""
    proc = _hanging_process()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=_always_timeout),
    ):
        try:
            result = await _invoke(name, timeout_seconds=1)
        except RAISED_FAILURES as exc:
            assert "timed out" in str(exc).lower()
            return

    _assert_reports_failure(name, result, "timed out")


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_no_command_lets_a_launch_failure_escape(name):
    """restic missing from PATH is not hypothetical — the dev container has no
    restic binary, and an image built without the fetcher stage would hit this
    on every run. An OSError raised into a pipeline aborts it before the run
    row is closed out, and a row stuck at `running` locks the job out of every
    future trigger."""
    boom = FileNotFoundError("No such file or directory: 'restic'")

    with patch("asyncio.create_subprocess_exec", side_effect=boom):
        try:
            result = await _invoke(name)
        except RAISED_FAILURES as exc:
            assert "No such file or directory" in str(exc)
            return

    _assert_reports_failure(name, result, "No such file or directory")


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_every_command_builds_its_argv_with_a_builder(name):
    """No command assembles its own command line.

    The Job detail page renders the builders' output as the commands a run
    issues (app/services/job_commands.py). A command that inlines its argv is
    invisible there and free to disagree with it — which is how an operator
    ends up trusting an exclude or a retention flag that is not applied.
    """
    marker = ["restic", f"--marker-{name}"]
    proc = make_fake_process(0, stdout="[]")
    captured: Dict[str, List[str]] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = [str(a) for a in args]
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(restic, BUILDERS[name], lambda *a, **k: list(marker)),
    ):
        await _invoke(name)

    assert captured["argv"] == marker, (
        f"{name} did not spawn what restic.{BUILDERS[name]}() returned"
    )


@pytest.mark.parametrize("name", REPO_SCOPED)
async def test_every_repository_command_gets_the_repository_environment(name):
    proc = make_fake_process(0, stdout="[]")
    captured: Dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await _invoke(name)

    env = captured["env"]
    assert env["RESTIC_REPOSITORY"] == REPO
    assert env["RESTIC_PASSWORD"] == PASSWORD
    assert env["RESTIC_CACHE_DIR"]
    assert env.get("PATH"), "the inherited environment must survive the overrides"


@pytest.mark.parametrize("name", REPO_SCOPED)
async def test_every_repository_command_takes_its_environment_from_one_builder(name):
    """`build_restic_env_overrides` is the only description of what restic is
    given. The snapshot listing used to keep a second copy of that dict, so it
    would have silently missed anything added here."""
    proc = make_fake_process(0, stdout="[]")
    captured: Dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(
            restic,
            "build_restic_env_overrides",
            lambda repo_path, password: {"RESTIC_MARKER": "from-the-builder"},
        ),
    ):
        await _invoke(name)

    assert captured["env"].get("RESTIC_MARKER") == "from-the-builder", (
        f"{name} built its own environment instead of calling the builder"
    )


@pytest.mark.parametrize("name", ALL_COMMANDS)
async def test_every_command_captures_both_streams(name):
    """restic reports per-file errors on stderr and its summary on stdout, so
    both pipes are read for every command. Both must also stay drained — a full
    stderr pipe blocks restic even while stdout is being consumed."""
    proc = make_fake_process(0, stdout="[]")
    captured: Dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await _invoke(name)

    assert captured["stdout"] == asyncio.subprocess.PIPE
    assert captured["stderr"] == asyncio.subprocess.PIPE


def _spawns_a_subprocess(path: Path) -> bool:
    """True when this module actually *calls* create_subprocess_exec.

    Parsed rather than grepped so a module that only names it in prose (as
    job_commands.py does, explaining what its tests compare against) is not
    mistaken for a second spawn site.
    """
    return any(
        isinstance(node, ast.Call)
        and (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
        )
        == "create_subprocess_exec"
        for node in ast.walk(ast.parse(path.read_text()))
    )


def test_only_restic_process_spawns_a_subprocess():
    """One spawn site in the whole app.

    This is what makes the properties above true by construction rather than by
    review: a new restic command cannot forget the registry, the timeout or the
    SIGTERM, because it does not spawn anything itself.
    """
    app_root = Path(__file__).resolve().parents[1] / "app"
    spawners = sorted(
        path.relative_to(app_root).as_posix()
        for path in app_root.rglob("*.py")
        if _spawns_a_subprocess(path)
    )
    assert spawners == ["services/restic_process.py"]


# ── run_restic itself ────────────────────────────────────────────────────────


async def test_run_restic_returns_the_exit_code_and_the_raw_streams():
    """Bytes, not text: decoding belongs to the caller, which is what keeps a
    stray non-UTF-8 byte in restic's stderr from being reclassified as a launch
    failure."""
    proc = make_fake_process(3, stdout="out", stderr="err")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        outcome = await run_restic(
            ["restic", "backup"], env_overrides={}, timeout_seconds=60
        )

    assert outcome == ResticOutcome(returncode=3, stdout=b"out", stderr=b"err")
    assert outcome.error is None and not outcome.timed_out


async def test_run_restic_marks_a_timeout_and_terminates_the_process():
    proc = _hanging_process()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=_always_timeout),
    ):
        outcome = await run_restic(
            ["restic", "check"], env_overrides={}, timeout_seconds=1
        )

    assert outcome.timed_out
    assert outcome.returncode == -1
    assert isinstance(outcome.error, asyncio.TimeoutError)
    proc.terminate.assert_called_once()


async def test_run_restic_carries_the_original_launch_exception():
    """Callers phrase their own message but chain the real cause, so the log
    still names what actually went wrong."""
    boom = PermissionError("Permission denied: 'restic'")

    with patch("asyncio.create_subprocess_exec", side_effect=boom):
        outcome = await run_restic(
            ["restic", "init"], env_overrides={}, timeout_seconds=60
        )

    assert outcome.error is boom
    assert not outcome.timed_out
    assert outcome.returncode == -1


async def test_run_restic_contains_a_failure_raised_while_consuming_output():
    """A pipe that breaks mid-read is reported the same way as a failed spawn,
    rather than raising into whatever pipeline is running."""
    proc = make_fake_process(0)

    async def exploding_consumer(_proc: Any) -> None:
        raise OSError("broken pipe")

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        outcome = await run_restic(
            ["restic", "backup"],
            env_overrides={},
            timeout_seconds=60,
            consume=exploding_consumer,
        )

    assert isinstance(outcome.error, OSError)
    assert "broken pipe" in str(outcome.error)


async def test_run_restic_hands_the_process_to_a_custom_consumer():
    """`restic backup` streams its output instead of buffering it, so it
    consumes the pipes itself and the outcome carries no bytes."""
    proc = make_fake_process(0, stdout="ignored")
    seen: Dict[str, Any] = {}

    async def consumer(p: Any) -> None:
        seen["proc"] = p

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        outcome = await run_restic(
            ["restic", "backup"], env_overrides={}, timeout_seconds=60, consume=consumer
        )

    assert seen["proc"] is proc
    assert outcome.returncode == 0
    assert outcome.stdout == b"" and outcome.stderr == b""


async def test_run_restic_registers_only_for_the_duration_of_the_call():
    run_id = uuid.uuid4()
    proc = make_fake_process(0)
    live: Dict[str, Any] = {}

    async def consumer(_p: Any) -> None:
        live["during"] = process_registry.get(run_id)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await run_restic(
            ["restic", "prune"],
            env_overrides={},
            timeout_seconds=60,
            run_id=run_id,
            consume=consumer,
        )

    assert live["during"] is proc
    assert process_registry.get(run_id) is None


# ── _terminate_then_kill ─────────────────────────────────────────────────────


async def test_terminate_then_kill_sigterm_only_when_process_exits_in_grace():
    """If the process exits during the grace period, only terminate() is called
    — restic gets a chance to clean up its lock file and we never SIGKILL it."""
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0

    await _terminate_then_kill(proc, grace_seconds=5.0)

    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


async def test_terminate_then_kill_falls_through_to_sigkill_after_grace():
    """If the process is still alive after the grace period, SIGKILL is sent
    as a last resort. This is the only path that should ever leave a stale
    lock file behind."""
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=-9)

    with patch("asyncio.wait_for", side_effect=_always_timeout):
        await _terminate_then_kill(proc, grace_seconds=0.01)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    # After kill we must wait again, otherwise we'd leak a zombie.
    assert proc.wait.await_count >= 1


def test_restic_process_owns_the_registration_helper():
    """`_tracked` moved here with the spawn it guards; nothing else needs it."""
    assert hasattr(restic_process, "_tracked")
