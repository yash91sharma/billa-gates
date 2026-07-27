import asyncio
import codecs
import contextlib
import json
import os
import re
import time
import uuid as _uuid
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

from app.core.logging import get_logger, log_call
from app.services import process_registry
from app.services.process_registry import _terminate_then_kill

logger = get_logger(__name__)


class ResticError(Exception):
    """Raised when a restic operation fails or times out."""

    pass


@contextlib.contextmanager
def _tracked(
    run_id: Optional[_uuid.UUID], proc: asyncio.subprocess.Process
) -> Iterator[None]:
    """Register the subprocess in the registry for the lifetime of the call.

    No-op when run_id is None — every restic command keeps backwards-compat
    callers (e.g. restic_version) working unchanged. Cleanup in the finally
    keeps the registry from leaking even when the wrapper raises.
    """
    if run_id is None:
        yield
        return
    process_registry.register(run_id, proc)
    try:
        yield
    finally:
        process_registry.unregister(run_id)


@log_call
def build_restic_env_overrides(repo_path: str, password: str) -> Dict[str, str]:
    """The environment variables every restic command is given, without the
    inherited process environment.

    Split out from :func:`_get_restic_env` so the job command preview
    (app/services/job_commands.py) can show the operator the same variables the
    subprocess actually receives, instead of a second hand-written copy that
    would drift.
    """
    return {
        "RESTIC_REPOSITORY": repo_path,
        "RESTIC_PASSWORD": password,
        "RESTIC_CACHE_DIR": os.environ.get(
            "RESTIC_CACHE_DIR", "/app/data/restic-cache"
        ),
    }


def _get_restic_env(repo_path: str, password: str) -> Dict[str, str]:
    """Build the process environment dictionary with configured repository path,
    password, and cache directory. Respects RESTIC_CACHE_DIR from host environment.
    """
    return {**os.environ, **build_restic_env_overrides(repo_path, password)}


# ── Command-line builders ────────────────────────────────────────────────────
#
# Every wrapper below execs the argv its builder returns, and nothing else.
# The builders are pure so the Job detail page can render exactly the command
# a run will issue (see app/services/job_commands.py). Never inline an argv in
# a wrapper again: the moment the preview and the subprocess are assembled by
# two different pieces of code, the page starts telling operators that flags
# are applied which are not — and retention/exclude mistakes of that kind are
# invisible until data is already lost.

RESTIC_BIN: str = "restic"


def build_cat_config_args() -> List[str]:
    """argv for the repo/password verification step."""
    return [RESTIC_BIN, "cat", "config"]


def build_init_args() -> List[str]:
    """argv for repository provisioning (job creation only, never a run)."""
    return [RESTIC_BIN, "init"]


def build_latest_snapshot_args() -> List[str]:
    """argv for the parent-snapshot lookup.

    `--no-lock` keeps this read-only step from blocking on a write lock held by
    a concurrent backup or left behind as a stale lock file.
    """
    return [RESTIC_BIN, "snapshots", "--latest", "1", "--json", "--no-lock"]


def build_unlock_args() -> List[str]:
    """argv for stale-lock removal."""
    return [RESTIC_BIN, "unlock"]


def build_prune_args() -> List[str]:
    """argv for a standalone prune (no retention flags — see restic_forget)."""
    return [RESTIC_BIN, "prune"]


def build_check_args(mode: str, subset_percent: Union[int, str, None]) -> List[str]:
    """argv for an integrity check. `structural` needs no extra flag.

    `subset_percent` is an int in every execution path. The union widens it for
    the command preview alone, which passes a placeholder for the percentage
    the operator types into the check dialog — so the flag's spelling still
    comes from here rather than being re-written by the preview.
    """
    args: List[str] = [RESTIC_BIN, "check"]
    if mode == "full":
        args.append("--read-data")
    elif mode == "subset" and subset_percent is not None:
        args.append(f"--read-data-subset={subset_percent}%")
    return args


def build_backup_args(
    source_path: str,
    *,
    parent_snapshot_id: Optional[str] = None,
    **kwargs: Any,
) -> List[str]:
    """argv for `restic backup`, assembled from a job's options.

    `--host` is pinned to a fixed string so retention isn't silently split
    per container ID (each rebuild gets a new hostname, and `restic forget`
    groups by host+paths by default).

    Snapshots carry no per-job identity tag: the repo at
    /destinations/<label>/<name> belongs to exactly one job, so the repo is
    already the scope. Retention across path changes (gaps.md C3) is handled
    by `restic forget --group-by ''`, which collapses host and paths into a
    single group. Any --tag values below are the user's own, from job.tags.
    """
    args: List[str] = [
        RESTIC_BIN,
        "backup",
        "--host",
        "billa-gates",
    ]

    # Explicit --parent lets restic skip the full-tree rescan even when host
    # or paths have changed; without it, any source_subpath change makes the
    # next backup re-read every file from disk (gaps.md C5). Omit on the
    # genuine first run — passing a bogus --parent would fail the backup.
    if parent_snapshot_id:
        args.extend(["--parent", parent_snapshot_id])

    if kwargs.get("exclude_patterns"):
        for pattern in kwargs["exclude_patterns"]:
            args.extend(["--exclude", pattern])

    if kwargs.get("exclude_caches"):
        args.append("--exclude-caches")

    if kwargs.get("exclude_if_present"):
        for file in kwargs["exclude_if_present"]:
            args.extend(["--exclude-if-present", file])

    if kwargs.get("one_file_system"):
        args.append("--one-file-system")

    if kwargs.get("no_scan"):
        args.append("--no-scan")

    if kwargs.get("tags"):
        for tag in kwargs["tags"]:
            args.extend(["--tag", tag])

    if kwargs.get("compression"):
        args.extend(["--compression", kwargs["compression"]])

    if kwargs.get("pack_size"):
        args.extend(["--pack-size", str(kwargs["pack_size"])])

    if kwargs.get("read_concurrency"):
        args.extend(["--read-concurrency", str(kwargs["read_concurrency"])])

    # Always emit JSON so summary/error lines are machine-parseable. Never
    # add --verbose: it makes restic print one JSON line per new/changed
    # file, which on a multi-million-file source is hundreds of MB of output
    # for no post-mortem value. Error lines and the final summary are emitted
    # regardless of verbosity.
    args.append("--json")

    args.append(source_path)
    return args


# Retention kwargs → `restic forget` flags. The only place the mapping lives.
FORGET_FLAG_MAP: Dict[str, str] = {
    "retain_keep_last": "--keep-last",
    "retain_keep_hourly": "--keep-hourly",
    "retain_keep_daily": "--keep-daily",
    "retain_keep_weekly": "--keep-weekly",
    "retain_keep_monthly": "--keep-monthly",
    "retain_keep_yearly": "--keep-yearly",
    "retain_keep_within": "--keep-within",
    "retain_keep_within_hourly": "--keep-within-hourly",
    "retain_keep_within_daily": "--keep-within-daily",
    "retain_keep_within_weekly": "--keep-within-weekly",
    "retain_keep_within_monthly": "--keep-within-monthly",
    "retain_keep_within_yearly": "--keep-within-yearly",
}


def build_forget_args(**retention_flags: Any) -> List[str]:
    """argv for `restic forget` with the job's retention policy.

    --group-by '' puts every snapshot in the repo into one retention group,
    so the policy applies across any historical path or host change. The
    original --group-by paths kept old-path snapshots forever whenever a
    job's source_subpath changed (gaps.md C3).

    Deliberately unfiltered: the repo holds exactly one job's snapshots.
    Scoping by a per-job tag would strand every snapshot taken before the
    current job row existed — they would never be pruned, and the repo would
    grow without bound after a job is recreated.
    """
    args: List[str] = [
        RESTIC_BIN,
        "forget",
        "--group-by",
        "",
    ]

    for kwarg_name, flag_name in FORGET_FLAG_MAP.items():
        if kwarg_name in retention_flags and retention_flags[kwarg_name] is not None:
            args.extend([flag_name, str(retention_flags[kwarg_name])])

    return args


@log_call
async def restic_version() -> Optional[str]:
    """Parse restic version. Returns None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "restic",
            "version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            await _terminate_then_kill(proc)
            return None

        output = stdout.decode()
        match = re.search(r"restic\s+([0-9.]+)", output)
        return match.group(1) if match else None
    except Exception:
        return None


@log_call
async def restic_cat_config(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Check repo exists and password correct.

    A 60s timeout is applied because this command runs on every backup as the
    init-check step. Without it, a hung remote backend (NFS unresponsive,
    SMB offline, cloud mount stalled) would block run_backup indefinitely,
    holding the run row at status=running and locking the job out of every
    future trigger via trigger_run's overlap check.
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_cat_config_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "cat config timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_init(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Initialize a new restic repo.

    Same lock-up hazard as restic_cat_config — if the init-check decides the
    repo doesn't exist and restic_init then hangs on an unresponsive backend,
    the runner is wedged indefinitely. A 60s timeout is more than enough for
    a healthy backend (init is metadata-only).
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_init_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "init timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_latest_snapshot_id(
    repo_path: str,
    password: str,
    *,
    timeout_seconds: int = 60,
    run_id: Optional[_uuid.UUID] = None,
) -> Optional[str]:
    """Return the id of the most recent snapshot in the repo, or None if no
    prior snapshot exists / the lookup fails. Used to pass --parent to
    `restic backup` so a host or path change doesn't force a full-tree rescan
    (gaps.md C5). Read-only — uses --no-lock so it never blocks on a write
    lock held by a concurrent backup or by a stale lock file.

    The repo belongs to exactly one job, so the newest snapshot in it is by
    definition this job's parent — no tag filter needed.
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_latest_snapshot_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                await _terminate_then_kill(proc)
                raise ResticError(
                    f"snapshots command timed out after {timeout_seconds} seconds"
                ) from exc
    except ResticError:
        raise
    except Exception as e:
        raise ResticError(f"failed to launch restic snapshots: {e}") from e

    if proc.returncode != 0:
        raise ResticError(
            f"snapshots command failed with exit code {proc.returncode}: "
            f"{stderr.decode()}"
        )
    try:
        snapshots = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise ResticError(f"snapshots command returned malformed JSON: {exc}") from exc
    if not isinstance(snapshots, list):
        raise ResticError(
            f"snapshots command returned non-list JSON: {type(snapshots).__name__}"
        )
    if not snapshots:
        return None
    snap_id = snapshots[0].get("id")
    if not isinstance(snap_id, str):
        raise ResticError("snapshots command returned snapshot without a string ID")
    return snap_id


# ── Streamed, bounded capture of `restic backup --json` output ───────────────
#
# In JSON mode restic emits a progress line continuously for the whole duration
# of a run, even when stdout is a pipe. The cadence is restic's to choose and it
# has already changed once: measured over the same 1.2 GB source, 0.18.1 emitted
# ~42 lines/s (~9.5 KB/s, ~34 MB/hour) and 0.19.1 ~6.5 lines/s (~1.7 KB/s). The
# bound below is what makes that irrelevant — do not re-derive it from a rate.
#
# Reading the stream with `communicate()` held every byte in memory until the
# process exited (~1 GB RSS on a five-hour backup once
# the decode/scrub/repr copies are counted), and then all of it was dropped
# before the run row was written. The collector below consumes the stream line
# by line and keeps a fixed-size view of it instead: error lines, the final
# summary, non-JSON diagnostics, and one continuously overwritten progress
# line. Memory is O(1) in the length of the run.

# Retained-output ceiling. Generous for a post-mortem (thousands of error
# lines) and still small enough to sit in a DB row and a run-detail response.
_MAX_RETAINED_OUTPUT_CHARS: int = 256 * 1024
_MAX_RETAINED_LINE_CHARS: int = 8 * 1024
_STREAM_CHUNK_BYTES: int = 65536
# How often the caller may be handed a snapshot of the retained output. At the
# tens-of-lines-per-second restic can emit, flushing per line would mean tens of
# DB writes per second.
_DEFAULT_PROGRESS_INTERVAL_SECONDS: float = 15.0


def _format_bytes(num_bytes: object) -> Optional[str]:
    """Render a byte count for the human-readable progress line."""
    if not isinstance(num_bytes, (int, float)) or isinstance(num_bytes, bool):
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(num_bytes)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return (
        f"{int(value)} {units[index]}" if index == 0 else f"{value:.1f} {units[index]}"
    )


def _format_eta(seconds: object) -> Optional[str]:
    """Render restic's `seconds_remaining` as a short ETA, or None if absent.

    restic omits it until it has scanned enough to estimate.
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    total = int(seconds)
    if total <= 0:
        return None
    if total < 60:
        return f"eta {total}s"
    if total < 3600:
        return f"eta {total // 60}m"
    return f"eta {total // 3600}h {(total % 3600) // 60}m"


def _format_progress(status: Dict[str, Any]) -> str:
    """Collapse one restic `message_type=status` line into a single line.

    Only what an operator watching a run wants to know — how far along, how
    many files, how many bytes, how much longer, and how many items restic
    could not read. Everything else in the status message (the rotating
    `current_files` list above all) is noise once it is one second old.

    `error_count` matters out of proportion to its size: files that fail during
    the scan never enter `total_files`, so a run that ends `warning` can show a
    spotless `100% · 1,234/1,234 files`. Without the error tally the progress
    line flatly contradicts the badge next to it.
    """
    parts: List[str] = []

    percent = status.get("percent_done")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        parts.append(f"{percent * 100:.0f}%")

    files_done = status.get("files_done")
    total_files = status.get("total_files")
    if isinstance(files_done, int) and isinstance(total_files, int):
        parts.append(f"{files_done:,}/{total_files:,} files")
    elif isinstance(files_done, int):
        parts.append(f"{files_done:,} files")

    bytes_done = _format_bytes(status.get("bytes_done"))
    total_bytes = _format_bytes(status.get("total_bytes"))
    if bytes_done and total_bytes:
        parts.append(f"{bytes_done}/{total_bytes}")
    elif bytes_done:
        parts.append(bytes_done)

    eta = _format_eta(status.get("seconds_remaining"))
    if eta:
        parts.append(eta)

    error_count = status.get("error_count")
    if (
        isinstance(error_count, int)
        and not isinstance(error_count, bool)
        and error_count > 0
    ):
        parts.append(f"{error_count:,} error{'' if error_count == 1 else 's'}")

    return f"progress: {' · '.join(parts)}" if parts else "progress: running"


class _BoundedOutput:
    """Fixed-size accumulator for a subprocess stream.

    Keeps whole lines up to `max_chars`, truncates any single oversized line,
    and counts what it had to drop so the caller can say output was omitted
    rather than silently losing it.
    """

    def __init__(
        self, password: str, max_chars: int = _MAX_RETAINED_OUTPUT_CHARS
    ) -> None:
        self._password = password
        self._max_chars = max_chars
        self._lines: List[str] = []
        self._chars = 0
        self._dropped = 0

    def scrub(self, line: str) -> str:
        """Strip the repo password. Per line now, since there is no longer a
        whole-stream string to run a single replace() over."""
        return line.replace(self._password, "") if self._password else line

    def add(self, line: str, *, force: bool = False) -> None:
        """Retain one line, unless that would push us past the ceiling.

        `force` is for the lines that must survive at any cost (the summary),
        which arrive last and would otherwise be lost behind an error flood.
        """
        if len(line) > _MAX_RETAINED_LINE_CHARS:
            line = line[:_MAX_RETAINED_LINE_CHARS] + "…<truncated>"
        if not force and self._chars + len(line) > self._max_chars:
            self._dropped += 1
            return
        self._lines.append(line)
        self._chars += len(line) + 1

    def text(self, *, extra: Optional[str] = None) -> str:
        parts = list(self._lines)
        if self._dropped:
            parts.append(
                f"... {self._dropped} more output line(s) omitted "
                f"(retained output is capped at {self._max_chars} characters)"
            )
        if extra:
            parts.append(extra)
        return "\n".join(parts)


class _BackupOutputCollector:
    """Classify `restic backup --json` lines into a bounded run record."""

    def __init__(self, password: str) -> None:
        self._out = _BoundedOutput(password)
        self.summary: Optional[Dict[str, Any]] = None
        self.progress: Optional[str] = None

    def feed(self, line: str) -> None:
        line = self._out.scrub(line.rstrip("\r"))
        stripped = line.strip()
        if not stripped:
            return

        parsed: Optional[Dict[str, Any]] = None
        if stripped.startswith("{"):
            try:
                candidate: Any = json.loads(stripped)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate

        if parsed is not None:
            message_type = parsed.get("message_type")
            if message_type == "status":
                # Never retained — this is the line that arrives 50x/second.
                self.progress = _format_progress(parsed)
                return
            if message_type == "summary":
                self.summary = parsed
                # Force-retained: it is the run's receipt and it arrives last,
                # after any per-file error flood that may have filled the cap.
                self._out.add(line, force=True)
                return

        self._out.add(line)

    def text(self) -> str:
        """The retained record, newest progress last."""
        return self._out.text(extra=self.progress)


async def _pump_stream(
    stream: asyncio.StreamReader, on_line: Callable[[str], Awaitable[None]]
) -> None:
    """Feed `on_line` complete lines as they arrive, with O(1) memory.

    Decoding is incremental so a multi-byte character split across two reads is
    not mangled into replacement characters, and a pathologically long line
    with no newline is flushed at the retention limit rather than growing
    without bound.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    while True:
        chunk = await stream.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        if "\n" in buffer:
            *complete, buffer = buffer.split("\n")
            for line in complete:
                await on_line(line)
        if len(buffer) > _MAX_RETAINED_LINE_CHARS:
            await on_line(buffer)
            buffer = ""
    buffer += decoder.decode(b"", final=True)
    if buffer:
        await on_line(buffer)


@log_call
async def restic_backup(
    repo_path: str,
    password: str,
    source_path: str,
    timeout_seconds: int,
    *,
    parent_snapshot_id: Optional[str] = None,
    run_id: Optional[_uuid.UUID] = None,
    on_output: Optional[Callable[[str], Awaitable[None]]] = None,
    progress_interval_seconds: Optional[float] = None,
    **kwargs: Any,
) -> Tuple[int, str, str, Optional[Dict[str, Any]]]:
    """Run a backup.

    Returns (returncode, retained stdout, retained stderr, parsed summary).
    Both output strings are bounded (see _BackupOutputCollector) — restic's
    progress firehose is collapsed into a single progress line rather than
    buffered.

    `on_output`, when given, is awaited with a snapshot of the retained stdout
    at most every `progress_interval_seconds` (and once as soon as there is
    anything to show). The backup runner uses it to keep the run row up to date
    while a long backup is still in flight. Exceptions from it are logged and
    swallowed: persistence is a side-effect and must never abort a backup.
    """
    env = _get_restic_env(repo_path, password)
    args: List[str] = build_backup_args(
        source_path, parent_snapshot_id=parent_snapshot_id, **kwargs
    )

    collector = _BackupOutputCollector(password)
    stderr_output = _BoundedOutput(password)
    interval: float = (
        _DEFAULT_PROGRESS_INTERVAL_SECONDS
        if progress_interval_seconds is None
        else progress_interval_seconds
    )
    # A monotonic deadline of 0 means the first line flushes immediately, so a
    # run that is slow to start still shows something before the first
    # interval elapses.
    next_flush: float = 0.0

    async def _handle_stdout_line(line: str) -> None:
        nonlocal next_flush
        collector.feed(line)
        if on_output is None:
            return
        now = time.monotonic()
        if now < next_flush:
            return
        next_flush = now + interval
        try:
            await on_output(collector.text())
        except Exception as exc:
            logger.warning(f"progress flush failed (non-fatal): {exc!r}")

    async def _handle_stderr_line(line: str) -> None:
        stderr_output.add(stderr_output.scrub(line.rstrip("\r")))

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_stream = proc.stdout
        stderr_stream = proc.stderr
        assert stdout_stream is not None and stderr_stream is not None
        with _tracked(run_id, proc):

            async def _drain() -> None:
                # Both pipes must be drained concurrently: a full stderr pipe
                # would block restic even while stdout is being consumed.
                await asyncio.gather(
                    _pump_stream(stdout_stream, _handle_stdout_line),
                    _pump_stream(stderr_stream, _handle_stderr_line),
                )
                await proc.wait()

            try:
                await asyncio.wait_for(_drain(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                # Keep what was collected: on a timeout the partial record is
                # the only evidence of where the run got to.
                return (-1, collector.text(), "backup timed out", None)
    except Exception as e:
        return (-1, "", str(e), None)

    # rc=3 is restic's "partial backup completed; snapshot was created" code —
    # the summary line is present and must be used so the snapshot can be
    # recorded as a warning run.
    assert proc.returncode is not None
    summary = collector.summary if proc.returncode in (0, 3) else None

    return proc.returncode, collector.text(), stderr_output.text(), summary


@log_call
async def restic_forget(
    repo_path: str,
    password: str,
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
    **retention_flags: Any,
) -> Tuple[int, str, str]:
    """Apply retention policy by removing snapshot pointers.

    Forget is cheap — it just rewrites the index to drop snapshot references.
    Prune is the heavy operation that rewrites pack files; it is *not* invoked
    here. Run :func:`restic_prune` separately (manual trigger or its own
    schedule) so a backup window stays predictable (gaps.md H1).
    """
    env = _get_restic_env(repo_path, password)
    args: List[str] = build_forget_args(**retention_flags)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "forget/prune timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_prune(
    repo_path: str,
    password: str,
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Standalone prune (no retention flags). Returns (returncode, stdout, stderr)."""
    env = _get_restic_env(repo_path, password)

    try:
        proc = await asyncio.create_subprocess_exec(
            *build_prune_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "prune timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_check(
    repo_path: str,
    password: str,
    mode: str,
    subset_percent: Optional[int],
    timeout_seconds: int,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Verify repo integrity."""
    env = _get_restic_env(repo_path, password)
    args: List[str] = build_check_args(mode, subset_percent)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "check timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


@log_call
async def restic_unlock(
    repo_path: str,
    password: str,
    timeout_seconds: int = 60,
    *,
    run_id: Optional[_uuid.UUID] = None,
) -> Tuple[int, str, str]:
    """Remove stale locks.

    Called both during init-check stale-lock recovery and in Step 4.5
    auto-unlock. A hung backend during unlock previously wedged the runner;
    60s is far more than needed for a metadata-only delete on a healthy
    backend.
    """
    env = _get_restic_env(repo_path, password)
    try:
        proc = await asyncio.create_subprocess_exec(
            *build_unlock_args(),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with _tracked(run_id, proc):
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except asyncio.TimeoutError:
                await _terminate_then_kill(proc)
                return (-1, "", "unlock timed out")
    except Exception as e:
        return (-1, "", str(e))

    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()
