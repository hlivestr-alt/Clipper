from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


DeliveryState = Literal[
    "unassigned",
    "assigned",
    "sending",
    "sent",
    "delivery_failed",
    "cancelled",
]


class WhatsAppClaimRequest(BaseModel):
    affiliate_name: str = Field(min_length=1, max_length=200)
    affiliate_identifier: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=255)
    campaign_or_sheet_row_identifier: str | None = Field(default=None, max_length=255)
    requested_batch: int | None = Field(default=None, ge=0)


class WhatsAppAssignmentActionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=255)
    error: str | None = Field(default=None, max_length=4000)
    drive_or_media_reference: str | None = Field(default=None, max_length=2000)
    operator_reason: str | None = Field(default=None, max_length=2000)


class WhatsAppDeliveryItemRequest(BaseModel):
    relative_path: str = Field(min_length=1, max_length=1000)
    status: Literal["pending", "uploading", "sent", "failed", "outcome_uncertain"]
    whatsapp_media_id: str | None = Field(default=None, max_length=255)
    whatsapp_message_id: str | None = Field(default=None, max_length=255)
    drive_or_media_reference: str | None = Field(default=None, max_length=2000)
    error: str | None = Field(default=None, max_length=4000)


class WhatsAppAssignmentResponse(BaseModel):
    affiliate_assignment_id: str
    batch_number: int
    affiliate_name: str
    affiliate_identifier: str
    delivery_status: DeliveryState
    canonical_folder_path: str
    files: list[dict[str, Any]]
    idempotency_key: str
    version: int
    assigned_at: str


class WhatsAppStatusResponse(BaseModel):
    counts: dict[str, int]
    assignments: list[dict[str, Any]]


class WhatsAppOutboxAckRequest(BaseModel):
    success: bool
    error: str | None = Field(default=None, max_length=4000)
