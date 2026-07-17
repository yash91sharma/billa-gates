"""Restic repository location, provisioning, and removal.

Owns the on-disk layout and the repo lifecycle operations the API layer needs
at job-create and job-delete time. This is the single source of truth for
where a job's repository lives; nothing else may build the path.

The layout is ``/destinations/<destination_label>/<name>``:

* ``destination_label`` selects *which physical drive* — ``/destinations`` is
  not a disk, it is a directory of mount points (one per backup drive).
* ``name`` selects *which repository on that drive*, and is user-entered.

Keying the leaf on the user's job name (rather than the job's machine-generated
UUID) is what makes a repository reconstructible: if the database is lost or a
job is deleted, the operator can create a job with the same name and
destination and continue the existing backup history.

Imports only ``restic`` and ``core.*`` so both ``services`` and ``api.routes``
can depend on it without a cycle.
"""

import asyncio
import os
import shutil
from enum import Enum
from typing import Tuple

from app.core import fs
from app.core.logging import get_logger, log_call
from app.services import restic

logger = get_logger(__name__)

# Root directory holding one mount point per backup drive. Module-level so
# tests can patch 'app.services.repository.DESTINATIONS_ROOT'.
DESTINATIONS_ROOT: str = "/destinations"

# restic exit codes we branch on. Documented at
# https://restic.readthedocs.io/en/stable/040_backup.html#exit-status-codes
RESTIC_RC_REPO_NOT_FOUND: int = 10
RESTIC_RC_LOCK_FAILED: int = 11
RESTIC_RC_WRONG_PASSWORD: int = 12

# Repo probe/init run inside an HTTP request (job creation), so the deadline
# has to stay short. Deliberately NOT AppSettings.metadata_timeout_seconds
# (default 600) — that would hang the request for ten minutes on a dead mount.
PROVISION_TIMEOUT_SECONDS: int = 60

# The marker file every restic repository has at its root. Used as proof of
# "this really is a repo" before deleting anything.
_REPO_MARKER = "config"


class RepositoryError(Exception):
    """Raised when a repository operation is refused as unsafe."""

    pass


class RepoProbe(str, Enum):
    """What `restic cat config` says about a path."""

    ok = "ok"
    wrong_password = "wrong_password"
    not_a_repo = "not_a_repo"
    unreachable = "unreachable"


class RepoOutcome(str, Enum):
    """Terminal state of :func:`ensure_repository`.

    `adopted` and `initialized` are both success; they are distinguished so
    the caller can tell the operator which one happened — "continuing an
    existing history" and "starting a new one" are very different messages.
    """

    adopted = "adopted"
    initialized = "initialized"
    wrong_password = "wrong_password"
    init_failed = "init_failed"
    unreachable = "unreachable"


@log_call
def build_repo_path(destination_label: str, name: str) -> str:
    """Return the repository path for a job.

    Both components are validated as single path components at the API
    boundary (`app/api/schemas/jobs.py::_validate_label`), so neither can
    contain '/' or traverse outside the destination root.
    """
    return f"{DESTINATIONS_ROOT}/{destination_label}/{name}"


@log_call
async def probe_repository(
    repo_path: str,
    password: str,
    timeout_seconds: int = PROVISION_TIMEOUT_SECONDS,
) -> Tuple[RepoProbe, str]:
    """Classify what is at `repo_path`, returning (probe, stderr detail).

    Anything other than a clean rc=0/10/12 is reported as `unreachable` — a
    stale lock, a network blip, or a timeout must never be mistaken for "no
    repo here", because the caller treats "no repo" as license to initialize.
    """
    rc, _, stderr = await restic.restic_cat_config(repo_path, password, timeout_seconds)

    if rc == 0:
        return RepoProbe.ok, ""
    if rc == RESTIC_RC_WRONG_PASSWORD:
        return RepoProbe.wrong_password, stderr
    if rc == RESTIC_RC_REPO_NOT_FOUND:
        return RepoProbe.not_a_repo, stderr
    return RepoProbe.unreachable, stderr


@log_call
async def ensure_repository(
    repo_path: str,
    password: str,
    timeout_seconds: int = PROVISION_TIMEOUT_SECONDS,
) -> Tuple[RepoOutcome, str]:
    """Make `repo_path` a usable repository, returning (outcome, detail).

    Called once at job creation — never during a backup run. Initializing
    lazily mid-run meant a run had to distinguish "genuine first run" from
    "the repo vanished", and left a window where the password could still be
    changed after the repo had already been initialized with the old one.
    Provisioning up front removes both problems: from creation onward a job's
    repository either exists or the job does not.

    Only a definitive "there is no repo here" (rc=10) leads to init.
    """
    probe, detail = await probe_repository(repo_path, password, timeout_seconds)

    if probe is RepoProbe.ok:
        logger.info("repo=%s adopted existing repository", repo_path)
        return RepoOutcome.adopted, detail
    if probe is RepoProbe.wrong_password:
        return RepoOutcome.wrong_password, detail
    if probe is RepoProbe.unreachable:
        return RepoOutcome.unreachable, detail

    logger.info("repo=%s not found, initializing", repo_path)
    rc, _, stderr = await restic.restic_init(repo_path, password, timeout_seconds)
    if rc != 0:
        return RepoOutcome.init_failed, stderr

    logger.info("repo=%s initialized", repo_path)
    return RepoOutcome.initialized, ""


@log_call
def _assert_safe_repo_path(repo_path: str) -> str:
    """Return the normalized path, or raise if it is not a job repository.

    Deleting is irreversible and the path is assembled from user input, so
    this refuses anything that is not exactly ``<root>/<label>/<name>``. That
    rules out the destinations root, a whole mounted drive, a path escaping
    the root via '..', and anything nested deeper than a repo.
    """
    root = os.path.normpath(DESTINATIONS_ROOT)
    normalized = os.path.normpath(repo_path)
    relative = os.path.relpath(normalized, root)
    parts = relative.split(os.sep)

    if relative.startswith("..") or len(parts) != 2 or not all(parts):
        raise RepositoryError(
            f"refusing to operate on '{repo_path}': expected a path of the "
            f"form '{DESTINATIONS_ROOT}/<destination_label>/<name>'"
        )
    return normalized


@log_call
async def remove_repository(repo_path: str) -> bool:
    """Delete a job's repository directory. Returns True if it was removed.

    Absent directory is a no-op (False) — a job whose repo was already gone
    still deletes cleanly. Refuses a directory that has no restic marker file
    so a mistyped or hand-made folder can never be destroyed.
    """
    normalized = _assert_safe_repo_path(repo_path)

    if not await fs.run_probe(os.path.isdir, normalized, default=False):
        logger.info("repo=%s absent, nothing to remove", normalized)
        return False

    marker = os.path.join(normalized, _REPO_MARKER)
    if not await fs.run_probe(os.path.isfile, marker, default=False):
        raise RepositoryError(
            f"refusing to delete '{repo_path}': not a restic repository "
            f"(no '{_REPO_MARKER}' file at its root)"
        )

    await asyncio.to_thread(shutil.rmtree, normalized)
    logger.info("repo=%s removed", normalized)
    return True
