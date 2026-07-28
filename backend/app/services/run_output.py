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
import re
from typing import Dict, List, Sequence, Set, Tuple

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
# How many items must share one message before they render as a tally instead
# of a list. Below this, the paths are the information; above it, the repeated
# sentence is noise and the *count* is the information.
MESSAGE_TALLY_THRESHOLD: int = 5
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


class FailedItem(str):
    """One rendered failure line, carrying the parts it was built from.

    It **is** the string every formatter, caller and test already expects — the
    parts ride along only so :func:`render_failed_items` can group a flood by
    cause. Recovering them from the rendered text afterwards is not possible:
    the format is ``"{path}: {message}"`` and a path may contain any number of
    colons, so the split is genuinely ambiguous. Carrying them costs two
    attributes and keeps every existing call site working unchanged.
    """

    message: str
    path: str

    def __new__(cls, *, item: str, message: str, phases: List[str]) -> "FailedItem":
        suffix: str = f" [{', '.join(phases)}]" if phases else ""
        rendered: str = f"{item}: {message}{suffix}" if item else f"{message}{suffix}"
        obj = super().__new__(cls, rendered)
        obj.message = message
        obj.path = item
        return obj


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

    return [
        FailedItem(item=item, message=msg, phases=phases)
        for (item, msg), phases in collected.items()
    ]


def _at_least_suffix(count: int) -> str:
    """`+` when parsing stopped at the limit, so a count reads as "at least N".

    Shared by every place that prints one of these counts: the number is only
    ever a floor once :func:`extract_failed_items` has hit
    :data:`FAILED_ITEM_PARSE_LIMIT`, and a bare "200 items" would read as the
    whole truth.
    """
    return "+" if count >= FAILED_ITEM_PARSE_LIMIT else ""


def _message_signature(message: str) -> str:
    """What two failures have in common when they share a cause.

    restic embeds the failing path *inside* the message text — the real line is
    `lstat /sources/FamilyMedia/thumbs/00/2d: too many open files in system`, not
    a bare errno — so grouping on the message itself groups nothing at all on
    precisely the floods that need it. Verified against the reported capture:
    3,418 lines, 3,418 distinct messages, one cause.

    The errno phrase is what restic puts last, after the final `": "`. Two
    different operations failing the same way (`lstat …: permission denied` and
    `open …: permission denied`) collapsing into one line is the intent, not a
    loss: it is one thing to go and fix.
    """
    _, separator, tail = message.rpartition(": ")
    return tail if separator and tail else message


def _tally_by_message(failed_items: Sequence[str]) -> List[str]:
    """Collapse runs of one cause into `N × <message> (first: <path>)`.

    :func:`extract_failed_items` already collapses identical `(item, message)`
    pairs, which is enough when a directory is reported twice. It does nothing
    for one cause hitting thousands of *different* paths — the shape of a
    resource limit rather than a per-file problem. Live example: ~3,600 lines of
    `too many open files in system`, one per thumbnail, which consumed the whole
    parse limit and rendered as fifty copies of one sentence. The count is the
    information there; the individual paths are not.

    Plain strings (a caller that built its own list) are passed through
    untouched — only parsed :class:`FailedItem`s know their message.
    """
    groups: Dict[str, List[FailedItem]] = {}
    for entry in failed_items:
        if isinstance(entry, FailedItem):
            groups.setdefault(_message_signature(entry.message), []).append(entry)

    rendered: List[str] = []
    tallied: Set[str] = set()
    # Walk the originals so a tally lands where its first item was reported,
    # keeping the order restic used.
    for entry in failed_items:
        if not isinstance(entry, FailedItem):
            rendered.append(entry)
            continue
        signature = _message_signature(entry.message)
        group = groups[signature]
        if len(group) < MESSAGE_TALLY_THRESHOLD:
            rendered.append(entry)
            continue
        if signature in tallied:
            continue
        tallied.add(signature)
        rendered.append(
            f"{len(group)}{_at_least_suffix(len(failed_items))} × {signature} "
            f"(first: {group[0].path})"
        )
    return rendered


def render_failed_items(failed_items: Sequence[str]) -> List[str]:
    """The item lines allowed into `BackupRun.error_output`: at most
    :data:`MAX_REPORTED_FAILED_ITEMS`, followed by an honest "... and N more".

    **Every formatter must build its list through here.** They used to cap
    independently — the rc=3 path at this limit, the rc!=0 path not at all — so
    one flood of unreadable files wrote a few KiB into the run row if the backup
    half-succeeded and ~1.8 MiB if it failed outright, from the same source and
    the same parse limit. `error_output` is read on every run-detail fetch, so
    the bound has to hold whichever way the run ended, and one renderer is what
    keeps the two paths from drifting apart again. It is also where the tally
    lives, for the same reason: one place decides what a flood looks like.
    """
    tallied: List[str] = _tally_by_message(failed_items)
    shown: List[str] = tallied[:MAX_REPORTED_FAILED_ITEMS]
    lines: List[str] = list(shown)
    hidden: int = len(tallied) - len(shown)
    if hidden > 0:
        lines.append(f"... and {hidden}{_at_least_suffix(len(failed_items))} more")
    return lines


# restic reports both descriptor limits with wording that differs by three
# words, and the two need opposite fixes — so the app has to tell them apart or
# stay quiet. ENFILE ("...in system") is the host's system-wide table; EMFILE is
# this process's own RLIMIT_NOFILE.
_OPEN_FILE_LIMIT_RE = re.compile(r"too many open files", re.IGNORECASE)
_SYSTEM_FILE_LIMIT_RE = re.compile(r"too many open files in system", re.IGNORECASE)

_ENFILE_NOTE: str = (
    "This run ran out of file descriptors on the HOST, not in this container. "
    '"too many open files in system" is errno ENFILE — the host\'s system-wide '
    "open-file table — so raising this container's `nofile` ulimit will not "
    "help.\n"
    "\n"
    "What does, in order:\n"
    "  1. Back up fewer files. A source holding hundreds of thousands of small "
    "generated files (a photo app's thumbnails, transcodes, a build cache) is "
    "what exhausts the table. Exclude the directories whose contents the "
    "application can rebuild.\n"
    "  2. Turn on 'Skip pre-scan' (no-scan) for this job. restic runs its "
    "size-estimate scan concurrently with the backup, so it walks the whole "
    "tree twice; the second walk buys only the progress percentage.\n"
    "  3. Raise the host's limit — on macOS that is `kern.maxfiles`. Note that "
    "a folder shared into a Linux container from macOS goes through a "
    "file-sharing bridge that holds a descriptor per file it has looked at, so "
    "the source's file count lands on the host's table. Mounting the share "
    "inside the container instead takes that bridge out of the path.\n"
    "  4. Avoid backing up a tree while another application is actively writing "
    "to it."
)

_EMFILE_NOTE: str = (
    "This run ran out of file descriptors. "
    '"too many open files" (without "in system") is this process\'s own limit, '
    "so raising the container's `nofile` ulimit is the fix — `--ulimit "
    "nofile=1048576` on `docker run`, or a `ulimits:` block in compose. "
    "Reducing the job's read concurrency lowers the demand in the meantime."
)


def diagnose_open_file_limit(*texts: str) -> str:
    """The one thing restic's own message cannot tell an operator: which limit.

    restic reports the symptom and stops. Reading ENFILE as a container limit
    sends someone to raise a `nofile` ulimit that cannot have any effect, and
    reading EMFILE as a host limit sends them to reconfigure a machine that was
    fine — so this returns advice only when it can tell the two apart, and ""
    otherwise. Kept narrow deliberately: it is prepended near the top of
    `error_output`, and `RunNotifier.failed` pushes only the first 200
    characters, so a wrong note would displace the real error in the alert.
    """
    joined: str = "\n".join(texts)
    if not _OPEN_FILE_LIMIT_RE.search(joined):
        return ""
    return _ENFILE_NOTE if _SYSTEM_FILE_LIMIT_RE.search(joined) else _EMFILE_NOTE


def format_partial_backup_error(failed_items: List[str], stderr: str) -> str:
    """Build the user-visible `error_output` for an rc=3 (partial) backup.

    The contract this enforces: the field is never uninformative. When restic
    named the items, they are listed (capped by :func:`render_failed_items`, with
    an honest count of what was not shown). When it did not, the retained stderr
    tail goes in verbatim rather than leaving the operator with a sentence they
    cannot act on.
    """
    count: int = len(failed_items)
    diagnosis: str = diagnose_open_file_limit(stderr, *failed_items)
    if count:
        parts: List[str] = [
            f"Partial backup: {count}{_at_least_suffix(count)} item(s) could "
            f"not be read; the snapshot was still saved."
        ]
        if diagnosis:
            parts.extend(("", diagnosis, ""))
        parts.extend(render_failed_items(failed_items))
        return "\n".join(parts)

    parts = [
        "Partial backup: some files could not be read; the snapshot was still saved."
    ]
    if diagnosis:
        parts.extend(("", diagnosis))
    tail: str = stderr.strip()
    if tail:
        parts.append("")
        parts.append("restic stderr:")
        parts.append(tail[-MAX_STDERR_TAIL_CHARS:])
    return "\n".join(parts)


def format_scan_errors(failed_items: List[str]) -> str:
    """Build `error_output` for a backup that exited **0** while restic reported
    errors on stderr.

    These are the ones nothing else catches. restic's scan pass hands an
    unlistable directory to `ScannerError`, which prints
    `{"message_type":"error","during":"scan",...}` to stderr, returns nil and
    never touches `error_count` — so the process still exits 0, the run is a
    clean success, and (until this) the app discarded the stderr that said
    otherwise. What the operator sees instead is the symptom: a progress line
    whose totals are a fraction of the real source, because the scan is what
    produces `total_files`/`total_bytes` and everything derived from them.

    The wording has to hold two things at once. It is not data loss — the
    archiver walks the tree itself and would have exited 3 had it failed to read
    something, so an rc=0 snapshot contains everything restic could see. But a
    share that fails to list a directory once is not healthy, and the run's
    reported size and percentage cannot be trusted, so it is not nothing either.

    The item list goes through :func:`render_failed_items`, like every other
    formatter here — see its docstring for why that is not optional.
    """
    if not failed_items:
        return ""

    count: int = len(failed_items)
    parts: List[str] = [
        f"Backup completed, but restic could not read {count}"
        f"{_at_least_suffix(count)} item(s) while sizing the source. restic "
        "reports these and keeps going, so the exit code was still 0.",
        "",
        "The snapshot was saved and every file restic could see was archived. "
        "What is affected is the estimate: the file count, size and percentage "
        "shown while this run was in flight were computed against an "
        "under-counted source. Check the item(s) below on the mount — a share "
        "that cannot list a directory once may be failing intermittently.",
        "",
    ]
    diagnosis: str = diagnose_open_file_limit(*failed_items)
    if diagnosis:
        parts.extend((diagnosis, ""))
    parts.extend(render_failed_items(failed_items))
    return "\n".join(parts)


def format_backup_error(rc: int, json_errors: List[str], stderr: str) -> str:
    """Build the user-visible error_output string for a failed backup run.

    Always includes the restic exit code and stderr, plus the per-file error
    lines restic emitted before giving up — they name the specific
    path/operation that caused the failure, which a post-mortem fatal does not.
    Order is chosen so the operator sees the high-level summary first, then the
    granular per-file context (gaps.md H5).

    A recognised cause goes **above** the stderr dump, not below it:
    `RunNotifier.failed` pushes only the first 200 characters of this string, so
    anything under a stderr block never reaches the alert.

    The item list goes through :func:`render_failed_items` — the same renderer
    the partial-backup path uses. This one used to print every parsed item
    instead, so the two paths bounded the same DB column differently.
    """
    parts: List[str] = [f"Backup failed (restic exit code {rc})."]
    diagnosis: str = diagnose_open_file_limit(stderr, *json_errors)
    if diagnosis:
        parts.append("")
        parts.append(diagnosis)
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
