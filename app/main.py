"""FastAPI app: POST /webhooks/intake -> verify -> validate -> classify -> HubSpot.

Run locally with:  uvicorn --factory app.main:create_app --reload
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app import idempotency
from app.classify import Classifier, build_classifier
from app.config import Settings
from app.hubspot import HubSpotClient, HubSpotError
from app.idempotency import Ledger
from app.logs import configure_logging, mask_email
from app.routing import deal_properties, note_html, route
from app.schemas import Classification, IntakePayload, IntakeResponse
from app.security import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, verify_signature

log = logging.getLogger("intake")


@dataclass
class Outcome:
    http_status: int
    body: dict[str, Any]
    log: dict[str, Any] = field(default_factory=dict)


class IntakeService:
    """The intake pipeline, independent of HTTP so it is easy to test and reuse."""

    def __init__(
        self,
        settings: Settings,
        classifier: Classifier,
        hubspot: HubSpotClient,
        ledger: Ledger,
    ) -> None:
        self.settings = settings
        self.classifier = classifier
        self.hubspot = hubspot
        self.ledger = ledger

    def handle(self, payload: IntakePayload, timestamp: int, intake_id: str) -> Outcome:
        keys = idempotency.candidate_keys(
            payload.email, payload.message, timestamp, self.settings.idempotency_window_seconds
        )
        key = keys[0]
        existing = self.ledger.claim(key, keys)
        if existing:
            return self._handle_existing(existing, payload, intake_id)

        deal_id: str | None = None
        try:
            # Second line of defence: the key is also stored on the deal in HubSpot,
            # which survives restarts and works across multiple app instances.
            prop = self.settings.hubspot_idempotency_property
            if prop and (found := self.hubspot.find_deal_by_property(prop, keys)):
                self.ledger.complete(key, deal_id=found)
                return self._duplicate(intake_id, deal_id=found, key=key, source="hubspot")

            classification = self.classifier.classify(payload)
            r = route(classification, self.settings)
            contact_id, contact_created = self.hubspot.upsert_contact(payload)
            props = deal_properties(payload, classification, r, prop, key)
            deal_id = self.hubspot.create_deal(props, contact_id)
            self.ledger.mark_deal_created(
                key, contact_id, deal_id, classification.model_dump_json()
            )
            note_id = self.hubspot.create_note(
                note_html(payload, classification, r), contact_id, deal_id
            )
            self.ledger.complete(key, note_id=note_id)
        except HubSpotError as exc:
            if deal_id is None:
                self.ledger.release(key)  # nothing was created: let the sender retry cleanly
            return Outcome(
                502,
                {"detail": "CRM unavailable; safe to retry", "intake_id": intake_id},
                {
                    "outcome": "hubspot_error",
                    "error": str(exc),
                    "hubspot_status": exc.status_code,
                    "deal_id": deal_id,
                    "idempotency_key": key[:16],
                },
            )
        except Exception:
            if deal_id is None:
                self.ledger.release(key)
            raise

        return self._success(
            "created",
            201,
            intake_id,
            key,
            classification,
            r.needs_review,
            contact_id=contact_id,
            deal_id=deal_id,
            note_id=note_id,
            extra={"contact_created": contact_created, "stage_id": r.stage_id},
        )

    def _handle_existing(
        self, entry: idempotency.LedgerEntry, payload: IntakePayload, intake_id: str
    ) -> Outcome:
        if entry.status == idempotency.PROCESSING:
            return Outcome(
                409,
                {"detail": "an identical request is already being processed"},
                {"outcome": "in_progress", "idempotency_key": entry.key[:16]},
            )
        classification = (
            Classification.model_validate_json(entry.classification_json)
            if entry.classification_json
            else None
        )
        if entry.status == idempotency.DEAL_CREATED and classification:
            # A previous attempt created the deal but failed before the note. Finish it.
            r = route(classification, self.settings)
            try:
                note_id = self.hubspot.create_note(
                    note_html(payload, classification, r), entry.contact_id, entry.deal_id
                )
            except HubSpotError as exc:
                return Outcome(
                    502,
                    {"detail": "CRM unavailable; safe to retry", "intake_id": intake_id},
                    {"outcome": "hubspot_error", "error": str(exc), "deal_id": entry.deal_id},
                )
            self.ledger.complete(entry.key, note_id=note_id)
            return self._success(
                "resumed",
                201,
                intake_id,
                entry.key,
                classification,
                r.needs_review,
                contact_id=entry.contact_id,
                deal_id=entry.deal_id,
                note_id=note_id,
            )
        return self._duplicate(
            intake_id,
            deal_id=entry.deal_id,
            key=entry.key,
            source="ledger",
            contact_id=entry.contact_id,
            note_id=entry.note_id,
            classification=classification,
            needs_review=route(classification, self.settings).needs_review
            if classification
            else None,
        )

    def _success(
        self,
        status: str,
        http_status: int,
        intake_id: str,
        key: str,
        classification: Classification,
        needs_review: bool,
        *,
        contact_id: str | None,
        deal_id: str | None,
        note_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> Outcome:
        response = IntakeResponse(
            status=status,
            intake_id=intake_id,
            request_type=classification.request_type,
            urgency=classification.urgency,
            needs_review=needs_review,
            contact_id=contact_id,
            deal_id=deal_id,
            note_id=note_id,
        )
        fields = {
            "outcome": status,
            "idempotency_key": key[:16],
            "request_type": classification.request_type.value,
            "urgency": classification.urgency.value,
            "line_of_business": classification.line_of_business,
            "needs_review": needs_review,
            "review_reason": classification.review_reason,
            "contact_id": contact_id,
            "deal_id": deal_id,
            "note_id": note_id,
            **(extra or {}),
        }
        return Outcome(http_status, response.model_dump(mode="json"), fields)

    def _duplicate(
        self,
        intake_id: str,
        *,
        deal_id: str | None,
        key: str,
        source: str,
        contact_id: str | None = None,
        note_id: str | None = None,
        classification: Classification | None = None,
        needs_review: bool | None = None,
    ) -> Outcome:
        response = IntakeResponse(
            status="duplicate",
            intake_id=intake_id,
            request_type=classification.request_type if classification else None,
            urgency=classification.urgency if classification else None,
            needs_review=needs_review,
            contact_id=contact_id,
            deal_id=deal_id,
            note_id=note_id,
        )
        fields = {
            "outcome": "duplicate",
            "duplicate_source": source,
            "idempotency_key": key[:16],
            "deal_id": deal_id,
        }
        return Outcome(200, response.model_dump(mode="json"), fields)


def create_app(
    settings: Settings | None = None,
    *,
    classifier: Classifier | None = None,
    hubspot: HubSpotClient | None = None,
    ledger: Ledger | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    classifier = classifier or build_classifier(
        settings.anthropic_api_key,
        settings.anthropic_model,
        settings.anthropic_timeout_seconds,
        settings.anthropic_max_retries,
    )
    hubspot = hubspot or HubSpotClient(
        settings.hubspot_token, settings.hubspot_base_url, max_retries=settings.hubspot_max_retries
    )
    ledger = ledger or Ledger(settings.idempotency_db_path)
    service = IntakeService(settings, classifier, hubspot, ledger)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        hubspot.close()

    app = FastAPI(title="Brokerage Intake", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/webhooks/intake",
        response_model=IntakeResponse,
        status_code=201,
        responses={401: {}, 409: {}, 422: {}, 502: {}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": IntakePayload.model_json_schema()}},
            }
        },
    )
    async def intake(request: Request) -> JSONResponse:
        started = time.perf_counter()
        intake_id = uuid.uuid4().hex[:12]
        fields: dict[str, Any] = {"intake_id": intake_id}
        outcome: Outcome | None = None
        try:
            # Verify the signature over the raw bytes *before* parsing anything.
            body = await request.body()
            try:
                ts = verify_signature(
                    settings.webhook_secret,
                    request.headers.get(TIMESTAMP_HEADER),
                    request.headers.get(SIGNATURE_HEADER),
                    body,
                    tolerance_seconds=settings.signature_tolerance_seconds,
                )
            except SignatureError as exc:
                reject = {"outcome": "rejected_signature", "reason": str(exc)}
                outcome = Outcome(401, {"detail": "invalid signature"}, reject)
                return JSONResponse(outcome.body, status_code=401)

            try:
                payload = IntakePayload.model_validate_json(body)
            except ValidationError as exc:
                errors = exc.errors(include_url=False, include_context=False, include_input=False)
                outcome = Outcome(422, {"detail": errors}, {"outcome": "rejected_payload"})
                return JSONResponse(outcome.body, status_code=422)

            fields["email"] = mask_email(payload.email)
            outcome = await run_in_threadpool(service.handle, payload, ts, intake_id)
            return JSONResponse(outcome.body, status_code=outcome.http_status)
        except Exception:
            fields["outcome"] = "error"
            log.exception("intake.failed", extra=fields)
            raise
        finally:
            if outcome is not None:
                fields.update(outcome.log)
                fields["http_status"] = outcome.http_status
                fields["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
                level = logging.INFO if outcome.http_status < 500 else logging.ERROR
                log.log(level, "intake.processed", extra=fields)

    return app
