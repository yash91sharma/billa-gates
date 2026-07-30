"""How much room is left on each backup destination.

The app knew *where* backups go and nothing about whether there is space to put
one there: an operator found out a drive was full when a run failed, or never
noticed that `restic forget` had been failing and the repository had been
growing for months. This module measures each `/destinations/<label>` and is the
**only** place in the app that calls `shutil.disk_usage` (pinned by
tests/test_destination_usage.py::test_disk_usage_is_measured_in_exactly_one_place)
— a second call site is how a second definition of "% used" appears.

Three properties are load-bearing.

**`free_bytes` comes from the kernel and is never derived as `total - used`.**
`shutil.disk_usage` is `statvfs`: `total = f_blocks * f_frsize`,
`free = f_bavail * f_frsize` (what an unprivileged process may write) and
`used = (f_blocks - f_bfree) * f_frsize`. So `used + free < total` on any
filesystem with root-reserved blocks — 5% by ext4 default — and `total - used`
is `f_bfree`, free space *including* the reserve. On a 4 TB destination that
overstates writable space by ~200 GB, it is the number an operator would size
the next backup against, and it disagrees with the `Avail` column of `df -h`
they will cross-check with, so the correct figure would look like the bug. The
gap is returned as `reserved_bytes` rather than left looking like an arithmetic
mistake, and `percent_used` divides by `total` (never by `used + free`) so the
percentage matches the denominator shown beside it. A consequence the UI has to
respect: a row can read 95% while `free_bytes` is 0, which is why "nearly full"
keys on free bytes, not on the percentage.

That percentage deliberately differs from `df`: `df`'s Use% is
used/(used+avail), which ignores the reserve and therefore runs a few points
higher (measured against this container's overlay: df 59%, this 55.4%). Only the
byte figures are meant to match `df` — `free_bytes` equals its `Avail` exactly.
The page states the difference, because an operator who cross-checks and finds
two percentages stops trusting both.

**A destination fails on its own row, never for the whole page.** Every label is
probed through `fs.run_probe` and all of them are gathered, so N hung SMB shares
cost one timeout rather than N. Note `run_probe`'s `default=` catches only
`asyncio.TimeoutError`: `shutil.disk_usage` raises `OSError` (EIO on a dying
disk, ENOENT on a label removed mid-request), so `_measure_label` contains its
own errors. Leaning on `default=` for them is how one bad drive becomes a 500
for the page whose job is to report on drives.

**The figures are honest about what they describe.** A `/destinations/<label>`
that is not a separate mount — a plain folder, or the empty mountpoint a
detached drive leaves behind — reports the filesystem backing `/destinations`,
i.e. the container itself. The bytes are still returned (they are true facts
about that path, and in the dev container `/destinations` is itself a bind mount
whose children legitimately share a device, so blanking them would empty the
page in the environment the app is developed in); what is corrected is the
*implication* of a dedicated drive, via `is_separate_mount`, `sentinel_present`
and `shares_filesystem_with`. Two labels on one device each report that whole
device, so the last of those lets the UI say "do not add these together".

The sentinel checker is **injected** rather than imported: `backup_runner`
imports this module to invalidate the cache after a run, so importing it back
would be a cycle — and the sentinel path must stay derived in the one place
CLAUDE.md requires (`backup_runner.check_destination_mount_file_exists`).
"""

import asyncio
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from time import monotonic as _monotonic
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.core import fs
from app.core.logging import get_logger, log_call

logger = get_logger(__name__)

# Module-level so tests can patch it, matching app/api/routes/mounts.py.
DESTINATIONS_ROOT: str = "/destinations"

# 300s rather than snapshot_listing's 30: the number only moves when a run
# writes, and a statvfs can spin up a sleeping USB drive. The post-run
# invalidation from each pipeline's `finally` is the trigger that matters; this
# TTL is only a floor on refresh storms. The page's Refresh button bypasses it
# with use_cache=False.
_DEFAULT_TTL_SECONDS: int = 300

SentinelCheck = Callable[[str], bool]


@dataclass(frozen=True)
class Measurement:
    """One destination's capacity at one instant.

    Frozen because the cache hands the same instance to every reader: a mutable
    measurement would let one caller rewrite another's numbers. `measured_at`
    travels with it so a cached row keeps the time it was taken and the page's
    "as of …" can never claim to be fresher than the figure it shows.
    """

    label: str
    path: str
    available: bool
    unavailable_reason: Optional[str]
    total_bytes: Optional[int]
    used_bytes: Optional[int]
    free_bytes: Optional[int]
    reserved_bytes: Optional[int]
    percent_used: Optional[float]
    filesystem_id: Optional[str]
    is_separate_mount: Optional[bool]
    sentinel_present: Optional[bool]
    shares_filesystem_with: Tuple[str, ...]
    measured_at: datetime


# Cache entry: (measurement, expiry_monotonic_seconds), keyed by label.
_cache: Dict[str, Tuple[Measurement, float]] = {}


def _clear_cache() -> None:
    """Drop every cached measurement.

    Used by tests so one test's cache does not leak into the next, and by
    `invalidate()` when called without a label.
    """
    _cache.clear()


@log_call
def invalidate(label: Optional[str] = None) -> None:
    """Forget the cached measurement for `label` (or all of them).

    Called from the `finally` of every run pipeline, which is the whole reason
    the page can be refreshed "after a job completes" without polling. It must
    therefore never raise: an exception there would strand the run row at
    `status=running` and lock the job out of every future trigger. A `dict.pop`
    with a default cannot, and an unknown label is a no-op rather than an error.
    """
    if label is None:
        _clear_cache()
        return
    _cache.pop(label, None)


def _utcnow() -> datetime:
    """Naive UTC, matching app.db.models._utcnow and every other stamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _unavailable(label: str, path: str, reason: str) -> Measurement:
    """A row that could not be measured: no numbers, one reason.

    Every figure is None rather than 0 — a zero here would render as a drive
    with no space, which is a different and much more alarming claim than "this
    drive did not answer".
    """
    return Measurement(
        label=label,
        path=path,
        available=False,
        unavailable_reason=reason,
        total_bytes=None,
        used_bytes=None,
        free_bytes=None,
        reserved_bytes=None,
        percent_used=None,
        filesystem_id=None,
        is_separate_mount=None,
        sentinel_present=None,
        shares_filesystem_with=(),
        measured_at=_utcnow(),
    )


def build_destination_path(label: str) -> str:
    """The directory a destination label names. Mirrors the join in
    `backup_runner.check_destination_mount_file_exists`, whose sentinel probe is
    what proves the mount is live."""
    return os.path.join(DESTINATIONS_ROOT, label)


def _device_id(path: str) -> Optional[str]:
    """`"<major>:<minor>"` of the device behind `path`, or None if unreadable.

    Opaque on purpose: it exists only so two rows can be compared for "same
    filesystem", never to be shown to anyone.
    """
    try:
        st_dev = os.stat(path).st_dev
    except OSError:
        return None
    return f"{os.major(st_dev)}:{os.minor(st_dev)}"


def _measure_label(label: str, sentinel_check: Optional[SentinelCheck]) -> Measurement:
    """Read one destination's capacity. Blocking — always via `fs.run_probe`.

    All three syscalls for a row (`disk_usage`, `stat` for the device, and the
    sentinel `exists`) happen here so one destination is one bounded thread hop
    and one `measured_at` covers the whole row.

    `OSError` is contained rather than raised: `run_probe`'s `default=` covers
    only a hang, and an EIO from one dying disk must not fail the request.
    """
    path = build_destination_path(label)
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        logger.warning("destination usage unreadable label=%s error=%r", label, exc)
        return _unavailable(label, path, str(exc))

    total, used, free = usage.total, usage.used, usage.free

    # Clamped because some network filesystems report used + free > total.
    reserved = max(0, total - used - free)
    percent_used = round(used / total * 100, 1) if total > 0 else None

    filesystem_id = _device_id(path)
    root_id = _device_id(DESTINATIONS_ROOT)
    is_separate_mount = (
        None if filesystem_id is None or root_id is None else filesystem_id != root_id
    )

    sentinel_present: Optional[bool] = None
    if sentinel_check is not None:
        try:
            sentinel_present = bool(sentinel_check(label))
        except OSError as exc:
            # The capacity figures are the point of the row; an unreadable
            # marker file costs the flag, not the measurement.
            logger.warning("sentinel check failed label=%s error=%r", label, exc)

    return Measurement(
        label=label,
        path=path,
        available=True,
        unavailable_reason=None,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        reserved_bytes=reserved,
        percent_used=percent_used,
        filesystem_id=filesystem_id,
        is_separate_mount=is_separate_mount,
        sentinel_present=sentinel_present,
        shares_filesystem_with=(),
        measured_at=_utcnow(),
    )


@log_call
async def measure(
    label: str,
    *,
    sentinel_check: Optional[SentinelCheck] = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    use_cache: bool = True,
) -> Measurement:
    """Measure one destination, serving a cached reading within `ttl_seconds`.

    Unavailable results are deliberately **not** cached — the same rule
    snapshot_listing follows for failed listings. One NAS reboot must not lock
    the page into "unavailable" for the whole TTL window.

    `use_cache=False` forces a fresh probe *and* replaces the cached entry, so
    the page's Refresh button updates what the next reader sees rather than
    handing back a value the cache immediately contradicts.
    """
    now = _monotonic()
    if use_cache:
        cached = _cache.get(label)
        if cached is not None and cached[1] > now:
            return cached[0]

    timeout_reason = (
        f"{build_destination_path(label)} did not respond within "
        f"{fs.FS_PROBE_TIMEOUT_SECONDS}s — the mount may be detached or hung"
    )
    measurement = await fs.run_probe(
        _measure_label,
        label,
        sentinel_check,
        default=_unavailable(label, build_destination_path(label), timeout_reason),
    )

    if measurement.available:
        _cache[label] = (measurement, now + ttl_seconds)

    return measurement


@log_call
async def list_usage(
    labels: Iterable[str],
    *,
    sentinel_check: Optional[SentinelCheck] = None,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    use_cache: bool = True,
) -> List[Measurement]:
    """Measure every label concurrently, sorted by label.

    Gathered rather than awaited in sequence: four destinations behind an
    unreachable NAS would otherwise cost four probe timeouts in one request,
    turning a slow page into one that looks broken.

    `shares_filesystem_with` is filled once every row is in, because it is a
    fact about the *set* — two labels that are folders on one device each report
    that whole device, and an operator adding the rows up would double-count.
    Unavailable rows have no device id and are never grouped with each other.
    """
    ordered = sorted(labels)
    rows: List[Measurement] = list(
        await asyncio.gather(
            *(
                measure(
                    label,
                    sentinel_check=sentinel_check,
                    ttl_seconds=ttl_seconds,
                    use_cache=use_cache,
                )
                for label in ordered
            )
        )
    )

    by_device: Dict[str, List[str]] = {}
    for row in rows:
        if row.filesystem_id is not None:
            by_device.setdefault(row.filesystem_id, []).append(row.label)

    return [
        replace(
            row,
            shares_filesystem_with=tuple(
                sibling
                for sibling in by_device.get(row.filesystem_id or "", [])
                if sibling != row.label
            ),
        )
        for row in rows
    ]


__all__ = [
    "Measurement",
    "build_destination_path",
    "invalidate",
    "list_usage",
    "measure",
]
