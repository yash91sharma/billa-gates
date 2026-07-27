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
"""

import shlex
from typing import Any, Dict, List, Optional

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
    source_path: str = backup_runner.build_source_path(
        job.source_label, job.source_subpath
    )
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
                    "Runs after a successful backup only; a failure here makes "
                    "the run a warning, because retention that stops applying "
                    "lets the repository grow without bound."
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
            "unlock_manual",
            "Unlock",
            (
                "Removes stale restic locks from this job's repository — the "
                "ones left behind when a previous run was killed mid-write and "
                "every later run fails with “repository is already locked”."
            ),
            restic.build_unlock_args(),
            env=env,
            runs=True,
            group=GROUP_ON_DEMAND,
            condition=(
                "Runs when you click “Unlock”. The same command also runs "
                "inside a backup — see “Clear stale locks” above."
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
