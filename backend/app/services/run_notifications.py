"""What the app's settings mean to a run, and the pushes a run sends.

A pipeline reads ``AppSettings`` exactly once, at the top, into the frozen
:class:`RunSettings` snapshot below, and everything downstream — the timeouts,
the auto-unlock decision, and every ntfy push — comes from that one read. The
snapshot is frozen because a backup can run for hours: a step that rewrote it
would make the completion push disagree with the start push.

This replaces a plain ``dict`` of eleven keys that was threaded through the
backup pipeline and read back as
``cast(str | None, settings_dict.get("ntfy_server_url"))`` at a dozen call
sites. A mistyped key there did not fail — it evaluated to ``None``, which is
falsy, so the effect of a typo was a notification that silently stopped being
sent.

Two rules hold for every push:

* **flag AND topic.** Each event has its own switch on the settings page, and
  the switches default on, so an install that has never configured ntfy would
  otherwise attempt a push on every single run.
* **a push can never fail a run.** ntfy is a side-effect. An exception escaping
  one would leave the run row at ``status=running``, and the overlap check
  would then skip every future trigger of that job — a broken notification
  server would stop backups. Every send is wrapped.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

from app.core.logging import get_logger, log_call
from app.db.models import AppSettings
from app.services.notifications import send_notification
from app.services.run_records import SessionFactory

logger = get_logger(__name__)

# How much of an error lands in a push body. ntfy renders a notification, not a
# log viewer; the run page holds the full text.
_ERROR_EXCERPT_CHARS: int = 200

# Used when a pipeline has nothing to say about why it failed. Rare, but a push
# whose body is empty tells the operator strictly less than the title did.
_UNKNOWN_ERROR: str = "Unknown error"


async def _try_notify(*args: Any, **kwargs: Any) -> None:
    """Send a push, absorbing any failure.

    A transient ntfy/network failure must be logged but must never crash the
    backup pipeline — see the module docstring for what a stranded run row
    costs.
    """
    try:
        await send_notification(*args, **kwargs)
    except Exception as exc:
        logger.warning(f"send_notification failed (non-fatal): {exc!r}")


@dataclass(frozen=True)
class RunSettings:
    """The settings snapshot a pipeline takes at startup.

    Defaults are the ones that apply when no ``AppSettings`` row exists yet — a
    run can be triggered before startup has seeded it. Note that every notify
    flag defaults **off** here while the column default is on: with no row
    there is no configured topic either, so pushing is not merely undesired,
    there is nowhere to push to.
    """

    ntfy_server_url: Optional[str] = None
    ntfy_topic: Optional[str] = None
    ntfy_token: Optional[str] = None
    notify_on_start: bool = False
    notify_on_success: bool = False
    notify_on_failure: bool = False
    notify_on_warning: bool = False
    notify_on_verification: bool = False
    default_job_timeout_hours: int = 24
    auto_unlock: bool = True
    metadata_timeout_seconds: int = 600

    @classmethod
    @log_call
    async def load(cls, factory: SessionFactory) -> "RunSettings":
        """Read the singleton settings row, or return the shipped defaults."""
        async with factory() as s:
            row: AppSettings | None = await s.get(AppSettings, 1)
            if row is None:
                logger.info("no AppSettings row; running with defaults")
                return cls()
            return cls(
                ntfy_server_url=row.ntfy_server_url,
                ntfy_topic=row.ntfy_topic,
                ntfy_token=row.ntfy_token,
                notify_on_start=row.notify_on_start,
                notify_on_success=row.notify_on_success,
                notify_on_failure=row.notify_on_failure,
                notify_on_warning=row.notify_on_warning,
                notify_on_verification=row.notify_on_verification,
                default_job_timeout_hours=row.default_job_timeout_hours,
                auto_unlock=row.auto_unlock,
                metadata_timeout_seconds=row.metadata_timeout_seconds,
            )

    def notifier(self, job_name: str) -> "RunNotifier":
        """The push helper for one job's run."""
        return RunNotifier(self, job_name)


class RunNotifier:
    """The pushes one run may send, each gated on its own setting.

    Every method is a no-op unless its event is switched on and a topic is
    configured, so pipelines call them unconditionally: the decision about
    whether the operator wants to hear about something lives here, not spread
    through the run steps.
    """

    def __init__(self, settings: RunSettings, job_name: str) -> None:
        self._settings = settings
        self._job_name = job_name

    async def _push(self, enabled: bool, title: str, message: str) -> None:
        if not enabled or not self._settings.ntfy_topic:
            return
        await _try_notify(
            self._settings.ntfy_server_url,
            self._settings.ntfy_topic,
            title,
            message,
            token=self._settings.ntfy_token,
        )

    async def backup_started(self, source_label: str, destination_label: str) -> None:
        await self._push(
            self._settings.notify_on_start,
            f"Starting backup: {self._job_name}",
            f"Source: {source_label}, Destination: {destination_label}",
        )

    async def backup_succeeded(
        self, duration_seconds: Optional[int], files_changed: Optional[int]
    ) -> None:
        await self._push(
            self._settings.notify_on_success,
            f"Backup succeeded: {self._job_name}",
            f"Duration: {duration_seconds}s, Files: {files_changed}",
        )

    async def backup_warned(
        self, duration_seconds: Optional[int], reasons: List[str]
    ) -> None:
        """Name what actually went wrong.

        A run is a warning because files were unreadable, because retention
        failed, or because retention was deliberately held back — or several of
        those at once. The caller assembles the reasons that really occurred;
        a body hardcoded to one of them misinforms the operator.
        """
        await self._push(
            self._settings.notify_on_warning,
            f"Backup completed with warnings: {self._job_name}",
            f"Duration: {duration_seconds}s — {'; '.join(reasons)}.",
        )

    async def failed(
        self,
        message: Optional[str],
        *,
        kind_label: str = "Backup",
        fallback: str = _UNKNOWN_ERROR,
    ) -> None:
        """`kind_label` names the operation that failed — Backup, Prune or
        Verification — since all three share this path and the title is the
        only place the distinction shows."""
        await self._push(
            self._settings.notify_on_failure,
            f"{kind_label} failed: {self._job_name}",
            (message or "")[:_ERROR_EXCERPT_CHARS] if message else fallback,
        )

    async def canceled(
        self, duration_seconds: Optional[int], *, kind_label: str
    ) -> None:
        """Gated on the warning switch: a cancel is not a failure — the operator
        asked for it — but it is not a clean outcome either."""
        await self._push(
            self._settings.notify_on_warning,
            f"{kind_label} canceled: {self._job_name}",
            f"Duration: {duration_seconds}s — canceled by user.",
        )

    async def verification_started(self, check_mode: str) -> None:
        await self._push(
            self._settings.notify_on_verification,
            f"Verification started: {self._job_name}",
            f"Running integrity check (mode: {check_mode})...",
        )

    async def verification_finished(self, *, passed: bool) -> None:
        status: str = "passed" if passed else "failed"
        await self._push(
            self._settings.notify_on_verification,
            f"Verification {status}: {self._job_name}",
            f"Check status: {status}",
        )
