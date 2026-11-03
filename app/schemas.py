"""Pydantic models for the webhook payload, the LLM classification and the API response."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RequestType(StrEnum):
    QUOTE = "quote"
    COI = "coi"
    POLICY_CHANGE = "policy_change"
    RENEWAL = "renewal"
    OTHER = "other"


class Urgency(StrEnum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


class IntakePayload(BaseModel):
    """An inbound request from the web form or the email-to-webhook bridge."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str | None = Field(default=None, max_length=40)
    company: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=10_000)
    attachment_filename: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("phone", "company", "attachment_filename")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value or None


class LLMClassification(BaseModel):
    """Exactly what the model is allowed to return. Anything else fails validation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_type: RequestType
    urgency: Urgency
    line_of_business: str | None = Field(default=None, max_length=100)
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("line_of_business")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value or None


class Classification(LLMClassification):
    """The model's answer plus flags set by our code (never by the model)."""

    needs_review: bool = False
    review_reason: str | None = None


class IntakeResponse(BaseModel):
    status: str  # "created" | "duplicate" | "resumed"
    intake_id: str
    request_type: RequestType
    urgency: Urgency
    needs_review: bool
    contact_id: str | None = None
    deal_id: str | None = None
    note_id: str | None = None
