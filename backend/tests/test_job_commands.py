"""Tests for the restic command preview shown on the Job detail page.

The feature has exactly one real requirement: the commands the UI shows are
the commands the runner runs. Every assertion here therefore compares the
preview against the *executing* code path — the argv
``asyncio.create_subprocess_exec`` was actually called with, and the kwargs
``run_backup`` actually hands the restic wrappers — never against a
hand-written string. A hardcoded expectation would keep passing while the two
drifted apart, and that is the one failure mode that matters here: a preview
that quietly lies about what is about to touch the user's data.
"""

import json
import re
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import BackupJob, ScheduleType
from app.services import backup_runner, job_commands, repository, restic
from app.services.restic import (
    restic_backup,
    restic_cat_config,
    restic_check,
    restic_forget,
    restic_latest_snapshot_id,
    restic_prune,
    restic_unlock,
)
from tests.conftest import make_job_payload

PASSWORD = "s3cr3t-repo-password"
REPO = "/destinations/main/Photos"


# ── helpers ───────────────────────────────────────────────────────────────────


class _FakeStream:
    """Minimal StreamReader stand-in (restic_backup drains its pipes)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return chunk


def _make_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> AsyncMock:
    proc = AsyncMock()
    proc.returncode = returncode
    proc.stdout = _FakeStream(stdout.encode())
    proc.stderr = _FakeStream(stderr.encode())
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


def _make_job(**overrides: Any) -> BackupJob:
    """An unsaved BackupJob — the preview is a pure function of job fields."""
    base: Dict[str, Any] = dict(
        id="job-1",
        name="Photos",
        source_label="pictures",
        source_subpath=None,
        destination_label="main",
        restic_password=PASSWORD,
        schedule_type=ScheduleType.interval,
        schedule_value="6h",
        enabled=True,
    )
    base.update(overrides)
    return BackupJob(**base)


def _by_step(commands: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c["step"]: c for c in commands}


# Every option a job can turn into a `restic backup` flag, set at once, so a
# flag dropped from either the builder or the preview shows up as a diff.
FULL_OPTIONS: Dict[str, Any] = dict(
    source_subpath="2024",
    exclude_patterns=["*.tmp", "node_modules"],
    exclude_caches=True,
    exclude_if_present=[".nobackup"],
    one_file_system=True,
    no_scan=True,
    tags=["photos", "nas"],
    compression="max",
    pack_size=64,
    read_concurrency=4,
)

FULL_RETENTION: Dict[str, Any] = dict(
    retain_keep_last=10,
    retain_keep_hourly=24,
    retain_keep_daily=7,
    retain_keep_weekly=4,
    retain_keep_monthly=12,
    retain_keep_yearly=3,
    retain_keep_within="30d",
    retain_keep_within_hourly="2d",
    retain_keep_within_daily="7d",
    retain_keep_within_weekly="1m",
    retain_keep_within_monthly="1y",
    retain_keep_within_yearly="2y",
)


# ── the argv builders are what the wrappers actually exec ─────────────────────
#
# Without these, `build_*_args` would be a second, parallel description of the
# command line — exactly the duplication this feature exists to avoid.


async def test_restic_backup_execs_build_backup_args():
    proc = _make_process(0)
    kwargs = dict(
        exclude_patterns=["*.tmp"],
        exclude_caches=True,
        exclude_if_present=[".nobackup"],
        one_file_system=True,
        no_scan=True,
        tags=["photos"],
        compression="max",
        pack_size=64,
        read_concurrency=4,
    )
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_backup(
            REPO,
            PASSWORD,
            "/sources/pictures",
            60,
            parent_snapshot_id="a" * 64,
            **kwargs,
        )

    assert list(spawn.call_args.args) == restic.build_backup_args(
        "/sources/pictures", parent_snapshot_id="a" * 64, **kwargs
    )


async def test_restic_backup_without_parent_execs_build_backup_args():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_backup(REPO, PASSWORD, "/sources/pictures", 60)

    assert list(spawn.call_args.args) == restic.build_backup_args("/sources/pictures")


async def test_restic_forget_execs_build_forget_args():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_forget(REPO, PASSWORD, 60, **FULL_RETENTION)

    assert list(spawn.call_args.args) == restic.build_forget_args(**FULL_RETENTION)


async def test_restic_cat_config_execs_build_cat_config_args():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_cat_config(REPO, PASSWORD, 60)

    assert list(spawn.call_args.args) == restic.build_cat_config_args()


async def test_restic_unlock_execs_build_unlock_args():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_unlock(REPO, PASSWORD, 60)

    assert list(spawn.call_args.args) == restic.build_unlock_args()


async def test_restic_latest_snapshot_id_execs_build_latest_snapshot_args():
    proc = _make_process(0, stdout="[]")
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_latest_snapshot_id(REPO, PASSWORD)

    assert list(spawn.call_args.args) == restic.build_latest_snapshot_args()


async def test_restic_prune_execs_build_prune_args():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_prune(REPO, PASSWORD, 60)

    assert list(spawn.call_args.args) == restic.build_prune_args()


async def test_restic_check_execs_build_check_args():
    for mode, subset in (("structural", None), ("subset", 5), ("full", None)):
        proc = _make_process(0)
        with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
            await restic_check(REPO, PASSWORD, mode, subset, 60)

        assert list(spawn.call_args.args) == restic.build_check_args(mode, subset)


async def test_restic_env_overrides_are_what_the_wrapper_passes():
    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_cat_config(REPO, PASSWORD, 60)

    env = spawn.call_args.kwargs["env"]
    overrides = restic.build_restic_env_overrides(REPO, PASSWORD)
    assert overrides
    for key, value in overrides.items():
        assert env[key] == value


# ── the preview matches the executed command line ────────────────────────────


async def test_backup_step_argv_is_what_restic_backup_execs():
    job = _make_job(**FULL_OPTIONS)
    step = _by_step(job_commands.build_job_commands(job))["backup"]

    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_backup(
            repository.build_repo_path(job.destination_label, job.name),
            job.restic_password,
            backup_runner.build_source_path(job.source_label, job.source_subpath),
            60,
            parent_snapshot_id=job_commands.PARENT_SNAPSHOT_PLACEHOLDER,
            **backup_runner.build_backup_kwargs(job),
        )

    assert step["argv"] == list(spawn.call_args.args)
    assert step["command"] == shlex.join(step["argv"])
    assert step["runs"] is True


async def test_forget_step_argv_is_what_restic_forget_execs():
    job = _make_job(**FULL_RETENTION)
    step = _by_step(job_commands.build_job_commands(job))["retention"]

    proc = _make_process(0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await restic_forget(
            repository.build_repo_path(job.destination_label, job.name),
            job.restic_password,
            60,
            **backup_runner.build_retention_kwargs(job),
        )

    assert step["argv"] == list(spawn.call_args.args)
    assert step["runs"] is True


async def test_metadata_steps_match_their_wrappers():
    job = _make_job()
    steps = _by_step(job_commands.build_job_commands(job))

    assert steps["verify_repository"]["argv"] == restic.build_cat_config_args()
    assert steps["unlock"]["argv"] == restic.build_unlock_args()
    assert steps["parent_lookup"]["argv"] == restic.build_latest_snapshot_args()


async def test_steps_are_listed_in_pipeline_order():
    job = _make_job(**FULL_RETENTION)
    steps = [
        c["step"]
        for c in job_commands.build_job_commands(job)
        if c["group"] == job_commands.GROUP_BACKUP_RUN
    ]

    assert steps == [
        "verify_repository",
        "unlock",
        "parent_lookup",
        "backup",
        "retention",
    ]


# ── the preview follows the job's own configuration ──────────────────────────


async def test_backup_command_uses_the_effective_source_path():
    job = _make_job(source_label="pictures", source_subpath="2024/raw")
    step = _by_step(job_commands.build_job_commands(job))["backup"]

    assert step["argv"][-1] == backup_runner.build_source_path("pictures", "2024/raw")
    assert "/sources/pictures/2024/raw" in step["command"]


async def test_backup_command_carries_every_configured_option():
    job = _make_job(**FULL_OPTIONS)
    command = _by_step(job_commands.build_job_commands(job))["backup"]["command"]

    assert "--exclude '*.tmp'" in command
    assert "--exclude node_modules" in command
    assert "--exclude-caches" in command
    assert "--exclude-if-present .nobackup" in command
    assert "--one-file-system" in command
    assert "--no-scan" in command
    assert "--tag photos" in command
    assert "--tag nas" in command
    assert "--compression max" in command
    assert "--pack-size 64" in command
    assert "--read-concurrency 4" in command


async def test_backup_command_omits_flags_for_unset_options():
    command = _by_step(job_commands.build_job_commands(_make_job()))["backup"][
        "command"
    ]

    for flag in (
        "--exclude",
        "--exclude-caches",
        "--exclude-if-present",
        "--one-file-system",
        "--no-scan",
        "--tag",
        "--compression",
        "--pack-size",
        "--read-concurrency",
    ):
        assert flag not in command


async def test_forget_command_carries_every_configured_retention_flag():
    job = _make_job(retain_keep_last=10, retain_keep_daily=7, retain_keep_within="30d")
    command = _by_step(job_commands.build_job_commands(job))["retention"]["command"]

    assert "--keep-last 10" in command
    assert "--keep-daily 7" in command
    assert "--keep-within 30d" in command
    assert "--keep-monthly" not in command
    # Retention must stay collapsed into a single group, or a path/host change
    # would strand old snapshots forever (gaps.md C3).
    assert "--group-by ''" in command


async def test_forget_step_is_marked_not_run_without_a_retention_policy():
    """No retention configured means `restic forget` is never invoked — the
    preview has to say so rather than show a command the runner will not run."""
    step = _by_step(job_commands.build_job_commands(_make_job()))["retention"]

    assert step["runs"] is False
    assert step["command"] is None
    assert step["argv"] == []
    assert step["condition"]


async def test_unlock_is_unconditional_only_while_auto_unlock_is_on():
    job = _make_job()

    on = _by_step(job_commands.build_job_commands(job, auto_unlock=True))["unlock"]
    off = _by_step(job_commands.build_job_commands(job, auto_unlock=False))["unlock"]

    assert on["runs"] is True
    assert off["runs"] is False
    # Auto-unlock off doesn't remove the command from the pipeline — it still
    # runs on the stale-lock retry, and the preview must say under what
    # condition rather than pretend restic unlock never happens.
    assert off["argv"] == restic.build_unlock_args()
    assert off["condition"]


async def test_env_names_the_repository_and_never_the_password():
    job = _make_job(**FULL_OPTIONS)
    commands = job_commands.build_job_commands(job)

    repo_path = repository.build_repo_path(job.destination_label, job.name)
    for command in commands:
        if not command["runs"] and command["command"] is None:
            continue
        assert command["env"]["RESTIC_REPOSITORY"] == repo_path
        assert command["env"]["RESTIC_PASSWORD"] != PASSWORD

    assert PASSWORD not in json.dumps(commands)


async def test_parent_snapshot_id_is_shown_as_a_placeholder():
    """The parent is resolved at run time, so the preview marks it instead of
    inventing an id — but it must still show that --parent is passed, since
    that is what keeps a backup incremental."""
    step = _by_step(job_commands.build_job_commands(_make_job()))["backup"]

    assert "--parent" in step["argv"]
    assert job_commands.PARENT_SNAPSHOT_PLACEHOLDER in step["argv"]
    assert step["condition"]


# ── the runner really uses the same inputs the preview was built from ────────


async def _seed_run(engine, job_id: uuid.UUID) -> str:
    from app.db.models import BackupRun, RunStatus, TriggeredBy

    run_id = str(uuid.uuid4())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            BackupRun(
                id=run_id,
                job_id=str(job_id),
                status=RunStatus.running,
                triggered_by=TriggeredBy.manual,
                started_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
    return run_id


async def test_run_backup_uses_the_options_and_retention_the_preview_shows(engine):
    """The other half of the contract: the pipeline must hand the restic
    wrappers exactly the inputs the preview was rendered from."""
    from app.db.models import AppSettings

    job_id = uuid.uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(AppSettings(id=1, ntfy_server_url="https://ntfy.sh", ntfy_topic=""))
        s.add(
            BackupJob(
                id=str(job_id),
                name="Photos",
                source_label="pictures",
                destination_label="main",
                restic_password=PASSWORD,
                schedule_type=ScheduleType.interval,
                schedule_value="6h",
                enabled=True,
                **FULL_OPTIONS,
                **FULL_RETENTION,
            )
        )
        await s.commit()
        job = await s.get(BackupJob, str(job_id))

    run_id = await _seed_run(engine, job_id)
    captured: Dict[str, Any] = {}

    async def fake_backup(*args: Any, **kwargs: Any):
        captured["backup"] = (args, kwargs)
        summary = {"message_type": "summary", "snapshot_id": "a" * 64}
        return (0, json.dumps(summary), "", summary)

    async def fake_forget(*args: Any, **kwargs: Any):
        captured["forget"] = (args, kwargs)
        return (0, "", "")

    with (
        patch("app.services.restic.restic_cat_config", return_value=(0, "{}", "")),
        patch("app.services.restic.restic_unlock", return_value=(0, "", "")),
        patch("app.services.restic.restic_backup", side_effect=fake_backup),
        patch("app.services.restic.restic_forget", side_effect=fake_forget),
        patch("app.services.backup_runner.send_notification"),
    ):
        await backup_runner.run_backup(job_id, uuid.UUID(run_id))

    backup_args, backup_kwargs = captured["backup"]
    assert backup_args[2] == backup_runner.build_source_path(
        job.source_label, job.source_subpath
    )
    options = {
        k: v
        for k, v in backup_kwargs.items()
        if k in backup_runner.BACKUP_OPTION_FIELDS
    }
    assert options == backup_runner.build_backup_kwargs(job)

    _, forget_kwargs = captured["forget"]
    retention = {
        k: v for k, v in forget_kwargs.items() if k in backup_runner.RETENTION_FIELDS
    }
    assert retention == backup_runner.build_retention_kwargs(job)


# ── the preview covers every command that is actually spawned ────────────────
#
# The tests above pin the argv of the steps the preview *has*. They cannot
# catch the opposite failure: someone adds a sixth restic call to run_backup
# (or a new mode to the check dialog) and forgets job_commands.py, leaving the
# page accurate but incomplete — which reads exactly like "these are all the
# commands" to an operator. The two tests below run the real pipelines with
# `asyncio.create_subprocess_exec` patched, collect every argv that was
# actually spawned, and compare that set against the preview. Add a restic
# call anywhere in a run and one of them fails until the preview lists it.
#
# The only tokens normalized away are the two the preview cannot know ahead of
# time: the parent snapshot id (resolved by the lookup command) and the
# read-data-subset percentage (typed into the check dialog).

# The autouse `_mock_restic_latest_snapshot_id` fixture stubs the parent lookup
# for every module but test_restic — which would hide that command from the
# spawn recorder. Captured at import time, before any fixture runs, so these
# tests can put the real implementation back.
REAL_LATEST_SNAPSHOT_ID = restic.restic_latest_snapshot_id

PARENT_ID = "b" * 64
_SUBSET_FLAG_RE = re.compile(r"^--read-data-subset=\d+%$")


def _normalize(argv: List[str]) -> tuple:
    """Replace run-time-resolved values with the placeholders the preview uses."""
    out: List[str] = []
    for index, token in enumerate(argv):
        if index > 0 and argv[index - 1] == "--parent":
            out.append(job_commands.PARENT_SNAPSHOT_PLACEHOLDER)
        elif _SUBSET_FLAG_RE.match(token):
            out.append(
                f"--read-data-subset={job_commands.CHECK_SUBSET_PERCENT_PLACEHOLDER}%"
            )
        else:
            out.append(token)
    return tuple(out)


def _recording_spawn(recorded: List[List[str]]):
    """Stand-in for create_subprocess_exec that records argv and answers each
    restic subcommand with output the wrapper can parse."""

    async def spawn(*args: Any, **kwargs: Any) -> AsyncMock:
        # Recorded verbatim, never str()-ed: a job loaded from the DB hands the
        # builder `CompressionMode.max`, a str subclass whose str() is
        # "CompressionMode.max" but whose value — the bytes the kernel actually
        # receives, and what shlex.join/JSON render — is "max". Stringifying
        # here would invent a mismatch that does not exist in production (and
        # hide one that did).
        argv = list(args)
        recorded.append(argv)
        if argv[:3] == ["restic", "cat", "config"]:
            return _make_process(0, stdout="{}")
        if argv[:2] == ["restic", "snapshots"]:
            return _make_process(0, stdout=json.dumps([{"id": PARENT_ID}]))
        if argv[:2] == ["restic", "backup"]:
            summary = {"message_type": "summary", "snapshot_id": "a" * 64}
            return _make_process(0, stdout=json.dumps(summary))
        return _make_process(0)

    return spawn


def _preview_argv(job: BackupJob, group: str, **kwargs: Any) -> set:
    return {
        _normalize(c["argv"])
        for c in job_commands.build_job_commands(job, **kwargs)
        if c["group"] == group and c["runs"]
    }


async def _seed_job(engine, **overrides: Any) -> BackupJob:
    from app.db.models import AppSettings

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(AppSettings(id=1, ntfy_server_url="https://ntfy.sh", ntfy_topic=""))
        job = BackupJob(
            id=str(uuid.uuid4()),
            name="Photos",
            source_label="pictures",
            destination_label="main",
            restic_password=PASSWORD,
            schedule_type=ScheduleType.interval,
            schedule_value="6h",
            enabled=True,
            **overrides,
        )
        s.add(job)
        await s.commit()
        return job


async def test_a_backup_run_spawns_exactly_the_commands_the_preview_lists(engine):
    job = await _seed_job(engine, **FULL_OPTIONS, **FULL_RETENTION)
    job_uuid = uuid.UUID(job.id)
    run_id = await _seed_run(engine, job_uuid)
    recorded: List[List[str]] = []

    with (
        patch("app.services.restic.restic_latest_snapshot_id", REAL_LATEST_SNAPSHOT_ID),
        patch("asyncio.create_subprocess_exec", side_effect=_recording_spawn(recorded)),
        patch("app.services.backup_runner.send_notification"),
    ):
        await backup_runner.run_backup(job_uuid, uuid.UUID(run_id))

    spawned = {_normalize(argv) for argv in recorded}
    assert spawned, "the pipeline spawned nothing — the test is not exercising it"
    assert spawned == _preview_argv(job, job_commands.GROUP_BACKUP_RUN)


async def test_a_backup_run_without_retention_spawns_what_that_preview_lists(engine):
    """The forget command is absent from both sides when nothing is retained."""
    job = await _seed_job(engine)
    job_uuid = uuid.UUID(job.id)
    run_id = await _seed_run(engine, job_uuid)
    recorded: List[List[str]] = []

    with (
        patch("app.services.restic.restic_latest_snapshot_id", REAL_LATEST_SNAPSHOT_ID),
        patch("asyncio.create_subprocess_exec", side_effect=_recording_spawn(recorded)),
        patch("app.services.backup_runner.send_notification"),
    ):
        await backup_runner.run_backup(job_uuid, uuid.UUID(run_id))

    spawned = {_normalize(argv) for argv in recorded}
    assert not any(argv[:2] == ["restic", "forget"] for argv in recorded)
    assert spawned == _preview_argv(job, job_commands.GROUP_BACKUP_RUN)


async def test_the_on_demand_actions_spawn_exactly_the_commands_the_preview_lists(
    engine, client
):
    """Prune, the three integrity-check modes and Unlock are button-triggered,
    not part of a backup — but they are still this job's restic commands, so
    the preview lists them and this pins that set too."""
    job = await _seed_job(engine)
    job_uuid = uuid.UUID(job.id)
    recorded: List[List[str]] = []

    with (
        patch("asyncio.create_subprocess_exec", side_effect=_recording_spawn(recorded)),
        patch("app.services.backup_runner.send_notification"),
    ):
        await backup_runner.run_prune(
            job_uuid, uuid.UUID(await _seed_run(engine, job_uuid))
        )
        for mode, percent in (("structural", None), ("subset", 5), ("full", None)):
            await backup_runner.run_check(
                job_uuid, uuid.UUID(await _seed_run(engine, job_uuid)), mode, percent, 1
            )
        resp = await client.post(f"/api/jobs/{job.id}/unlock")
    assert resp.status_code == 200

    spawned = {_normalize(argv) for argv in recorded}
    assert spawned == _preview_argv(job, job_commands.GROUP_ON_DEMAND)


async def test_on_demand_commands_are_grouped_apart_from_the_backup_run():
    job = _make_job()
    commands = job_commands.build_job_commands(job)

    groups = {c["step"]: c["group"] for c in commands}
    assert groups["backup"] == job_commands.GROUP_BACKUP_RUN
    assert groups["prune"] == job_commands.GROUP_ON_DEMAND
    assert groups["unlock_manual"] == job_commands.GROUP_ON_DEMAND
    for step in ("check_structural", "check_subset", "check_full"):
        assert groups[step] == job_commands.GROUP_ON_DEMAND

    # Every on-demand entry has to name the button that issues it — the whole
    # point of the split is that these never happen on a schedule.
    for command in commands:
        if command["group"] == job_commands.GROUP_ON_DEMAND:
            assert command["condition"]


async def test_on_demand_commands_are_the_ones_the_buttons_run():
    steps = _by_step(job_commands.build_job_commands(_make_job()))

    assert steps["prune"]["argv"] == restic.build_prune_args()
    assert steps["unlock_manual"]["argv"] == restic.build_unlock_args()
    assert steps["check_structural"]["argv"] == restic.build_check_args(
        "structural", None
    )
    assert steps["check_full"]["argv"] == restic.build_check_args("full", None)
    assert steps["check_subset"]["argv"] == restic.build_check_args(
        "subset", job_commands.CHECK_SUBSET_PERCENT_PLACEHOLDER
    )
    assert (
        job_commands.CHECK_SUBSET_PERCENT_PLACEHOLDER
        in steps["check_subset"]["command"]
    )


# ── GET /api/jobs/{id}/commands ──────────────────────────────────────────────


async def _create_job(client, **overrides) -> Dict[str, Any]:
    payload = make_job_payload(**overrides)
    with patch("os.path.isdir", return_value=True):
        resp = await client.post("/api/jobs", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_get_commands_returns_the_pipeline_for_the_job(client):
    job = await _create_job(client, name="Photos", destination_label="main")

    resp = await client.get(f"/api/jobs/{job['id']}/commands")

    assert resp.status_code == 200
    body = resp.json()
    assert [c["step"] for c in body if c["group"] == job_commands.GROUP_BACKUP_RUN] == [
        "verify_repository",
        "unlock",
        "parent_lookup",
        "backup",
        "retention",
    ]
    backup = _by_step(body)["backup"]
    assert backup["command"].startswith("restic backup ")
    assert backup["env"]["RESTIC_REPOSITORY"] == "/destinations/main/Photos"
    for command in body:
        assert command["title"]
        assert command["description"]


async def test_get_commands_renders_enum_backed_options_by_value(client):
    """`compression` is a str-Enum on a job loaded from the DB. It must reach
    the client as `--compression max` — the value restic is given — and never
    as the member's repr."""
    job = await _create_job(client, compression="max")

    body = (await client.get(f"/api/jobs/{job['id']}/commands")).json()
    backup = _by_step(body)["backup"]

    assert "--compression max" in backup["command"]
    assert "CompressionMode" not in backup["command"]
    assert "max" in backup["argv"]


async def test_get_commands_lists_the_button_triggered_commands_separately(client):
    job = await _create_job(client)

    body = (await client.get(f"/api/jobs/{job['id']}/commands")).json()
    on_demand = {
        c["step"]: c for c in body if c["group"] == job_commands.GROUP_ON_DEMAND
    }

    assert on_demand["prune"]["command"] == "restic prune"
    assert on_demand["check_structural"]["command"] == "restic check"
    assert on_demand["check_full"]["command"] == "restic check --read-data"
    assert on_demand["unlock_manual"]["command"] == "restic unlock"
    assert "--read-data-subset=" in on_demand["check_subset"]["command"]
    # Each one has to name the user action that issues it, so the page can't
    # read as "this is what your schedule does".
    for command in on_demand.values():
        condition = command["condition"].lower()
        assert "click" in condition or "dialog" in condition


async def test_get_commands_never_returns_the_repository_password(client):
    job = await _create_job(client, restic_password="hunter2-very-secret")

    resp = await client.get(f"/api/jobs/{job['id']}/commands")

    assert resp.status_code == 200
    assert "hunter2-very-secret" not in resp.text


async def test_get_commands_404_for_unknown_job(client):
    resp = await client.get(f"/api/jobs/{uuid.uuid4()}/commands")
    assert resp.status_code == 404


async def test_get_commands_reflects_a_job_update(client):
    """The preview is rebuilt from the stored job on every fetch, so editing
    the job changes the commands — the whole point of not caching it."""
    job = await _create_job(client)

    before = _by_step((await client.get(f"/api/jobs/{job['id']}/commands")).json())
    assert before["retention"]["runs"] is False
    assert "--exclude" not in before["backup"]["command"]

    with patch("os.path.isdir", return_value=True):
        resp = await client.put(
            f"/api/jobs/{job['id']}",
            json={"exclude_patterns": ["*.iso"], "retain_keep_daily": 7},
        )
    assert resp.status_code == 200, resp.text

    after = _by_step((await client.get(f"/api/jobs/{job['id']}/commands")).json())
    assert "--exclude '*.iso'" in after["backup"]["command"]
    assert after["retention"]["runs"] is True
    assert "--keep-daily 7" in after["retention"]["command"]


async def test_get_commands_follows_the_auto_unlock_setting(client):
    job = await _create_job(client)

    resp = await client.put(
        "/api/settings",
        json={"ntfy_server_url": "https://ntfy.sh", "auto_unlock": False},
    )
    assert resp.status_code == 200, resp.text

    body = _by_step((await client.get(f"/api/jobs/{job['id']}/commands")).json())
    assert body["unlock"]["runs"] is False
