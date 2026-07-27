"""Pin the restic facts the mocked suite assumes.

Every other restic test patches `asyncio.create_subprocess_exec` and feeds our
parsers a hand-written string. That is the right trade — it is fast,
deterministic, and it can stage things a real binary will not produce on demand
(a five-hour output stream, a hung backend, a stale lock). But it leaves the
mocks as the only description of restic in the repo, and nothing was checking
that description against reality.

It was wrong. `test_snapshot_listing` invented a top-level `total_size` key,
asserted `size_bytes` mapped from it, and passed — while production returned
`None` on every call and the UI's Size column was permanently blank, because
restic emits no such field.

So this module does two things, both without needing restic installed:

1. Runs the real parsers over **verbatim recorded restic output**
   (`fixtures/restic_0_19_1/`, see its PROVENANCE.md) — so a field the app reads
   must actually exist in restic's output, not just in a fixture.
2. Checks the hand-written fixtures in the other test modules against those
   recordings: a fixture may **omit** keys restic emits (tests need not be
   exhaustive) but may never **invent** one restic does not emit.

On a restic version bump: re-capture the fixtures per PROVENANCE.md and run this
module. What fails here is what the bump actually changed for us.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.services import repository
from app.services.restic import _newest_snapshot, _parse_snapshot_time
from app.services.restic_stream import BackupOutputCollector, format_progress
from app.services.run_output import extract_failed_items
from app.services.snapshot_listing import _normalize

FIXTURES = Path(__file__).parent / "fixtures" / "restic_0_19_1"

# The version these recordings came from. Bumping the image without re-capturing
# should be a visible act, not a silent drift.
RECORDED_VERSION = "0.19.1"


def read(name: str) -> str:
    return (FIXTURES / name).read_text()


def read_json(name: str) -> Any:
    return json.loads(read(name))


def jsonl(text: str) -> List[Dict[str, Any]]:
    """Parse the JSON objects out of a captured stream, ignoring plain-text lines
    (restic mixes human warnings into stderr alongside its JSON)."""
    out: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


def summary_line(stream: str) -> Dict[str, Any]:
    for obj in jsonl(stream):
        if obj.get("message_type") == "summary":
            return obj
    raise AssertionError("no summary line in captured stream")


# ── The recordings are what they claim to be ──────────────────────────────────


def test_recordings_come_from_the_pinned_restic_version():
    """Guards against the fixtures silently ageing past the shipped binary."""
    assert read_json("version.json")["version"] == RECORDED_VERSION
    assert RECORDED_VERSION in read("version.txt")


def test_recorded_version_matches_the_dockerfile_floor():
    """The image's RESTIC_VERSION and these recordings must describe the same
    binary — otherwise this whole module is validating the wrong contract."""
    dockerfile = (Path(__file__).parent.parent.parent / "Dockerfile").read_text()
    assert f"ARG RESTIC_VERSION={RECORDED_VERSION}" in dockerfile, (
        "Dockerfile RESTIC_VERSION and tests/fixtures/restic_0_19_1 disagree — "
        "re-capture the fixtures (see PROVENANCE.md) after a version bump"
    )


# ── Exit codes the pipeline branches on ───────────────────────────────────────


def test_repo_not_found_and_wrong_password_stderr_are_distinguishable():
    """We branch on rc, never on message text (gaps.md H5) — but the recorded
    text documents *why* rc is the only reliable signal: both are 'Fatal:' lines
    with wording restic is free to change."""
    assert "repository does not exist" in read("cat_config_rc10.stderr")
    assert "wrong password" in read("cat_config_rc12.stderr")


def test_rc_constants_match_the_recorded_exit_codes():
    assert repository.RESTIC_RC_REPO_NOT_FOUND == 10
    assert repository.RESTIC_RC_WRONG_PASSWORD == 12
    assert repository.RESTIC_RC_LOCK_FAILED == 11


def test_all_source_paths_missing_is_a_fatal_not_a_partial_backup():
    """restic 0.19.0 started exiting 3 when *some* source path is missing. That
    is only reachable with multiple sources; `restic_backup` passes exactly one
    (see test_backup_runner's single-source invariant), and the single-missing
    case is still a plain fatal — which the runner classifies as `failed`, not a
    `warning` with retention applied afterwards.
    """
    stderr = read("backup_missing_source.stderr")
    exit_errors = [o for o in jsonl(stderr) if o.get("message_type") == "exit_error"]
    assert exit_errors, "expected an exit_error line"
    assert exit_errors[0]["code"] == 1
    assert "do not exist" in exit_errors[0]["message"]
    # No snapshot was written, so no summary line exists to read stats from —
    # stdout was empty in the capture, which is why only stderr is recorded.
    assert not [o for o in jsonl(stderr) if o.get("message_type") == "summary"]


# ── The stream split that `extract_failed_items` depends on ──────────────────


def test_partial_backup_puts_error_lines_on_stderr_and_summary_on_stdout():
    """The single most load-bearing assumption in the codebase. Parsing stdout
    alone made every rc=3 run report zero failed items."""
    stdout, stderr = read("backup_rc3.stdout"), read("backup_rc3.stderr")

    stdout_types = {o.get("message_type") for o in jsonl(stdout)}
    stderr_types = {o.get("message_type") for o in jsonl(stderr)}

    assert "summary" in stdout_types, "summary must be on stdout"
    assert "error" not in stdout_types, "error lines are NOT on stdout"
    assert "error" in stderr_types, "error lines must be on stderr"


def test_extract_failed_items_parses_the_real_stderr():
    """Run the real parser over recorded bytes: it must name the path, and it
    must collapse the duplicate the scanner and archiver both report."""
    items = extract_failed_items(read("backup_rc3.stderr"), read("backup_rc3.stdout"))

    assert len(items) == 1, f"duplicate scan/archival report not collapsed: {items}"
    assert "/sources/partial/secret" in items[0]
    assert "permission denied" in items[0]


def test_recorded_error_line_shape_is_what_the_parser_expects():
    """If restic moved the path out of `item` or nested the message differently,
    extract_failed_items would silently yield nothing useful."""
    errors = [
        o for o in jsonl(read("backup_rc3.stderr")) if o["message_type"] == "error"
    ]
    assert errors
    for err in errors:
        assert isinstance(err["item"], str)
        assert isinstance(err["error"]["message"], str)
        assert err["during"] in {"scan", "archival"}


def test_partial_backup_still_creates_a_snapshot():
    """rc=3 is success-with-warnings precisely because the snapshot exists; if
    that stopped being true, `backup_success = True` on rc=3 would be wrong."""
    assert summary_line(read("backup_rc3.stdout"))["snapshot_id"]


# ── Every field the app reads must exist in real output ───────────────────────

# What backup_runner's stats step pulls off the summary line.
_SUMMARY_FIELDS_READ_BY_APP = (
    "files_new",
    "files_changed",
    "files_unmodified",
    "dirs_new",
    "dirs_changed",
    "dirs_unmodified",
    "data_added",
    "data_added_packed",
    "total_bytes_processed",
    "snapshot_id",
)


@pytest.mark.parametrize("field", _SUMMARY_FIELDS_READ_BY_APP)
def test_summary_field_the_app_reads_exists_in_real_output(field):
    """A stats column silently stuck at NULL is the failure mode here — exactly
    what happened to snapshot size."""
    assert field in summary_line(read("backup_rc0.stdout"))


def test_summary_has_no_field_the_app_reads_under_a_different_name():
    """0.19.0 added backup_start/backup_end. Additive keys are fine (the stats
    step uses .get()), but record them so a future rename is visible here."""
    recorded = set(summary_line(read("backup_rc0.stdout")))
    assert {"backup_start", "backup_end", "total_duration"} <= recorded


@pytest.mark.parametrize(
    "field", ("percent_done", "files_done", "total_files", "bytes_done", "total_bytes")
)
def test_progress_field_the_app_reads_exists_in_a_real_status_line(field):
    assert field in read_json("backup_status_line.json")


def test_format_progress_renders_a_real_status_line():
    """The parser must produce something an operator can read from real bytes,
    not just from a fixture shaped to suit it.

    Expectations are derived from the recording rather than hard-coded: the exact
    percentage and byte count depend on when during the backup the line happened
    to be captured, so literals here would break on every re-capture and teach
    whoever bumps restic to edit the assertion instead of reading it.
    """
    status = read_json("backup_status_line.json")
    rendered = format_progress(status)

    assert rendered.startswith("progress: ")
    assert f"{status['percent_done'] * 100:.0f}%" in rendered
    assert f"{status['files_done']:,}/{status['total_files']:,} files" in rendered
    # Byte counts are rendered in binary units, never raw.
    assert str(status["total_bytes"]) not in rendered
    assert any(unit in rendered for unit in ("B", "KiB", "MiB", "GiB"))
    assert "eta" in rendered, "seconds_remaining is present in the recording"


def test_collector_classifies_a_real_backup_stream():
    """End-to-end over recorded stdout: the summary is captured, and the status
    firehose is collapsed to one progress line rather than retained."""
    stdout = read("backup_progress.stdout")
    status_lines = [o for o in jsonl(stdout) if o.get("message_type") == "status"]
    assert len(status_lines) > 1, "recording must contain the progress firehose"

    collector = BackupOutputCollector(password="s3cret-pw")
    for line in stdout.splitlines():
        collector.feed(line)

    assert collector.summary is not None
    assert collector.summary["message_type"] == "summary"
    # Many status lines in, exactly one human progress line out — none retained.
    assert '"message_type":"status"' not in collector.text()
    assert collector.text().count("progress: ") == 1


# ── `restic snapshots --json` shape ───────────────────────────────────────────


def test_snapshot_record_has_no_top_level_total_size():
    """The regression this module exists for. restic reports snapshot size at
    `summary.total_bytes_processed`; there has never been a top-level
    `total_size`. A fixture invented one, so `size_bytes` was asserted working
    while every real response carried null."""
    for snap in read_json("snapshots.json"):
        assert "total_size" not in snap
        assert "total_size" not in snap.get("summary", {})


@pytest.mark.parametrize("field", ("id", "time", "hostname", "paths"))
def test_snapshot_field_the_app_reads_exists_in_real_output(field):
    for snap in read_json("snapshots.json"):
        assert field in snap


def test_normalize_maps_a_real_snapshot_record():
    """`_normalize` over recorded bytes — including the size, which is the field
    that was broken."""
    raw = read_json("snapshots.json")[0]
    out = _normalize(raw)

    assert out["snapshot_id"] == raw["id"]
    assert out["snapshot_time"] == raw["time"]
    assert out["hostname"] == "billa-gates"
    assert out["paths"] == raw["paths"]
    assert out["tags"] == ["daily", "important"]
    assert out["size_bytes"] == raw["summary"]["total_bytes_processed"], (
        "snapshot size must come from summary.total_bytes_processed — restic has "
        "no top-level total_size, and reading one yields a permanently blank "
        "Size column in the UI"
    )
    assert out["size_bytes"] > 0


def test_normalize_survives_a_real_record_without_tags():
    """restic omits `tags` entirely when a snapshot has none — it does not send
    null. The second recorded snapshot is such a record."""
    untagged = read_json("snapshots.json")[1]
    assert "tags" not in untagged, "fixture no longer covers the untagged case"

    out = _normalize(untagged)
    assert out["tags"] is None
    assert out["size_bytes"] == untagged["summary"]["total_bytes_processed"]


def test_normalize_survives_a_pre_0_17_record_without_a_summary():
    """Snapshots written by restic < 0.17 carry no `summary` block at all, and a
    repo adopted from an older install can hold them. Size is unknown, not a
    crash."""
    ancient = {
        "id": "c" * 64,
        "time": "2024-01-01T00:00:00Z",
        "hostname": "billa-gates",
        "paths": ["/sources/documents"],
    }
    out = _normalize(ancient)
    assert out["size_bytes"] is None
    assert out["snapshot_id"] == ancient["id"]


def test_latest_flag_groups_by_host_and_paths_returning_one_per_group():
    """`--latest 1` is not "the newest snapshot" — it is the newest *per
    host+paths group* unless --group-by says otherwise. 0.19.0 broke this and
    0.19.1 restored it.

    The parent lookup therefore picks by timestamp rather than by position
    (`_newest_snapshot`); this recording only has one group, so it pins the
    agreement between selector and reality, and
    tests/test_restic.py stages the multi-group case the recording cannot.
    """
    latest = read_json("snapshots_latest.json")
    all_snaps = read_json("snapshots.json")

    # Both recorded snapshots share host+paths, so --latest 1 collapses to one.
    assert len({(s["hostname"], tuple(s["paths"])) for s in all_snaps}) == 1
    assert len(latest) == 1
    assert latest[0]["time"] == max(s["time"] for s in all_snaps)

    # The real selector, over the real bytes: same answer restic itself gives.
    chosen = _newest_snapshot(latest)
    assert chosen is not None
    assert chosen["id"] == latest[0]["id"]
    assert _parse_snapshot_time(chosen["time"]) is not None, (
        "restic's timestamp format must stay parseable — the parent lookup "
        "orders by instant, and an unparseable stamp silently degrades it to "
        "restic's row order"
    )


def test_cat_config_reports_repository_format_version_2():
    """A repo-format bump would mean a migration, not just a version bump."""
    assert read_json("cat_config.json")["version"] == 2


# ── The hand-written fixtures must not contradict the recordings ──────────────


def _restic_summary_keys() -> set:
    return set(summary_line(read("backup_rc0.stdout")))


def _restic_snapshot_keys() -> set:
    keys: set = set()
    for snap in read_json("snapshots.json"):
        keys |= set(snap)
    return keys


def test_hand_written_backup_summary_invents_no_field():
    """`test_restic.BACKUP_SUMMARY` stands in for a real summary line in ~40
    tests. It may omit keys; it may not make them up."""
    from tests.test_restic import BACKUP_SUMMARY

    fixture_keys = set(json.loads(BACKUP_SUMMARY))
    invented = fixture_keys - _restic_summary_keys()
    assert not invented, (
        f"BACKUP_SUMMARY has keys restic 0.19.1 never emits: {sorted(invented)}"
    )


@pytest.mark.parametrize(
    "module_name,attr",
    (
        ("tests.test_integration_backup_lifecycle", "_BACKUP_SUMMARY"),
        ("tests.test_backup_runner", "BACKUP_SUMMARY"),
    ),
)
def test_hand_written_summary_dicts_invent_no_field(module_name, attr):
    """The runner and integration suites use dict fixtures rather than JSON
    strings; same rule applies."""
    import importlib

    fixture = getattr(importlib.import_module(module_name), attr)
    invented = set(fixture) - _restic_summary_keys()
    assert not invented, (
        f"{module_name}.{attr} has keys restic 0.19.1 never emits: {sorted(invented)}"
    )


def test_hand_written_snapshot_fixture_invents_no_field():
    """This is the assertion that would have caught the `total_size` bug on the
    day the fixture was written."""
    from tests.test_snapshot_listing import _RESTIC_SNAPSHOTS_JSON

    for snap in json.loads(_RESTIC_SNAPSHOTS_JSON):
        invented = set(snap) - _restic_snapshot_keys()
        assert not invented, (
            f"snapshot fixture has keys restic 0.19.1 never emits: "
            f"{sorted(invented)} — the app cannot read a field restic does not send"
        )


@pytest.mark.parametrize("fixture_name", ("RC3_STDERR", "RC3_STDERR_DOUBLE_REPORTED"))
def test_hand_written_partial_backup_stderr_matches_the_recorded_shape(fixture_name):
    """test_backup_runner's rc=3 fixtures claim to be a capture. Hold them to it:
    the error lines must carry the same keys the recording does, or the parser
    they exercise is being fed a shape restic never sends."""
    import tests.test_backup_runner as trb

    fixture = getattr(trb, fixture_name)
    recorded = [
        o for o in jsonl(read("backup_rc3.stderr")) if o["message_type"] == "error"
    ]
    recorded_keys = set(recorded[0])

    errors = [o for o in jsonl(fixture) if o.get("message_type") == "error"]
    assert errors, "rc=3 stderr fixture must carry message_type=error lines"
    for err in errors:
        assert set(err) == recorded_keys, (
            f"{fixture_name} error line keys {sorted(set(err))} differ from the "
            f"recorded {sorted(recorded_keys)}"
        )
        assert "message" in err["error"]
        assert err["during"] in {"scan", "archival"}

    # And the exit_error that closes the stream.
    exits = [o for o in jsonl(fixture) if o.get("message_type") == "exit_error"]
    assert exits and exits[0]["code"] == 3


def test_hand_written_rc3_fixture_is_parsed_the_same_way_as_the_recording():
    """The fixture and the real capture must drive `extract_failed_items` to the
    same conclusion — one named item, deduplicated."""
    from tests.test_backup_runner import RC3_STDERR_DOUBLE_REPORTED

    from_fixture = extract_failed_items(RC3_STDERR_DOUBLE_REPORTED)
    from_recording = extract_failed_items(read("backup_rc3.stderr"))

    assert len(from_fixture) == len(from_recording) == 1
    assert "permission denied" in from_fixture[0]
    assert "permission denied" in from_recording[0]


def test_snapshot_fixtures_do_not_carry_a_per_job_tag():
    """Snapshots must never be tagged with job identity (CLAUDE.md: the repo is
    the scope). A fixture showing `job:<uuid>` tags describes an app that does
    not exist and invites someone to start filtering on it."""
    from tests.test_snapshot_listing import _RESTIC_SNAPSHOTS_JSON

    for snap in json.loads(_RESTIC_SNAPSHOTS_JSON):
        for tag in snap.get("tags") or []:
            assert not tag.startswith("job:"), (
                f"fixture tag {tag!r} implies per-job snapshot tagging, which "
                f"restic_backup deliberately does not do"
            )
