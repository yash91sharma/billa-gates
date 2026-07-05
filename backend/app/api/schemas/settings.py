"""Pydantic schemas for AppSettings and health/utility endpoints."""

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_TOPIC_RE = re.compile(r"^[a-zA-Z0-9_-]{0,64}$")
_MAX_URL_LENGTH = 512


class SettingsUpdate(BaseModel):
    """Fields accepted when updating AppSettings via PUT /api/settings."""

    ntfy_server_url: str = Field(max_length=_MAX_URL_LENGTH)
    ntfy_topic: str = Field(default="", max_length=64)
    ntfy_token: Optional[str] = None
    notify_on_start: bool = True
    notify_on_success: bool = True
    notify_on_failure: bool = True
    notify_on_warning: bool = True
    notify_on_verification: bool = True
    default_job_timeout_hours: int = Field(24, ge=1, le=168)
    keep_last_runs: int = Field(100, ge=1, le=10000)
    auto_unlock: bool = True
    metadata_timeout_seconds: int = Field(600, ge=10, le=86400)

    @field_validator("ntfy_server_url")
    @classmethod
    def validate_ntfy_url(cls, v: str) -> str:
        """Reject any URL that does not use http or https."""
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("ntfy_server_url must start with http:// or https://")
        return v

    @field_validator("ntfy_topic")
    @classmethod
    def validate_ntfy_topic(cls, v: str) -> str:
        """Allow only alphanumeric chars, hyphens, and underscores (or empty)."""
        if v and not _TOPIC_RE.match(v):
            raise ValueError(
                "ntfy_topic may only contain letters, digits, '-', and '_'"
            )
        return v


class SettingsResponse(BaseModel):
    """Full settings record returned by GET/PUT /api/settings.

    Mirrors ``SettingsUpdate`` fields plus computed/server-owned ones, with
    one deliberate difference: ``ntfy_token`` is always ``None`` (the value
    is stored but never exposed back to the client). Defined separately
    rather than inheriting from ``SettingsUpdate`` so the token-narrowing is
    a real type, not an LSP override.
    """

    id: int
    ntfy_server_url: str
    ntfy_topic: str
    ntfy_token: None = None
    notify_on_start: bool
    notify_on_success: bool
    notify_on_failure: bool
    notify_on_warning: bool
    notify_on_verification: bool
    default_job_timeout_hours: int
    keep_last_runs: int
    auto_unlock: bool
    metadata_timeout_seconds: int
    restic_version: Optional[str] = None


class HealthResponse(BaseModel):
    """Response shape for GET /api/health."""

    scheduler_running: bool
    restic_version: Optional[str]
    db_ok: bool


class NtfyTestResult(BaseModel):
    """Response shape for POST /api/settings/test-ntfy."""

    ok: bool
    error: Optional[str] = None


class ResticUpdateCheck(BaseModel):
    """Response shape for GET /api/settings/restic-update-check."""

    # Currently installed version (from AppSettings), or None if not detected.
    current: Optional[str]
    # Latest version from GitHub releases API, or None if the check failed.
    latest: Optional[str]
    # True when latest > current; None when comparison is not possible.
    update_available: Optional[bool]
