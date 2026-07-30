"""Tests for app.services.destination_usage — capacity of each backup drive.

These tests carry three things the implementation must not lose:

1. The byte semantics. `shutil.disk_usage` is `statvfs`, where `used + free`
   does not equal `total` (root-reserved blocks), so `free` has to come from
   the kernel and never from `total - used`.
2. Per-row failure. One hung or unreadable destination must cost one row, not
   the whole page — and `fs.run_probe`'s `default=` only catches a timeout, so
   the probe has to contain its own OSErrors.
3. The cache contract, which mirrors snapshot_listing: TTL hits, no caching of
   failures, and an `invalidate` that a pipeline `finally` can call safely.
"""

import ast
import dataclasses
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import destination_usage
from app.services.destination_usage import (
    Measurement,
    _clear_cache,
    invalidate,
    list_usage,
    measure,
)


@pytest.fixture(autouse=True)
def _clear_usage_cache():
    """Each test starts with a fresh cache and ends clean."""
    _clear_cache()
    yield
    _clear_cache()


def _usage(total: int, used: int, free: int):
    """A stand-in for shutil.disk_usage's named tuple."""
    return shutil._ntuple_diskusage(total=total, used=used, free=free)


def _root(tmp_path: Path, *labels: str) -> Path:
    """Create a destinations root holding `labels`."""
    root = tmp_path / "destinations"
    root.mkdir(exist_ok=True)
    for label in labels:
        (root / label).mkdir()
    return root


def _rows_by_label(rows: list[Measurement]) -> dict[str, Measurement]:
    return {m.label: m for m in rows}


def _fake_stat_with_devices(devices: dict[str, int]):
    """An os.stat replacement that reports a chosen st_dev per basename."""
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        base = os.path.basename(os.path.normpath(str(path)))
        if base in devices:
            fields = list(st)
            fields[2] = devices[base]
            return os.stat_result(tuple(fields))
        return st

    return fake_stat


# ── byte semantics ────────────────────────────────────────────────────────────


async def test_bytes_come_from_the_kernel_verbatim(tmp_path):
    """total/used/free are reported exactly as statvfs gave them.

    The three do not add up, and that is the point: `free` is f_bavail (what an
    unprivileged process may write) while `used` counts root-reserved blocks.
    """
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.available is True
    assert m.total_bytes == 1000
    assert m.used_bytes == 600
    assert m.free_bytes == 300
    assert m.free_bytes != m.total_bytes - m.used_bytes, (
        "free must be f_bavail, not total - used — the latter includes "
        "root-reserved blocks and overstates writable space"
    )


async def test_reserved_bytes_is_the_gap_between_total_and_used_plus_free(tmp_path):
    """The gap is surfaced rather than left looking like an arithmetic bug."""
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.reserved_bytes == 100


async def test_reserved_bytes_is_never_negative(tmp_path):
    """Some network filesystems report used + free > total."""
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 700, 400)),
    ):
        m = await measure("main")

    assert m.reserved_bytes == 0


async def test_percent_used_is_used_over_total_not_used_over_used_plus_free(tmp_path):
    """The denominator of the percentage must be the total shown on the row.

    used/(used+free) reads 66.7% here while the row's own numbers say 60%, and a
    percentage an operator cannot derive from the figures beside it reads as a
    bug. Note this is deliberately *not* what `df` prints: `df`'s Use% is
    used/(used+avail), so it runs a few points higher (measured live against this
    container's overlay: df 59%, this 55.4%). That is a real difference in
    definition, not a rounding error, and the page says so — the number here is
    checkable against its own row, which the df-style one would not be.
    """
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.percent_used == 60.0


async def test_percent_used_is_none_when_total_is_zero(tmp_path):
    """An autofs placeholder or empty mountpoint reports a zero-byte total."""
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(0, 0, 0)),
    ):
        m = await measure("main")

    assert m.available is True
    assert m.percent_used is None


async def test_percent_used_can_read_below_100_while_free_is_zero(tmp_path):
    """Reserved blocks mean a full drive does not read 100%.

    Pins the fact the UI's near-full flag keys on free_bytes, never on the
    percentage.
    """
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 950, 0)),
    ):
        m = await measure("main")

    assert m.percent_used == 95.0
    assert m.free_bytes == 0


async def test_percent_used_is_rounded_to_one_decimal(tmp_path):
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(3000, 1000, 1900)),
    ):
        m = await measure("main")

    assert m.percent_used == 33.3


async def test_the_label_and_path_are_reported_on_every_row(tmp_path):
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.label == "main"
    assert m.path == os.path.join(str(root), "main")


# ── mount identity ───────────────────────────────────────────────────────────


async def test_a_label_on_the_same_device_as_the_root_is_not_a_separate_mount(tmp_path):
    """A plain folder under /destinations is not a drive.

    The bytes are still reported — they are true facts about that path — but the
    row says the figures do not describe a dedicated drive.
    """
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.is_separate_mount is False
    assert m.total_bytes == 1000, "the numbers must not be blanked"


async def test_a_label_on_a_different_device_is_a_separate_mount(tmp_path):
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
        patch.object(os, "stat", side_effect=_fake_stat_with_devices({"main": 99})),
    ):
        m = await measure("main")

    assert m.is_separate_mount is True


async def test_labels_sharing_a_device_are_cross_referenced(tmp_path):
    """Two labels on one device each report the whole device.

    Adding the rows up double-counts, so each names the other and the UI can say
    so instead of leaving an operator to sum them.
    """
    root = _root(tmp_path, "main", "spare")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        rows = _rows_by_label(await list_usage(["main", "spare"]))

    assert rows["main"].filesystem_id == rows["spare"].filesystem_id
    assert rows["main"].shares_filesystem_with == ("spare",)
    assert rows["spare"].shares_filesystem_with == ("main",)


async def test_a_label_on_its_own_device_shares_with_nobody(tmp_path):
    root = _root(tmp_path, "main", "offsite")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
        patch.object(
            os,
            "stat",
            side_effect=_fake_stat_with_devices({"main": 91, "offsite": 92}),
        ),
    ):
        rows = await list_usage(["main", "offsite"])

    for m in rows:
        assert m.shares_filesystem_with == ()


async def test_unavailable_rows_are_never_cross_referenced(tmp_path):
    """A row with no filesystem_id must not group with another that has none."""
    root = _root(tmp_path, "main", "offsite")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=OSError(5, "I/O error")),
    ):
        rows = await list_usage(["main", "offsite"])

    for m in rows:
        assert m.available is False
        assert m.shares_filesystem_with == ()


async def test_the_sentinel_is_reported_through_the_injected_checker(tmp_path):
    """The sentinel path stays derived in one place (backup_runner), so the
    checker is injected rather than re-derived here."""
    root = _root(tmp_path, "main")
    seen: list[str] = []

    def checker(label: str) -> bool:
        seen.append(label)
        return True

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main", sentinel_check=checker)

    assert m.sentinel_present is True
    assert seen == ["main"]


async def test_sentinel_is_unknown_when_no_checker_is_injected(tmp_path):
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main")

    assert m.sentinel_present is None


async def test_a_raising_sentinel_checker_does_not_fail_the_row(tmp_path):
    """The capacity figures are the point of the row; an unreadable marker file
    must cost the flag, not the measurement."""
    root = _root(tmp_path, "main")

    def checker(label: str) -> bool:
        raise OSError(13, "Permission denied")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        m = await measure("main", sentinel_check=checker)

    assert m.available is True
    assert m.total_bytes == 1000
    assert m.sentinel_present is None


# ── per-row failure ──────────────────────────────────────────────────────────


async def test_an_oserror_becomes_that_rows_reason_not_an_exception(tmp_path):
    """fs.run_probe's `default=` only catches asyncio.TimeoutError, so the probe
    has to contain its own OSErrors. Without that, one dying disk is a 500 for
    the whole page."""
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(
            shutil, "disk_usage", side_effect=OSError(5, "Input/output error")
        ),
    ):
        m = await measure("main")

    assert m.available is False
    assert m.unavailable_reason is not None
    assert "Input/output error" in m.unavailable_reason
    assert (m.total_bytes, m.used_bytes, m.free_bytes, m.percent_used) == (
        None,
        None,
        None,
        None,
    )
    assert m.filesystem_id is None
    assert m.is_separate_mount is None


async def test_a_missing_label_directory_is_unavailable_not_a_crash(tmp_path):
    root = _root(tmp_path)

    with patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)):
        m = await measure("gone")

    assert m.available is False
    assert m.unavailable_reason is not None


async def test_one_hung_label_does_not_make_the_others_unavailable(tmp_path):
    """A mounted-but-hung SMB share costs its own row and nothing else."""
    root = _root(tmp_path, "main", "nas")

    def slow_or_fast(path, *args, **kwargs):
        if os.path.basename(os.path.normpath(str(path))) == "nas":
            time.sleep(1.0)
        return _usage(1000, 600, 300)

    start = time.monotonic()
    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=slow_or_fast),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        rows = _rows_by_label(await list_usage(["main", "nas"]))
    elapsed = time.monotonic() - start

    assert rows["main"].available is True
    assert rows["main"].total_bytes == 1000
    assert rows["nas"].available is False
    assert rows["nas"].unavailable_reason is not None
    assert rows["nas"].total_bytes is None
    assert elapsed < 0.9, "the probe must return at the timeout, not at the hang"


async def test_two_hung_labels_cost_one_timeout_not_two(tmp_path):
    """Destinations are probed concurrently, so N dead shares cost one wait."""
    root = _root(tmp_path, "nas1", "nas2", "nas3")

    def hung(path, *args, **kwargs):
        time.sleep(1.0)
        return _usage(1000, 600, 300)

    start = time.monotonic()
    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=hung),
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.2),
    ):
        rows = await list_usage(["nas1", "nas2", "nas3"])
    elapsed = time.monotonic() - start

    assert all(m.available is False for m in rows)
    assert elapsed < 0.9, "probes must be gathered, not awaited one after another"


async def test_list_usage_returns_rows_sorted_by_label(tmp_path):
    root = _root(tmp_path, "b", "a", "c")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        rows = await list_usage(["b", "a", "c"])

    assert [m.label for m in rows] == ["a", "b", "c"]


async def test_list_usage_with_no_labels_is_empty(tmp_path):
    with patch.object(destination_usage, "DESTINATIONS_ROOT", str(tmp_path)):
        assert await list_usage([]) == []


# ── cache ────────────────────────────────────────────────────────────────────


def _counting_usage(calls: list[str]):
    def counting(path, *args, **kwargs):
        calls.append(os.path.basename(os.path.normpath(str(path))))
        return _usage(1000, 600, 300)

    return counting


async def test_measurement_is_cached_within_the_ttl(tmp_path):
    root = _root(tmp_path, "main")
    calls: list[str] = []

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=_counting_usage(calls)),
    ):
        await measure("main")
        await measure("main")

    assert calls == ["main"]


async def test_cache_expires_after_the_ttl(tmp_path):
    root = _root(tmp_path, "main")
    calls: list[str] = []

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=_counting_usage(calls)),
    ):
        await measure("main", ttl_seconds=0)
        await measure("main", ttl_seconds=0)

    assert calls == ["main", "main"]


async def test_cache_is_keyed_by_label(tmp_path):
    root = _root(tmp_path, "main", "offsite")
    calls: list[str] = []

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=_counting_usage(calls)),
    ):
        await measure("main")
        await measure("offsite")

    assert calls == ["main", "offsite"]


async def test_use_cache_false_forces_a_fresh_probe(tmp_path):
    """What the page's Refresh button rides on: a button that returns a
    five-minute-old number reads as broken."""
    root = _root(tmp_path, "main")
    calls: list[str] = []

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=_counting_usage(calls)),
    ):
        await measure("main")
        await measure("main", use_cache=False)

    assert calls == ["main", "main"]


async def test_a_fresh_probe_replaces_the_cached_entry(tmp_path):
    """use_cache=False must not leave the stale value behind for the next
    reader — Refresh is expected to update the page, not just this response."""
    root = _root(tmp_path, "main")

    with patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)):
        with patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)):
            await measure("main")
        with patch.object(shutil, "disk_usage", return_value=_usage(1000, 800, 100)):
            await measure("main", use_cache=False)
        with patch.object(shutil, "disk_usage", return_value=_usage(1000, 999, 0)):
            cached = await measure("main")

    assert cached.used_bytes == 800


async def test_unavailable_results_are_not_cached(tmp_path):
    """One NAS reboot must not lock the page into 'unavailable' for the TTL."""
    root = _root(tmp_path, "main")

    with patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)):
        with patch.object(shutil, "disk_usage", side_effect=OSError(5, "I/O error")):
            first = await measure("main")
        with patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)):
            second = await measure("main")

    assert first.available is False
    assert second.available is True
    assert second.total_bytes == 1000


async def test_invalidate_refreshes_one_label_and_leaves_the_others_cached(tmp_path):
    """Scoped on purpose: a global clear would throw away a good measurement of
    a hung share no run touched, making the next page load eat a timeout."""
    root = _root(tmp_path, "main", "offsite")
    calls: list[str] = []

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", side_effect=_counting_usage(calls)),
    ):
        await measure("main")
        await measure("offsite")
        invalidate("main")
        await measure("main")
        await measure("offsite")

    assert calls == ["main", "offsite", "main"]


async def test_invalidate_with_no_label_clears_everything(tmp_path):
    root = _root(tmp_path, "main", "offsite")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        await measure("main")
        await measure("offsite")
        assert destination_usage._cache
        invalidate()

    assert destination_usage._cache == {}


async def test_invalidate_never_raises_for_an_unknown_label():
    """It is called from each pipeline's `finally`, where an exception would
    strand the run row at status=running and lock the job out of every future
    trigger."""
    invalidate("never-measured")
    invalidate(None)


async def test_measured_at_is_the_time_of_measurement_not_of_the_request(tmp_path):
    """A cached row keeps its own timestamp, so the page's 'as of …' can never
    claim to be fresher than the number it is showing."""
    root = _root(tmp_path, "main")

    with (
        patch.object(destination_usage, "DESTINATIONS_ROOT", str(root)),
        patch.object(shutil, "disk_usage", return_value=_usage(1000, 600, 300)),
    ):
        first = await measure("main")
        time.sleep(0.01)
        second = await measure("main")

    assert isinstance(first.measured_at, datetime)
    assert first.measured_at.tzinfo is None, "naive UTC, like every other stamp"
    assert second.measured_at == first.measured_at


# ── house guard ──────────────────────────────────────────────────────────────


def test_disk_usage_is_measured_in_exactly_one_place():
    """Only destination_usage.py may call disk_usage/statvfs.

    A second call site is how a second definition of "% used" appears — and how
    one of them ends up deriving free space as total - used.
    """
    app_dir = Path(__file__).resolve().parent.parent / "app"
    callers: set[str] = set()

    for py in app_dir.rglob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            if name in ("disk_usage", "statvfs"):
                callers.add(str(py.relative_to(app_dir)))

    assert callers == {"services/destination_usage.py"}, (
        "disk usage must be measured only in destination_usage.py, found: "
        f"{sorted(callers)}"
    )


def test_a_measurement_cannot_be_edited_after_it_is_taken():
    """A measurement is a reading at an instant, and the cache hands the same
    instance to every reader — a mutable one would let one caller rewrite
    another's numbers."""
    m = Measurement(
        label="main",
        path="/destinations/main",
        available=True,
        unavailable_reason=None,
        total_bytes=1,
        used_bytes=1,
        free_bytes=0,
        reserved_bytes=0,
        percent_used=100.0,
        filesystem_id="1:2",
        is_separate_mount=True,
        sentinel_present=True,
        shares_filesystem_with=(),
        measured_at=datetime(2026, 7, 29, 12, 0, 0),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.total_bytes = 2  # type: ignore[misc]
