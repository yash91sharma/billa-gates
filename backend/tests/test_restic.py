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

REPO = "/destinations/main/abc123"
PASSWORD = "s3cr3t"
# Constant job_id for tests whose assertions don't depend on the value but
# whose call into restic_backup/restic_forget now requires one.
# Notes: `restic_forget_prune` was renamed to `restic_forget` once `--prune` was
# decoupled from forget (gaps.md H1) — prune now runs on its own schedule.
TEST_JOB_ID = "00000000-0000-4000-8000-000000000000"


def _make_process(returncode: int, stdout: str = "", stderr: str = "") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.stdout = AsyncMock()
    proc.stderr = AsyncMock()

    async def stdout_read(*args, **kwargs):
        return stdout.encode()

    async def stderr_read(*args, **kwargs):
        return stderr.encode()

    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.stdout.read = stdout_read
    proc.stderr.read = stderr_read
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
                job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
        )

    cmd_list = list(captured["args"])
    assert "--tag" in cmd_list


async def test_backup_includes_job_tag():
    """Every backup must carry --tag job:<job_id> so retention can be scoped
    by tag rather than by (host, paths). Without this tag, changing a job's
    source_subpath puts new snapshots in a different `restic forget --group-by
    paths` group and old-path snapshots are kept forever (gaps.md C3)."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    job_id = "11111111-2222-3333-4444-555555555555"
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            job_id=job_id,
        )

    args_list = list(captured["args"])
    # The job tag must appear as a `--tag` flag followed by `job:<id>`.
    indexes = [i for i, a in enumerate(args_list) if a == "--tag"]
    assert any(args_list[i + 1] == f"job:{job_id}" for i in indexes), (
        f"Expected --tag job:{job_id} in args, got {args_list}"
    )


async def test_backup_job_tag_coexists_with_user_tags():
    """User-supplied tags (job.tags) and the system-managed job tag must both
    end up on the snapshot — restic accepts multiple --tag flags."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    job_id = "11111111-2222-3333-4444-555555555555"
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            job_id=job_id,
            tags=["weekly"],
        )

    args_list = list(captured["args"])
    tag_values = [args_list[i + 1] for i, a in enumerate(args_list) if a == "--tag"]
    assert f"job:{job_id}" in tag_values
    assert "weekly" in tag_values


async def test_backup_password_never_in_stdout():
    output = f"some output {PASSWORD} more output"
    proc = _make_process(0, stdout=output + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO,
            PASSWORD,
            "/sources/documents",
            timeout_seconds=3600,
            job_id=TEST_JOB_ID,
        )
    assert PASSWORD not in stdout


# ── restic_latest_snapshot_id ────────────────────────────────────────────────


async def test_latest_snapshot_id_returns_id_when_present():
    """restic snapshots --tag job:X --latest 1 --json returns a single-element
    array; the helper extracts the id so the caller can pass it as --parent."""
    from app.services.restic import restic_latest_snapshot_id

    snap_id = "ffffffff" + "0" * 56
    out = json.dumps([{"id": snap_id, "time": "2026-05-01T00:00:00Z"}])
    proc = _make_process(0, stdout=out)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_latest_snapshot_id(REPO, PASSWORD, job_id=TEST_JOB_ID)
    assert result == snap_id


async def test_latest_snapshot_id_returns_none_on_empty_list():
    """A genuine first run has no prior snapshots; the helper must return None
    so the caller knows to omit --parent (passing --parent on first run would
    make restic fail with 'parent snapshot not found')."""
    from app.services.restic import restic_latest_snapshot_id

    proc = _make_process(0, stdout="[]")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await restic_latest_snapshot_id(REPO, PASSWORD, job_id=TEST_JOB_ID)
    assert result is None


async def test_latest_snapshot_id_raises_on_nonzero_rc():
    import pytest

    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(1, stderr="Fatal: unable to open repo")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD, job_id=TEST_JOB_ID)
    assert "snapshots command failed with exit code 1" in str(exc_info.value)


async def test_latest_snapshot_id_raises_on_malformed_json():
    import pytest

    from app.services.restic import ResticError, restic_latest_snapshot_id

    proc = _make_process(0, stdout="not-json-at-all")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(ResticError) as exc_info:
            await restic_latest_snapshot_id(REPO, PASSWORD, job_id=TEST_JOB_ID)
    assert "malformed JSON" in str(exc_info.value)


async def test_latest_snapshot_id_uses_tag_and_latest_flags():
    """Must scope by --tag job:<id> and --latest 1 so the lookup is O(1) and
    only ever returns this job's snapshots (not snapshots belonging to other
    jobs sharing the destination)."""
    from app.services.restic import restic_latest_snapshot_id

    proc = _make_process(0, stdout="[]")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_latest_snapshot_id(REPO, PASSWORD, job_id=TEST_JOB_ID)

    args_list = list(captured["args"])
    assert "--json" in args_list
    # --tag job:<id>
    tag_indexes = [i for i, a in enumerate(args_list) if a == "--tag"]
    assert any(args_list[i + 1] == f"job:{TEST_JOB_ID}" for i in tag_indexes), (
        f"Expected --tag job:{TEST_JOB_ID} in args, got {args_list}"
    )
    # --latest 1
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
                job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
        )

    args_list = list(captured["args"])
    assert "--host" in args_list
    host_idx = args_list.index("--host")
    assert args_list[host_idx + 1] == "billa-gates"


async def test_forget_prune_scopes_by_job_tag_with_empty_group_by():
    """`restic forget` must scope retention by --tag job:<job_id> with
    --group-by '' (single group across all paths). The old --group-by paths
    silently kept old-path snapshots forever whenever a job's source_subpath
    changed (gaps.md C3)."""
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            job_id=job_id,
            retain_keep_last=5,
        )

    args_list = list(captured["args"])
    # --group-by must be present and be the empty string (one logical group).
    assert "--group-by" in args_list
    gb_idx = args_list.index("--group-by")
    assert args_list[gb_idx + 1] == ""
    # --tag must scope to this job_id.
    tag_indexes = [i for i, a in enumerate(args_list) if a == "--tag"]
    assert any(args_list[i + 1] == f"job:{job_id}" for i in tag_indexes), (
        f"Expected --tag job:{job_id} in forget args, got {args_list}"
    )


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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
            job_id=TEST_JOB_ID,
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
                job_id=TEST_JOB_ID,
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
                job_id=TEST_JOB_ID,
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
