"""Unit tests for restic subprocess wrappers."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.restic import (
    _terminate_then_kill,
    restic_backup,
    restic_cat_config,
    restic_check,
    restic_forget_prune,
    restic_init,
    restic_prune,
    restic_snapshots,
    restic_unlock,
    restic_version,
)

REPO = "/destinations/main/abc123"
PASSWORD = "s3cr3t"


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
    proc.kill = MagicMock()

    async def fake_wait_for(coro, timeout):
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
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )
    assert code == 0
    assert summary is not None
    assert summary["files_new"] == 10


async def test_backup_failure_nonzero():
    proc = _make_process(1, stderr="Fatal: unable to open source")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
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
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
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
                REPO, PASSWORD, "/sources/documents", timeout_seconds=1
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
            REPO, PASSWORD, "/sources/documents/photos", timeout_seconds=3600
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


async def test_backup_password_never_in_stdout():
    output = f"some output {PASSWORD} more output"
    proc = _make_process(0, stdout=output + "\n" + BACKUP_SUMMARY)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, stdout, stderr, summary = await restic_backup(
            REPO, PASSWORD, "/sources/documents", timeout_seconds=3600
        )
    assert PASSWORD not in stdout


# ── restic_snapshots ──────────────────────────────────────────────────────────

SNAPSHOTS_JSON = json.dumps(
    [
        {
            "id": "abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
            "time": "2024-01-01T12:00:00Z",
            "hostname": "myhost",
            "paths": ["/sources/documents"],
            "tags": ["weekly"],
        }
    ]
)


async def test_snapshots_success():
    proc = _make_process(0, stdout=SNAPSHOTS_JSON)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, snapshots, stderr = await restic_snapshots(REPO, PASSWORD)
    assert code == 0
    assert len(snapshots) == 1
    assert snapshots[0]["hostname"] == "myhost"


async def test_snapshots_failure():
    proc = _make_process(1, stderr="unable to open repo")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        code, snapshots, stderr = await restic_snapshots(REPO, PASSWORD)
    assert code != 0
    assert snapshots == []


# ── restic_forget_prune ───────────────────────────────────────────────────────


async def test_forget_prune_with_keep_last():
    proc = _make_process(0, stdout="removed 2 snapshots")
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        code, out, err = await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_last=7
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
        await restic_forget_prune(
            REPO,
            PASSWORD,
            timeout_seconds=3600,
            retain_keep_daily=7,
            retain_keep_weekly=4,
        )

    args_str = " ".join(str(a) for a in captured["args"])
    assert "--keep-daily" in args_str
    assert "--keep-weekly" in args_str


async def test_forget_prune_includes_prune_flag():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_last=5
        )

    assert "--prune" in captured["args"]


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
            code, out, err = await restic_forget_prune(
                REPO, PASSWORD, timeout_seconds=1, retain_keep_last=5
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


# ── restic_backup: flag coverage ──────────────────────────────────────────────


async def test_backup_includes_pinned_host_flag():
    """Every backup must run with --host backup-server so retention does not get
    silently split across container rebuilds (each new container otherwise gets a
    random hostname, and `restic forget --keep-last N` groups by host+paths)."""
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(REPO, PASSWORD, "/sources/documents", timeout_seconds=3600)

    args_list = list(captured["args"])
    assert "--host" in args_list
    host_idx = args_list.index("--host")
    assert args_list[host_idx + 1] == "backup-server"


async def test_forget_prune_includes_group_by_paths():
    """`restic forget` must use --group-by paths so retention applies regardless
    of which container hostname wrote the snapshot."""
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_last=5
        )

    args_list = list(captured["args"])
    assert "--group-by" in args_list
    gb_idx = args_list.index("--group-by")
    assert args_list[gb_idx + 1] == "paths"


async def test_backup_json_flag_included():
    proc = _make_process(0, stdout=BACKUP_SUMMARY)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_backup(REPO, PASSWORD, "/sources/documents", timeout_seconds=3600)

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


# ── restic_forget_prune: retention flag coverage ──────────────────────────────


async def test_forget_prune_keep_within_flag():
    proc = _make_process(0)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_within="7d"
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
        await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_hourly=24
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
        await restic_forget_prune(
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
        await restic_forget_prune(
            REPO, PASSWORD, timeout_seconds=3600, retain_keep_within_hourly="2d"
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
                REPO, PASSWORD, "/sources/documents", timeout_seconds=1
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
            code, _, err = await restic_forget_prune(
                REPO, PASSWORD, timeout_seconds=1, retain_keep_last=5
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
