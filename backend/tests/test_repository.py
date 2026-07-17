"""Tests for app.services.repository — repo layout, provisioning, removal."""

import os
import shutil
from unittest.mock import AsyncMock, patch

import pytest

from app.services import repository
from app.services.repository import (
    RepoOutcome,
    RepoProbe,
    RepositoryError,
    build_repo_path,
    ensure_repository,
    probe_repository,
    remove_repository,
)

REPO = "/destinations/main/photos"
PASSWORD = "secret123"


# ── build_repo_path ──────────────────────────────────────────────────────────


def test_build_repo_path_uses_destination_label_and_name():
    assert build_repo_path("main", "photos") == "/destinations/main/photos"


def test_build_repo_path_is_keyed_on_name_not_uuid():
    """The whole point of the refactor: the path a user can reconstruct."""
    first = build_repo_path("main", "photos")
    second = build_repo_path("main", "photos")
    assert first == second == "/destinations/main/photos"


def test_build_repo_path_honors_patched_root():
    with patch("app.services.repository.DESTINATIONS_ROOT", "/tmp/dest"):
        assert build_repo_path("main", "photos") == "/tmp/dest/main/photos"


# ── probe_repository ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rc,expected",
    [
        (0, RepoProbe.ok),
        (repository.RESTIC_RC_WRONG_PASSWORD, RepoProbe.wrong_password),
        (repository.RESTIC_RC_REPO_NOT_FOUND, RepoProbe.not_a_repo),
        (repository.RESTIC_RC_LOCK_FAILED, RepoProbe.unreachable),
        (1, RepoProbe.unreachable),
        (-1, RepoProbe.unreachable),
    ],
)
async def test_probe_repository_maps_restic_exit_codes(rc, expected):
    with patch(
        "app.services.restic.restic_cat_config",
        new=AsyncMock(return_value=(rc, "", "stderr text")),
    ):
        probe, _ = await probe_repository(REPO, PASSWORD)
    assert probe is expected


async def test_probe_repository_returns_stderr_detail():
    with patch(
        "app.services.restic.restic_cat_config",
        new=AsyncMock(return_value=(12, "", "wrong password")),
    ):
        _, detail = await probe_repository(REPO, PASSWORD)
    assert "wrong password" in detail


# ── ensure_repository ────────────────────────────────────────────────────────


async def test_ensure_repository_adopts_existing_repo_without_init():
    """The recovery CUJ: an existing repo is adopted, never re-initialized."""
    init = AsyncMock()
    with (
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(0, "", "")),
        ),
        patch("app.services.restic.restic_init", new=init),
    ):
        outcome, _ = await ensure_repository(REPO, PASSWORD)

    assert outcome is RepoOutcome.adopted
    init.assert_not_awaited()


async def test_ensure_repository_initializes_when_absent():
    with (
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(repository.RESTIC_RC_REPO_NOT_FOUND, "", "")),
        ),
        patch(
            "app.services.restic.restic_init",
            new=AsyncMock(return_value=(0, "", "")),
        ),
    ):
        outcome, _ = await ensure_repository(REPO, PASSWORD)

    assert outcome is RepoOutcome.initialized


async def test_ensure_repository_reports_init_failure():
    with (
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(repository.RESTIC_RC_REPO_NOT_FOUND, "", "")),
        ),
        patch(
            "app.services.restic.restic_init",
            new=AsyncMock(return_value=(1, "", "permission denied")),
        ),
    ):
        outcome, detail = await ensure_repository(REPO, PASSWORD)

    assert outcome is RepoOutcome.init_failed
    assert "permission denied" in detail


async def test_ensure_repository_never_inits_over_wrong_password():
    """A repo with a different password must not be touched."""
    init = AsyncMock()
    with (
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(repository.RESTIC_RC_WRONG_PASSWORD, "", "")),
        ),
        patch("app.services.restic.restic_init", new=init),
    ):
        outcome, _ = await ensure_repository(REPO, PASSWORD)

    assert outcome is RepoOutcome.wrong_password
    init.assert_not_awaited()


async def test_ensure_repository_never_inits_when_unreachable():
    """A transient backend failure must not be mistaken for a missing repo."""
    init = AsyncMock()
    with (
        patch(
            "app.services.restic.restic_cat_config",
            new=AsyncMock(return_value=(-1, "", "cat config timed out")),
        ),
        patch("app.services.restic.restic_init", new=init),
    ):
        outcome, _ = await ensure_repository(REPO, PASSWORD)

    assert outcome is RepoOutcome.unreachable
    init.assert_not_awaited()


# ── remove_repository ────────────────────────────────────────────────────────


def _make_repo(tmp_path, label="main", name="photos"):
    repo = tmp_path / label / name
    repo.mkdir(parents=True)
    (repo / "config").write_text("restic config")
    (repo / "data").mkdir()
    return repo


async def test_remove_repository_deletes_a_real_repo(tmp_path):
    repo = _make_repo(tmp_path)
    with patch("app.services.repository.DESTINATIONS_ROOT", str(tmp_path)):
        removed = await remove_repository(str(repo))

    assert removed is True
    assert not repo.exists()


async def test_remove_repository_is_noop_when_absent(tmp_path):
    missing = tmp_path / "main" / "gone"
    with patch("app.services.repository.DESTINATIONS_ROOT", str(tmp_path)):
        removed = await remove_repository(str(missing))

    assert removed is False


async def test_remove_repository_refuses_directory_without_config(tmp_path):
    """A user folder that isn't a restic repo must never be rmtree'd."""
    plain = tmp_path / "main" / "family-photos"
    plain.mkdir(parents=True)
    (plain / "holiday.jpg").write_text("not a repo")

    with patch("app.services.repository.DESTINATIONS_ROOT", str(tmp_path)):
        with pytest.raises(RepositoryError, match="not a restic repository"):
            await remove_repository(str(plain))

    assert (plain / "holiday.jpg").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "/destinations",  # the root itself
        "/destinations/main",  # a whole mounted drive
        "/destinations/main/photos/sub",  # too deep
        "/destinations/main/../../etc",  # traversal
        "/etc/passwd",  # outside the root entirely
    ],
)
async def test_remove_repository_rejects_unsafe_paths(bad):
    with pytest.raises(RepositoryError, match="refusing"):
        await remove_repository(bad)


async def test_remove_repository_rejects_mount_root_before_touching_disk(tmp_path):
    """The guard must run before any filesystem call."""
    label_dir = tmp_path / "main"
    label_dir.mkdir()
    (label_dir / "keepme.txt").write_text("precious")

    with patch("app.services.repository.DESTINATIONS_ROOT", str(tmp_path)):
        with pytest.raises(RepositoryError):
            await remove_repository(str(label_dir))

    assert (label_dir / "keepme.txt").exists()


async def test_remove_repository_uses_a_worker_thread(tmp_path):
    """rmtree is blocking IO and must not run on the event loop.

    Note `asyncio.to_thread` is shared with fs.run_probe's isdir/isfile
    probes, so assert on the specific rmtree call rather than the count.
    """
    repo = _make_repo(tmp_path)
    with (
        patch("app.services.repository.DESTINATIONS_ROOT", str(tmp_path)),
        patch(
            "app.services.repository.asyncio.to_thread", new=AsyncMock()
        ) as to_thread,
    ):
        await remove_repository(str(repo))

    offloaded = [call.args for call in to_thread.await_args_list]
    assert (shutil.rmtree, os.path.normpath(str(repo))) in offloaded
