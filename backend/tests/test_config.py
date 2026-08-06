"""Tests for ``app/core/config.py``.

The module had 0% coverage — no test imported it. It is small, but it carries
the timezone the whole deployment runs in: the README's recommended `TZ` env
var is what makes restic stamp snapshots with a local offset, what schedule
triggers are evaluated against, and what run timestamps mean. A `Settings`
that silently ignored `TZ` would move every scheduled backup by hours.
"""

import importlib

import pytest

import app.core.config as config_module
from app.core.config import Settings


def test_tz_defaults_to_utc(monkeypatch):
    """With nothing in the environment, the app runs on UTC."""
    monkeypatch.delenv("TZ", raising=False)

    assert Settings(_env_file=None).tz == "UTC"


@pytest.mark.parametrize(
    "value",
    ["America/Los_Angeles", "Europe/London", "Asia/Kolkata", "UTC"],
)
def test_tz_is_read_from_the_environment(monkeypatch, value):
    """`TZ` is the documented deployment knob; it has to actually land."""
    monkeypatch.setenv("TZ", value)

    assert Settings(_env_file=None).tz == value


def test_tz_env_var_is_matched_case_insensitively(monkeypatch):
    """The field is `tz` but the environment variable everyone sets is `TZ`.

    pydantic-settings matches case-insensitively by default; this pins that,
    because a switch to `case_sensitive = True` would leave the field on its
    UTC default while `TZ` looks correctly set in the container.
    """
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("TZ", "Australia/Sydney")

    assert Settings(_env_file=None).tz == "Australia/Sydney"


def test_unrelated_environment_variables_are_ignored(monkeypatch):
    """`extra = "ignore"` — the process environment is full of unrelated names.

    Without it, any env var in the container would raise a ValidationError at
    import time and the app would not boot.
    """
    monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "x")
    monkeypatch.setenv("RESTIC_PASSWORD", "not-a-setting")

    settings = Settings(_env_file=None)

    assert settings.tz
    assert not hasattr(settings, "some_unrelated_variable")


def test_module_exposes_an_importable_singleton():
    """Callers do `from app.core.config import settings`, not `Settings()`."""
    assert isinstance(config_module.settings, Settings)
    assert isinstance(config_module.settings.tz, str)


def test_the_module_imports_cleanly_with_a_populated_environment(monkeypatch):
    """Import must not raise — it happens at app startup, before any handler."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    reloaded = importlib.reload(config_module)

    assert reloaded.settings.tz == "Europe/Berlin"

    # Leave the module as the rest of the session found it.
    monkeypatch.undo()
    importlib.reload(config_module)
