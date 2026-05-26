"""Shared serialization helpers for API response schemas."""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import PlainSerializer


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    """Render a datetime as a UTC ISO 8601 string ending in ``Z``.

    The DB stores naive UTC values (see ``app.db.models._utcnow``). If we
    serialize them without a tz designator, ``new Date(...)`` in the browser
    treats them as LOCAL time, which makes a PST user see UTC clock values
    rendered verbatim as PST. The trailing ``Z`` tells JS the instant is UTC
    so ``toLocaleString()`` can convert to the user's local zone.

    Naive values are assumed to be UTC. Tz-aware values are normalized to
    UTC first so a stray non-UTC datetime cannot leak the wrong offset.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


UTCDateTime = Annotated[
    datetime,
    PlainSerializer(_utc_iso, return_type=str, when_used="json"),
]
"""Use in place of ``datetime`` on API response schemas so JSON output is
always a UTC ISO 8601 string with a ``Z`` suffix. Optional fields stay
``None``; in-process ``model_dump()`` (python mode) still returns the
original ``datetime`` object.
"""
