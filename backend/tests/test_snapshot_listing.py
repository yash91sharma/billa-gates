"""Tests for the on-demand snapshot listing service.

The snapshot listing service replaces the old `Snapshot` ORM table: the restic
repository is the single source of truth, and the UI queries restic on demand
through this service. A small TTL cache absorbs dashboard refresh storms.

See gaps.md C4-Alt for the architectural motivation.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import snapshot_listing
from app.services.snapshot_listing import (
    SnapshotListingError,
    _clear_cache,
    list_snapshots,
)

REPO = "/destinations/main/job-uuid-123"
PASSWORD = "s3cr3t"
JOB_ID = "11111111-2222-3333-4444-555555555555"


def _make_process(returncode: int, stdout: str = "", stderr: str = "") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    return proc


_SIZE_FIRST = 1024 * 1024 * 500
_SIZE_SECOND = 1024 * 1024 * 510

# Shaped after real `restic snapshots --json` output — see
# tests/fixtures/restic_0_19_1/snapshots.json, which this is checked against by
# test_restic_contract.py. Two things here are load-bearing and were wrong
# before: the size lives in the `summary` sub-object (there is no top-level
# `total_size`; inventing one is how `size_bytes` came to be null in production
# while these tests passed), and snapshots carry no `job:<uuid>` tag — the repo
# is the scope, so per-job tagging does not exist (CLAUDE.md).
#
# The second record deliberately has no `tags` key at all: restic omits it
# rather than sending null, and it carries a `parent`, as every non-initial
# snapshot does.
_RESTIC_SNAPSHOTS_JSON = json.dumps(
    [
        {
            "id": "a" * 64,
            "short_id": "a" * 8,
            "time": "2026-05-01T12:00:00Z",
            "tree": "c" * 64,
            "hostname": "billa-gates",
            "username": "root",
            "paths": ["/sources/documents"],
            "tags": ["weekly", "important"],
            "program_version": "restic 0.19.1",
            "summary": {
                "backup_start": "2026-05-01T12:00:00Z",
                "backup_end": "2026-05-01T12:04:00Z",
                "files_new": 10,
                "files_changed": 0,
                "files_unmodified": 0,
                "dirs_new": 3,
                "dirs_changed": 0,
                "dirs_unmodified": 0,
                "data_added": _SIZE_FIRST,
                "data_added_packed": _SIZE_FIRST - 4096,
                "total_files_processed": 10,
                "total_bytes_processed": _SIZE_FIRST,
            },
        },
        {
            "id": "b" * 64,
            "short_id": "b" * 8,
            "time": "2026-05-02T12:00:00Z",
            "parent": "a" * 64,
            "tree": "d" * 64,
            "hostname": "billa-gates",
            "username": "root",
            "paths": ["/sources/documents"],
            "program_version": "restic 0.19.1",
            "summary": {
                "backup_start": "2026-05-02T12:00:00Z",
                "backup_end": "2026-05-02T12:01:00Z",
                "files_new": 1,
                "files_changed": 0,
                "files_unmodified": 10,
                "dirs_new": 0,
                "dirs_changed": 1,
                "dirs_unmodified": 2,
                "data_added": 4096,
                "data_added_packed": 4000,
                "total_files_processed": 11,
                "total_bytes_processed": _SIZE_SECOND,
            },
        },
    ]
)


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """Each test starts with a fresh cache and ends clean."""
    _clear_cache()
    yield
    _clear_cache()


# ── args ──────────────────────────────────────────────────────────────────────


async def test_list_snapshots_passes_no_lock_and_json_flags():
    """Every call must include `--json` and `--no-lock`. --no-lock matters
    because the listing is read-only and must not be blocked by a concurrent
    backup or a stale lock file (gaps.md C4-Alt)."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await list_snapshots(REPO, PASSWORD, use_cache=False)

    args_list = list(captured["args"])
    assert "snapshots" in args_list
    assert "--json" in args_list
    assert "--no-lock" in args_list


async def test_list_snapshots_applies_no_tag_filter():
    """The repo belongs to one job, so the listing must not filter by tag.

    Filtering would hide every snapshot taken before the current job row
    existed — which is exactly the history a recreated job is meant to adopt.
    """
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert "--tag" not in list(captured["args"])


async def test_list_snapshots_passes_env_vars():
    proc = _make_process(0, stdout="[]")
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await list_snapshots(REPO, PASSWORD, use_cache=False)

    env = captured["kwargs"].get("env", {})
    assert env.get("RESTIC_REPOSITORY") == REPO
    assert env.get("RESTIC_PASSWORD") == PASSWORD


# ── parsing ───────────────────────────────────────────────────────────────────


async def test_list_snapshots_returns_normalized_dicts():
    """The service must translate restic's raw JSON keys (`id`, `time`,
    `summary.total_bytes_processed`) into the stable response shape the API
    exposes (`snapshot_id`, `snapshot_time`, `size_bytes`) so changes to restic's
    schema do not leak into the API contract."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert len(result) == 2
    first = result[0]
    assert first["snapshot_id"] == "a" * 64
    assert first["snapshot_time"] == "2026-05-01T12:00:00Z"
    assert first["hostname"] == "billa-gates"
    assert first["paths"] == ["/sources/documents"]
    assert first["tags"] == ["weekly", "important"]
    assert first["size_bytes"] == _SIZE_FIRST
    # Internal restic keys must not leak through.
    for leaked in ("id", "time", "summary", "tree", "username", "program_version"):
        assert leaked not in first, (
            f"restic-internal key {leaked!r} leaked into the API"
        )


async def test_list_snapshots_exposes_exactly_the_documented_response_keys():
    """The response shape is the API contract (SnapshotResponse). Pin it, so a
    field added to restic's output cannot quietly become part of it."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert set(result[0]) == {
        "snapshot_id",
        "snapshot_time",
        "hostname",
        "paths",
        "tags",
        "size_bytes",
    }


async def test_list_snapshots_reads_size_from_the_summary_sub_object():
    """Regression guard for a bug that shipped: `size_bytes` was read from a
    top-level `total_size` that restic has never emitted, so the UI's Size
    column was blank for every snapshot while the tests passed against a fixture
    that invented the key. See tests/test_restic_contract.py."""
    raw = json.dumps(
        [
            {
                "id": "e" * 64,
                "time": "2026-05-03T12:00:00Z",
                "hostname": "billa-gates",
                "paths": ["/sources/documents"],
                # A top-level total_size must be ignored even if something one
                # day emits it — the summary is the source of truth.
                "total_size": 999,
                "summary": {"total_bytes_processed": 4242},
            }
        ]
    )
    proc = _make_process(0, stdout=raw)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert result[0]["size_bytes"] == 4242


async def test_list_snapshots_handles_empty_array():
    """A repo that legitimately has no snapshots for this job (genuine first
    run or all snapshots forgotten by retention) returns an empty list, NOT
    an error."""
    proc = _make_process(0, stdout="[]")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)
    assert result == []


async def test_list_snapshots_handles_missing_optional_fields():
    """restic omits `tags` entirely when there are none, and snapshots written
    before restic 0.17 have no `summary` block at all — a repo adopted from an
    older install can hold both. Unknown size, not a crash."""
    minimal = json.dumps(
        [
            {
                "id": "c" * 64,
                "time": "2026-05-01T12:00:00Z",
                "hostname": "h",
                "paths": ["/sources/x"],
            }
        ]
    )
    proc = _make_process(0, stdout=minimal)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)
    assert len(result) == 1
    assert result[0]["tags"] is None
    assert result[0]["size_bytes"] is None


@pytest.mark.parametrize("summary", (None, "not-a-dict", 42, [], {}))
async def test_list_snapshots_survives_a_malformed_summary_block(summary):
    """`summary` is restic's, not ours. A non-dict value must yield an unknown
    size rather than an AttributeError that turns the whole listing into a 503
    and tells the user their destination is unmounted."""
    raw = json.dumps(
        [
            {
                "id": "f" * 64,
                "time": "2026-05-01T12:00:00Z",
                "hostname": "h",
                "paths": ["/sources/x"],
                "summary": summary,
            }
        ]
    )
    proc = _make_process(0, stdout=raw)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)
    assert result[0]["size_bytes"] is None


async def test_list_snapshots_preserves_restic_ordering():
    """restic returns snapshots oldest-first and the UI relies on that ordering;
    the service must not sort or reverse them."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        result = await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert [s["snapshot_id"] for s in result] == ["a" * 64, "b" * 64]


# ── failure modes ─────────────────────────────────────────────────────────────


async def test_list_snapshots_raises_on_nonzero_rc():
    """A non-zero exit code surfaces as a typed SnapshotListingError so the
    route layer can map it to an HTTP 500 / 503 instead of silently returning
    an empty list (which is what the old reconcile-then-wipe path did — see
    gaps.md C4)."""
    proc = _make_process(1, stderr="Fatal: unable to open repo")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(SnapshotListingError):
            await list_snapshots(REPO, PASSWORD, use_cache=False)


async def test_list_snapshots_raises_on_malformed_json():
    """rc=0 with stdout that is not a valid JSON array is treated as a failure
    (the prior `restic_snapshots` swallowed JSONDecodeError and returned an
    empty list, which caused the reconcile-step DB wipe in C4)."""
    proc = _make_process(0, stdout="not-json-at-all")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(SnapshotListingError):
            await list_snapshots(REPO, PASSWORD, use_cache=False)


@pytest.mark.parametrize("payload", ('{"snapshots": []}', '"a string"', "42", "null"))
async def test_list_snapshots_raises_on_json_that_is_not_a_list(payload):
    """rc=0 with well-formed JSON that is not an array is still a failure. A bare
    `{}` would otherwise be iterated as its keys and produce garbage snapshot
    records, or an empty list — which is the C4 failure mode of telling the user
    their backups are gone."""
    proc = _make_process(0, stdout=payload)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(SnapshotListingError) as exc_info:
            await list_snapshots(REPO, PASSWORD, use_cache=False)
    assert "non-list" in str(exc_info.value)


async def test_list_snapshots_raises_when_restic_cannot_be_launched():
    """No restic on PATH, fork failure, or a bad executable bit. This must be a
    typed error, not a bare OSError escaping into the route — and never an empty
    list, which the UI would render as "no snapshots yet"."""
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("No such file or directory: 'restic'"),
    ):
        with pytest.raises(SnapshotListingError) as exc_info:
            await list_snapshots(REPO, PASSWORD, use_cache=False)
    assert "failed to launch restic" in str(exc_info.value)


async def test_launch_failure_is_not_cached():
    """A transient launch failure must not lock the UI into an error for the full
    TTL window — the next request has to retry."""
    calls = {"n": 0}
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("fork failed")
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=flaky):
        with pytest.raises(SnapshotListingError):
            await list_snapshots(REPO, PASSWORD)
        result = await list_snapshots(REPO, PASSWORD)

    assert calls["n"] == 2
    assert len(result) == 2


async def test_list_snapshots_times_out():
    """A hung backend (NFS unresponsive, restic stuck on a network call) must
    be killed by the timeout — without it, the UI request hangs forever and
    we replay gaps.md H6 for the new code path."""
    proc = AsyncMock()
    proc.communicate = AsyncMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    proc.returncode = None

    async def fake_wait_for(coro, timeout):
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError()

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with patch("asyncio.wait_for", side_effect=fake_wait_for):
            with pytest.raises(SnapshotListingError) as exc_info:
                await list_snapshots(REPO, PASSWORD, timeout_seconds=1, use_cache=False)

    assert "timed out" in str(exc_info.value).lower()
    proc.terminate.assert_called_once()


# ── TTL cache ─────────────────────────────────────────────────────────────────


async def test_list_snapshots_cache_hit_within_ttl_skips_restic():
    """A second call within the TTL window must not shell out to restic.
    This is what makes the architecture viable — a dashboard refresh hitting
    N jobs in quick succession invokes restic N times, not N×refresh-rate."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        first = await list_snapshots(REPO, PASSWORD, ttl_seconds=30)
        second = await list_snapshots(REPO, PASSWORD, ttl_seconds=30)

    assert call_count["n"] == 1, "second call within TTL must use cache"
    assert first == second


async def test_list_snapshots_cache_miss_after_ttl_expires():
    """After the TTL elapses, the cache entry is stale and the next call must
    re-invoke restic so users see fresh data."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        return proc

    # Freeze time, then advance past the TTL.
    fake_now = {"t": 1000.0}

    def fake_monotonic():
        return fake_now["t"]

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(snapshot_listing, "_monotonic", fake_monotonic),
    ):
        await list_snapshots(REPO, PASSWORD, ttl_seconds=30)
        fake_now["t"] += 31  # 1s past TTL
        await list_snapshots(REPO, PASSWORD, ttl_seconds=30)

    assert call_count["n"] == 2, "cache must miss after TTL expires"


async def test_list_snapshots_cache_keyed_by_repo_path():
    """Two different repos must have independent cache entries; a hit on repo
    A must not return repo B's snapshots."""
    snaps_a = json.dumps(
        [
            {
                "id": "a" * 64,
                "time": "2026-05-01T00:00:00Z",
                "hostname": "h",
                "paths": ["/x"],
            }
        ]
    )
    snaps_b = json.dumps(
        [
            {
                "id": "b" * 64,
                "time": "2026-05-02T00:00:00Z",
                "hostname": "h",
                "paths": ["/y"],
            }
        ]
    )

    call_order: list[str] = []

    async def fake_exec(*args, **kwargs):
        repo = kwargs.get("env", {}).get("RESTIC_REPOSITORY")
        call_order.append(repo)
        out = snaps_a if repo == "/destinations/A" else snaps_b
        return _make_process(0, stdout=out)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        res_a = await list_snapshots("/destinations/A", PASSWORD)
        res_b = await list_snapshots("/destinations/B", PASSWORD)

    assert res_a[0]["snapshot_id"] == "a" * 64
    assert res_b[0]["snapshot_id"] == "b" * 64
    assert call_order == ["/destinations/A", "/destinations/B"]


async def test_clear_cache_forces_next_call_to_restic():
    """_clear_cache() exists so the test suite (and any future explicit
    invalidation point such as 'just completed a backup') can force a fresh
    read. Without it, mocked tests would interfere with each other through
    the module-level cache."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await list_snapshots(REPO, PASSWORD, ttl_seconds=30)
        _clear_cache()
        await list_snapshots(REPO, PASSWORD, ttl_seconds=30)

    assert call_count["n"] == 2


async def test_list_snapshots_use_cache_false_bypasses_cache():
    """`use_cache=False` (the test default) must skip both lookup and store —
    otherwise the rest of the test file would pollute each other's caches
    even with `_clear_cache()` in autouse fixture."""
    proc = _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await list_snapshots(REPO, PASSWORD, use_cache=False)
        await list_snapshots(REPO, PASSWORD, use_cache=False)

    assert call_count["n"] == 2


async def test_failed_calls_are_not_cached():
    """A SnapshotListingError must not be sticky in the cache — otherwise a
    transient backend hiccup would make the UI show 'no snapshots' for the
    full TTL window even after restic recovered."""
    call_count = {"n": 0}

    async def fake_exec(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _make_process(1, stderr="transient")
        return _make_process(0, stdout=_RESTIC_SNAPSHOTS_JSON)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        with pytest.raises(SnapshotListingError):
            await list_snapshots(REPO, PASSWORD, ttl_seconds=30)
        # Immediate retry — must hit restic again, not return cached failure.
        result = await list_snapshots(REPO, PASSWORD, ttl_seconds=30)

    assert call_count["n"] == 2
    assert len(result) == 2
