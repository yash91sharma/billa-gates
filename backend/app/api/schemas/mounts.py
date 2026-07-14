"""Pydantic schemas for mount-related endpoints."""

from typing import Any, Dict, List

from pydantic import BaseModel, field_validator, model_validator

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
