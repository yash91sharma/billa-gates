"""Unit tests for restic subprocess wrappers."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.restic import (
    _terminate_then_kill,
    restic_backup,
    restic_cat_config,
    restic_check,
    restic_forget,
    restic_init,
    restic_prune,
    restic_unlock,
    restic_version,
)

REPO = "/destinations/main/photos"
PASSWORD = "s3cr3t"
# Note: `restic_forget_prune` was renamed to `restic_forget` once `--prune` was
# decoupled from forget (gaps.md H1) — prune now runs on its own schedule.


class _FakeStream:
    """Minimal StreamReader stand-in that hands out a payload in chunks.

    `restic_backup` consumes stdout/stderr incrementally (see
    app/services/restic.py::_pump_stream), so the fake has to *drain* — an
    always-returns-everything read() would spin forever. `chunk_size` lets a
    test force reads to land mid-line (or mid-UTF-8-character).
    """

    def __init__(self, data: bytes, chunk_size: int = 65536) -> None:
        self._data = data
        self._pos = 0
        self._chunk_size = chunk_size

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        size = self._chunk_size if n is None or n < 0 else min(n, self._chunk_size)
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def _make_process(
    returncode: int, stdout: str = "", stderr: str = "", chunk_size: int = 65536
) -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.stdout = _FakeStream(stdout.encode(), chunk_size)
    proc.stderr = _FakeStream(stderr.encode(), chunk_size)
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


# ── restic_version ────────────────────────────────────────────────────────────


async def test_version_returns_string_on_success():
    proc = _make_process(0, stdout="restic 0.17.3 compiled with go1.22.2\n")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_version()
    assert result == "0.17.3"


async def test_version_returns_none_on_failure():
    proc = _make_process(1, stderr="restic: command not found")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_version()
    assert result is None


async def test_version_returns_none_on_timeout():
    async def slow_communicate():
        await asyncio.sleep(100)
        return b"", b""

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            result = await restic_version()
    assert result is None


# ── restic_cat_config ─────────────────────────────────────────────────────────


async def test_cat_config_success():
    proc = _make_process(0, stdout='{"version": 2}')
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr = await restic_cat_config(REPO, PASSWORD)
    assert code == 0
    assert "version" in stdout


async def test_cat_config_wrong_password_returns_nonzero():
    proc = _make_process(1, stderr="wrong password or no key found")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr = await restic_cat_config(REPO, PASSWORD)
    assert code != 0
    assert "wrong password" in stderr


async def test_cat_config_repo_not_found():
    proc = _make_process(1, stderr="Fatal: no such file or directory")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr = await restic_cat_config(REPO, PASSWORD)
    assert code != 0
    assert "no such file" in stderr.lower()


async def test_cat_config_passes_env_vars():
    proc = _make_process(0, stdout='{"version":2}')
    captured_kwargs = {}

    async def fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_cat_config(REPO, PASSWORD)

    env = captured_kwargs.get("env", {})
    assert env.get("RESTIC_REPOSITORY") == REPO
    assert env.get("RESTIC_PASSWORD") == PASSWORD


async def test_cat_config_respects_custom_cache_dir():
    import os

    proc = _make_process(0, stdout='{"version":2}')
    captured_kwargs = {}

    async def fake_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.dict(os.environ, {"RESTIC_CACHE_DIR": "/my/custom/cache"}),
    ):
        await restic_cat_config(REPO, PASSWORD)

    env = captured_kwargs.get("env", {})
    assert env.get("RESTIC_CACHE_DIR") == "/my/custom/cache"


async def test_cat_config_times_out_returns_minus_one_and_terminates_process():
    """A hung backend (NFS unresponsive, SMB offline, cloud mount stalled)
    must not wedge the backup runner. `restic cat config` runs on every
    backup as the init-check step, so without a timeout a single bad mount
    point can lock every future trigger out via overlap detection."""

    async def slow_communicate():
        await asyncio.sleep(100)
        return b"", b""

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
    ):
        code, stdout, stderr = await restic_cat_config(
            REPO, PASSWORD, timeout_seconds=1
        )

    assert code == -1
    assert stdout == ""
    assert "timed out" in stderr.lower()
    proc.terminate.assert_called()


async def test_cat_config_default_timeout_is_60_seconds():
    """The default must stay at 60s — long enough to absorb a transient
    network blip on a healthy backend, short enough that a wedged mount
    point doesn't hold the runner for hours."""
    import inspect

    sig = inspect.signature(restic_cat_config)
    assert sig.parameters["timeout_seconds"].default == 60


# ── restic_init ───────────────────────────────────────────────────────────────


async def test_init_success():
    proc = _make_process(0, stdout="created restic repository")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr = await restic_init(REPO, PASSWORD)
    assert code == 0


async def test_init_failure():
    proc = _make_process(1, stderr="permission denied")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr = await restic_init(REPO, PASSWORD)
    assert code != 0
    assert "permission denied" in stderr


async def test_init_times_out_returns_minus_one_and_terminates_process():
    """`restic init` is invoked when the init-check decides the repo is
    new. A hung backend at that moment must not strand the runner — same
    hazard as cat_config."""

    async def slow_communicate():
        await asyncio.sleep(100)
        return b"", b""

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
    ):
        code, stdout, stderr = await restic_init(REPO, PASSWORD, timeout_seconds=1)

    assert code == -1
    assert stdout == ""
    assert "timed out" in stderr.lower()
    proc.terminate.assert_called()


async def test_init_default_timeout_is_60_seconds():
    import inspect

    sig = inspect.signature(restic_init)
    assert sig.parameters["timeout_seconds"].default == 60


# ── restic_backup ─────────────────────────────────────────────────────────────

BACKUP_SUMMARY = json.dumps(
    {
        "message_type": "summary",
        "files_new": 10,
        "files_changed": 5,
        "files_unmodified": 1000,
        "dirs_new": 2,
        "dirs_changed": 1,
        "dirs_unmodified": 50,
        "data_added": 1024000,
        "data_added_packed": 900000,
        "total_bytes_processed": 50000000,
        "snapshot_id": "abc123def456abc123def456abc123def456"
        "abc123def456abc123def456abc123def456abc1",
    }
)


async def test_backup_success_returns_zero_and_summary():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )
    assert code == 0
    assert summary is not None
    assert summary["files_new"] == 10


async def test_backup_failure_nonzero():
    proc = _make_process(1, stderr="Fatal: unable to open source")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )
    assert code != 0
    assert summary is None


async def test_backup_rc3_returns_summary_partial_backup():
    """restic exit code 3 means partial backup but snapshot was created. The
    JSON summary line is present in stdout and must be parsed just like rc=0."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    proc.returncode = 3
    proc.communicate = AsyncMock(return_value=(BACKUP_SUMMARY.encode(), b""))
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )
    assert code == 3
    assert summary is not None
    assert summary["snapshot_id"]


async def test_backup_timeout_kills_process():
    killed = {"called": False}

    async def slow_communicate():
        await asyncio.sleep(100)
        return b"", b""

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.returncode = None
    proc.kill = MagicMock(side_effect=lambda: killed.__setitem__("called", True))
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, stdout, stderr, summary = await restic_backup(
                REPO,
                PASSWORD,
                "/sources/documents",
                timeout_seconds=1,
            )
    assert code != 0
    assert "timed out" in stderr.lower()


async def test_backup_source_path_with_subpath():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents/photos",
            timeout_seconds=3600,
        )

    assert "/sources/documents/photos" in captured["args"]


async def test_backup_exclude_patterns_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            exclude_patterns=["node_modules/", "*.tmp"],
        )

    cmd = " ".join(str(a) for a in captured["args"])
    assert "--exclude" in cmd
    assert "node_modules/" in cmd


async def test_backup_exclude_caches_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            exclude_caches=True,
        )

    assert "--exclude-caches" in captured["args"]


async def test_backup_tags_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            tags=["weekly", "documents"],
        )

    cmd_list = list(captured["args"])
    assert "--tag" in cmd_list


async def test_backup_writes_no_job_identity_tag():
    """Snapshots carry no per-job identity tag.

    The repo at /destinations/<label>/<name> belongs to exactly one job, so
    the repo is already the scope; retention across path changes (gaps.md C3)
    is handled by `restic forget --group-by ''`. A job-id tag would also make
    every snapshot invisible to a job recreated over the same repo.
    """
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )

    args_list = list(captured["args"])
    assert "--tag" not in args_list
    assert not any(str(a).startswith("job:") for a in args_list)


async def test_backup_still_passes_user_tags():
    """job.tags is a user-facing feature and is unaffected by the removal of
    the internal identity tag."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            tags=["weekly", "offsite"],
        )

    args_list = list(captured["args"])
    tag_values = [args_list[i + 1] for i, a in enumerate(args_list) if a == "--tag"]
    assert tag_values == ["weekly", "offsite"]


async def test_backup_does_not_pass_verbose():
    """--verbose makes restic emit one JSON line per new/changed file; on a
    multi-million-file source that is hundreds of MB buffered in memory and
    persisted to the DB. Progress/summary/error reporting all work without
    it, so the backup command must not include it. --json must stay."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )

    args_list = list(captured["args"])
    assert "--verbose" not in args_list
    assert "--json" in args_list


async def test_backup_password_never_in_stdout():
    output = f"some output {PASSWORD} more output"
    proc = _make_process(0, stdout=output + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )
    assert PASSWORD not in stdout


async def test_backup_password_never_in_stderr():
    """stderr is persisted verbatim into BackupRun.error_output on failure, so
    it needs the same password strip as stdout."""
    err = f"Fatal: something mentioning {PASSWORD} failed"
    proc = _make_process(1, stderr=err)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )
    assert PASSWORD not in stderr


# ── restic_backup: streamed, bounded output ──────────────────────────────────
#
# In JSON mode restic emits a progress line ~50 times a second for the whole
# run (~12 KB/s, ~44 MB/hour). Buffering that whole stream costs GBs of RAM on
# a long backup — and all of it was dropped before the run row was written.
# The wrapper must consume the stream incrementally and keep a bounded view of
# it: errors, the summary, non-JSON diagnostics, and one live progress line.


def _status_line(files_done: int, percent: float = 0.5) -> str:
    return json.dumps(
        {
            "message_type": "status",
            "seconds_remaining": 120,
            "percent_done": percent,
            "total_files": 68900,
            "files_done": files_done,
            "total_bytes": 21260929843,
            "bytes_done": 13314398208,
            "current_files": ["/sources/photos/2021/album/IMG_1234.jpg"],
        }
    )


def _error_line(path: str) -> str:
    return json.dumps(
        {
            "message_type": "error",
            "error": {"message": "input/output error"},
            "during": "archival",
            "item": path,
        }
    )


async def test_backup_drops_progress_lines_from_retained_output():
    """Progress lines must never reach the retained output — they are the
    entire memory problem and carry no post-mortem value."""
    stream = (
        "\n".join(_status_line(i) for i in range(500))
        + "\n"
        + _error_line("/sources/documents/locked.db")
        + "\n"
        + BACKUP_SUMMARY
    )
    proc = _make_process(0, stdout=stream)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, _stderr, summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert code == 0
    assert '"message_type":"status"' not in stdout.replace(" ", "")
    assert "/sources/documents/locked.db" in stdout, "error lines must be kept"
    assert summary is not None and summary["files_new"] == 10
    assert len(stdout) < 10_000, (
        f"500 progress lines must not reach the run row (got {len(stdout)} chars)"
    )


async def test_backup_keeps_one_live_progress_line():
    """'Some sort of progress' is kept: a single, continuously overwritten
    human-readable line, so the run row shows where the backup got to."""
    stream = "\n".join(_status_line(i, percent=i / 100) for i in range(1, 100))
    proc = _make_process(0, stdout=stream + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _code, stdout, _stderr, _summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    progress_lines = [ln for ln in stdout.splitlines() if ln.startswith("progress:")]
    assert len(progress_lines) == 1, "exactly one progress line, always the newest"
    assert "99/68900 files" in progress_lines[0].replace(",", "")


async def test_backup_retained_output_is_capped():
    """A pathological run (millions of per-file errors) must not put the whole
    stream in the run row either — the retained view is hard-capped."""
    from app.services.restic import _MAX_RETAINED_OUTPUT_CHARS

    stream = "\n".join(_error_line(f"/sources/documents/f{i}") for i in range(20000))
    proc = _make_process(0, stdout=stream + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _code, stdout, _stderr, summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert len(stdout) <= _MAX_RETAINED_OUTPUT_CHARS + 4096
    assert "omitted" in stdout, "the user must be told output was dropped"
    assert summary is not None, (
        "the summary drives the run's stats columns and must survive the cap"
    )


async def test_backup_retains_error_lines_written_to_stderr():
    """restic writes per-file `message_type=error` lines to *stderr*; stdout
    carries only `status` and `summary`. Verified against 0.18.1 and 0.19.1 — see
    the stream capture in tests/test_backup_runner.py. The wrapper must hand
    that stderr back intact, because it is the only record of which file
    failed, and it must stay bounded like stdout does.
    """
    stderr = (
        "\n".join(_error_line(f"/sources/documents/f{i}") for i in range(3))
        + '\n{"message_type":"exit_error","code":3,"message":"Warning: at least '
        'one source file could not be read"}'
    )
    proc = _make_process(3, stdout=BACKUP_SUMMARY, stderr=stderr)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, err, summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert code == 3
    assert summary is not None, "rc=3 still produced a snapshot"
    assert "/sources/documents/f0" in err
    assert "/sources/documents/f2" in err
    assert "/sources/documents/f0" not in stdout, (
        "stdout is not where the caller can find the failing items"
    )


async def test_backup_stderr_flood_stays_bounded():
    """A million-file permission failure is drained and capped, not buffered —
    the error path must not reintroduce the memory problem stdout already
    solved."""
    from app.services.restic import _MAX_RETAINED_OUTPUT_CHARS

    stderr = "\n".join(_error_line(f"/sources/documents/f{i}") for i in range(20000))
    proc = _make_process(3, stdout=BACKUP_SUMMARY, stderr=stderr)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _code, _stdout, err, _summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert len(err) <= _MAX_RETAINED_OUTPUT_CHARS + 4096
    assert "omitted" in err, "the user must be told error output was dropped"


def test_format_progress_reports_error_count():
    """restic's status line carries `error_count`, and dropping it is why a run
    could show `100% · 1,234/1,234 files` next to a warning badge with nothing
    connecting the two."""
    from app.services.restic import _format_progress

    line = _format_progress(
        {
            "message_type": "status",
            "percent_done": 1,
            "total_files": 1234,
            "files_done": 1234,
            "error_count": 3,
        }
    )
    assert "100%" in line
    assert "3 errors" in line

    singular = _format_progress({"percent_done": 0.5, "error_count": 1})
    assert "1 error" in singular and "1 errors" not in singular

    clean = _format_progress({"percent_done": 0.5, "error_count": 0})
    assert "error" not in clean, "a clean run must not grow a scary suffix"


async def test_backup_flushes_output_snapshots_while_running():
    """The wrapper hands the caller periodic snapshots so a live run can show
    progress; without it the run row stays empty until restic exits."""
    snapshots: list[str] = []

    async def on_output(text: str) -> None:
        snapshots.append(text)

    stream = "\n".join(_status_line(i) for i in range(200)) + "\n" + BACKUP_SUMMARY
    proc = _make_process(0, stdout=stream)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            on_output=on_output,
            progress_interval_seconds=0,
        )

    assert snapshots, "on_output must be called during the run"
    assert any("progress:" in s for s in snapshots)


async def test_backup_flush_is_throttled():
    """Flushing on every status line would mean ~50 DB writes a second."""
    calls: list[str] = []

    async def on_output(text: str) -> None:
        calls.append(text)

    stream = "\n".join(_status_line(i) for i in range(500)) + "\n" + BACKUP_SUMMARY
    proc = _make_process(0, stdout=stream)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            on_output=on_output,
            progress_interval_seconds=3600,
        )

    assert len(calls) == 1, (
        f"expected a single initial flush inside the interval, got {len(calls)}"
    )


async def test_backup_survives_a_failing_output_flush():
    """A DB hiccup while persisting progress must never abort the backup."""

    async def on_output(text: str) -> None:
        raise RuntimeError("db is locked")

    proc = _make_process(0, stdout=_status_line(1) + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, _stdout, _stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            on_output=on_output,
            progress_interval_seconds=0,
        )

    assert code == 0
    assert summary is not None


async def test_backup_scrubs_password_from_streamed_lines():
    """Scrubbing moved per-line with the streaming rewrite; it must still hold
    for retained lines and for the progress line's file paths."""
    proc = _make_process(
        0, stdout=f"warning: could not read {PASSWORD}\n" + BACKUP_SUMMARY
    )
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _code, stdout, _stderr, _summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert PASSWORD not in stdout
    assert "could not read" in stdout


async def test_backup_handles_multibyte_split_across_chunks():
    """The stream is read in fixed-size chunks; a UTF-8 character straddling a
    chunk boundary must not be mangled into replacement characters."""
    name = "/sources/documents/café-αβγ-日本語.txt"
    # 3-byte chunks guarantee multi-byte characters are split mid-sequence.
    proc = _make_process(
        0, stdout=f"warning: skipped {name}\n" + BACKUP_SUMMARY, chunk_size=3
    )
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _code, stdout, _stderr, _summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )

    assert name in stdout


# ── restic_latest_snapshot_id ────────────────────────────────────────────────


async def test_latest_snapshot_id_returns_id_when_present():
    """restic snapshots --tag job:X --latest 1 --json returns a single-element
    array; the helper extracts the id so the caller can pass it as --parent."""
    from app.services.restic import restic_latest_snapshot_id

    snap_id = "ffffffff" + "0" * 56
    out = json.dumps([{"id": snap_id, "time": "2026-05-01T00:00:00Z"}])
    proc = _make_process(0, stdout=out)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_latest_snapshot_id(REPO, PASSWORD)
    assert result == snap_id


async def test_latest_snapshot_id_returns_none_on_empty_list():
    """A genuine first run has no prior snapshots; the helper must return None
    so the caller knows to omit --parent (passing --parent on first run would
    make restic fail with 'parent snapshot not found')."""
    from app.services.restic import restic_latest_snapshot_id

    proc = _make_process(0, stdout="[]")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_latest_snapshot_id(REPO, PASSWORD)
    assert result is None


async def test_latest_snapshot_id_raises_on_nonzero_rc():
    import pytest

    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(1, stderr="Fatal: unable to open repo")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD)
    assert "snapshots command failed with exit code 1" in str(exc_info.value)


async def test_latest_snapshot_id_raises_on_malformed_json():
    import pytest

    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(0, stdout="not-json-at-all")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD)
    assert "malformed JSON" in str(exc_info.value)


async def test_latest_snapshot_id_uses_latest_flag_without_tag():
    """--latest 1 keeps the lookup O(1). No --tag: the repo holds exactly one
    job's snapshots, so the newest one is by definition this job's parent."""
    from app.services.restic import restic_latest_snapshot_id

    proc = _make_process(0, stdout="[]")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_latest_snapshot_id(REPO, PASSWORD)

    args_list = list(captured["args"])
    assert "--json" in args_list
    assert "--tag" not in args_list
    assert "--latest" in args_list
    latest_idx = args_list.index("--latest")
    assert args_list[latest_idx + 1] == "1"


# ── restic_backup --parent flag ──────────────────────────────────────────────


async def test_backup_passes_parent_when_provided():
    """When the orchestrator finds a prior snapshot for this job (via
    restic_latest_snapshot_id), it must pass --parent <id> to restic backup
    so restic does an incremental rescan instead of a full-tree re-upload
    after any host/path change (gaps.md C5)."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    parent_id = "deadbeef" * 8
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            parent_snapshot_id=parent_id,
        )

    args_list = list(captured["args"])
    assert "--parent" in args_list
    p_idx = args_list.index("--parent")
    assert args_list[p_idx + 1] == parent_id


async def test_backup_omits_parent_when_none():
    """First-ever backup for a job has no prior snapshot; --parent must be
    absent or restic would fail with 'parent snapshot not found'."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            parent_snapshot_id=None,
        )

    assert "--parent" not in captured["args"]


# ── restic_forget ───────────────────────────────────────────────────────


async def test_forget_prune_with_keep_last():
    proc = _make_process(0, stdout="removed 2 snapshots")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        code, out, err = await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_last=7,
        )

    assert code == 0
    assert "--keep-last" in captured["args"]
    assert "7" in [str(a) for a in captured["args"]]


async def test_forget_prune_with_multiple_retention():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_daily=7,
            retain_keep_weekly=4,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-daily" in args_str
    assert "--keep-weekly" in args_str


async def test_forget_does_not_include_prune_flag():
    """`restic forget` must NOT carry --prune — prune is expensive and is now
    a separately-scheduled / manually-triggered operation (gaps.md H1).
    Bundling forget+prune made every backup window unpredictable."""
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_last=5,
        )

    assert "--prune" not in captured["args"]


async def test_forget_prune_timeout():
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    proc.wait = AsyncMock()

    async def fake_wait_for(coro, timeout):
        # The mocked communicate raises TimeoutError synchronously when called,
        # so coro may not be a coroutine here; tolerate both shapes.
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, out, err = await restic_forget(
                REPO,
                PASSWORD,
                timeout_seconds=1,
                retain_keep_last=5,
            )
    assert code != 0
    assert "timed out" in err.lower()


# ── restic_prune ──────────────────────────────────────────────────────────────


async def test_prune_success():
    proc = _make_process(0, stdout="no data was removed")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        code, out, err = await restic_prune(REPO, PASSWORD, timeout_seconds=3600)

    assert code == 0
    assert "prune" in [str(a) for a in captured["args"]]
    assert "--keep-last" not in captured["args"]


# ── restic_check ──────────────────────────────────────────────────────────────


async def test_check_structural_no_read_data():
    proc = _make_process(0, stdout="no errors were found")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        code, out, err = await restic_check(REPO, PASSWORD, "structural", None, 3600)

    assert code == 0
    args_str = " ".join(str(a) for a in captured["args"])
    assert "--read-data" not in args_str
    assert "--read-data-subset" not in args_str


async def test_check_subset_includes_percent():
    proc = _make_process(0, stdout="no errors were found")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_check(REPO, PASSWORD, "subset", 5, 3600)

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--read-data-subset" in args_str
    assert "5%" in args_str


async def test_check_full_includes_read_data():
    proc = _make_process(0, stdout="no errors were found")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_check(REPO, PASSWORD, "full", None, 3600)

    assert "--read-data" in captured["args"]


async def test_check_failure():
    proc = _make_process(1, stderr="Fatal: pack file corrupted")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, out, err = await restic_check(REPO, PASSWORD, "structural", None, 3600)
    assert code != 0


# ── restic_unlock ─────────────────────────────────────────────────────────────


async def test_unlock_success():
    proc = _make_process(0, stdout="successfully removed 1 locks")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, out, err = await restic_unlock(REPO, PASSWORD)
    assert code == 0
    assert "lock" in out.lower()


async def test_unlock_failure():
    proc = _make_process(1, stderr="unable to connect")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, out, err = await restic_unlock(REPO, PASSWORD)
    assert code != 0


async def test_unlock_passes_correct_env():
    proc = _make_process(0, stdout="removed locks")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_unlock(REPO, PASSWORD)

    env = captured["kwargs"].get("env", {})
    assert env.get("RESTIC_REPOSITORY") == REPO
    assert env.get("RESTIC_PASSWORD") == PASSWORD


async def test_unlock_times_out_returns_minus_one_and_terminates_process():
    """`restic unlock` is called in the init-check stale-lock retry path and
    again in the Step 4.5 auto-unlock. A hung backend during unlock would
    block backups indefinitely without this timeout."""

    async def slow_communicate():
        await asyncio.sleep(100)
        return b"", b""

    proc = AsyncMock()
    proc.communicate = slow_communicate
    proc.returncode = None
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
    ):
        code, stdout, stderr = await restic_unlock(REPO, PASSWORD, timeout_seconds=1)

    assert code == -1
    assert stdout == ""
    assert "timed out" in stderr.lower()
    proc.terminate.assert_called()


async def test_unlock_default_timeout_is_60_seconds():
    import inspect

    sig = inspect.signature(restic_unlock)
    assert sig.parameters["timeout_seconds"].default == 60


# ── restic_backup: flag coverage ──────────────────────────────────────────────


async def test_backup_includes_pinned_host_flag():
    """Every backup must run with --host billa-gates so retention does not get
    silently split across container rebuilds (each new container otherwise gets a
    random hostname, and `restic forget --keep-last N` groups by host+paths)."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )

    args_list = list(captured["args"])
    assert "--host" in args_list
    host_idx = args_list.index("--host")
    assert args_list[host_idx + 1] == "billa-gates"


async def test_forget_uses_empty_group_by_and_no_tag_filter():
    """`restic forget` must use --group-by '' (a single group across all paths
    and hosts). The old --group-by paths silently kept old-path snapshots
    forever whenever a job's source_subpath changed (gaps.md C3); --group-by ''
    is what fixes that, and it does so without any tag filter.

    It must NOT filter by tag: retention has to reach every snapshot in the
    repo, including ones written before the current job row existed. A job-id
    filter would leave those unprunable and the repo would grow without bound.
    """
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_last=5,
        )

    args_list = list(captured["args"])
    assert "--group-by" in args_list
    gb_idx = args_list.index("--group-by")
    assert args_list[gb_idx + 1] == ""
    assert "--tag" not in args_list
    assert not any(str(a).startswith("job:") for a in args_list)


async def test_backup_json_flag_included():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
        )

    assert "--json" in captured["args"]


async def test_backup_one_file_system_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            one_file_system=True,
        )

    assert "--one-file-system" in captured["args"]


async def test_backup_one_file_system_flag_absent_when_false():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            one_file_system=False,
        )

    assert "--one-file-system" not in captured["args"]


async def test_backup_no_scan_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            no_scan=True,
        )

    assert "--no-scan" in captured["args"]


async def test_backup_pack_size_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            pack_size=128,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--pack-size" in args_str
    assert "128" in args_str


async def test_backup_read_concurrency_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            read_concurrency=4,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--read-concurrency" in args_str
    assert "4" in args_str


@pytest.mark.parametrize("mode", ["auto", "off", "max", "fastest", "better"])
async def test_backup_compression_flag(mode):
    """Every zstd mode restic 0.19.1 accepts is forwarded verbatim.

    `fastest` and `better` arrived in restic 0.19.0 — verified accepted by the
    0.19.1 binary; the pre-0.19 binary rejects them with "invalid compression
    mode", which is why the image floor matters (see Dockerfile RESTIC_VERSION).
    """
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            compression=mode,
        )

    args = [str(a) for a in captured["args"]]
    assert args[args.index("--compression") + 1] == mode


async def test_backup_exclude_if_present_flag():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            exclude_if_present=[".nobackup", ".ignore"],
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--exclude-if-present" in args_str
    assert ".nobackup" in args_str


# ── restic_forget: retention flag coverage ──────────────────────────────


async def test_forget_prune_keep_within_flag():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_within="7d",
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-within" in args_str
    assert "7d" in args_str


async def test_forget_prune_keep_hourly_flag():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_hourly=24,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-hourly" in args_str
    assert "24" in args_str


async def test_forget_prune_keep_monthly_and_yearly_flags():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_monthly=6,
            retain_keep_yearly=2,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-monthly" in args_str
    assert "--keep-yearly" in args_str
    assert "6" in args_str
    assert "2" in args_str


async def test_forget_prune_keep_within_hourly_flag():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_within_hourly="2d",
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-within-hourly" in args_str
    assert "2d" in args_str


# ── _terminate_then_kill ──────────────────────────────────────────────────────


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

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.wait_for", side_effect=fake_wait_for):
        await _terminate_then_kill(proc, grace_seconds=0.01)

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    # After kill we must wait again, otherwise we'd leak a zombie.
    assert proc.wait.await_count >= 1


async def test_version_timeout_terminates_before_kill():
    """restic_version's timeout path uses _terminate_then_kill (SIGTERM-first)
    for consistency with every other wrapper, even though `restic version`
    holds no lock — keeps the codebase free of raw proc.kill() calls."""
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            result = await restic_version()

    assert result is None
    proc.terminate.assert_called_once()


async def test_backup_timeout_terminates_before_kill():
    """On backup timeout, _terminate_then_kill is used (SIGTERM-first) instead
    of the old proc.kill() so restic can clean up its lock file."""
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, _, stderr, _ = await restic_backup(
                REPO,
                PASSWORD,
                "/sources/documents",
                timeout_seconds=1,
            )

    assert code != 0
    assert "timed out" in stderr.lower()
    proc.terminate.assert_called_once()


async def test_forget_prune_timeout_terminates_before_kill():
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, _, err = await restic_forget(
                REPO,
                PASSWORD,
                timeout_seconds=1,
                retain_keep_last=5,
            )

    assert code != 0
    assert "timed out" in err.lower()
    proc.terminate.assert_called_once()


async def test_prune_timeout_terminates_before_kill():
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, _, err = await restic_prune(REPO, PASSWORD, timeout_seconds=1)

    assert code != 0
    assert "timed out" in err.lower()
    proc.terminate.assert_called_once()


async def test_check_timeout_terminates_before_kill():
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            code, _, err = await restic_check(REPO, PASSWORD, "structural", None, 1)

    assert code != 0
    assert "timed out" in err.lower()
    proc.terminate.assert_called_once()


# ── Launch failures: restic missing, fork failed, not executable ──────────────
#
# Every wrapper catches an exception from create_subprocess_exec and returns
# (-1, "", str(e)) instead of letting OSError escape into the backup pipeline,
# where it would abort run_backup before the run row could be finalized and
# leave the job wedged at status=running (which locks out every future trigger
# via trigger_run's overlap check).
#
# This is not a hypothetical path: the dev container has no restic binary, and
# an image built without the restic-fetcher stage would hit it on every run.


def _launch_failure(exc: Exception):
    return patch("asyncio.create_subprocess_exec", side_effect=exc)


@pytest.mark.parametrize(
    "exc",
    (
        FileNotFoundError("No such file or directory: 'restic'"),
        PermissionError("Permission denied: 'restic'"),
        OSError("fork failed"),
    ),
    ids=("missing_binary", "not_executable", "fork_failed"),
)
async def test_cat_config_launch_failure_returns_minus_one(exc):
    with _launch_failure(exc):
        rc, out, err = await restic_cat_config(REPO, PASSWORD)
    assert rc == -1
    assert out == ""
    assert str(exc) in err


async def test_init_launch_failure_returns_minus_one():
    with _launch_failure(FileNotFoundError("restic not found")):
        rc, out, err = await restic_init(REPO, PASSWORD)
    assert rc == -1
    assert "restic not found" in err


async def test_backup_launch_failure_returns_minus_one_and_no_summary():
    """A failed launch must not look like a successful backup: no summary means
    the stats step writes nothing, and rc=-1 lands in the `failed` branch."""
    with _launch_failure(FileNotFoundError("restic not found")):
        rc, out, err, summary = await restic_backup(REPO, PASSWORD, "/sources/x", 3600)
    assert rc == -1
    assert out == ""
    assert "restic not found" in err
    assert summary is None


@pytest.mark.parametrize(
    "wrapper,args",
    (
        (restic_forget, (REPO, PASSWORD, 60)),
        (restic_prune, (REPO, PASSWORD, 60)),
        (restic_unlock, (REPO, PASSWORD, 60)),
    ),
    ids=("forget", "prune", "unlock"),
)
async def test_wrapper_launch_failure_returns_minus_one(wrapper, args):
    with _launch_failure(OSError("fork failed")):
        rc, out, err = await wrapper(*args)
    assert rc == -1
    assert out == ""
    assert "fork failed" in err


async def test_check_launch_failure_returns_minus_one():
    with _launch_failure(OSError("fork failed")):
        rc, out, err = await restic_check(REPO, PASSWORD, "structural", None, 60)
    assert rc == -1
    assert "fork failed" in err


async def test_version_launch_failure_returns_none():
    """`restic_version` is called at startup to populate AppSettings. A missing
    binary must degrade to an unknown version, never crash the lifespan."""
    with _launch_failure(FileNotFoundError("restic not found")):
        assert await restic_version() is None


# ── restic_version parsing ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "output,expected",
    (
        ("restic 0.19.1 compiled with go1.26.4 on linux/arm64\n", "0.19.1"),
        ("restic 0.18.1 compiled with go1.25.1 on linux/amd64\n", "0.18.1"),
        ("restic 1.0 compiled with go1.30 on linux/arm64\n", "1.0"),
        ("restic 0.19.1-dev compiled with go1.26.4\n", "0.19.1"),
    ),
)
async def test_version_parses_real_version_banners(output, expected):
    """The health endpoint and the run log both surface this string; a parse
    miss shows the user "unknown restic" on a working install."""
    proc = _make_process(0, stdout=output)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await restic_version() == expected


@pytest.mark.parametrize("output", ("", "\n", "garbage with no version", "restic\n"))
async def test_version_returns_none_for_unparseable_output(output):
    proc = _make_process(0, stdout=output)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        assert await restic_version() is None


# ── restic_latest_snapshot_id failure modes ───────────────────────────────────
#
# A wrong answer here is worse than no answer: `--parent` pointing at a bogus id
# fails the whole backup, so every one of these must raise rather than return a
# value the caller would pass to restic.


async def test_latest_snapshot_id_raises_when_restic_cannot_be_launched():
    from app.services.restic import ResticError, restic_latest_snapshot_id

    with _launch_failure(FileNotFoundError("restic not found")):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD)
    assert "failed to launch" in str(exc_info.value)


async def test_latest_snapshot_id_raises_on_timeout_and_terminates():
    """A hung listing must not stall the run indefinitely before the backup has
    even started — the parent lookup runs inside the run pipeline."""
    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = AsyncMock()
    proc.communicate = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = None

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(ResticError) as exc_info:
                await restic_latest_snapshot_id(REPO, PASSWORD, timeout_seconds=1)

    assert "timed out" in str(exc_info.value)
    proc.terminate.assert_called_once()


@pytest.mark.parametrize("payload", ('{"a": 1}', '"str"', "42", "null"))
async def test_latest_snapshot_id_raises_on_non_list_json(payload):
    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(0, stdout=payload)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD)
    assert "non-list" in str(exc_info.value)


@pytest.mark.parametrize("bad_id", (None, 42, [], {}))
async def test_latest_snapshot_id_raises_when_id_is_not_a_string(bad_id):
    """Passing a non-string straight through would render as e.g. `--parent None`
    and fail the backup with a confusing restic error."""
    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(0, stdout=json.dumps([{"id": bad_id}]))
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD)
    assert "string ID" in str(exc_info.value)


async def test_latest_snapshot_id_uses_no_lock_so_it_never_blocks():
    """Read-only lookup: it must not wait on a write lock held by a concurrent
    backup, or on a stale lock file left by a killed one."""
    from app.services.restic import restic_latest_snapshot_id

    proc = _make_process(0, stdout=json.dumps([{"id": "a" * 64}]))
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_latest_snapshot_id(REPO, PASSWORD)

    assert "--no-lock" in captured["args"]


# ── Exit code 130: terminated by signal (new in restic 0.19.0) ────────────────


@pytest.mark.parametrize("wrapper_rc", (130,))
async def test_backup_rc130_returns_no_summary(wrapper_rc):
    """restic 0.19.0 returns 130 on SIGINT *and* SIGTERM (it returned 1 before).
    SIGTERM is what `_terminate_then_kill` sends when a run is canceled, so the
    wrapper must not treat 130 as a completed backup: no summary, so the stats
    step cannot overwrite the run with numbers from a killed process.
    """
    proc = _make_process(wrapper_rc, stdout=BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        rc, _, _, summary = await restic_backup(REPO, PASSWORD, "/sources/x", 3600)

    assert rc == 130
    assert summary is None, "only rc 0 and 3 may yield a summary"


@pytest.mark.parametrize("rc", (1, 2, 10, 11, 12, 130, 137))
async def test_backup_summary_only_returned_for_rc_zero_and_three(rc):
    proc = _make_process(rc, stdout=BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _, _, _, summary = await restic_backup(REPO, PASSWORD, "/sources/x", 3600)
    assert summary is None


@pytest.mark.parametrize("rc", (0, 3))
async def test_backup_summary_returned_for_success_and_partial(rc):
    proc = _make_process(rc, stdout=BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        _, _, _, summary = await restic_backup(REPO, PASSWORD, "/sources/x", 3600)
    assert summary is not None
    assert summary["message_type"] == "summary"


# ── restic_forget flag mapping, exhaustively ──────────────────────────────────

_ALL_RETENTION_FLAGS = {
    "retain_keep_last": ("--keep-last", 7),
    "retain_keep_hourly": ("--keep-hourly", 24),
    "retain_keep_daily": ("--keep-daily", 7),
    "retain_keep_weekly": ("--keep-weekly", 4),
    "retain_keep_monthly": ("--keep-monthly", 12),
    "retain_keep_yearly": ("--keep-yearly", 3),
    "retain_keep_within": ("--keep-within", "30d"),
    "retain_keep_within_hourly": ("--keep-within-hourly", "48h"),
    "retain_keep_within_daily": ("--keep-within-daily", "7d"),
    "retain_keep_within_weekly": ("--keep-within-weekly", "56d"),
    "retain_keep_within_monthly": ("--keep-within-monthly", "6m"),
    "retain_keep_within_yearly": ("--keep-within-yearly", "2y"),
}


@pytest.mark.parametrize(
    "kwarg,flag,value",
    [(k, f, v) for k, (f, v) in _ALL_RETENTION_FLAGS.items()],
)
async def test_forget_maps_every_retention_field_to_its_flag(kwarg, flag, value):
    """All twelve retention fields, one test each. A field silently not reaching
    restic means the policy the user configured is not the policy being applied —
    and `forget` still exits 0, so the run reports success while snapshots pile
    up or vanish."""
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(REPO, PASSWORD, 60, **{kwarg: value})

    args = captured["args"]
    assert flag in args, f"{kwarg} did not become {flag}"
    assert args[args.index(flag) + 1] == str(value)


async def test_forget_omits_flags_whose_value_is_none():
    """An unset retention field must not become `--keep-daily None`, which restic
    rejects — failing forget while the backup itself reports success."""
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            60,
            retain_keep_last=5,
            **{k: None for k in _ALL_RETENTION_FLAGS if k != "retain_keep_last"},
        )

    args = captured["args"]
    assert "--keep-last" in args
    assert "None" not in args
    for flag, _ in _ALL_RETENTION_FLAGS.values():
        if flag != "--keep-last":
            assert flag not in args


async def test_forget_passes_all_flags_together():
    """A fully-populated policy must reach restic intact, not just one flag at a
    time."""
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    kwargs = {k: v for k, (_, v) in _ALL_RETENTION_FLAGS.items()}
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(REPO, PASSWORD, 60, **kwargs)

    args = captured["args"]
    for flag, value in _ALL_RETENTION_FLAGS.values():
        assert args[args.index(flag) + 1] == str(value)


async def test_forget_rc3_is_reported_to_the_caller():
    """restic 0.19.0 returns 3 when forget fails to remove one or more snapshots
    (it returned 0 before). The wrapper passes rc through unchanged so the runner
    can mark retention failed — reporting success there is what let a repo grow
    without bound for months."""
    proc = _make_process(3, stdout="", stderr="unable to remove snapshot")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        rc, _, err = await restic_forget(REPO, PASSWORD, 60, retain_keep_last=1)
    assert rc == 3
    assert "unable to remove" in err


# ── restic_check argument construction ────────────────────────────────────────


async def test_check_subset_without_percent_falls_back_to_structural():
    """`mode=subset` with no percentage must not emit a malformed
    `--read-data-subset=None%`, which restic rejects — turning a verification
    into a hard failure the user reads as repository corruption."""
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_check(REPO, PASSWORD, "subset", None, 60)

    args = captured["args"]
    assert not any("read-data" in a for a in args)
    assert args[:2] == ["restic", "check"]


@pytest.mark.parametrize("percent", (1, 10, 50, 100))
async def test_check_subset_percent_is_formatted_as_restic_expects(percent):
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_check(REPO, PASSWORD, "subset", percent, 60)

    assert f"--read-data-subset={percent}%" in captured["args"]


async def test_check_unknown_mode_adds_no_flags():
    """An unrecognised mode degrades to a structural check rather than building a
    command line restic will refuse."""
    proc = _make_process(0, stdout="")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = [str(a) for a in args]
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_check(REPO, PASSWORD, "not-a-mode", 50, 60)

    assert captured["args"] == ["restic", "check"]


# ── Pure formatters and the bounded collector ─────────────────────────────────
#
# These are what an operator watching a run actually reads, and they are the
# reason `restic_backup` can consume a 210 MB output stream in O(1) memory. They
# take restic's JSON straight from the wire, so every branch has to survive a
# field being absent, a bool where a number belongs, and a line that is not JSON
# at all.

from app.services.restic import (  # noqa: E402
    _MAX_RETAINED_LINE_CHARS,
    _BackupOutputCollector,
    _BoundedOutput,
    _format_bytes,
    _format_eta,
    _format_progress,
    _pump_stream,
)


@pytest.mark.parametrize(
    "num_bytes,expected",
    (
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1024.0 TiB"),  # clamps at the largest unit rather than wrapping
    ),
)
def test_format_bytes_renders_each_unit(num_bytes, expected):
    assert _format_bytes(num_bytes) == expected


@pytest.mark.parametrize("value", (None, "1024", [], {}, True, False))
def test_format_bytes_rejects_non_numbers(value):
    """restic sends JSON; a bool is not a byte count and `isinstance(True, int)`
    is True in Python, so bools must be excluded explicitly."""
    assert _format_bytes(value) is None


@pytest.mark.parametrize(
    "seconds,expected",
    (
        (1, "eta 1s"),
        (59, "eta 59s"),
        (60, "eta 1m"),
        (3599, "eta 59m"),
        (3600, "eta 1h 0m"),
        (7320, "eta 2h 2m"),
        (86400, "eta 24h 0m"),
    ),
)
def test_format_eta_renders_each_magnitude(seconds, expected):
    assert _format_eta(seconds) == expected


@pytest.mark.parametrize("value", (None, 0, -1, "60", True, False, []))
def test_format_eta_omitted_when_unknown_or_invalid(value):
    """restic omits `seconds_remaining` until it has scanned enough to estimate,
    and sends 0 at the end — neither should render as "eta 0s"."""
    assert _format_eta(value) is None


def test_format_progress_on_an_empty_status_line():
    """Better a bare "running" than an empty string the UI renders as a blank
    progress area."""
    assert _format_progress({}) == "progress: running"


def test_format_progress_full_line_orders_parts_for_reading():
    rendered = _format_progress(
        {
            "message_type": "status",
            "percent_done": 0.4567,
            "files_done": 1234,
            "total_files": 5000,
            "bytes_done": 1024**3,
            "total_bytes": 4 * 1024**3,
            "seconds_remaining": 125,
            "error_count": 3,
        }
    )
    assert rendered == (
        "progress: 46% · 1,234/5,000 files · 1.0 GiB/4.0 GiB · eta 2m · 3 errors"
    )


def test_format_progress_thousands_separators_on_large_counts():
    """A seven-digit file count without separators is unreadable at a glance."""
    rendered = _format_progress({"files_done": 1234567, "total_files": 2000000})
    assert "1,234,567/2,000,000 files" in rendered


def test_format_progress_files_done_without_total_when_scan_is_skipped():
    """With `--no-scan` restic never learns the total, so it sends `files_done`
    alone. The line must still render rather than dropping the count."""
    rendered = _format_progress({"files_done": 42, "bytes_done": 2048})
    assert "42 files" in rendered
    assert "/" not in rendered.split("files")[0]
    assert "2.0 KiB" in rendered


def test_format_progress_bytes_done_without_total():
    rendered = _format_progress({"bytes_done": 5 * 1024**2})
    assert "5.0 MiB" in rendered


@pytest.mark.parametrize(
    "error_count,expected",
    ((1, "1 error"), (2, "2 errors"), (1500, "1,500 errors")),
)
def test_format_progress_pluralises_the_error_tally(error_count, expected):
    """`error_count` matters out of proportion to its size: files that fail during
    the scan never enter `total_files`, so a partial backup can otherwise show a
    spotless 100% next to a `warning` badge."""
    rendered = _format_progress({"percent_done": 1.0, "error_count": error_count})
    assert expected in rendered


@pytest.mark.parametrize("error_count", (0, None, False, True, "3"))
def test_format_progress_omits_error_tally_when_absent_or_zero(error_count):
    rendered = _format_progress({"percent_done": 0.5, "error_count": error_count})
    assert "error" not in rendered


@pytest.mark.parametrize(
    "status",
    (
        {"percent_done": True},
        {"files_done": True, "total_files": True},
        {"percent_done": "50%"},
        {"files_done": None},
    ),
)
def test_format_progress_ignores_fields_of_the_wrong_type(status):
    """Never raise on restic's output — a malformed status line must degrade to a
    shorter progress line, not abort the run's progress persistence."""
    assert _format_progress(status).startswith("progress:")


# ── _BoundedOutput ────────────────────────────────────────────────────────────


def test_bounded_output_retains_lines_in_order():
    out = _BoundedOutput(password="")
    for line in ("first", "second", "third"):
        out.add(line)
    assert out.text() == "first\nsecond\nthird"


def test_bounded_output_truncates_a_single_oversized_line():
    """One pathological line (a multi-megabyte filename list, a binary blob on
    stderr) must not consume the whole budget."""
    out = _BoundedOutput(password="")
    out.add("x" * (_MAX_RETAINED_LINE_CHARS * 3))
    text = out.text()
    assert "…<truncated>" in text
    assert len(text) < _MAX_RETAINED_LINE_CHARS * 2


def test_bounded_output_drops_past_the_cap_and_says_so():
    """Silently losing output is worse than a short record — the operator has to
    know the log is incomplete."""
    out = _BoundedOutput(password="", max_chars=100)
    for i in range(50):
        out.add(f"line-{i:03d} " + "y" * 20)
    text = out.text()
    assert "more output line(s) omitted" in text
    assert "capped at 100 characters" in text


def test_bounded_output_force_bypasses_the_cap():
    """The summary arrives last, after any error flood that may have filled the
    cap, and it drives every stats column — it must survive regardless."""
    out = _BoundedOutput(password="", max_chars=50)
    for i in range(20):
        out.add(f"noise-{i}" + "z" * 20)
    out.add("THE-SUMMARY", force=True)
    assert "THE-SUMMARY" in out.text()


def test_bounded_output_scrubs_the_password_per_line():
    """There is no whole-stream string to run a single replace() over any more, so
    scrubbing happens per line — a repo password must never reach the DB row."""
    out = _BoundedOutput(password="s3cr3t")
    out.add(out.scrub("connecting with password s3cr3t to repo"))
    text = out.text()
    assert "s3cr3t" not in text
    assert "connecting with password  to repo" == text


def test_bounded_output_scrub_is_a_noop_for_an_empty_password():
    out = _BoundedOutput(password="")
    assert out.scrub("nothing to remove") == "nothing to remove"


def test_bounded_output_appends_extra_last():
    """The live progress line is appended at render time so it always reads as the
    newest thing in the record."""
    out = _BoundedOutput(password="")
    out.add("earlier")
    assert out.text(extra="progress: 50%") == "earlier\nprogress: 50%"


# ── _BackupOutputCollector ────────────────────────────────────────────────────


def test_collector_never_retains_the_status_firehose():
    collector = _BackupOutputCollector(password="")
    for pct in (0.1, 0.2, 0.3):
        collector.feed(json.dumps({"message_type": "status", "percent_done": pct}))

    text = collector.text()
    assert "message_type" not in text
    assert text.count("progress:") == 1
    assert "30%" in text, "the newest status line wins"


def test_collector_captures_and_retains_the_summary():
    collector = _BackupOutputCollector(password="")
    collector.feed(BACKUP_SUMMARY)

    assert collector.summary is not None
    assert collector.summary["files_new"] == 10
    assert "summary" in collector.text()


def test_collector_retains_error_lines():
    collector = _BackupOutputCollector(password="")
    line = json.dumps(
        {
            "message_type": "error",
            "error": {"message": "permission denied"},
            "item": "/sources/x/secret",
        }
    )
    collector.feed(line)
    assert "/sources/x/secret" in collector.text()


@pytest.mark.parametrize(
    "line",
    (
        "Fatal: unable to open repository",
        "warning: some plain text diagnostic",
        "{not valid json at all",
        '{"unterminated": ',
        "[1, 2, 3]",
        '"just a string"',
    ),
)
def test_collector_retains_non_json_and_malformed_lines(line):
    """restic mixes plain-text diagnostics into its JSON streams, and a truncated
    line can arrive if the process is killed mid-write. Anything unparseable is
    kept verbatim — it is often the only clue about what went wrong."""
    collector = _BackupOutputCollector(password="")
    collector.feed(line)
    assert line.strip() in collector.text()


def test_collector_ignores_blank_lines():
    collector = _BackupOutputCollector(password="")
    for line in ("", "   ", "\r", "\n"):
        collector.feed(line)
    assert collector.text() == ""


def test_collector_strips_carriage_returns():
    """restic's progress output is CR-terminated when it thinks it is on a
    terminal; a stray \\r would render as a control character in the UI."""
    collector = _BackupOutputCollector(password="")
    collector.feed("some line\r")
    assert "\r" not in collector.text()


def test_collector_scrubs_the_password_from_every_classification():
    collector = _BackupOutputCollector(password="s3cr3t")
    collector.feed("Fatal: repo s3cr3t unreachable")
    collector.feed(json.dumps({"message_type": "summary", "note": "s3cr3t"}))
    assert "s3cr3t" not in collector.text()


def test_collector_summary_survives_an_error_flood():
    """The end-to-end version of the force-retention rule: thousands of error
    lines then the summary, which must still be readable in the record."""
    collector = _BackupOutputCollector(password="")
    for i in range(20000):
        collector.feed(
            json.dumps(
                {
                    "message_type": "error",
                    "error": {"message": "permission denied"},
                    "item": f"/sources/x/file-{i}",
                }
            )
        )
    collector.feed(BACKUP_SUMMARY)

    text = collector.text()
    assert collector.summary is not None
    assert "more output line(s) omitted" in text
    assert '"message_type": "summary"' in text or '"message_type":"summary"' in text


# ── _pump_stream ──────────────────────────────────────────────────────────────


async def _collect_lines(data: bytes, chunk_size: int = 65536):
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    await _pump_stream(_FakeStream(data, chunk_size), on_line)
    return lines


async def test_pump_stream_yields_complete_lines():
    assert await _collect_lines(b"a\nb\nc\n") == ["a", "b", "c"]


async def test_pump_stream_flushes_a_final_line_without_a_newline():
    """restic's last line has no trailing newline when the process exits — and
    that last line is the summary, which drives every stats column."""
    assert await _collect_lines(b"first\nsecond-no-newline") == [
        "first",
        "second-no-newline",
    ]


async def test_pump_stream_on_an_empty_stream_yields_nothing():
    assert await _collect_lines(b"") == []


async def test_pump_stream_reassembles_a_line_split_across_reads():
    lines = await _collect_lines(b'{"message_type":"summary"}\n', chunk_size=7)
    assert lines == ['{"message_type":"summary"}']


async def test_pump_stream_does_not_mangle_a_multibyte_char_split_across_reads():
    """A UTF-8 character straddling a chunk boundary must not become replacement
    characters — filenames in error lines are frequently non-ASCII."""
    payload = "/sources/photos/naïve-résumé-日本語.txt\n".encode()
    for chunk_size in range(1, 12):
        lines = await _collect_lines(payload, chunk_size=chunk_size)
        assert lines == ["/sources/photos/naïve-résumé-日本語.txt"], (
            f"mangled at chunk_size={chunk_size}"
        )


async def test_pump_stream_flushes_an_unterminated_line_at_the_retention_limit():
    """A stream with no newlines at all (a binary blob on stderr) must not grow
    the buffer without bound — that unbounded growth is exactly what the
    `communicate()` version did, and it OOM-killed the container.

    The pump's guarantee is about the *buffer*: it flushes once the accumulator
    passes the retention limit, so memory stays O(chunk + limit) no matter how
    long the stream is. Trimming an individual line to a storable length is
    `_BoundedOutput.add`'s job, not the pump's.
    """
    chunk = 1024
    total = _MAX_RETAINED_LINE_CHARS * 3
    lines = await _collect_lines(b"x" * total, chunk_size=chunk)

    assert len(lines) >= 2, "buffer was never flushed — it grew for the whole stream"
    assert sum(len(line) for line in lines) == total, "no data lost across flushes"
    # Each flush happens as soon as the limit is passed, so a line can overshoot
    # by at most one read.
    assert all(len(line) <= _MAX_RETAINED_LINE_CHARS + chunk for line in lines)


async def test_pump_stream_bounded_flush_output_is_still_trimmed_by_the_collector():
    """The two halves of the memory guarantee, together: the pump keeps the
    buffer small, and the collector keeps what it retains small."""
    collector = _BackupOutputCollector(password="")

    async def on_line(line: str) -> None:
        collector.feed(line)

    await _pump_stream(
        _FakeStream(b"x" * (_MAX_RETAINED_LINE_CHARS * 3), 1024), on_line
    )

    for line in collector.text().splitlines():
        assert len(line) <= _MAX_RETAINED_LINE_CHARS + len("…<truncated>")


# ── Process-registry tracking ─────────────────────────────────────────────────


async def test_wrapper_without_run_id_does_not_touch_the_registry():
    """`run_id` is optional so callers outside a run (restic_version at startup,
    repository provisioning at job create) work unchanged. Those must not leave
    an entry in the registry — a stale handle there would let a later cancel
    signal a process that no longer belongs to that run."""
    from app.services import process_registry

    proc = _make_process(0, stdout="{}")
    before = dict(process_registry._processes)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        await restic_cat_config(REPO, PASSWORD)

    assert process_registry._processes == before


async def test_wrapper_with_run_id_registers_and_unregisters():
    """Registered for the lifetime of the call, gone afterwards — the cancel
    endpoint reaches the live process through this map."""
    import uuid as _uuid

    from app.services import process_registry

    run_id = _uuid.uuid4()
    proc = _make_process(0, stdout="{}")
    seen = {}

    async def fake_exec(*args, **kwargs):
        return proc

    original_register = process_registry.register

    def spy_register(rid, p):
        seen["registered"] = (rid, p)
        original_register(rid, p)

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(process_registry, "register", spy_register),
    ):
        await restic_cat_config(REPO, PASSWORD, run_id=run_id)

    assert seen["registered"][0] == run_id
    assert process_registry.get(run_id) is None, "handle must be released"


async def test_registry_is_cleaned_up_even_when_the_wrapper_times_out():
    """The unregister lives in a finally block precisely so a timeout does not
    leak the handle."""
    import uuid as _uuid

    from app.services import process_registry

    run_id = _uuid.uuid4()
    proc = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)
    proc.returncode = 0
    proc.communicate = AsyncMock()

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            await restic_cat_config(REPO, PASSWORD, timeout_seconds=1, run_id=run_id)

    assert process_registry.get(run_id) is None
