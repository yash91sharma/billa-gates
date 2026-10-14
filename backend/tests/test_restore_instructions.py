"""Tests for the restore instructions on the Job detail page.

Billa-Gates never restores; the operator does, by hand, with restic. So these
commands are the one place the app hands someone a command line to run
themselves, usually in a hurry and often after something has already gone
wrong. Two of the ways that card used to fail are what these tests pin:

* The repository path was printed unquoted, so any job whose name contains a
  space (which the name validator allows) produced `export
  RESTIC_REPOSITORY=/destinations/offsite/Documents Offsite`, and restic answered
  "repository does not exist". Every assertion below therefore splits the
  command the way a shell would (`shlex.split`) and compares the resulting
  argv. A string comparison would pass on exactly that bug.
* The target was `./restored`, which inside the container resolves to
  `/app/restored`: the container's own disk, which is not a volume and
  disappears when the container is recreated.

The commands are never executed by the app, so unlike the Commands tab there is
no runner to compare against. What they must agree with instead is the layout
the backups actually write: the repository path from `build_repo_path` and the
path inside each snapshot from `build_source_path`.
"""

import json
import shlex
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.db.models import BackupJob, ScheduleType
from app.services import backup_runner, job_commands, repository
from tests.conftest import make_job_payload

PASSWORD = "Fam1ly Ph0tos & $pw"


def _make_job(**overrides: Any) -> BackupJob:
    base: Dict[str, Any] = dict(
        id="job-1",
        name="Family Photos",
        source_label="photos",
        destination_label="usb",
        restic_password=PASSWORD,
        schedule_type=ScheduleType.cron,
        schedule_value="0 3 * * *",
        enabled=True,
    )
    base.update(overrides)
    return BackupJob(**base)


def _argvs(commands: List[str]) -> List[List[str]]:
    """Split each command exactly as a POSIX shell would."""
    return [shlex.split(c) for c in commands]


# Names the validator accepts that a naive template mangles: spaces, inner
# dots, non-ASCII letters. (Quotes and `$` are rejected at the API boundary.)
TRICKY_NAMES = ["Family Photos", "Photos", "Fotos 2024.v2", "Фото семьи", "my_docs-1"]
TRICKY_SOURCES = ["photos", "My Photos"]


# ── commands for a computer with the drive attached ──────────────────────────


@pytest.mark.parametrize("name", TRICKY_NAMES)
@pytest.mark.parametrize("source", TRICKY_SOURCES)
def test_linux_commands_split_into_the_intended_argv(name: str, source: str) -> None:
    info = job_commands.build_restore_instructions(
        _make_job(name=name, source_label=source), restore_root_mounted=False
    )
    repo = f"{job_commands.DRIVE_PLACEHOLDER}/{name}"
    snapshot_ref = f"latest:/sources/{source}"

    assert _argvs(info["linux_commands"]) == [
        ["sudo", "restic", "-r", repo, "snapshots"],
        [
            "sudo",
            "restic",
            "-r",
            repo,
            "restore",
            snapshot_ref,
            "--target",
            job_commands.TARGET_PLACEHOLDER,
            "--sparse",
        ],
    ]


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_a_placeholder_replaced_by_a_path_with_spaces_stays_one_argument(
    name: str,
) -> None:
    """The operator edits these commands before running them. `shlex.join`
    quotes only when it has to, so `--target /path/to/empty-folder` (and the
    drive path of a job named without spaces) came out unquoted, and typing
    `/Users/me/Restored Files` over the placeholder split it into two
    arguments: the very bug this card was rebuilt to remove. An argument that
    holds a placeholder is therefore always quoted."""
    info = job_commands.build_restore_instructions(
        _make_job(name=name), restore_root_mounted=False
    )
    drive = "/mnt/My Backup Drive"
    target = "/Users/me/Restored Files"

    for command in info["linux_commands"] + info["desktop_commands"]:
        edited = command.replace(job_commands.DRIVE_PLACEHOLDER, drive).replace(
            job_commands.TARGET_PLACEHOLDER, target
        )
        argv = shlex.split(edited)
        assert argv[argv.index("-r") + 1] == f"{drive}/{name}"
        if "--target" in argv:
            assert argv[argv.index("--target") + 1] == target


def test_desktop_commands_are_the_linux_ones_without_sudo() -> None:
    """macOS and Windows: the repository is readable without root there, and
    `sudo` on a Mac would restore every file as owned by root."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=False
    )

    assert _argvs(info["desktop_commands"]) == [
        argv[1:] for argv in _argvs(info["linux_commands"])
    ]
    assert all(argv[0] == "sudo" for argv in _argvs(info["linux_commands"]))


def test_host_repository_is_the_container_path_rebased_onto_the_drive() -> None:
    """The app cannot know where the drive is mounted on the host, but it does
    know where the repository sits relative to the drive's root. Derived from
    `build_repo_path`, not re-assembled from the name."""
    job = _make_job()
    info = job_commands.build_restore_instructions(job, restore_root_mounted=False)
    container_repo = repository.build_repo_path(job.destination_label, job.name)

    assert info["repository_path"] == container_repo
    assert info["destination_path"] == "/destinations/usb"
    assert container_repo.startswith(info["destination_path"] + "/")
    relative = container_repo[len(info["destination_path"]) :]
    for argv in _argvs(info["linux_commands"]):
        assert argv[3] == job_commands.DRIVE_PLACEHOLDER + relative
    assert info["repository_folder"] == "Family Photos"


def test_paths_follow_the_destination_root_the_backups_use() -> None:
    """Never a hand-rolled `/destinations/...`: patching the root moves every
    path the card prints."""
    with patch.object(repository, "DESTINATIONS_ROOT", "/mnt/drives"):
        info = job_commands.build_restore_instructions(
            _make_job(), restore_root_mounted=True
        )

    assert info["repository_path"] == "/mnt/drives/usb/Family Photos"
    assert info["destination_path"] == "/mnt/drives/usb"
    assert (
        info["compose_mapping"] == f"{job_commands.DRIVE_PLACEHOLDER}:/mnt/drives/usb"
    )
    container = _argvs(info["container_commands"])
    assert container[1][2] == "/mnt/drives/usb/Family Photos"


@pytest.mark.parametrize("source", TRICKY_SOURCES)
def test_snapshot_reference_is_the_path_the_backups_read(source: str) -> None:
    """`latest:<path>` restores the contents of that path straight into the
    target. It has to be the path restic was given at backup time, or restic
    reports it as not found."""
    info = job_commands.build_restore_instructions(
        _make_job(source_label=source), restore_root_mounted=False
    )
    expected = backup_runner.build_source_path(source)

    assert info["snapshot_path"] == expected
    restore_argv = _argvs(info["linux_commands"])[1]
    assert restore_argv[restore_argv.index("restore") + 1] == f"latest:{expected}"


def test_compose_mapping_is_the_line_that_tells_the_operator_the_host_path() -> None:
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=False
    )

    assert info["compose_mapping"] == "/path/to/backup-drive:/destinations/usb"
    assert info["drive_placeholder"] == "/path/to/backup-drive"
    assert info["target_placeholder"] == "/path/to/empty-folder"


def test_drive_marker_is_the_sentinel_the_runs_check_for() -> None:
    """How the operator recognizes the right drive on another computer: the
    same file every run checks for at the drive's top level."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=False
    )

    assert info["drive_marker"] == backup_runner.SENTINEL_FILENAME
    assert backup_runner.SENTINEL_FILENAME == ".billa_gates_check"


def test_repository_contents_is_restics_repository_layout() -> None:
    """What the operator should find inside the folder they point restic at.
    Recorded from a repository this app created (restic 0.19.1)."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=False
    )

    assert info["repository_contents"] == [
        "config",
        "data/",
        "index/",
        "keys/",
        "locks/",
        "snapshots/",
    ]


# ── commands inside the app's container ──────────────────────────────────────


def test_container_commands_are_withheld_when_restore_root_is_not_mounted() -> None:
    """Without a mounted /restore, a restore inside the container lands on the
    container's own disk and is lost when the container is recreated. Offering
    the command anyway is how the old card sent restores to /app/restored."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=False
    )

    assert info["restore_root_mounted"] is False
    assert info["restore_root"] == "/restore"
    assert info["container_commands"] == []


def test_container_commands_restore_into_the_mounted_restore_root() -> None:
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=True
    )
    repo = "/destinations/usb/Family Photos"

    assert info["restore_root_mounted"] is True
    assert _argvs(info["container_commands"]) == [
        ["docker", "exec", "-it", job_commands.CONTAINER_NAME, "sh"],
        ["restic", "-r", repo, "snapshots"],
        [
            "restic",
            "-r",
            repo,
            "restore",
            "latest:/sources/photos",
            "--target",
            "/restore/Family Photos",
            "--sparse",
        ],
    ]


# ── properties every command must have ───────────────────────────────────────


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_every_restore_target_is_absolute_and_never_inside_the_app(name: str) -> None:
    info = job_commands.build_restore_instructions(
        _make_job(name=name), restore_root_mounted=True
    )
    commands = (
        info["linux_commands"] + info["desktop_commands"] + info["container_commands"]
    )
    targets = [
        argv[argv.index("--target") + 1]
        for argv in _argvs(commands)
        if "--target" in argv
    ]

    assert len(targets) == 3
    for target in targets:
        assert target.startswith("/"), target
        assert not target.startswith("/app"), target


def test_no_command_carries_or_exports_the_password() -> None:
    """restic prompts for the password itself. Printing an `export` line was
    both a quoting trap (passwords may contain spaces, `&` and `$`) and an
    invitation to leave the password in shell history."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=True
    )

    assert PASSWORD not in json.dumps(info)
    for command in (
        info["linux_commands"] + info["desktop_commands"] + info["container_commands"]
    ):
        assert "RESTIC_PASSWORD" not in command
        assert "export" not in shlex.split(command)


def test_every_restore_is_sparse() -> None:
    """Without --sparse a VM image or other sparse file comes back fully
    allocated (measured: 603 MB on disk restored as 855 MB)."""
    info = job_commands.build_restore_instructions(
        _make_job(), restore_root_mounted=True
    )
    restores = [
        argv
        for argv in _argvs(
            info["linux_commands"]
            + info["desktop_commands"]
            + info["container_commands"]
        )
        if "restore" in argv
    ]

    assert len(restores) == 3
    assert all("--sparse" in argv for argv in restores)


# ── GET /api/jobs/{id}/restore ───────────────────────────────────────────────


async def _create_job(client, **overrides) -> Dict[str, Any]:
    payload = make_job_payload(**overrides)
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_get_restore_returns_the_instructions_for_the_job(client):
    job = await _create_job(
        client, name="Family Photos", source_label="photos", destination_label="usb"
    )

    with patch("app.services.job_commands.os.path.ismount", return_value=False):
        resp = await client.get(f"/api/jobs/{job['id']}/restore")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repository_folder"] == "Family Photos"
    assert body["repository_path"] == "/destinations/usb/Family Photos"
    assert body["snapshot_path"] == "/sources/photos"
    assert body["restore_root_mounted"] is False
    assert body["container_commands"] == []
    assert (
        shlex.split(body["linux_commands"][0])[3]
        == "/path/to/backup-drive/Family Photos"
    )


async def test_get_restore_offers_the_container_commands_when_restore_is_mounted(
    client,
):
    job = await _create_job(client)

    with patch(
        "app.services.job_commands.os.path.ismount", return_value=True
    ) as ismount:
        body = (await client.get(f"/api/jobs/{job['id']}/restore")).json()

    ismount.assert_called_once_with("/restore")
    assert body["restore_root_mounted"] is True
    assert len(body["container_commands"]) == 3


async def test_get_restore_treats_a_hung_restore_probe_as_not_mounted(client):
    """A dead network share at /restore must not hang the job page."""
    import time

    job = await _create_job(client)

    def slow_ismount(path: str) -> bool:
        time.sleep(0.5)
        return True

    with (
        patch("app.core.fs.FS_PROBE_TIMEOUT_SECONDS", 0.05),
        patch("app.services.job_commands.os.path.ismount", side_effect=slow_ismount),
    ):
        resp = await client.get(f"/api/jobs/{job['id']}/restore")

    assert resp.status_code == 200, resp.text
    assert resp.json()["restore_root_mounted"] is False


async def test_get_restore_never_returns_the_repository_password(client):
    job = await _create_job(client, restic_password="hunter2 very secret")

    resp = await client.get(f"/api/jobs/{job['id']}/restore")

    assert resp.status_code == 200
    assert "hunter2 very secret" not in resp.text


async def test_get_restore_404_for_unknown_job(client):
    import uuid

    resp = await client.get(f"/api/jobs/{uuid.uuid4()}/restore")
    assert resp.status_code == 404


async def test_get_restore_reflects_a_source_change(client):
    """source_label is editable; the snapshot path in the commands follows it."""
    job = await _create_job(client, source_label="documents")

    with patch("os.path.isdir", return_value=True):
        resp = await client.put(
            f"/api/jobs/{job['id']}", json={"source_label": "photos"}
        )
    assert resp.status_code == 200, resp.text

    body = (await client.get(f"/api/jobs/{job['id']}/restore")).json()
    assert body["snapshot_path"] == "/sources/photos"
