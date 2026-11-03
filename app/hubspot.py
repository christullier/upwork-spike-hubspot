"""Minimal HubSpot CRM v3 client: contacts, deals, notes, with retry/backoff.

Only the endpoints this service needs are wrapped. Every call goes through
``_request``, which retries 429 and 5xx responses (and network errors) with
exponential backoff, honouring ``Retry-After`` when HubSpot sends it.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.schemas import IntakePayload

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# HubSpot-defined default association type IDs (CRM v4 association types).
ASSOC_DEAL_TO_CONTACT = 3
ASSOC_NOTE_TO_CONTACT = 202
ASSOC_NOTE_TO_DEAL = 214

_EXISTING_ID = re.compile(r"Existing ID:\s*(\d+)")


class HubSpotError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _association(to_id: str, type_id: int) -> dict:
    return {
        "to": {"id": to_id},
        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": type_id}],
    }


def contact_properties(payload: IntakePayload) -> dict[str, str]:
    """Map the intake payload to HubSpot contact properties, skipping empty values."""
    first, _, last = payload.name.partition(" ")
    props = {
        "email": payload.email,
        "firstname": first,
        "lastname": last.strip(),
        "phone": payload.phone or "",
        "company": payload.company or "",
    }
    # Never overwrite a value already in the CRM with a blank one.
    return {k: v for k, v in props.items() if v}


class HubSpotClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.hubapi.com",
        *,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_backoff: float = 30.0,
        timeout: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # ---- transport -------------------------------------------------------

    def _retry_delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and (header := response.headers.get("Retry-After")):
            try:
                return min(float(header), self.max_backoff)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(header).timestamp() - time.time()
                    return min(max(wait, 0.0), self.max_backoff)
                except (TypeError, ValueError):
                    pass
        delay = self.backoff_base * (2**attempt)
        return min(delay + random.uniform(0, self.backoff_base), self.max_backoff)  # noqa: S311

    def _request(self, method: str, path: str, json: dict | None = None) -> dict[str, Any]:
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = self._http.request(method, path, json=json)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise HubSpotError(f"{method} {path} failed: {exc!r}") from exc
                reason = type(exc).__name__
            else:
                if response.status_code < 400:
                    return response.json() if response.content else {}
                if response.status_code not in RETRYABLE_STATUS or attempt >= self.max_retries:
                    raise HubSpotError(
                        f"{method} {path} -> {response.status_code}",
                        status_code=response.status_code,
                        body=response.text[:1000],
                    )
                reason = str(response.status_code)

            delay = self._retry_delay(attempt, response)
            log.warning(
                "hubspot retry",
                extra={"path": path, "reason": reason, "attempt": attempt + 1, "delay_s": delay},
            )
            self._sleep(delay)
            attempt += 1

    # ---- contacts --------------------------------------------------------

    def find_contact_by_email(self, email: str) -> str | None:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email"],
            "limit": 1,
        }
        results = self._request("POST", "/crm/v3/objects/contacts/search", body).get("results", [])
        return results[0]["id"] if results else None

    def upsert_contact(self, payload: IntakePayload) -> tuple[str, bool]:
        """Return (contact_id, created). Dedupes on email."""
        props = contact_properties(payload)
        contact_id = self.find_contact_by_email(payload.email)
        if contact_id:
            self._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
            return contact_id, False
        try:
            created = self._request("POST", "/crm/v3/objects/contacts", {"properties": props})
            return created["id"], True
        except HubSpotError as exc:
            # Lost a race with another request (or search index lag): HubSpot says
            # "Contact already exists. Existing ID: 123". Update that one instead.
            match = _EXISTING_ID.search(exc.body) if exc.status_code == 409 else None
            if not match:
                raise
            contact_id = match.group(1)
            self._request("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
            return contact_id, False

    # ---- deals -----------------------------------------------------------

    def find_deal_by_property(self, prop: str, values: list[str]) -> str | None:
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": prop, "operator": "IN", "values": values}]}
            ],
            "properties": [prop],
            "limit": 1,
        }
        results = self._request("POST", "/crm/v3/objects/deals/search", body).get("results", [])
        return results[0]["id"] if results else None

    def create_deal(self, properties: dict[str, str], contact_id: str) -> str:
        body = {
            "properties": properties,
            "associations": [_association(contact_id, ASSOC_DEAL_TO_CONTACT)],
        }
        return self._request("POST", "/crm/v3/objects/deals", body)["id"]

    # ---- notes -----------------------------------------------------------

    def create_note(self, body_html: str, contact_id: str, deal_id: str) -> str:
        body = {
            "properties": {
                "hs_timestamp": str(int(time.time() * 1000)),
                "hs_note_body": body_html,
            },
            "associations": [
                _association(contact_id, ASSOC_NOTE_TO_CONTACT),
                _association(deal_id, ASSOC_NOTE_TO_DEAL),
            ],
        }
        return self._request("POST", "/crm/v3/objects/notes", body)["id"]

    # ---- setup helpers ---------------------------------------------------

    def get_object(
        self, object_type: str, object_id: str, properties: list[str], associations: list[str] = ()
    ) -> dict:
        query = f"properties={','.join(properties)}"
        if associations:
            query += f"&associations={','.join(associations)}"
        return self._request("GET", f"/crm/v3/objects/{object_type}/{object_id}?{query}")

    def account_details(self) -> dict:
        """Portal ID and UI domain, used to print record links."""
        return self._request("GET", "/account-info/v3/details")

    def list_deal_pipelines(self) -> list[dict]:
        return self._request("GET", "/crm/v3/pipelines/deals").get("results", [])
