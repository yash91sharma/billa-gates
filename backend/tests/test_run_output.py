"""Tests for app/services/run_output.py — the text a run shows the operator.

These functions run over untrusted subprocess output on every run, and their
result is what the run-detail page renders. Two properties matter beyond
"does it parse": a crash here would happen *after* the backup has already
succeeded (turning a good run into a failed one), and everything they return
is stored in a DB column that is loaded on every run-detail fetch, so the
output has to stay bounded whatever restic threw at it.

Moved here from tests/test_backup_runner.py when the parsers were split out of
the pipeline module; the assertions are unchanged.
"""

import json

import pytest

from app.services.run_output import (
    FAILED_ITEM_PARSE_LIMIT,
    MAX_REPORTED_FAILED_ITEMS,
    RETENTION_SKIPPED_PARTIAL_NOTE,
    extract_failed_items,
    filter_backup_output,
    format_backup_error,
    format_partial_backup_error,
    render_failed_items,
)

# The same capture, verbatim, when the unreadable item is a *directory*:
# restic reports it once from the scanner and once from the archiver. One
# folder, two error lines — the count shown to the user must be 1.
RC3_STDERR_DOUBLE_REPORTED = (
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/locked: permission denied"},"during":'
    '"archival","item":"/sources/Docs/locked"}\n'
    '{"message_type":"error","error":{"message":"openfile for readdirnames '
    'failed: open /sources/Docs/locked: permission denied"},"during":"scan",'
    '"item":"/sources/Docs/locked"}\n'
    '{"message_type":"exit_error","code":3,"message":"Warning: at least one '
    'source file could not be read"}'
)


# ── extract_failed_items: robustness against whatever restic actually writes ──


@pytest.mark.parametrize(
    "line",
    (
        "not json at all",
        '{"unterminated',
        "[]",
        '"a bare string"',
        "null",
        "{}",
    ),
)
def test_extract_failed_items_skips_unparseable_lines(line):
    assert extract_failed_items(line) == []


def test_extract_failed_items_ignores_non_error_message_types():
    """status and summary lines share the stream with error lines; only errors
    name a failed item."""
    stream = "\n".join(
        [
            json.dumps({"message_type": "status", "percent_done": 0.5}),
            json.dumps({"message_type": "summary", "files_new": 1}),
            json.dumps({"message_type": "verbose_status", "item": "/x"}),
        ]
    )
    assert extract_failed_items(stream) == []


def test_extract_failed_items_skips_error_lines_with_neither_item_nor_message():
    """An error line carrying no path and no text tells the operator nothing —
    listing it as a failed item would inflate the count with blanks."""
    stream = json.dumps({"message_type": "error", "error": {}, "item": ""})
    assert extract_failed_items(stream) == []


def test_extract_failed_items_handles_a_non_dict_error_value():
    """`error` is restic's field; if it is ever a bare string the parser must
    still surface it rather than raising."""
    stream = json.dumps(
        {"message_type": "error", "error": "permission denied", "item": "/sources/x"}
    )
    items = extract_failed_items(stream)
    assert len(items) == 1
    assert "permission denied" in items[0]


def test_extract_failed_items_merges_phases_for_one_item():
    """Scanner and archiver both report an unreadable directory. One entry, both
    phases — counting events would report two failures for one folder."""
    items = extract_failed_items(RC3_STDERR_DOUBLE_REPORTED)
    assert len(items) == 1


def test_extract_failed_items_names_both_phases_it_merged():
    """`during` separates a file that could not be read (archival) from a
    directory that could not even be listed (scan) — different causes, different
    fixes — so a merged entry must still carry both."""
    (item,) = extract_failed_items(RC3_STDERR_DOUBLE_REPORTED)
    assert "archival" in item
    assert "scan" in item


def test_extract_failed_items_reads_every_stream_it_is_given():
    """The rc=3 path passes stderr *and* stdout: restic writes its per-file
    error lines to stderr, but scanning stdout too costs one pass over an
    already-bounded string and covers merged streams and older builds."""
    stderr = json.dumps(
        {"message_type": "error", "error": {"message": "denied"}, "item": "/a"}
    )
    stdout = json.dumps(
        {"message_type": "error", "error": {"message": "denied"}, "item": "/b"}
    )
    items = extract_failed_items(stderr, stdout)
    assert len(items) == 2


def test_extract_failed_items_respects_the_parse_limit():
    """A share that denies a million files must not write a million lines into
    the run row — error_output is read on every run-detail fetch."""
    stream = "\n".join(
        json.dumps(
            {
                "message_type": "error",
                "error": {"message": "permission denied"},
                "item": f"/sources/x/file-{i}",
                "during": "archival",
            }
        )
        for i in range(FAILED_ITEM_PARSE_LIMIT * 3)
    )
    assert len(extract_failed_items(stream)) <= FAILED_ITEM_PARSE_LIMIT


# ── render_failed_items: the one place the rendered list is capped ────────────


def test_render_failed_items_returns_everything_under_the_cap():
    items = [f"/sources/f{i}: denied" for i in range(3)]
    assert render_failed_items(items) == items


def test_render_failed_items_counts_what_it_hid():
    rendered = render_failed_items([f"/sources/f{i}: denied" for i in range(120)])
    assert len(rendered) == MAX_REPORTED_FAILED_ITEMS + 1
    assert rendered[-1] == f"... and {120 - MAX_REPORTED_FAILED_ITEMS} more"


def test_render_failed_items_marks_a_count_that_is_only_a_floor():
    """Once parsing stopped at the limit the count is a floor, not the truth;
    a bare 'and N more' would read as the whole story."""
    rendered = render_failed_items(
        [f"/sources/f{i}: denied" for i in range(FAILED_ITEM_PARSE_LIMIT)]
    )
    hidden = FAILED_ITEM_PARSE_LIMIT - MAX_REPORTED_FAILED_ITEMS
    assert rendered[-1] == f"... and {hidden}+ more"


# ── filter_backup_output ──────────────────────────────────────────────────────


def test_filter_backup_output_keeps_unparseable_lines():
    """Non-JSON diagnostics are often the only clue about a weird run, so they
    are kept verbatim rather than dropped with the progress noise."""
    out = filter_backup_output(
        "\n".join(
            [
                "Fatal: something restic printed as plain text",
                '{"truncated json',
                json.dumps({"message_type": "status", "percent_done": 0.5}),
                json.dumps({"message_type": "summary", "files_new": 1}),
            ]
        )
    )

    assert "Fatal: something restic printed as plain text" in out
    assert '{"truncated json' in out
    assert "status" not in out, "progress lines must be stripped"
    assert "summary" in out, "the summary is the run's receipt"


# ── format_backup_error / format_partial_backup_error ────────────────────────


def test_format_backup_error_always_names_the_exit_code():
    """Whatever else is missing, the operator gets the code to search for."""
    assert "exit code 130" in format_backup_error(130, [], "")
    assert "exit code 1" in format_backup_error(1, [], "")


def test_format_backup_error_includes_stderr_and_per_file_errors():
    out = format_backup_error(1, ["/sources/x: denied"], "Fatal: repo locked")
    assert "Fatal: repo locked" in out
    assert "/sources/x: denied" in out
    # Summary first, granular context after.
    assert out.index("Fatal: repo locked") < out.index("/sources/x: denied")


def test_format_partial_backup_error_says_the_snapshot_survived():
    """rc=3 is not a failure — the snapshot was written. An operator who reads
    this as data loss goes looking for a restore they don't need."""
    out = format_partial_backup_error(["/sources/x: denied"], "")
    assert "could not be read" in out
    assert "snapshot was still saved" in out


def test_format_partial_backup_error_falls_back_to_the_stderr_tail():
    """When nothing parsed, the bare sentence is unactionable — the operator
    cannot tell a permissions problem from a vanished subfolder. The retained
    tail goes in verbatim instead."""
    out = format_partial_backup_error([], "Fatal: unexplained thing restic said")
    assert "Fatal: unexplained thing restic said" in out


def test_format_partial_backup_error_is_never_uninformative():
    """Neither items nor stderr: the field still has to explain the warning."""
    out = format_partial_backup_error([], "")
    assert "could not be read" in out
    assert "snapshot was still saved" in out


_ITEM_PREFIX = "/sources/Docs/"


def _item_lines(rendered: str) -> list[str]:
    """The per-item lines a formatter actually wrote."""
    return [ln for ln in rendered.splitlines() if ln.startswith(_ITEM_PREFIX)]


def _flood(count: int, line_chars: int = 0) -> list[str]:
    pad = "d" * line_chars
    return [f"{_ITEM_PREFIX}{pad}/f{i}: permission denied" for i in range(count)]


def test_format_backup_error_caps_the_item_list():
    """The rc!=0 path used to render every parsed item while the rc=3 path
    stopped at 50, so one flood of unreadable files wrote a few KiB into the run
    row if the backup half-succeeded and ~1.8 MiB if it failed outright.
    `error_output` is loaded on every run-detail fetch; the bound has to hold
    whichever way the run ended."""
    out = format_backup_error(1, _flood(200), "Fatal: nope")

    assert len(_item_lines(out)) == MAX_REPORTED_FAILED_ITEMS
    assert f"... and {200 - MAX_REPORTED_FAILED_ITEMS}" in out, (
        "the operator has to be told the list was truncated, and by how much"
    )


def test_both_error_formatters_cap_the_item_list_identically():
    """The anti-drift guard. Two formatters each holding their own opinion about
    the limit is exactly how the asymmetry appeared; they now share one
    renderer, and this fails the moment either grows a second one."""
    items = _flood(200)
    assert _item_lines(format_backup_error(1, items, "Fatal: nope")) == _item_lines(
        format_partial_backup_error(items, "Fatal: nope")
    )


def test_error_output_stays_small_enough_to_load_on_every_fetch():
    """Ceiling check against the worst input the pipeline can hand these: the
    parse limit's worth of items, each already truncated upstream to the
    collector's per-line cap. Before the shared renderer the rc!=0 path came out
    at ~1.8 MiB here."""
    from app.services.restic_stream import (
        MAX_RETAINED_LINE_CHARS,
        MAX_RETAINED_OUTPUT_CHARS,
    )

    items = _flood(FAILED_ITEM_PARSE_LIMIT, line_chars=MAX_RETAINED_LINE_CHARS)
    stderr = "x" * MAX_RETAINED_OUTPUT_CHARS

    # What the row can hold: the capped item block, plus the stderr the restic
    # collector already bounds, plus headlines.
    ceiling = (
        MAX_REPORTED_FAILED_ITEMS * (MAX_RETAINED_LINE_CHARS + 128)
        + MAX_RETAINED_OUTPUT_CHARS
        + 1024
    )
    assert len(format_backup_error(1, items, stderr)) <= ceiling
    assert len(format_partial_backup_error(items, stderr)) <= ceiling


# ── The withheld-retention note ───────────────────────────────────────────────


def test_retention_skipped_note_explains_the_trade_rather_than_a_fault():
    """`prune_status=skipped` is the same value a job with no retention policy
    gets, so without this note a withheld policy reads as "nothing configured".
    Nothing broke here, and the wording must not suggest it did."""
    assert "not applied" in RETENTION_SKIPPED_PARTIAL_NOTE
    assert "nothing was deleted" in RETENTION_SKIPPED_PARTIAL_NOTE
    assert "keeps growing" in RETENTION_SKIPPED_PARTIAL_NOTE
