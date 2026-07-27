"""Tests for app/services/run_notifications.py — settings snapshot + pushes.

Two things are being pinned here. First, that a push is gated on its own
per-event flag *and* a configured topic — the settings page exposes five
independent switches and an operator who turns one off expects silence from
that event only. Second, that a notification failure can never reach the
pipeline: ntfy is a side-effect, and an exception escaping a push would strand
the run row at status=running, which locks the job out of every future trigger.

Before this module the settings were carried through the pipeline as a plain
dict of 11 keys read back with `cast(str | None, settings.get("ntfy_topic"))` at
a dozen call sites, where a mistyped key silently disabled a notification
instead of failing.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import AppSettings
from app.services.run_notifications import RunNotifier, RunSettings

ALL_EVENTS_ON = dict(
    ntfy_server_url="https://ntfy.sh",
    ntfy_topic="alerts",
    ntfy_token="tok",
    notify_on_start=True,
    notify_on_success=True,
    notify_on_failure=True,
    notify_on_warning=True,
    notify_on_verification=True,
)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_settings(factory, **overrides) -> None:
    async with factory() as s:
        s.add(AppSettings(id=1, **overrides))
        await s.commit()


def _notifier(**overrides) -> tuple[RunNotifier, AsyncMock]:
    settings = RunSettings(**{**ALL_EVENTS_ON, **overrides})
    sender = AsyncMock()
    return RunNotifier(settings, "Photos"), sender


# ── RunSettings.load ──────────────────────────────────────────────────────────


async def test_load_reads_the_settings_row(factory):
    await _seed_settings(
        factory,
        ntfy_server_url="https://push.example",
        ntfy_topic="backups",
        ntfy_token="secret",
        notify_on_start=False,
        default_job_timeout_hours=6,
        auto_unlock=False,
        metadata_timeout_seconds=90,
    )

    settings = await RunSettings.load(factory)

    assert settings.ntfy_server_url == "https://push.example"
    assert settings.ntfy_topic == "backups"
    assert settings.ntfy_token == "secret"
    assert settings.notify_on_start is False
    assert settings.default_job_timeout_hours == 6
    assert settings.auto_unlock is False
    assert settings.metadata_timeout_seconds == 90


async def test_load_without_a_settings_row_is_silent_but_usable(factory):
    """A run can fire before startup has seeded AppSettings. It must still run
    — with the same defaults the app ships — and must not try to push to a
    server nobody configured."""
    settings = await RunSettings.load(factory)

    assert settings.ntfy_topic is None
    assert settings.notify_on_failure is False
    assert settings.default_job_timeout_hours == 24
    assert settings.auto_unlock is True
    assert settings.metadata_timeout_seconds == 600


# ── Gating ────────────────────────────────────────────────────────────────────


async def test_no_push_without_a_topic():
    """Every gate is "flag AND topic": the flags default on, so an install that
    has never configured ntfy would otherwise attempt a push on every run."""
    notifier, sender = _notifier(ntfy_topic="")

    with patch("app.services.run_notifications.send_notification", sender):
        await notifier.backup_started("documents", "main")
        await notifier.backup_succeeded(12, 3)
        await notifier.failed("boom")

    sender.assert_not_called()


@pytest.mark.parametrize(
    "flag,call",
    (
        ("notify_on_start", lambda n: n.backup_started("documents", "main")),
        ("notify_on_success", lambda n: n.backup_succeeded(12, 3)),
        ("notify_on_warning", lambda n: n.backup_warned(12, ["something"])),
        ("notify_on_failure", lambda n: n.failed("boom")),
        ("notify_on_warning", lambda n: n.canceled(12, kind_label="Backup")),
        ("notify_on_verification", lambda n: n.verification_started("structural")),
        ("notify_on_verification", lambda n: n.verification_finished(passed=True)),
    ),
)
async def test_each_event_is_gated_on_its_own_flag(flag, call):
    notifier, sender = _notifier(**{flag: False})

    with patch("app.services.run_notifications.send_notification", sender):
        await call(notifier)

    sender.assert_not_called()


async def test_the_other_flags_do_not_gate_an_event():
    """Turning success pushes off must not silence failures."""
    notifier, sender = _notifier(
        notify_on_start=False, notify_on_success=False, notify_on_warning=False
    )

    with patch("app.services.run_notifications.send_notification", sender):
        await notifier.failed("boom")

    sender.assert_called_once()


# ── Message content ───────────────────────────────────────────────────────────


async def _sent(coro_factory, **overrides) -> tuple:
    notifier, sender = _notifier(**overrides)
    with patch("app.services.run_notifications.send_notification", sender):
        await coro_factory(notifier)
    sender.assert_called_once()
    return sender.call_args


async def test_push_carries_the_server_and_token_from_settings():
    args = await _sent(lambda n: n.failed("boom"))
    assert args[0][0] == "https://ntfy.sh"
    assert args[0][1] == "alerts"
    assert args[1]["token"] == "tok"


@pytest.mark.parametrize(
    "call,title",
    (
        (lambda n: n.backup_started("documents", "main"), "Starting backup: Photos"),
        (lambda n: n.backup_succeeded(12, 3), "Backup succeeded: Photos"),
        (
            lambda n: n.backup_warned(12, ["nope"]),
            "Backup completed with warnings: Photos",
        ),
        (lambda n: n.failed("boom"), "Backup failed: Photos"),
        (lambda n: n.failed("boom", kind_label="Prune"), "Prune failed: Photos"),
        (lambda n: n.canceled(12, kind_label="Backup"), "Backup canceled: Photos"),
        (
            lambda n: n.canceled(12, kind_label="Verification"),
            "Verification canceled: Photos",
        ),
        (
            lambda n: n.verification_started("full"),
            "Verification started: Photos",
        ),
        (
            lambda n: n.verification_finished(passed=True),
            "Verification passed: Photos",
        ),
        (
            lambda n: n.verification_finished(passed=False),
            "Verification failed: Photos",
        ),
    ),
)
async def test_titles_name_the_job_and_the_outcome(call, title):
    """The title is all a phone shows on the lock screen — it has to say which
    job and what happened without being opened."""
    args = await _sent(call)
    assert args[0][2] == title


async def test_warning_body_lists_every_reason_that_applied():
    """A run can be a warning because files were unreadable, because retention
    failed, or both — a body hardcoded to one of them misinforms."""
    args = await _sent(
        lambda n: n.backup_warned(
            90, ["some files could not be read", "retention failed"]
        )
    )
    body = args[0][3]
    assert "some files could not be read" in body
    assert "retention failed" in body
    assert "90" in body


async def test_failure_body_is_truncated_to_a_push_sized_excerpt():
    args = await _sent(lambda n: n.failed("x" * 5000))
    assert len(args[0][3]) == 200


async def test_failure_body_falls_back_when_there_is_no_message():
    args = await _sent(lambda n: n.failed(""))
    assert args[0][3] == "Unknown error"


async def test_failure_body_fallback_can_name_the_step():
    args = await _sent(
        lambda n: n.failed("", fallback="Unknown error during init check")
    )
    assert args[0][3] == "Unknown error during init check"


# ── Failures never reach the pipeline ─────────────────────────────────────────


async def test_a_broken_ntfy_server_cannot_fail_a_run():
    """The lock-up this prevents: an exception here escapes the pipeline, the
    run row stays at status=running, and the overlap check then skips every
    future trigger of that job."""
    notifier, _ = _notifier()
    exploding = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch("app.services.run_notifications.send_notification", exploding):
        await notifier.failed("boom")

    exploding.assert_called_once()


async def test_a_broken_ntfy_server_is_logged(caplog):
    notifier, _ = _notifier()
    exploding = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch("app.services.run_notifications.send_notification", exploding):
        await notifier.backup_succeeded(1, 1)

    assert "send_notification failed" in caplog.text


# ── The settings snapshot is a snapshot ───────────────────────────────────────


async def test_settings_are_frozen():
    """Loaded once at the top of a pipeline and read for the rest of a run that
    may last hours; a step that could rewrite them would make the completion
    push disagree with the start push."""
    settings = RunSettings(**ALL_EVENTS_ON)
    with pytest.raises(Exception):
        settings.ntfy_topic = "somewhere-else"  # type: ignore[misc]


async def test_notifier_is_built_from_settings_for_one_job():
    settings = RunSettings(**ALL_EVENTS_ON)
    notifier = settings.notifier("Documents")
    sender = AsyncMock()

    with patch("app.services.run_notifications.send_notification", sender):
        await notifier.failed("boom")

    assert sender.call_args[0][2] == "Backup failed: Documents"


async def test_uuid_job_names_are_not_special_cased():
    """Guard against anyone deciding to format the title from an id instead."""
    name = str(uuid.uuid4())
    settings = RunSettings(**ALL_EVENTS_ON)
    sender = AsyncMock()

    with patch("app.services.run_notifications.send_notification", sender):
        await settings.notifier(name).failed("boom")

    assert name in sender.call_args[0][2]
