"""The pure output layer: what an operator watching a run actually reads.

These formatters and the bounded collector are the reason `restic_backup` can
consume a 210 MB output stream in O(1) memory (app/services/restic_stream.py).
They take restic's JSON straight from the wire, so every branch has to survive
a field being absent, a bool where a number belongs, and a line that is not
JSON at all.

Nothing here spawns a process or touches a database — which is the point of the
module being separate. `tests/test_restic.py` covers the same code through
`restic_backup`, over a real (mocked) subprocess; these tests reach it directly,
and `tests/test_restic_contract.py` runs it over verbatim recorded restic
output.
"""

import json

import pytest

from app.services.restic_stream import (
    MAX_RETAINED_LINE_CHARS,
    BackupOutputCollector,
    BoundedOutput,
    _format_bytes,
    _format_eta,
    format_progress,
    pump_stream,
)
from tests.conftest import FakeStream
from tests.test_restic import BACKUP_SUMMARY


@pytest.mark.parametrize(
    "num_bytes,expected",
    (
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1024.0 TiB"),  # clamps at the largest unit rather than wrapping
    ),
)
def test_format_bytes_renders_each_unit(num_bytes, expected):
    assert _format_bytes(num_bytes) == expected


@pytest.mark.parametrize("value", (None, "1024", [], {}, True, False))
def test_format_bytes_rejects_non_numbers(value):
    """restic sends JSON; a bool is not a byte count and `isinstance(True, int)`
    is True in Python, so bools must be excluded explicitly."""
    assert _format_bytes(value) is None


@pytest.mark.parametrize(
    "seconds,expected",
    (
        (1, "eta 1s"),
        (59, "eta 59s"),
        (60, "eta 1m"),
        (3599, "eta 59m"),
        (3600, "eta 1h 0m"),
        (7320, "eta 2h 2m"),
        (86400, "eta 24h 0m"),
    ),
)
def test_format_eta_renders_each_magnitude(seconds, expected):
    assert _format_eta(seconds) == expected


@pytest.mark.parametrize("value", (None, 0, -1, "60", True, False, []))
def test_format_eta_omitted_when_unknown_or_invalid(value):
    """restic omits `seconds_remaining` until it has scanned enough to estimate,
    and sends 0 at the end — neither should render as "eta 0s"."""
    assert _format_eta(value) is None


def test_format_progress_on_an_empty_status_line():
    """Better a bare "running" than an empty string the UI renders as a blank
    progress area."""
    assert format_progress({}) == "progress: running"


def test_format_progress_full_line_orders_parts_for_reading():
    rendered = format_progress(
        {
            "message_type": "status",
            "percent_done": 0.4567,
            "files_done": 1234,
            "total_files": 5000,
            "bytes_done": 1024**3,
            "total_bytes": 4 * 1024**3,
            "seconds_remaining": 125,
            "error_count": 3,
        }
    )
    assert rendered == (
        "progress: 46% · 1,234/5,000 files · 1.0 GiB/4.0 GiB · eta 2m · 3 errors"
    )


def test_format_progress_thousands_separators_on_large_counts():
    """A seven-digit file count without separators is unreadable at a glance."""
    rendered = format_progress({"files_done": 1234567, "total_files": 2000000})
    assert "1,234,567/2,000,000 files" in rendered


def test_format_progress_files_done_without_total_when_scan_is_skipped():
    """With `--no-scan` restic never learns the total, so it sends `files_done`
    alone. The line must still render rather than dropping the count."""
    rendered = format_progress({"files_done": 42, "bytes_done": 2048})
    assert "42 files" in rendered
    assert "/" not in rendered.split("files")[0]
    assert "2.0 KiB" in rendered


def test_format_progress_bytes_done_without_total():
    rendered = format_progress({"bytes_done": 5 * 1024**2})
    assert "5.0 MiB" in rendered


@pytest.mark.parametrize(
    "error_count,expected",
    ((1, "1 error"), (2, "2 errors"), (1500, "1,500 errors")),
)
def test_format_progress_pluralises_the_error_tally(error_count, expected):
    """`error_count` matters out of proportion to its size: files that fail during
    the scan never enter `total_files`, so a partial backup can otherwise show a
    spotless 100% next to a `warning` badge."""
    rendered = format_progress({"percent_done": 1.0, "error_count": error_count})
    assert expected in rendered


@pytest.mark.parametrize("error_count", (0, None, False, True, "3"))
def test_format_progress_omits_error_tally_when_absent_or_zero(error_count):
    rendered = format_progress({"percent_done": 0.5, "error_count": error_count})
    assert "error" not in rendered


@pytest.mark.parametrize(
    "status",
    (
        {"percent_done": True},
        {"files_done": True, "total_files": True},
        {"percent_done": "50%"},
        {"files_done": None},
    ),
)
def test_format_progress_ignores_fields_of_the_wrong_type(status):
    """Never raise on restic's output — a malformed status line must degrade to a
    shorter progress line, not abort the run's progress persistence."""
    assert format_progress(status).startswith("progress:")


# ── BoundedOutput ────────────────────────────────────────────────────────────


def test_bounded_output_retains_lines_in_order():
    out = BoundedOutput(password="")
    for line in ("first", "second", "third"):
        out.add(line)
    assert out.text() == "first\nsecond\nthird"


def test_bounded_output_truncates_a_single_oversized_line():
    """One pathological line (a multi-megabyte filename list, a binary blob on
    stderr) must not consume the whole budget."""
    out = BoundedOutput(password="")
    out.add("x" * (MAX_RETAINED_LINE_CHARS * 3))
    text = out.text()
    assert "…<truncated>" in text
    assert len(text) < MAX_RETAINED_LINE_CHARS * 2


def test_bounded_output_drops_past_the_cap_and_says_so():
    """Silently losing output is worse than a short record — the operator has to
    know the log is incomplete."""
    out = BoundedOutput(password="", max_chars=100)
    for i in range(50):
        out.add(f"line-{i:03d} " + "y" * 20)
    text = out.text()
    assert "more output line(s) omitted" in text
    assert "capped at 100 characters" in text


def test_bounded_output_force_bypasses_the_cap():
    """The summary arrives last, after any error flood that may have filled the
    cap, and it drives every stats column — it must survive regardless."""
    out = BoundedOutput(password="", max_chars=50)
    for i in range(20):
        out.add(f"noise-{i}" + "z" * 20)
    out.add("THE-SUMMARY", force=True)
    assert "THE-SUMMARY" in out.text()


def test_bounded_output_scrubs_the_password_per_line():
    """There is no whole-stream string to run a single replace() over any more, so
    scrubbing happens per line — a repo password must never reach the DB row."""
    out = BoundedOutput(password="s3cr3t")
    out.add(out.scrub("connecting with password s3cr3t to repo"))
    text = out.text()
    assert "s3cr3t" not in text
    assert "connecting with password  to repo" == text


def test_bounded_output_scrub_is_a_noop_for_an_empty_password():
    out = BoundedOutput(password="")
    assert out.scrub("nothing to remove") == "nothing to remove"


def test_bounded_output_appends_extra_last():
    """The live progress line is appended at render time so it always reads as the
    newest thing in the record."""
    out = BoundedOutput(password="")
    out.add("earlier")
    assert out.text(extra="progress: 50%") == "earlier\nprogress: 50%"


# ── BackupOutputCollector ────────────────────────────────────────────────────


def test_collector_never_retains_the_status_firehose():
    collector = BackupOutputCollector(password="")
    for pct in (0.1, 0.2, 0.3):
        collector.feed(json.dumps({"message_type": "status", "percent_done": pct}))

    text = collector.text()
    assert "message_type" not in text
    assert text.count("progress:") == 1
    assert "30%" in text, "the newest status line wins"


def test_collector_captures_and_retains_the_summary():
    collector = BackupOutputCollector(password="")
    collector.feed(BACKUP_SUMMARY)

    assert collector.summary is not None
    assert collector.summary["files_new"] == 10
    assert "summary" in collector.text()


def test_collector_retains_error_lines():
    collector = BackupOutputCollector(password="")
    line = json.dumps(
        {
            "message_type": "error",
            "error": {"message": "permission denied"},
            "item": "/sources/x/secret",
        }
    )
    collector.feed(line)
    assert "/sources/x/secret" in collector.text()


@pytest.mark.parametrize(
    "line",
    (
        "Fatal: unable to open repository",
        "warning: some plain text diagnostic",
        "{not valid json at all",
        '{"unterminated": ',
        "[1, 2, 3]",
        '"just a string"',
    ),
)
def test_collector_retains_non_json_and_malformed_lines(line):
    """restic mixes plain-text diagnostics into its JSON streams, and a truncated
    line can arrive if the process is killed mid-write. Anything unparseable is
    kept verbatim — it is often the only clue about what went wrong."""
    collector = BackupOutputCollector(password="")
    collector.feed(line)
    assert line.strip() in collector.text()


def test_collector_ignores_blank_lines():
    collector = BackupOutputCollector(password="")
    for line in ("", "   ", "\r", "\n"):
        collector.feed(line)
    assert collector.text() == ""


def test_collector_strips_carriage_returns():
    """restic's progress output is CR-terminated when it thinks it is on a
    terminal; a stray \\r would render as a control character in the UI."""
    collector = BackupOutputCollector(password="")
    collector.feed("some line\r")
    assert "\r" not in collector.text()


def test_collector_scrubs_the_password_from_every_classification():
    collector = BackupOutputCollector(password="s3cr3t")
    collector.feed("Fatal: repo s3cr3t unreachable")
    collector.feed(json.dumps({"message_type": "summary", "note": "s3cr3t"}))
    assert "s3cr3t" not in collector.text()


def test_collector_summary_survives_an_error_flood():
    """The end-to-end version of the force-retention rule: thousands of error
    lines then the summary, which must still be readable in the record."""
    collector = BackupOutputCollector(password="")
    for i in range(20000):
        collector.feed(
            json.dumps(
                {
                    "message_type": "error",
                    "error": {"message": "permission denied"},
                    "item": f"/sources/x/file-{i}",
                }
            )
        )
    collector.feed(BACKUP_SUMMARY)

    text = collector.text()
    assert collector.summary is not None
    assert "more output line(s) omitted" in text
    assert '"message_type": "summary"' in text or '"message_type":"summary"' in text


# ── pump_stream ──────────────────────────────────────────────────────────────


async def _collect_lines(data: bytes, chunk_size: int = 65536):
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    await pump_stream(FakeStream(data, chunk_size), on_line)
    return lines


async def test_pump_stream_yields_complete_lines():
    assert await _collect_lines(b"a\nb\nc\n") == ["a", "b", "c"]


async def test_pump_stream_flushes_a_final_line_without_a_newline():
    """restic's last line has no trailing newline when the process exits — and
    that last line is the summary, which drives every stats column."""
    assert await _collect_lines(b"first\nsecond-no-newline") == [
        "first",
        "second-no-newline",
    ]


async def test_pump_stream_on_an_empty_stream_yields_nothing():
    assert await _collect_lines(b"") == []


async def test_pump_stream_reassembles_a_line_split_across_reads():
    lines = await _collect_lines(b'{"message_type":"summary"}\n', chunk_size=7)
    assert lines == ['{"message_type":"summary"}']


async def test_pump_stream_does_not_mangle_a_multibyte_char_split_across_reads():
    """A UTF-8 character straddling a chunk boundary must not become replacement
    characters — filenames in error lines are frequently non-ASCII."""
    payload = "/sources/photos/naïve-résumé-日本語.txt\n".encode()
    for chunk_size in range(1, 12):
        lines = await _collect_lines(payload, chunk_size=chunk_size)
        assert lines == ["/sources/photos/naïve-résumé-日本語.txt"], (
            f"mangled at chunk_size={chunk_size}"
        )


async def test_pump_stream_flushes_an_unterminated_line_at_the_retention_limit():
    """A stream with no newlines at all (a binary blob on stderr) must not grow
    the buffer without bound — that unbounded growth is exactly what the
    `communicate()` version did, and it OOM-killed the container.

    The pump's guarantee is about the *buffer*: it flushes once the accumulator
    passes the retention limit, so memory stays O(chunk + limit) no matter how
    long the stream is. Trimming an individual line to a storable length is
    `BoundedOutput.add`'s job, not the pump's.
    """
    chunk = 1024
    total = MAX_RETAINED_LINE_CHARS * 3
    lines = await _collect_lines(b"x" * total, chunk_size=chunk)

    assert len(lines) >= 2, "buffer was never flushed — it grew for the whole stream"
    assert sum(len(line) for line in lines) == total, "no data lost across flushes"
    # Each flush happens as soon as the limit is passed, so a line can overshoot
    # by at most one read.
    assert all(len(line) <= MAX_RETAINED_LINE_CHARS + chunk for line in lines)


async def test_pump_stream_bounded_flush_output_is_still_trimmed_by_the_collector():
    """The two halves of the memory guarantee, together: the pump keeps the
    buffer small, and the collector keeps what it retains small."""
    collector = BackupOutputCollector(password="")

    async def on_line(line: str) -> None:
        collector.feed(line)

    await pump_stream(FakeStream(b"x" * (MAX_RETAINED_LINE_CHARS * 3), 1024), on_line)

    for line in collector.text().splitlines():
        assert len(line) <= MAX_RETAINED_LINE_CHARS + len("…<truncated>")
