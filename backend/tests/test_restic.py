"""Unit tests for restic subprocess wrappers."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_backup_compression_flag():
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
            compression="max",
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--compression" in args_str
    assert "max" in args_str


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
