"""The exact restic commands a job causes, for the Job detail page.

Two groups:

* ``backup_run`` — what one backup run issues, in pipeline order. This is the
  set a schedule fires unattended, so it is the one an operator needs to be
  able to check before trusting it with their data.
* ``on_demand`` — what the Prune, Integrity Check and Unlock buttons issue.
  Same repository, same job, but nothing here ever happens on a schedule; the
  UI keeps them visibly apart for that reason.

Everything is assembled from the same functions the runners use:
``restic.build_*_args`` (the argv every wrapper execs),
``backup_runner.build_backup_kwargs`` / ``build_retention_kwargs`` (the job
fields the pipeline passes), and ``repository.build_repo_path`` /
``backup_runner.build_source_path`` (the paths it reads and writes). Nothing
in this module may describe a command in its own words — a preview built from
a parallel copy of the flag logic silently stops matching reality the first
time a flag is added, and an operator acting on a wrong exclude or retention
list finds out only after data is gone.

Two properties are pinned by tests/test_job_commands.py and must stay true:
the argv of every step matches what ``create_subprocess_exec`` is actually
called with, *and* the set of steps here matches the set of commands the
pipelines actually spawn. The second is what makes "these are all the
commands" a safe thing for the page to imply — add a restic call to a runner
and the test fails until it is listed here.

The preview is derived on every request rather than stored, so an edit to the
job is reflected immediately.

The restore instructions at the bottom of this module are a different kind of
command: ones the app never runs, handed to an operator to type by hand. See
``build_restore_instructions``.
"""

import os
import posixpath
import shlex
from typing import Any, Dict, List, Optional

from app.core import fs
from app.core.logging import get_logger, log_call
from app.db.models import BackupJob
from app.services import backup_runner, repository, restic

logger = get_logger(__name__)

# Commands a scheduled (or manually triggered) backup run issues by itself.
GROUP_BACKUP_RUN: str = "backup_run"
# Commands that only ever run because someone clicked a button.
GROUP_ON_DEMAND: str = "on_demand"

# The parent snapshot is looked up at run time (it is whatever the newest
# snapshot in the repo happens to be), so the preview marks the slot instead of
# inventing an id. The flag itself is shown because its presence is what keeps
# a backup incremental after a path or host change (gaps.md C5).
PARENT_SNAPSHOT_PLACEHOLDER: str = "<id-of-latest-snapshot>"

# Likewise for the subset percentage: it comes from the check dialog at click
# time, not from the job, so no stored value could be shown honestly here.
CHECK_SUBSET_PERCENT_PLACEHOLDER: str = "<percent>"

# And for the lock id: Unlock describes each lock it is about to remove, so the
# id is whatever the repository happens to hold at click time — one command per
# lock found, none at all on a repository that is not locked.
LOCK_ID_PLACEHOLDER: str = "<id-of-each-lock>"

# The repository password is never echoed back to the client — not in a job
# response, and not here.
MASKED_PASSWORD: str = "<this job's repository password>"


def _env_for_display(repo_path: str) -> Dict[str, str]:
    """The environment restic is given, with the password replaced by a label.

    Built from the same helper the subprocesses use so a new variable shows up
    here automatically.
    """
    return restic.build_restic_env_overrides(repo_path, MASKED_PASSWORD)


def _step(
    step: str,
    title: str,
    description: str,
    argv: List[str],
    *,
    env: Dict[str, str],
    runs: bool,
    group: str = GROUP_BACKUP_RUN,
    condition: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "step": step,
        "title": title,
        "description": description,
        "group": group,
        "runs": runs,
        "condition": condition,
        "env": env,
        "argv": argv,
        "command": shlex.join(argv),
    }


def _skipped_step(
    step: str, title: str, description: str, condition: str
) -> Dict[str, Any]:
    """A pipeline step that this job's configuration turns off entirely.

    Rendered without a command on purpose: showing one for a step that never
    executes is the same lie as omitting a flag that does.
    """
    return {
        "step": step,
        "title": title,
        "description": description,
        "group": GROUP_BACKUP_RUN,
        "runs": False,
        "condition": condition,
        "env": {},
        "argv": [],
        "command": None,
    }


def _backup_run_commands(
    job: BackupJob, repo_path: str, env: Dict[str, str], *, auto_unlock: bool
) -> List[Dict[str, Any]]:
    """The pipeline in run_backup order (app/services/backup_runner.py)."""
    source_path: str = backup_runner.build_source_path(job.source_label)
    unlock_description = (
        "Removes lock files left behind by a run that was killed mid-write. "
        "Snapshots and their data are not touched."
    )

    commands: List[Dict[str, Any]] = [
        _step(
            "verify_repository",
            "Verify the repository",
            (
                "Reads the repository config to prove the destination is "
                "reachable and the stored password opens it. A run never "
                "initializes a repository, so if this fails the backup stops "
                "here rather than starting an empty history."
            ),
            restic.build_cat_config_args(),
            env=env,
            runs=True,
        ),
        _step(
            "unlock",
            "Clear stale locks",
            unlock_description,
            restic.build_unlock_args(),
            env=env,
            runs=auto_unlock,
            condition=(
                "Runs on every backup because “Auto unlock” is on in Settings, "
                "and again if the repository turns out to be locked."
                if auto_unlock
                else "Only if the repository is locked — “Auto unlock” is off "
                "in Settings, so this is issued once as a retry after a lock "
                "failure instead of on every backup."
            ),
        ),
        _step(
            "parent_lookup",
            "Find the parent snapshot",
            (
                "Looks up the newest snapshot in the repository so the backup "
                "below can be incremental. Read-only, and returns nothing on "
                "the very first backup, when there is no snapshot yet."
            ),
            restic.build_latest_snapshot_args(),
            env=env,
            runs=True,
        ),
        _step(
            "backup",
            "Back up the source",
            (
                "The backup itself: reads "
                f"{source_path} and writes a new snapshot to the repository."
            ),
            restic.build_backup_args(
                source_path,
                parent_snapshot_id=PARENT_SNAPSHOT_PLACEHOLDER,
                **backup_runner.build_backup_kwargs(job),
            ),
            env=env,
            runs=True,
            condition=(
                f"{PARENT_SNAPSHOT_PLACEHOLDER} is resolved by the previous "
                "command; on the first backup there is no parent yet and "
                "--parent is left off. If the repository turns out to be "
                "locked, the lock is cleared and this exact command is retried "
                "once."
            ),
        ),
    ]

    retention_kwargs: Dict[str, Any] = backup_runner.build_retention_kwargs(job)
    retention_description = (
        "Applies the retention policy by dropping snapshot references. It does "
        "not free disk space on its own — that is what Prune does."
    )
    if retention_kwargs:
        commands.append(
            _step(
                "retention",
                "Apply the retention policy",
                retention_description,
                restic.build_forget_args(**retention_kwargs),
                env=env,
                runs=True,
                condition=(
                    "Runs after a backup that read everything. A partial backup "
                    "(some files unreadable) holds it back, so an incomplete "
                    "snapshot cannot push a complete one out of the policy — "
                    "that run says so on its page. A failure here makes the run "
                    "a warning, because retention that stops applying lets the "
                    "repository grow without bound."
                ),
            )
        )
    else:
        commands.append(
            _skipped_step(
                "retention",
                "Apply the retention policy",
                retention_description,
                (
                    "Not run: this job has no retention policy configured, so "
                    "every snapshot is kept forever and there is nothing to "
                    "forget."
                ),
            )
        )
    return commands


def _on_demand_commands(env: Dict[str, str]) -> List[Dict[str, Any]]:
    """The button-triggered commands (run_prune, run_check, the unlock route).

    None of these are part of a backup and none of them are scheduled — the
    integrity check in particular was removed from the backup pipeline, so a
    job that never has its button clicked never verifies itself.
    """
    return [
        _step(
            "prune",
            "Prune Old Files",
            (
                "Deletes the data behind forgotten snapshots. This is the only "
                "thing that frees space on the destination drive — the "
                "retention policy above just drops snapshot references. Heavy "
                "on disk and I/O, and no other run for this job can start "
                "while it works."
            ),
            restic.build_prune_args(),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition="Runs when you click “Prune Old Files”. Never scheduled.",
        ),
        _step(
            "check_structural",
            "Integrity Check — structural",
            (
                "Verifies that the repository's metadata is complete and "
                "consistent. Read-only: nothing is backed up, changed or "
                "deleted."
            ),
            restic.build_check_args("structural", None),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                "Runs when you click “Integrity Check” and leave the mode on "
                "Structural (the dialog's default)."
            ),
        ),
        _step(
            "check_subset",
            "Integrity Check — subset",
            (
                "Structural verification plus a re-read of a percentage of the "
                "pack data, so a fraction of the actual bytes is checked "
                "against its hashes."
            ),
            restic.build_check_args("subset", CHECK_SUBSET_PERCENT_PLACEHOLDER),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                "Runs when you pick Subset in the Integrity Check dialog; "
                f"{CHECK_SUBSET_PERCENT_PLACEHOLDER} is the percentage you "
                "enter there."
            ),
        ),
        _step(
            "check_full",
            "Integrity Check — full",
            (
                "Structural verification plus a re-read of every pack file in "
                "the repository. The most thorough check and the slowest — it "
                "reads the whole repository off the destination drive."
            ),
            restic.build_check_args("full", None),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition="Runs when you pick Full in the Integrity Check dialog.",
        ),
        _step(
            "inspect_locks",
            "Unlock — list locks",
            (
                "Lists the locks currently held on this job's repository. Runs "
                "twice around the unlock below, because `restic unlock` exits "
                "0 whether it removed every lock or none of them — comparing "
                "the two listings is what lets the result name the locks that "
                "actually went."
            ),
            restic.build_list_locks_args(),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                "Runs when you click “Unlock”, before and after the removal. Read-only."
            ),
        ),
        _step(
            "describe_lock",
            "Unlock — describe a lock",
            (
                "Reads one lock's owner, age and kind before it is removed, so "
                "the result can say what was cleared instead of just how many. "
                "Runs once per lock found, and not at all on a repository that "
                "holds none."
            ),
            restic.build_cat_lock_args(LOCK_ID_PLACEHOLDER),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                f"Runs when you click “Unlock”. {LOCK_ID_PLACEHOLDER} is "
                "resolved by the listing above. Read-only."
            ),
        ),
        _step(
            "unlock_manual",
            "Unlock",
            (
                "Removes every restic lock from this job's repository, "
                "including ones restic will not remove on its own — a lock "
                "left by a run that was killed mid-write is not considered "
                "stale for 30 minutes, and never at all once the container "
                "that wrote it has been replaced. Snapshots and their data are "
                "not touched."
            ),
            restic.build_unlock_args(remove_all=True),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                "Runs when you click “Unlock”. A backup runs the narrower "
                "form of this command, without --remove-all — see “Clear "
                "stale locks” above."
            ),
        ),
    ]


@log_call
def build_job_commands(
    job: BackupJob, *, auto_unlock: bool = True
) -> List[Dict[str, Any]]:
    """Return the restic commands this job causes: the backup run first, in
    pipeline order, then the button-triggered ones.

    `auto_unlock` comes from AppSettings and decides whether `restic unlock`
    runs on every backup or only on the stale-lock retry.
    """
    repo_path: str = repository.build_repo_path(job.destination_label, job.name)
    env: Dict[str, str] = _env_for_display(repo_path)

    return [
        *_backup_run_commands(job, repo_path, env, auto_unlock=auto_unlock),
        *_on_demand_commands(env),
    ]


# ── Restore instructions ─────────────────────────────────────────────────────
#
# Billa-Gates never restores: the operator runs restic by hand, often after
# something has already gone wrong, and sometimes on a computer where this app
# is not running at all. These are the only commands the app hands someone to
# type, so they are built here, shell-quoted, from the same path builders the
# backups use, and never assembled in the frontend. The card used to be a
# template there, which printed an unquoted repository path (a job named
# "Documents Offsite" became "repository does not exist") and a relative
# `--target ./restored` that resolved to the container's own disk.
#
# They are never executed by the app, so they don't go through
# `restic.build_*_args()` and aren't part of the Commands-tab parity tests:
# there is no runner to drift from. What they must agree with is the layout the
# backups write, which is why every path comes from `build_repo_path` and
# `build_source_path`.

# Where the app's container sees a restore target. The app never creates it: the
# operator maps a host folder here in docker-compose.yml. Without that mount a
# restore inside the container lands on the container's own disk and is lost
# when the container is recreated, so the in-container commands are withheld.
RESTORE_ROOT: str = "/restore"

# The app cannot know where a drive is mounted on the host, or on another
# computer, so the host-side commands carry placeholders and the card says what
# to put in their place.
DRIVE_PLACEHOLDER: str = "/path/to/backup-drive"
TARGET_PLACEHOLDER: str = "/path/to/empty-folder"

# The container_name in the README's compose file; the card says to use yours.
CONTAINER_NAME: str = "billa-gates"

# restic's repository layout, so the operator can confirm they are pointing at
# the right folder before typing a password into it. Recorded from a repository
# this app created (restic 0.19.1).
REPOSITORY_CONTENTS: List[str] = [
    "config",
    "data/",
    "index/",
    "keys/",
    "locks/",
    "snapshots/",
]


def _restore_argvs(repo: str, snapshot_ref: str, target: str) -> List[List[str]]:
    """List the snapshots, then restore one into `target`.

    `-r` rather than an `export RESTIC_REPOSITORY=` line: it survives `sudo`,
    which drops exported variables on most Linux systems. No password: restic
    prompts for it, so a password containing spaces, `&` or `$` cannot be
    mis-quoted and does not end up in shell history.

    `<snapshot>:<path>` restores the contents of that path straight into the
    target, rather than under `<target>/sources/<label>/...`; verified on
    restic 0.16.5, 0.17.0, 0.18.1 and 0.19.1. `--sparse` keeps sparse files,
    such as VM images, from coming back fully allocated.
    """
    bin_ = restic.RESTIC_BIN
    return [
        [bin_, "-r", repo, "snapshots"],
        [bin_, "-r", repo, "restore", snapshot_ref, "--target", target, "--sparse"],
    ]


def _join_for_editing(argv: List[str]) -> str:
    """`shlex.join`, except that an argument holding a placeholder is always
    single-quoted.

    The operator types over the placeholders before running the command, and
    `shlex.join` quotes only when it has to: `--target /path/to/empty-folder`
    came out bare, so typing `/Users/me/Restored Files` over it split the path
    into two arguments. Quoted, whatever they type stays one argument (a `'`
    inside is escaped the same way `shlex.quote` does it).
    """

    def quote(arg: str) -> str:
        if DRIVE_PLACEHOLDER in arg or TARGET_PLACEHOLDER in arg:
            return "'" + arg.replace("'", "'\"'\"'") + "'"
        return shlex.quote(arg)

    return " ".join(quote(arg) for arg in argv)


@log_call
def build_restore_instructions(
    job: BackupJob, *, restore_root_mounted: bool
) -> Dict[str, Any]:
    """Return everything the Restore card needs to tell someone how to get
    this job's files back by hand.

    Three ways, which the card keeps apart:

    * ``linux_commands``: the NAS itself, or any Linux computer, with the drive
      attached. `sudo`, because the container writes the repository as root
      with owner-only permissions, and only root restores the original owners.
    * ``desktop_commands``: the same commands without `sudo`, for macOS and
      Windows. Those filesystems merge names that differ only in letter case
      or Unicode form, so a full restore there can silently lose files; the
      card says so. Measured: two photos overwritten without any error.
    * ``container_commands``: inside the app's container, restoring into
      `RESTORE_ROOT`. Empty unless a folder is actually mounted there.

    Host-side paths use `DRIVE_PLACEHOLDER`, with the repository's position on
    the drive taken from `build_repo_path`: the one fact that holds on every
    computer is that the repository is a folder named after the job at the
    top level of the drive.
    """
    repo_path: str = repository.build_repo_path(job.destination_label, job.name)
    destination_path: str = posixpath.dirname(repo_path)
    repository_folder: str = posixpath.relpath(repo_path, destination_path)
    snapshot_path: str = backup_runner.build_source_path(job.source_label)
    snapshot_ref: str = f"latest:{snapshot_path}"

    host_repo: str = posixpath.join(DRIVE_PLACEHOLDER, repository_folder)
    desktop = _restore_argvs(host_repo, snapshot_ref, TARGET_PLACEHOLDER)
    linux = [["sudo", *argv] for argv in desktop]
    container: List[List[str]] = []
    if restore_root_mounted:
        container = [
            ["docker", "exec", "-it", CONTAINER_NAME, "sh"],
            *_restore_argvs(
                repo_path,
                snapshot_ref,
                posixpath.join(RESTORE_ROOT, repository_folder),
            ),
        ]

    return {
        "repository_folder": repository_folder,
        "destination_label": job.destination_label,
        "destination_path": destination_path,
        "repository_path": repo_path,
        "snapshot_path": snapshot_path,
        "repository_contents": list(REPOSITORY_CONTENTS),
        "drive_marker": backup_runner.SENTINEL_FILENAME,
        "drive_placeholder": DRIVE_PLACEHOLDER,
        "target_placeholder": TARGET_PLACEHOLDER,
        "compose_mapping": f"{DRIVE_PLACEHOLDER}:{destination_path}",
        "restore_root": RESTORE_ROOT,
        "restore_root_mounted": restore_root_mounted,
        "linux_commands": [_join_for_editing(argv) for argv in linux],
        "desktop_commands": [_join_for_editing(argv) for argv in desktop],
        "container_commands": [shlex.join(argv) for argv in container],
    }


@log_call
async def restore_root_mounted() -> bool:
    """Whether a host folder is mounted at `RESTORE_ROOT` in this container.

    Probed through `fs.run_probe`, so a hung network share mapped to /restore
    costs the in-container method rather than hanging the job page; a probe
    that times out counts as not mounted.
    """
    return bool(await fs.run_probe(os.path.ismount, RESTORE_ROOT, default=False))
