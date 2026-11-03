"""Routing rules. Plain code, not the model, decides where a request goes.

The LLM supplies labels (request_type, urgency); everything below is a
deterministic lookup that a broker can read, test and change.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from app.config import Settings
from app.schemas import Classification, IntakePayload, RequestType, Urgency

LABELS: dict[RequestType, str] = {
    RequestType.QUOTE: "New quote",
    RequestType.COI: "COI request",
    RequestType.POLICY_CHANGE: "Policy change",
    RequestType.RENEWAL: "Renewal",
    RequestType.OTHER: "General inquiry",
}

# HubSpot deal "Priority" (hs_priority) internal values.
PRIORITY: dict[Urgency, str] = {
    Urgency.HIGH: "high",
    Urgency.NORMAL: "medium",
    Urgency.LOW: "low",
}


@dataclass(frozen=True)
class Route:
    pipeline_id: str
    stage_id: str
    priority: str
    owner_id: str | None
    label: str
    needs_review: bool


def route(classification: Classification, settings: Settings) -> Route:
    # Anything we could not classify confidently goes to a human first.
    needs_review = classification.needs_review or classification.request_type is RequestType.OTHER
    stage_key = "review" if needs_review else classification.request_type.value
    return Route(
        pipeline_id=settings.pipeline_id,
        stage_id=settings.stage_ids[stage_key],
        priority=PRIORITY[classification.urgency],
        owner_id=settings.owner_ids.get(stage_key),
        label=LABELS[classification.request_type],
        needs_review=needs_review,
    )


def deal_properties(
    payload: IntakePayload,
    classification: Classification,
    r: Route,
    idempotency_property: str | None,
    idempotency_key: str,
) -> dict[str, str]:
    who = payload.company or payload.name
    name = f"{r.label}: {who}"
    if classification.line_of_business:
        name += f" ({classification.line_of_business})"
    props = {
        "dealname": name[:250],
        "pipeline": r.pipeline_id,
        "dealstage": r.stage_id,
        "hs_priority": r.priority,
        "description": classification.summary,
    }
    if r.owner_id:
        props["hubspot_owner_id"] = r.owner_id
    if idempotency_property:
        props[idempotency_property] = idempotency_key
    return props


def note_html(payload: IntakePayload, classification: Classification, r: Route) -> str:
    rows = [
        ("AI summary", classification.summary),
        ("Request type", classification.request_type.value),
        ("Urgency", classification.urgency.value),
        ("Line of business", classification.line_of_business or "not stated"),
    ]
    if payload.attachment_filename:
        rows.append(("Attachment", payload.attachment_filename))
    if r.needs_review:
        rows.append(("Needs human review", classification.review_reason or "unclassified request"))

    details = "<br>".join(f"<strong>{escape(k)}:</strong> {escape(v)}" for k, v in rows)
    message = escape(payload.message).replace("\n", "<br>")
    return f"<p>{details}</p><p><strong>Original message:</strong></p><p>{message}</p>"
