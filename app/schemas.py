"""Pydantic request/response models for the three-way merge API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .merge import DELETE_TOKEN


class MergeCreateRequest(BaseModel):
    """The three versions of a configuration document."""

    base: Any = Field(..., description="Common ancestor version of the document")
    left: Any = Field(..., description="One side's modified version")
    right: Any = Field(..., description="The other side's modified version")


class ResolutionRequest(BaseModel):
    """A batch of conflict resolutions submitted by the client."""

    revision: int = Field(
        ..., ge=1, description="Revision the client last observed from the server"
    )
    resolutions: dict[str, Any] = Field(
        ...,
        description=(
            "Map of JSON Pointer -> chosen value. "
            f"Use {DELETE_TOKEN!r} as the value to delete the key."
        ),
    )


class Conflict(BaseModel):
    pointer: str = Field(..., description="JSON Pointer (RFC 6901) of the conflict")
    base: Any
    left: Any
    right: Any


class MergeResponse(BaseModel):
    merge_id: str
    revision: int
    status: Literal["open", "resolved"]
    conflicts: list[Conflict]
    merged: Any = Field(
        ...,
        description=(
            "Working document; unresolved conflicts appear as "
            '{"__conflict__": {...}} placeholders while status is "open".'
        ),
    )
    result: Any | None = Field(
        default=None,
        description="Immutable final document, present only once status is resolved.",
    )
    created_at: str
    updated_at: str
