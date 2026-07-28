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
    SCAN_STABLE_SECONDS,
    STALL_SECONDS,
    BackupOutputCollector,
    BoundedOutput,
    ScanState,
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


def scanned(**fields):
    """A status line from *after* restic finished sizing the source.

    restic gates `seconds_remaining` on its internal `scanFinished`
    (internal/ui/backup/progress.go), so an eta in the line is the app's proof
    that the totals next to it are final rather than a running subtotal.
    """
    return {"message_type": "status", "seconds_remaining": 125, **fields}


def test_format_progress_on_an_empty_status_line():
    """Better a bare "running" than an empty string the UI renders as a blank
    progress area."""
    assert format_progress({}) == "progress: running"


def test_format_progress_full_line_orders_parts_for_reading():
    rendered = format_progress(
        scanned(
            percent_done=0.4567,
            files_done=1234,
            total_files=5000,
            bytes_done=1024**3,
            total_bytes=4 * 1024**3,
            seconds_elapsed=600,
            error_count=3,
        )
    )
    assert rendered == (
        "progress: 46% · 1,234/5,000 files · 1.0 GiB/4.0 GiB · "
        "10m elapsed · 1.7 MiB/s avg · eta 2m · 3 errors"
    )


def test_format_progress_thousands_separators_on_large_counts():
    """A seven-digit file count without separators is unreadable at a glance."""
    rendered = format_progress(scanned(files_done=1234567, total_files=2000000))
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
        {"seconds_elapsed": "600", "bytes_done": 1024},
        {"total_files": "many", "total_bytes": None},
    ),
)
def test_format_progress_ignores_fields_of_the_wrong_type(status):
    """Never raise on restic's output — a malformed status line must degrade to a
    shorter progress line, not abort the run's progress persistence."""
    assert format_progress(status).startswith("progress:")


# ── ScanState: telling a running subtotal from a final one ───────────────────


def test_the_scan_is_unfinished_until_restic_can_estimate():
    """restic's scanner reports a *running* subtotal after every item and runs
    concurrently with the archiver, so until it is done the totals are a moving
    denominator and nothing derived from them can be trusted."""
    state = ScanState()
    state.observe({"total_files": 300, "total_bytes": 10**8, "bytes_done": 10**7})
    assert state.finished is False


def test_an_eta_proves_the_scan_finished():
    state = ScanState()
    state.observe({"total_files": 300, "total_bytes": 10**8, "seconds_remaining": 90})
    assert state.finished is True


def test_the_scan_stays_finished_after_the_eta_disappears():
    """restic drops `seconds_remaining` again whenever the measured rate falls to
    ≤1024 B/s (and `omitempty` removes the zero), which is routine on a slow
    share. Re-entering the scanning display then would be a lie in the other
    direction, so the flag is sticky."""
    state = ScanState()
    state.observe({"total_bytes": 10**8, "seconds_remaining": 90})
    state.observe({"total_bytes": 10**8, "bytes_done": 5 * 10**7})
    assert state.finished is True


def test_totals_holding_still_end_the_scan_when_restic_never_estimates():
    """The eta is a one-way signal — absent on a share too slow for restic to
    estimate. Without this fallback such a run would show "scanning source…" for
    its entire length."""
    state = ScanState()
    state.observe({"total_files": 300, "total_bytes": 10**8, "seconds_elapsed": 1})
    state.observe(
        {
            "total_files": 300,
            "total_bytes": 10**8,
            "seconds_elapsed": 1 + SCAN_STABLE_SECONDS,
        }
    )
    assert state.finished is True


def test_the_stable_totals_guess_is_taken_back_if_the_scan_resumes():
    """The fallback is a guess, unlike the eta. A scanner stuck on one huge
    directory for longer than the threshold — the exact case this class exists
    for, on a slow share — would otherwise pin a wrong percentage on the rest of
    the run."""
    state = ScanState()
    state.observe({"total_files": 300, "total_bytes": 10**8, "seconds_elapsed": 1})
    state.observe(
        {
            "total_files": 300,
            "total_bytes": 10**8,
            "seconds_elapsed": 1 + SCAN_STABLE_SECONDS,
        }
    )
    assert state.finished is True

    state.observe(
        {
            "total_files": 9000,
            "total_bytes": 10**10,
            "seconds_elapsed": 2 + SCAN_STABLE_SECONDS,
        }
    )
    assert state.finished is False


def test_an_eta_is_never_taken_back_by_a_growing_total():
    """restic's own signal outranks ours. It gates the eta on `scanFinished`, so
    totals that move afterwards are restic's business, not evidence against it."""
    state = ScanState()
    state.observe({"total_files": 300, "total_bytes": 10**8, "seconds_remaining": 42})
    state.observe({"total_files": 900, "total_bytes": 10**9, "seconds_elapsed": 5})
    assert state.finished is True


def test_growing_totals_keep_the_scan_open():
    state = ScanState()
    for i in range(10):
        state.observe(
            {
                "total_files": 300 + i,
                "total_bytes": 10**8 + i,
                "seconds_elapsed": i * SCAN_STABLE_SECONDS,
            }
        )
    assert state.finished is False


def test_no_scan_is_not_a_scan_in_progress():
    """`--no-scan` means restic never sizes the source: no totals ever arrive and
    `percent_done` stays 0.0 for the whole run. That is not a scan the operator
    is waiting on, and the line must not claim it is."""
    state = ScanState()
    state.observe({"percent_done": 0.0, "files_done": 42, "bytes_done": 2048})
    assert state.has_totals is False

    rendered = format_progress(
        {"percent_done": 0.0, "files_done": 42, "bytes_done": 2048}, state
    )
    assert "scanning" not in rendered
    assert "0%" not in rendered, "percent_done is pinned at 0.0 under --no-scan"
    assert "42 files" in rendered


# ── Progress rendering across the phases of a run ────────────────────────────


def test_the_percentage_is_withheld_while_the_denominator_is_still_moving():
    """The reported symptom: `43% · 72/3,086 files · 1.6 GiB/3.7 GiB` on a 40 GB
    source. `percent_done` is `bytes_done / total_bytes`, so mid-scan it is a
    ratio of two moving numbers — showing it invites the operator to plan around
    a number that will fall as the scan catches up."""
    state = ScanState()
    status = {
        "percent_done": 0.43,
        "files_done": 72,
        "total_files": 3086,
        "bytes_done": 1717986918,
        "total_bytes": 3972844749,
    }
    state.observe(status)
    rendered = format_progress(status, state)

    assert "43%" not in rendered
    assert rendered.startswith("progress: scanning source…")
    # The totals are marked as floors, not answers.
    assert "72/3,086+ files" in rendered
    assert "1.6 GiB/3.7+ GiB" in rendered


def test_the_percentage_returns_once_the_scan_is_done():
    state = ScanState()
    status = scanned(
        percent_done=0.43,
        files_done=72,
        total_files=3086,
        bytes_done=1717986918,
        total_bytes=3972844749,
    )
    state.observe(status)
    rendered = format_progress(status, state)

    assert rendered.startswith("progress: 43% · 72/3,086 files")
    assert "+" not in rendered
    assert "scanning" not in rendered


def test_elapsed_and_average_rate_are_reported():
    """`seconds_elapsed` is in every status line and was being dropped. Without it
    the line cannot be sanity-checked: a 40 GB backup crawling at 300 KiB/s and
    one running at 300 MiB/s look identical."""
    rendered = format_progress(
        scanned(bytes_done=600 * 1024 * 1024, total_bytes=10**10, seconds_elapsed=600)
    )
    assert "10m elapsed" in rendered
    assert "1.0 MiB/s avg" in rendered


def test_the_rate_is_labelled_an_average_not_a_current_speed():
    """It is cumulative (bytes_done / seconds_elapsed), unlike restic's own eta,
    which uses a windowed estimator. Unlabelled, the two disagreeing looks like a
    bug in the page."""
    rendered = format_progress(
        scanned(bytes_done=1024**3, seconds_elapsed=1024, total_bytes=2 * 1024**3)
    )
    assert "/s avg" in rendered


@pytest.mark.parametrize(
    "status",
    (
        {"percent_done": 1.4, "total_bytes": 100, "bytes_done": 140},
        {"files_done": 4001, "total_files": 4000, "total_bytes": 100},
    ),
)
def test_passing_the_estimate_is_reported_instead_of_a_percentage_over_100(status):
    """The fingerprint of a scan that under-counted: the archiver walks the tree
    itself, so it keeps going past whatever the scanner managed to count. "140%"
    reads as a display bug and gets ignored; naming it points at the source."""
    state = ScanState()
    state.observe(scanned(**status))
    rendered = format_progress(scanned(**status), state)

    assert state.exceeded is True
    assert "%" not in rendered
    assert "past scan estimate" in rendered
    assert "under-counted" in rendered


def test_no_eta_is_shown_once_the_estimate_has_been_passed():
    """Captured live from restic 0.19.1: with one directory unreadable during
    the scan and readable by the time the archiver reached it, the run ended at
    435,042/435,041 files and restic reported `seconds_remaining=3639421728`.
    It computes the eta as `total.Bytes - processed.Bytes` over **unsigned**
    integers, so overtaking the estimate underflows the subtraction — the line
    read `eta 1010950h 28m`."""
    status = {
        "percent_done": 1.0000000001,
        "files_done": 435042,
        "total_files": 435041,
        "bytes_done": 43110463919,
        "total_bytes": 43110463913,
        "seconds_remaining": 3639421728,
        "seconds_elapsed": 8,
    }
    state = ScanState()
    state.observe(status)
    rendered = format_progress(status, state)

    assert "eta" not in rendered
    assert "past scan estimate" in rendered


def test_a_stalled_backup_says_how_long_nothing_has_been_read():
    """The run that prompted this sat at 1.6 GiB for 30+ minutes while its eta
    drifted from 24m to 1h 6m — restic keeps quoting an eta from a decaying rate
    estimate, and nothing on the page said the byte count had stopped moving."""
    state = ScanState()
    first = scanned(bytes_done=1717986918, total_bytes=4 * 10**9, seconds_elapsed=1000)
    state.observe(first)
    later = scanned(
        bytes_done=1717986918,
        total_bytes=4 * 10**9,
        seconds_elapsed=1000 + STALL_SECONDS + 120,
    )
    state.observe(later)

    assert "no data read for 7m" in format_progress(later, state)


def test_progress_that_is_still_moving_is_not_called_a_stall():
    state = ScanState()
    for i in range(10):
        status = scanned(
            bytes_done=10**8 * (i + 1),
            total_bytes=10**10,
            seconds_elapsed=i * STALL_SECONDS,
        )
        state.observe(status)
    assert "no data read" not in format_progress(status, state)


def test_format_progress_judges_a_lone_status_line_on_its_own_evidence():
    """Called without state (the contract tests, and any future caller with a
    single recorded line), the line still has to be classified — from the eta it
    carries."""
    assert "scanning source…" in format_progress(
        {"total_files": 10, "total_bytes": 100, "bytes_done": 50}
    )
    assert "50%" in format_progress(
        {
            "percent_done": 0.5,
            "total_files": 10,
            "total_bytes": 100,
            "bytes_done": 50,
            "seconds_remaining": 30,
        }
    )


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


def test_bounded_output_keeps_the_tail_when_a_flood_fills_the_cap():
    """The line that ends a run arrives **last**, so on a flood it is exactly the
    line the cap discards.

    Reported live: a backup over an SMB source hit `too many open files in
    system` on thousands of paths, and the run recorded 3,418 omitted lines and
    no cause at all — restic's terminating `Fatal:` had been dropped behind the
    flood that caused it. A reserved tail is wording-independent, which matters
    because restic's message text is explicitly not a contract (gaps.md H5).
    """
    out = BoundedOutput(password="", max_chars=4000)
    for i in range(2000):
        out.add(f'{{"message_type":"error","item":"/sources/photos/{i}"}}')
    out.add("Fatal: unable to open repository: too many open files in system")

    text = out.text()
    assert "Fatal: unable to open repository" in text, (
        "the fatal is the whole point of keeping the record"
    )
    assert "/sources/photos/0" in text, "the head still shows where it started"
    assert "more output line(s) omitted" in text


def test_bounded_output_stays_bounded_once_it_keeps_a_tail():
    """The tail is carved out of the existing budget, not added to it — the
    ceiling that lets this string sit in a row read on every run-detail fetch
    has to hold whatever restic threw at it."""
    out = BoundedOutput(password="", max_chars=4000)
    for i in range(5000):
        out.add(f"line {i} " + "z" * 60)

    # Budget, plus the one-line omission notice.
    assert len(out.text()) <= 4000 + 200


def test_bounded_output_adds_no_tail_when_nothing_was_dropped():
    """Short output must read exactly as it did before — no marker, no repeats."""
    out = BoundedOutput(password="")
    for line in ("first", "second", "third"):
        out.add(line)
    assert out.text() == "first\nsecond\nthird"


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
        collector.feed(json.dumps(scanned(percent_done=pct, total_bytes=10**9)))

    text = collector.text()
    assert "message_type" not in text
    assert text.count("progress:") == 1
    assert "30%" in text, "the newest status line wins"


def test_collector_carries_scan_state_across_status_lines():
    """A single status line cannot say whether the scan has finished — only the
    sequence can. The collector is the only thing that sees the sequence, so it
    owns the state and hands it to the (pure) formatter."""
    collector = BackupOutputCollector(password="")
    collector.feed(
        json.dumps(
            {"message_type": "status", "total_bytes": 10**9, "bytes_done": 10**8}
        )
    )
    assert "scanning source…" in (collector.progress or "")

    # restic estimates once, then stops (rate too slow) — the line must not fall
    # back to calling it a scan.
    collector.feed(
        json.dumps(
            {"message_type": "status", "total_bytes": 10**9, "seconds_remaining": 60}
        )
    )
    collector.feed(
        json.dumps(
            {
                "message_type": "status",
                "percent_done": 0.5,
                "total_bytes": 10**9,
                "bytes_done": 5 * 10**8,
            }
        )
    )
    assert "50%" in (collector.progress or "")
    assert "scanning" not in (collector.progress or "")


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
