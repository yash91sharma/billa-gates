"""Datetime serialization tests for API response schemas.

The backend stores naive UTC datetimes (see ``app.db.models._utcnow``).
When those values cross the API boundary, the JSON payload MUST mark
them as UTC (trailing ``Z``) so the browser's ``new Date(...)`` parses
the instant correctly and ``toLocaleString()`` renders in the user's
local timezone.

Without the ``Z`` suffix, JS interprets a no-tz ISO string as LOCAL
time, which is why a PST user saw a UTC clock value rendered verbatim
as PST.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.api.schemas.jobs import JobResponse, RunSummarySchema, SnapshotResponse
from app.api.schemas.runs import RunDetailSchema


def _base_run_payload(**overrides):
    payload = {
        "id": "run-1",
        "job_id": "job-1",
        "status": "success",
        "triggered_by": "manual",
        "started_at": datetime(2026, 5, 26, 18, 37, 22),
    }
    payload.update(overrides)
    return payload


def _base_job_payload(**overrides):
    payload = {
        "id": "job-1",
        "name": "test",
        "source_label": "src",
        "destination_label": "dst",
        "schedule_type": "interval",
        "schedule_value": "1h",
        "enabled": True,
        "created_at": datetime(2026, 5, 26, 18, 37, 22),
        "updated_at": datetime(2026, 5, 26, 18, 37, 22),
    }
    payload.update(overrides)
    return payload


def _base_snapshot_payload(**overrides):
    payload = {
        "snapshot_id": "abc123",
        "snapshot_time": datetime(2026, 5, 26, 18, 37, 22),
        "hostname": "host",
        "paths": ["/sources/foo"],
    }
    payload.update(overrides)
    return payload


class TestRunSummarySerialization:
    def test_naive_started_at_gets_z_suffix(self):
        run = RunSummarySchema(**_base_run_payload())
        data = run.model_dump(mode="json")
        assert data["started_at"] == "2026-05-26T18:37:22Z"

    def test_tz_aware_utc_started_at_gets_z_suffix(self):
        run = RunSummarySchema(
            **_base_run_payload(
                started_at=datetime(2026, 5, 26, 18, 37, 22, tzinfo=timezone.utc)
            )
        )
        data = run.model_dump(mode="json")
        assert data["started_at"] == "2026-05-26T18:37:22Z"

    def test_tz_aware_non_utc_is_normalized_to_utc_with_z(self):
        # America/Los_Angeles is UTC-7 in May (PDT). 11:37 PDT == 18:37 UTC.
        pdt = timezone(timedelta(hours=-7))
        run = RunSummarySchema(
            **_base_run_payload(
                started_at=datetime(2026, 5, 26, 11, 37, 22, tzinfo=pdt)
            )
        )
        data = run.model_dump(mode="json")
        assert data["started_at"] == "2026-05-26T18:37:22Z"

    def test_finished_at_none_stays_none(self):
        run = RunSummarySchema(**_base_run_payload(finished_at=None))
        data = run.model_dump(mode="json")
        assert data["finished_at"] is None

    def test_finished_at_naive_gets_z_suffix(self):
        run = RunSummarySchema(
            **_base_run_payload(finished_at=datetime(2026, 5, 26, 19, 0, 0))
        )
        data = run.model_dump(mode="json")
        assert data["finished_at"] == "2026-05-26T19:00:00Z"

    def test_non_datetime_fields_unaffected(self):
        run = RunSummarySchema(**_base_run_payload(status="failed"))
        data = run.model_dump(mode="json")
        assert data["status"] == "failed"
        assert data["id"] == "run-1"


class TestRunDetailSerialization:
    def test_run_detail_inherits_z_suffix_behavior(self):
        run = RunDetailSchema(**_base_run_payload(backup_output="ok"))
        data = run.model_dump(mode="json")
        assert data["started_at"] == "2026-05-26T18:37:22Z"
        assert data["backup_output"] == "ok"


class TestJobResponseSerialization:
    def test_created_and_updated_get_z_suffix(self):
        job = JobResponse(**_base_job_payload())
        data = job.model_dump(mode="json")
        assert data["created_at"] == "2026-05-26T18:37:22Z"
        assert data["updated_at"] == "2026-05-26T18:37:22Z"

    def test_next_run_time_none_stays_none(self):
        job = JobResponse(**_base_job_payload(next_run_time=None))
        data = job.model_dump(mode="json")
        assert data["next_run_time"] is None

    def test_next_run_time_naive_gets_z_suffix(self):
        job = JobResponse(
            **_base_job_payload(next_run_time=datetime(2026, 5, 26, 20, 0, 0))
        )
        data = job.model_dump(mode="json")
        assert data["next_run_time"] == "2026-05-26T20:00:00Z"


class TestSnapshotResponseSerialization:
    def test_snapshot_time_naive_gets_z_suffix(self):
        snap = SnapshotResponse(**_base_snapshot_payload())
        data = snap.model_dump(mode="json")
        assert data["snapshot_time"] == "2026-05-26T18:37:22Z"


class TestPythonModeUnaffected:
    """``model_dump()`` (python mode) returns datetime objects, not strings.

    The Z-suffix conversion is a JSON-mode concern only; in-process callers
    that need a datetime should still get one.
    """

    def test_python_mode_returns_datetime_object(self):
        run = RunSummarySchema(**_base_run_payload())
        data = run.model_dump()
        assert isinstance(data["started_at"], datetime)


@pytest.mark.parametrize(
    "value,expected",
    [
        (datetime(2026, 1, 1, 0, 0, 0), "2026-01-01T00:00:00Z"),
        (datetime(2026, 12, 31, 23, 59, 59), "2026-12-31T23:59:59Z"),
        (
            datetime(2026, 6, 15, 12, 0, 0, 123456),
            "2026-06-15T12:00:00.123456Z",
        ),
    ],
)
def test_naive_isoformat_variants(value, expected):
    run = RunSummarySchema(**_base_run_payload(started_at=value))
    data = run.model_dump(mode="json")
    assert data["started_at"] == expected


# ── Optional datetimes that are actually absent ──────────────────────────────
#
# `_utc_iso`'s None branch was the one line of the module the suite never
# executed. It is not decoration: `finished_at` is null for the whole duration
# of a run, which is precisely when the UI is being watched. A serializer that
# raised on None — or that rendered the string "None" with a Z stapled to it —
# would break the run detail page for every in-flight backup while leaving
# every finished one looking perfect.


def test_finished_at_is_null_while_a_run_is_still_going():
    run = RunSummarySchema(**_base_run_payload(status="running", finished_at=None))

    data = run.model_dump(mode="json")

    assert data["finished_at"] is None


def test_a_null_datetime_is_not_rendered_as_a_string():
    """`null`, not `"None"` / `"NoneZ"` — `new Date("NoneZ")` is Invalid Date."""
    run = RunSummarySchema(**_base_run_payload(status="running", finished_at=None))

    data = run.model_dump(mode="json")

    assert not isinstance(data["finished_at"], str)


def test_a_null_datetime_survives_json_encoding():
    """The serializer runs for real during response encoding, not just dump."""
    import json as _json

    run = RunSummarySchema(**_base_run_payload(status="running", finished_at=None))

    payload = _json.loads(run.model_dump_json())

    assert payload["finished_at"] is None
    assert payload["started_at"].endswith("Z")


def test_a_null_datetime_stays_none_in_python_mode():
    run = RunSummarySchema(**_base_run_payload(status="running", finished_at=None))

    assert run.model_dump()["finished_at"] is None


def test_the_run_detail_schema_handles_a_null_finished_at():
    """RunDetail is the page that is open while a backup is running."""
    detail = RunDetailSchema(**_base_run_payload(status="running", finished_at=None))

    data = detail.model_dump(mode="json")

    assert data["finished_at"] is None
    assert data["started_at"].endswith("Z")


def test_an_aware_non_utc_datetime_is_normalized_to_utc():
    """restic stamps snapshots with the writing machine's local offset.

    A value that reached the schema still carrying `-07:00` must be converted,
    not merely relabelled — stapling a Z onto a local clock reading moves the
    instant by the offset.
    """
    pacific = timezone(timedelta(hours=-7))
    aware = datetime(2026, 5, 17, 3, 0, 0, tzinfo=pacific)

    run = RunSummarySchema(**_base_run_payload(started_at=aware))
    rendered = run.model_dump(mode="json")["started_at"]

    assert rendered == "2026-05-17T10:00:00Z"
