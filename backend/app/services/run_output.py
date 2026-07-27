"""The text a run writes into its output columns.

Everything here is pure: restic's raw ``--json`` streams (and a couple of
fixed notes) in, the strings ``BackupRun.error_output`` /
``backup_output`` / ``prune_error_output`` hold out. No DB, no subprocess, no
settings — which is what makes it testable against recorded restic output
without staging a run (tests/test_run_output.py, tests/test_restic_contract.py).

Two properties are load-bearing and apply to every formatter below:

* **Nothing here may raise.** It runs over untrusted subprocess output *after*
  the backup has already finished, so an exception would turn a run that
  succeeded into a failed one. A truncated final line is entirely normal when a
  process is killed mid-write.
* **Everything here is bounded.** These strings are stored in DB columns that
  are read on every run-detail fetch. A share that denies a million files must
  not produce a million-line column, which is why parsing stops at
  :data:`FAILED_ITEM_PARSE_LIMIT` and rendering at
  :data:`MAX_REPORTED_FAILED_ITEMS`.
"""

import json
from typing import Dict, List, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

# How many failing items are parsed out of the streams, and how many of those
# are rendered into `error_output`. The parse limit keeps a pathological run (a
# share that denies every one of a million files) from building a million-entry
# list; the render limit keeps the DB column — which is loaded on every
# run-detail fetch — small. Both are far above the count an operator will
# actually read before going to look at the mount.
FAILED_ITEM_PARSE_LIMIT: int = 200
MAX_REPORTED_FAILED_ITEMS: int = 50
# Fallback when restic said something we could not parse: keep the tail, since
# the fatal and the exit_error line arrive last.
MAX_STDERR_TAIL_CHARS: int = 4000

# Written to BackupRun.prune_error_output when a partial backup withholds
# retention. `prune_status=skipped` alone is ambiguous — it is the same value a
# job with no retention policy gets — so without this note an operator reads a
# withheld policy as "nothing configured" and never learns the repository has
# stopped shrinking. The wording has to explain the trade rather than sound like
# a fault: nothing broke here.
RETENTION_SKIPPED_PARTIAL_NOTE: str = (
    "Retention (restic forget) was not applied because this backup was partial: "
    "some files could not be read, and an incomplete snapshot must not be "
    "allowed to push a complete one out of the retention policy. The snapshot "
    "itself was saved and nothing was deleted. Retention runs again after a "
    "backup that reads everything — until then this repository keeps growing, "
    "so fix the unreadable items above."
)


def extract_failed_items(
    *streams: str, limit: int = FAILED_ITEM_PARSE_LIMIT
) -> List[str]:
    """Pull per-file error messages out of restic's --json streams so the run
    record can show *which* items failed, not just that something did.

    **Both** streams must be passed for a partial backup. restic writes its
    `message_type=error` lines to stderr, not stdout — verified against restic
    0.18.1 and 0.19.1, where stdout carried only `status` and `summary`. This
    function used to be called with stdout alone, so every rc=3 run recorded zero
    failed items and the run page showed a bare "some files could not be read"
    with no paths after it. stdout is still scanned because it costs one pass
    over an already-bounded string and covers merged streams and older builds.

    One failure can be reported more than once — an unreadable directory comes
    back from both the scanner and the archiver (observed with 0.18.1 and 0.19.1) —
    so identical (item, message) pairs are collapsed into one entry and their
    phases merged. Counting the error *events* would report two failures for
    one folder and inflate the count on every real mount.

    Parsing stops at `limit` distinct items; the caller renders fewer still.
    """
    # Insertion-ordered: (item, message) -> phases seen, in the order restic
    # reported them.
    collected: Dict[Tuple[str, str], List[str]] = {}
    for stream in streams:
        for line in stream.split("\n"):
            if len(collected) >= limit:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("message_type") != "error":
                continue
            err = obj.get("error", {})
            raw_msg = err.get("message") if isinstance(err, dict) else err
            # Do not stringify before the emptiness check below: str(None) is
            # "None", which is truthy, so an error line carrying neither a path
            # nor a message used to survive the guard and be rendered to the
            # operator as a failed item literally named "None".
            msg = "" if raw_msg is None else str(raw_msg)
            item = str(obj.get("item") or "")
            if not item and not msg:
                continue
            phases: List[str] = collected.setdefault((item, msg), [])
            # `during` separates a file that could not be read (archival) from
            # a directory that could not even be listed (scan) — different
            # causes, different fixes.
            during = obj.get("during")
            if during and during not in phases:
                phases.append(str(during))

    items: List[str] = []
    for (item, msg), phases in collected.items():
        suffix: str = f" [{', '.join(phases)}]" if phases else ""
        items.append(f"{item}: {msg}{suffix}" if item else f"{msg}{suffix}")
    return items


def _at_least_suffix(count: int) -> str:
    """`+` when parsing stopped at the limit, so a count reads as "at least N".

    Shared by every place that prints one of these counts: the number is only
    ever a floor once :func:`extract_failed_items` has hit
    :data:`FAILED_ITEM_PARSE_LIMIT`, and a bare "200 items" would read as the
    whole truth.
    """
    return "+" if count >= FAILED_ITEM_PARSE_LIMIT else ""


def render_failed_items(failed_items: List[str]) -> List[str]:
    """The item lines allowed into `BackupRun.error_output`: at most
    :data:`MAX_REPORTED_FAILED_ITEMS`, followed by an honest "... and N more".

    **Every formatter must build its list through here.** They used to cap
    independently — the rc=3 path at this limit, the rc!=0 path not at all — so
    one flood of unreadable files wrote a few KiB into the run row if the backup
    half-succeeded and ~1.8 MiB if it failed outright, from the same source and
    the same parse limit. `error_output` is read on every run-detail fetch, so
    the bound has to hold whichever way the run ended, and one renderer is what
    keeps the two paths from drifting apart again.
    """
    shown: List[str] = failed_items[:MAX_REPORTED_FAILED_ITEMS]
    lines: List[str] = list(shown)
    hidden: int = len(failed_items) - len(shown)
    if hidden > 0:
        lines.append(f"... and {hidden}{_at_least_suffix(len(failed_items))} more")
    return lines


def format_partial_backup_error(failed_items: List[str], stderr: str) -> str:
    """Build the user-visible `error_output` for an rc=3 (partial) backup.

    The contract this enforces: the field is never uninformative. When restic
    named the items, they are listed (capped by :func:`render_failed_items`, with
    an honest count of what was not shown). When it did not, the retained stderr
    tail goes in verbatim rather than leaving the operator with a sentence they
    cannot act on.
    """
    count: int = len(failed_items)
    if count:
        parts: List[str] = [
            f"Partial backup: {count}{_at_least_suffix(count)} item(s) could "
            f"not be read; the snapshot was still saved."
        ]
        parts.extend(render_failed_items(failed_items))
        return "\n".join(parts)

    parts = [
        "Partial backup: some files could not be read; the snapshot was still saved."
    ]
    tail: str = stderr.strip()
    if tail:
        parts.append("")
        parts.append("restic stderr:")
        parts.append(tail[-MAX_STDERR_TAIL_CHARS:])
    return "\n".join(parts)


def format_backup_error(rc: int, json_errors: List[str], stderr: str) -> str:
    """Build the user-visible error_output string for a failed backup run.

    Always includes the restic exit code and stderr. When restic emitted
    per-file JSON error lines on stdout before giving up, those are included
    too — they name the specific path/operation that caused the failure,
    which stderr (usually a single post-mortem fatal) does not. Order is
    chosen so the operator sees the high-level summary first, then the
    granular per-file context (gaps.md H5).

    The item list goes through :func:`render_failed_items` — the same renderer
    the partial-backup path uses. This one used to print every parsed item
    instead, so the two paths bounded the same DB column differently.
    """
    parts: List[str] = [f"Backup failed (restic exit code {rc})."]
    if stderr.strip():
        parts.append("")
        parts.append(stderr.strip())
    if json_errors:
        parts.append("")
        parts.append("Per-file errors:")
        parts.extend(render_failed_items(json_errors))
    return "\n".join(parts)


def filter_backup_output(backup_stdout: str) -> str:
    """Strip restic's JSON progress lines (message_type=status) before the
    stdout is persisted to BackupRun.backup_output.

    The stored output exists to answer "what happened in this run" — error
    lines, the summary, and any non-JSON diagnostics. Progress lines are
    emitted throttled for the whole duration of the run and carry no
    post-mortem value; on a many-hour run they are thousands of lines that
    bloat the DB row and the run-detail page.
    """
    kept: List[str] = []
    for line in backup_stdout.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if obj.get("message_type") == "status":
                continue
        kept.append(line)
    return "\n".join(kept)
