"""Digesting `restic backup --json` output into a bounded, readable record.

Pure: no subprocess, no database, no settings. Everything here takes restic's
bytes and returns text, which is what lets it be tested directly against
recorded restic output (tests/test_restic_stream.py, and
tests/test_restic_contract.py over the real recordings in
tests/fixtures/restic_0_19_1/).

In JSON mode restic emits a progress line continuously for the whole duration
of a run, even when stdout is a pipe. The cadence is restic's to choose and it
has already changed once: measured over the same 1.2 GB source, 0.18.1 emitted
~42 lines/s (~9.5 KB/s, ~34 MB/hour) and 0.19.1 ~6.5 lines/s (~1.7 KB/s). The
bound below is what makes that irrelevant — do not re-derive it from a rate.

Reading the stream with `communicate()` held every byte in memory until the
process exited (~1 GB RSS on a five-hour backup once the decode/scrub/repr
copies are counted), and then all of it was dropped before the run row was
written. :class:`BackupOutputCollector` consumes the stream line by line and
keeps a fixed-size view of it instead: error lines, the final summary, non-JSON
diagnostics, and one continuously overwritten progress line. Memory is O(1) in
the length of the run.
"""

import codecs
import json
from collections import deque
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
)

from app.core.logging import get_logger

logger = get_logger(__name__)

# Retained-output ceiling. Generous for a post-mortem (thousands of error
# lines) and still small enough to sit in a DB row and a run-detail response.
MAX_RETAINED_OUTPUT_CHARS: int = 256 * 1024
MAX_RETAINED_LINE_CHARS: int = 8 * 1024
# Reserved *inside* MAX_RETAINED_OUTPUT_CHARS for the end of the stream. See
# BoundedOutput: a run's fatal arrives last, so a head-only budget drops it.
MAX_RETAINED_TAIL_CHARS: int = 8 * 1024
STREAM_CHUNK_BYTES: int = 65536

# How long restic's scan totals must hold still before the app concludes the
# scan is over. Only needed because restic's own signal (an eta) is suppressed
# below 1024 B/s — see :class:`ScanState`.
SCAN_STABLE_SECONDS: float = 30.0
# How long `bytes_done` may sit unchanged before the progress line says so.
STALL_SECONDS: float = 300.0


class ByteStream(Protocol):
    """The one method :func:`pump_stream` needs from an `asyncio.StreamReader`.

    Stated as a protocol so the pump can be exercised without a subprocess.
    """

    async def read(self, n: int = -1) -> bytes: ...


def _format_bytes(num_bytes: object) -> Optional[str]:
    """Render a byte count for the human-readable progress line."""
    if not isinstance(num_bytes, (int, float)) or isinstance(num_bytes, bool):
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(num_bytes)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return (
        f"{int(value)} {units[index]}" if index == 0 else f"{value:.1f} {units[index]}"
    )


def _number(value: object) -> Optional[float]:
    """The one type guard restic's JSON needs: a number that is not a bool.

    `isinstance(True, int)` is True in Python, and a bool where a byte count
    belongs would render as "1 B".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def format_duration(seconds: float) -> str:
    """A duration at the precision an operator reads at a glance.

    Public because `restic.py` names the deadline it hit in the message it
    returns when a command times out, and a second definition of "how long is
    that" would eventually print the elapsed time on the progress line and the
    limit in the failure message in two different shapes.
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def _format_eta(seconds: object) -> Optional[str]:
    """Render restic's `seconds_remaining` as a short ETA, or None if absent.

    restic omits it until it has scanned enough to estimate.
    """
    value = _number(seconds)
    if value is None or value <= 0:
        return None
    return f"eta {format_duration(value)}"


def _format_rate(bytes_done: Optional[float], seconds_elapsed: Optional[float]) -> str:
    """Average throughput so far, or "" when there is nothing to divide.

    Deliberately cumulative and deliberately labelled: restic's own eta comes
    from a windowed estimator, so the two will disagree, and an unlabelled
    number that disagrees with the eta beside it reads as a bug in the page.
    """
    if not bytes_done or not seconds_elapsed or seconds_elapsed < 1:
        return ""
    rendered = _format_bytes(bytes_done / seconds_elapsed)
    return f"{rendered}/s avg" if rendered else ""


class ScanState:
    """What the app knows about restic's scan pass, accumulated across lines.

    This exists because a single `status` line cannot be read on its own.
    restic's scanner (internal/archiver/scanner.go) reports a **running
    subtotal** after every item and runs concurrently with the archiver, so
    `total_files`/`total_bytes` are a floor until it finishes — and
    `percent_done` is nothing but `bytes_done / total_bytes`, a ratio of two
    moving numbers. Rendering that as a percentage is what made a 40 GB source
    show `43% · 72/3,086 files · 1.6 GiB/3.7 GiB`.

    Two signals say the scan is over, and both are needed:

    * **An eta.** restic gates `seconds_remaining` on its internal
      `scanFinished` (internal/ui/backup/progress.go), so its presence is
      proof. The flag is sticky because restic also zeroes the eta whenever the
      measured rate drops to ≤1024 B/s — routine on a slow share — and
      `omitempty` then removes the field entirely.
    * **Totals that stop moving.** The eta is therefore one-way: its absence
      proves nothing. Without this fallback a backup too slow for restic to
      estimate would read "scanning source…" from start to finish.

    Elapsed seconds drive both thresholds rather than a line count: the
    emission cadence is restic's to choose and already changed between 0.18.1
    (~42 lines/s) and 0.19.1 (~6.5 lines/s).

    Nothing here raises. It runs over untrusted subprocess output on the hot
    path of a live backup, and an exception would abort progress persistence.
    """

    def __init__(self) -> None:
        self.finished: bool = False
        # Which signal ended the scan. Only the eta is proof; the stable-totals
        # fallback is a guess, and a guess has to be revocable — see `observe`.
        self._finished_by_eta: bool = False
        # The archiver walks the tree itself and does not stop at whatever the
        # scanner managed to count, so it can pass the estimate outright. That
        # is the fingerprint of a scan that missed part of the source.
        self.exceeded: bool = False
        self._totals: Optional[Tuple[float, float]] = None
        self._totals_at: Optional[float] = None
        self._bytes_done: Optional[float] = None
        self._bytes_at: Optional[float] = None
        self._elapsed: Optional[float] = None
        self._announced: bool = False

    @property
    def has_totals(self) -> bool:
        """False under `--no-scan`, where restic never sizes the source at all —
        no totals arrive and `percent_done` stays 0.0 for the whole run. That is
        not a scan anyone is waiting on and must not be displayed as one."""
        return self._totals is not None and any(self._totals)

    @property
    def scanning(self) -> bool:
        return self.has_totals and not self.finished

    @property
    def elapsed(self) -> Optional[float]:
        return self._elapsed

    @property
    def stalled_for(self) -> Optional[float]:
        """Seconds since `bytes_done` last moved, once that exceeds
        :data:`STALL_SECONDS`. restic keeps quoting an eta off a decaying rate
        estimate while nothing is being read, so a stalled run looks alive."""
        if self._elapsed is None or self._bytes_at is None:
            return None
        idle = self._elapsed - self._bytes_at
        return idle if idle >= STALL_SECONDS else None

    def observe(self, status: Dict[str, Any]) -> None:
        elapsed = _number(status.get("seconds_elapsed"))
        if elapsed is not None:
            self._elapsed = elapsed

        if (
            not self._finished_by_eta
            and (_number(status.get("seconds_remaining")) or 0) > 0
        ):
            self._finished_by_eta = True
            self._finish(status, "restic reported an eta")

        totals = (
            _number(status.get("total_files")) or 0.0,
            _number(status.get("total_bytes")) or 0.0,
        )
        if totals != self._totals:
            # Totals moving again after the fallback called the scan over means
            # the fallback was wrong — a scanner stuck on one huge directory for
            # longer than the threshold, which on a slow share is exactly the
            # case this whole class exists for. Take the percentage back rather
            # than showing the wrong one for the rest of the run.
            if self.finished and not self._finished_by_eta:
                logger.info(
                    "scan resumed after stable-totals guess; totals were provisional"
                )
                self.finished = False
            self._totals = totals
            self._totals_at = self._elapsed
        elif self._totals_at is None:
            self._totals_at = self._elapsed
        elif (
            not self.finished
            and any(totals)
            and self._elapsed is not None
            and self._elapsed - self._totals_at >= SCAN_STABLE_SECONDS
        ):
            self._finish(status, f"totals unchanged for {SCAN_STABLE_SECONDS:.0f}s")

        done = _number(status.get("bytes_done"))
        if done is not None:
            if done != self._bytes_done:
                self._bytes_done = done
                self._bytes_at = self._elapsed
            elif self._bytes_at is None:
                self._bytes_at = self._elapsed

        percent = _number(status.get("percent_done"))
        files_done = _number(status.get("files_done"))
        total_files = _number(status.get("total_files"))
        if (percent is not None and percent > 1.0) or (
            files_done is not None
            and total_files is not None
            and total_files > 0
            and files_done > total_files
        ):
            self.exceeded = True

    def _finish(self, status: Dict[str, Any], reason: str) -> None:
        self.finished = True
        if self._announced:
            return
        self._announced = True
        # The one durable record of what restic thought the source was. A run
        # that is later killed keeps no progress line at all, and the summary
        # only ever describes what was archived — never the estimate that the
        # percentage and eta were computed against.
        logger.info(
            f"scan complete: total_files={status.get('total_files')} "
            f"total_bytes={status.get('total_bytes')} ({reason})"
        )


def format_progress(status: Dict[str, Any], state: Optional[ScanState] = None) -> str:
    """Collapse one restic `message_type=status` line into a single line.

    Only what an operator watching a run wants to know — how far along, how
    many files, how many bytes, how fast, how much longer, and what restic
    could not read. Everything else in the status message (the rotating
    `current_files` list above all) is noise once it is one second old.

    The first slot answers "how far along", and which of three answers goes
    there is the whole point of :class:`ScanState`:

    * **`scanning source…`** — the totals are still a running subtotal, so the
      percentage is withheld and the denominators are printed with a `+`. They
      are floors, not answers.
    * **`past scan estimate`** — the archiver has gone beyond what the scanner
      counted, which means the scan missed part of the source. A percentage
      over 100 reads as a display bug and gets ignored; naming it points at
      the mount, where the cause is.
    * **the percentage** — earned only once the denominator is final.

    `state` is optional so a lone recorded line can still be rendered (the
    contract tests do this); it is then judged on the evidence it carries by
    itself, which is the eta.

    Two things this line says that restic's numbers do not:

    * `percent_done` is `bytes_done / total_bytes`, and both `files_done` and
      `bytes_done` count **every** file walked, unmodified ones included (an
      unchanged file is credited its whole size in one call). It is not an
      upload percentage — only the summary's `data_added` is.
    * `error_count` counts the **archiver's** errors only. A directory the
      *scanner* could not list goes to stderr as `during: "scan"`, is swallowed
      (restic still exits 0) and never reaches this line — which is why
      `run_backup` parses stderr on a clean exit too, and why the
      "past scan estimate" case above exists at all.
    """
    if state is None:
        state = ScanState()
        state.observe(status)

    parts: List[str] = []
    percent = _number(status.get("percent_done"))

    if state.scanning:
        parts.append("scanning source…")
    elif state.exceeded:
        parts.append("past scan estimate")
    elif percent is not None and _number(status.get("total_bytes")):
        parts.append(f"{percent * 100:.0f}%")

    # A `+` on the denominators while the scanner is still counting: the totals
    # are a floor. Nothing marks them afterwards, when they are the real answer.
    floor = "+" if state.scanning else ""

    files_done = status.get("files_done")
    total_files = status.get("total_files")
    if isinstance(files_done, int) and isinstance(total_files, int):
        parts.append(f"{files_done:,}/{total_files:,}{floor} files")
    elif isinstance(files_done, int):
        parts.append(f"{files_done:,} files")

    bytes_done = _format_bytes(status.get("bytes_done"))
    total_bytes = _format_bytes(status.get("total_bytes"))
    if bytes_done and total_bytes:
        # "3.7+ GiB", not "3.7 GiB+": the marker belongs to the number, and this
        # keeps it in the same place as the "3,086+ files" beside it.
        parts.append(f"{bytes_done}/{total_bytes.replace(' ', floor + ' ', 1)}")
    elif bytes_done:
        parts.append(bytes_done)

    if state.elapsed:
        parts.append(f"{format_duration(state.elapsed)} elapsed")

    rate = _format_rate(_number(status.get("bytes_done")), state.elapsed)
    if rate:
        parts.append(rate)

    # No eta once the archiver has passed the estimate. restic computes it as
    # `total.Bytes - processed.Bytes` over **unsigned** integers, so the moment
    # processed overtakes total the subtraction underflows and it reports a
    # number near 2^64 — observed live as `eta 1010950h 28m`. Even without the
    # overflow the eta is derived from the denominator we have just declared
    # untrustworthy, so there is nothing to salvage by clamping it.
    eta = None if state.exceeded else _format_eta(status.get("seconds_remaining"))
    if eta:
        parts.append(eta)

    stalled = state.stalled_for
    if stalled:
        parts.append(f"no data read for {format_duration(stalled)}")

    error_count = status.get("error_count")
    if (
        isinstance(error_count, int)
        and not isinstance(error_count, bool)
        and error_count > 0
    ):
        parts.append(f"{error_count:,} error{'' if error_count == 1 else 's'}")

    if state.exceeded:
        parts.append("restic's scan under-counted this source")

    return f"progress: {' · '.join(parts)}" if parts else "progress: running"


class BoundedOutput:
    """Fixed-size accumulator for a subprocess stream.

    Keeps whole lines up to `max_chars`, truncates any single oversized line,
    and counts what it had to drop so the caller can say output was omitted
    rather than silently losing it.

    **Both ends of the stream are kept, not just the head.** A budget spent
    front-to-back discards the newest lines — and the line that ends a run
    always arrives last, so on an error flood the cap threw away precisely the
    line explaining the failure it was reporting. Observed live: a backup over
    an SMB source hit `too many open files in system` on thousands of paths and
    recorded "3418 more output line(s) omitted" with no cause anywhere in the
    row, because restic's terminating `Fatal:` sat behind the flood that caused
    it. A reserved tail is carved out of `max_chars` (never added to it — this
    string is loaded on every run-detail fetch) and is deliberately
    wording-independent: restic's message text is not a contract, so matching
    on `Fatal:` would be one rename away from losing the line again.
    """

    def __init__(
        self,
        password: str,
        max_chars: int = MAX_RETAINED_OUTPUT_CHARS,
        tail_chars: int = MAX_RETAINED_TAIL_CHARS,
    ) -> None:
        self._password = password
        self._max_chars = max_chars
        # Never more than half the budget, so a small `max_chars` (the tests
        # use 50) cannot leave the head with nothing.
        self._tail_max = min(tail_chars, max_chars // 2)
        self._head_max = max_chars - self._tail_max
        self._lines: List[str] = []
        self._chars = 0
        self._dropped = 0
        self._tail: Deque[str] = deque()
        self._tail_chars = 0

    def scrub(self, line: str) -> str:
        """Strip the repo password. Per line now, since there is no longer a
        whole-stream string to run a single replace() over."""
        return line.replace(self._password, "") if self._password else line

    def add(self, line: str, *, force: bool = False) -> None:
        """Retain one line, unless that would push us past the ceiling.

        A line that does not fit the head is not lost outright: it enters the
        tail ring, where it survives until a newer line evicts it. `force` is
        for the lines that must survive at any cost (the summary), which arrive
        last and would otherwise be lost behind an error flood.
        """
        if len(line) > MAX_RETAINED_LINE_CHARS:
            line = line[:MAX_RETAINED_LINE_CHARS] + "…<truncated>"
        if force or self._chars + len(line) <= self._head_max:
            self._lines.append(line)
            self._chars += len(line) + 1
            return
        self._dropped += 1
        self._tail.append(line)
        self._tail_chars += len(line) + 1
        while self._tail and self._tail_chars > self._tail_max:
            self._tail_chars -= len(self._tail.popleft()) + 1

    def text(self, *, extra: Optional[str] = None) -> str:
        parts = list(self._lines)
        if self._dropped:
            # Only what neither end kept is "omitted" — the tail lines are
            # printed right below, and counting them as missing would send the
            # operator looking for output that is on the screen.
            hidden = self._dropped - len(self._tail)
            if hidden > 0:
                parts.append(
                    f"... {hidden} more output line(s) omitted "
                    f"(retained output is capped at {self._max_chars} "
                    f"characters); the last {len(self._tail)} line(s) follow:"
                )
            parts.extend(self._tail)
        if extra:
            parts.append(extra)
        return "\n".join(parts)


class BackupOutputCollector:
    """Classify `restic backup --json` lines into a bounded run record."""

    def __init__(self, password: str) -> None:
        self._out = BoundedOutput(password)
        self.summary: Optional[Dict[str, Any]] = None
        self.progress: Optional[str] = None
        # The collector is the only thing that sees the whole sequence of status
        # lines, so it owns the scan state and hands it to the pure formatter.
        self.scan = ScanState()

    def feed(self, line: str) -> None:
        line = self._out.scrub(line.rstrip("\r"))
        stripped = line.strip()
        if not stripped:
            return

        parsed: Optional[Dict[str, Any]] = None
        if stripped.startswith("{"):
            try:
                candidate: Any = json.loads(stripped)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate

        if parsed is not None:
            message_type = parsed.get("message_type")
            if message_type == "status":
                # Never retained — this is the line that arrives 50x/second.
                self.scan.observe(parsed)
                self.progress = format_progress(parsed, self.scan)
                return
            if message_type == "summary":
                self.summary = parsed
                # Force-retained: it is the run's receipt and it arrives last,
                # after any per-file error flood that may have filled the cap.
                self._out.add(line, force=True)
                return

        self._out.add(line)

    def text(self) -> str:
        """The retained record, newest progress last."""
        return self._out.text(extra=self.progress)


async def pump_stream(
    stream: ByteStream, on_line: Callable[[str], Awaitable[None]]
) -> None:
    """Feed `on_line` complete lines as they arrive, with O(1) memory.

    Decoding is incremental so a multi-byte character split across two reads is
    not mangled into replacement characters, and a pathologically long line
    with no newline is flushed at the retention limit rather than growing
    without bound.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    while True:
        chunk = await stream.read(STREAM_CHUNK_BYTES)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        if "\n" in buffer:
            *complete, buffer = buffer.split("\n")
            for line in complete:
                await on_line(line)
        if len(buffer) > MAX_RETAINED_LINE_CHARS:
            await on_line(buffer)
            buffer = ""
    buffer += decoder.decode(b"", final=True)
    if buffer:
        await on_line(buffer)
