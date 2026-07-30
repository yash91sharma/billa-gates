"""Pydantic schemas for mount-related endpoints."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator, model_validator

from app.api.schemas.base import UTCDateTime
from app.api.schemas.jobs import _validate_label


class RenameDestinationRequest(BaseModel):
    """Payload for POST /api/mounts/destinations/rename."""

    old_label: str
    new_label: str

    @field_validator("new_label")
    @classmethod
    def validate_new_label(cls, v: str) -> str:
        # new_label becomes destination_label on every affected job, which is
        # concatenated into the repo path — same traversal rules as job labels.
        return _validate_label(v, "new_label")

    @model_validator(mode="after")
    def labels_must_differ(self) -> "RenameDestinationRequest":
        if self.old_label == self.new_label:
            raise ValueError("old_label and new_label must be different")
        return self


class RenameDestinationResult(BaseModel):
    """Response returned after a successful destination rename."""

    # Each entry has "id" and "name" keys for the affected job.
    affected_jobs: List[Dict[str, Any]]


class DestinationUsage(BaseModel):
    """Capacity of one destination, as measured by services/destination_usage.

    The byte fields are reported exactly as the kernel gave them, so
    `used_bytes + free_bytes` does **not** equal `total_bytes`: the difference is
    `reserved_bytes` (root-reserved blocks). `free_bytes` is what a backup may
    actually write and is never derived as total - used, and `percent_used`
    divides by `total_bytes` so it agrees with the numbers beside it — see the
    module docstring of app/services/destination_usage.py for why both matter.

    Every figure is Optional because a detached, unreadable or hung destination
    yields a row with `available=false` and a reason instead of invented zeros.
    """

    label: str
    path: str
    available: bool
    unavailable_reason: Optional[str] = None
    total_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    reserved_bytes: Optional[int] = None
    percent_used: Optional[float] = None
    # Opaque device identity, exposed only so the UI can spot two labels that
    # are folders on one drive and refuse to add their capacities together.
    filesystem_id: Optional[str] = None
    is_separate_mount: Optional[bool] = None
    shares_filesystem_with: List[str] = []
    sentinel_present: Optional[bool] = None
    job_count: int = 0
    job_names: List[str] = []
    measured_at: UTCDateTime


class DestinationUsageResponse(BaseModel):
    """Response for GET /api/mounts/destinations/usage.

    `measured_at` is the **oldest** row's timestamp: it drives the page's
    "as of …" line, which must never claim to be fresher than the stalest figure
    on screen. It falls back to now when there are no destinations at all.
    """

    measured_at: UTCDateTime
    destinations: List[DestinationUsage]
