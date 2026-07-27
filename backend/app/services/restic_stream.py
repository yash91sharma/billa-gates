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
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)

# Retained-output ceiling. Generous for a post-mortem (thousands of error
# lines) and still small enough to sit in a DB row and a run-detail response.
MAX_RETAINED_OUTPUT_CHARS: int = 256 * 1024
MAX_RETAINED_LINE_CHARS: int = 8 * 1024
STREAM_CHUNK_BYTES: int = 65536


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


def _format_eta(seconds: object) -> Optional[str]:
    """Render restic's `seconds_remaining` as a short ETA, or None if absent.

    restic omits it until it has scanned enough to estimate.
    """
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    total = int(seconds)
    if total <= 0:
        return None
    if total < 60:
        return f"eta {total}s"
    if total < 3600:
        return f"eta {total // 60}m"
    return f"eta {total // 3600}h {(total % 3600) // 60}m"


def format_progress(status: Dict[str, Any]) -> str:
    """Collapse one restic `message_type=status` line into a single line.

    Only what an operator watching a run wants to know — how far along, how
    many files, how many bytes, how much longer, and how many items restic
    could not read. Everything else in the status message (the rotating
    `current_files` list above all) is noise once it is one second old.

    `error_count` matters out of proportion to its size: files that fail during
    the scan never enter `total_files`, so a run that ends `warning` can show a
    spotless `100% · 1,234/1,234 files`. Without the error tally the progress
    line flatly contradicts the badge next to it.
    """
    parts: List[str] = []

    percent = status.get("percent_done")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        parts.append(f"{percent * 100:.0f}%")

    files_done = status.get("files_done")
    total_files = status.get("total_files")
    if isinstance(files_done, int) and isinstance(total_files, int):
        parts.append(f"{files_done:,}/{total_files:,} files")
    elif isinstance(files_done, int):
        parts.append(f"{files_done:,} files")

    bytes_done = _format_bytes(status.get("bytes_done"))
    total_bytes = _format_bytes(status.get("total_bytes"))
    if bytes_done and total_bytes:
        parts.append(f"{bytes_done}/{total_bytes}")
    elif bytes_done:
        parts.append(bytes_done)

    eta = _format_eta(status.get("seconds_remaining"))
    if eta:
        parts.append(eta)

    error_count = status.get("error_count")
    if (
        isinstance(error_count, int)
        and not isinstance(error_count, bool)
        and error_count > 0
    ):
        parts.append(f"{error_count:,} error{'' if error_count == 1 else 's'}")

    return f"progress: {' · '.join(parts)}" if parts else "progress: running"


class BoundedOutput:
    """Fixed-size accumulator for a subprocess stream.

    Keeps whole lines up to `max_chars`, truncates any single oversized line,
    and counts what it had to drop so the caller can say output was omitted
    rather than silently losing it.
    """

    def __init__(
        self, password: str, max_chars: int = MAX_RETAINED_OUTPUT_CHARS
    ) -> None:
        self._password = password
        self._max_chars = max_chars
        self._lines: List[str] = []
        self._chars = 0
        self._dropped = 0

    def scrub(self, line: str) -> str:
        """Strip the repo password. Per line now, since there is no longer a
        whole-stream string to run a single replace() over."""
        return line.replace(self._password, "") if self._password else line

    def add(self, line: str, *, force: bool = False) -> None:
        """Retain one line, unless that would push us past the ceiling.

        `force` is for the lines that must survive at any cost (the summary),
        which arrive last and would otherwise be lost behind an error flood.
        """
        if len(line) > MAX_RETAINED_LINE_CHARS:
            line = line[:MAX_RETAINED_LINE_CHARS] + "…<truncated>"
        if not force and self._chars + len(line) > self._max_chars:
            self._dropped += 1
            return
        self._lines.append(line)
        self._chars += len(line) + 1

    def text(self, *, extra: Optional[str] = None) -> str:
        parts = list(self._lines)
        if self._dropped:
            parts.append(
                f"... {self._dropped} more output line(s) omitted "
                f"(retained output is capped at {self._max_chars} characters)"
            )
        if extra:
            parts.append(extra)
        return "\n".join(parts)


class BackupOutputCollector:
    """Classify `restic backup --json` lines into a bounded run record."""

    def __init__(self, password: str) -> None:
        self._out = BoundedOutput(password)
        self.summary: Optional[Dict[str, Any]] = None
        self.progress: Optional[str] = None

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
                self.progress = format_progress(parsed)
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
