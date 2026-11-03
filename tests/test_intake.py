"""End-to-end tests of POST /webhooks/intake with HubSpot and Claude mocked."""

import json
import logging

import httpx
import pytest

from tests.fakes import STAGES, HubSpotRoutes, load_fixture, post_intake, signed_headers

CASES = [
    # fixture, model says (type, urgency, line), expected stage, expected priority
    ("intake_quote.json", ("quote", "low", "commercial auto"), "stage-quote", "low"),
    ("intake_coi.json", ("coi", "high", "general liability"), "stage-coi", "high"),
    ("intake_policy_change.json", ("policy_change", "normal", "personal auto"),
     "stage-policy-change", "medium"),
    ("intake_renewal.json", ("renewal", "normal", "workers compensation"),
     "stage-renewal", "medium"),
    ("intake_other.json", ("other", "low", None), "stage-review", "low"),
]  # fmt: skip


@pytest.mark.parametrize(("fixture", "model_says", "stage", "priority"), CASES)
def test_happy_path_each_request_type(
    client, claude, hubspot, fixture, model_says, stage, priority
):
    request_type, urgency, line = model_says
    payload = load_fixture(fixture)
    claude.reply_classification(
        request_type=request_type,
        urgency=urgency,
        line_of_business=line,
        summary=f"Summary for {request_type}.",
    )

    resp = post_intake(client, payload)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "created"
    assert data["request_type"] == request_type
    assert data["urgency"] == urgency
    assert data["needs_review"] is (request_type == "other")
    assert (data["contact_id"], data["deal_id"], data["note_id"]) == (
        "contact-new",
        "deal-1",
        "note-1",
    )

    deal = HubSpotRoutes.body(hubspot.deal_create)
    props = deal["properties"]
    assert props["pipeline"] == "intake-pipeline"
    assert props["dealstage"] == stage
    assert props["hs_priority"] == priority
    assert props["description"] == f"Summary for {request_type}."
    assert len(props["intake_idempotency_key"]) == 64
    assert deal["associations"][0]["to"]["id"] == "contact-new"
    # Owner routing is code-driven config: only the COI desk has an owner here.
    assert props.get("hubspot_owner_id") == ("owner-coi-desk" if request_type == "coi" else None)

    note = HubSpotRoutes.body(hubspot.note_create)["properties"]["hs_note_body"]
    assert f"Summary for {request_type}." in note
    assert payload["message"].split()[0] in note  # original message included

    contact = HubSpotRoutes.body(hubspot.contact_create)["properties"]
    assert contact["email"] == payload["email"]


def test_note_escapes_html_in_customer_message(client, claude, hubspot):
    payload = load_fixture("intake_other.json") | {"message": "<script>alert(1)</script> hi"}
    claude.reply_classification(request_type="other", urgency="low")
    assert post_intake(client, payload).status_code == 201
    note = HubSpotRoutes.body(hubspot.note_create)["properties"]["hs_note_body"]
    assert "<script>" not in note and "&lt;script&gt;" in note


def test_existing_contact_is_updated_not_duplicated(client, claude, hubspot_mock):
    routes = HubSpotRoutes(hubspot_mock, existing_contact_id="contact-42")
    claude.reply_classification(request_type="coi", urgency="high")

    resp = post_intake(client, load_fixture("intake_coi.json"))

    assert resp.status_code == 201
    assert resp.json()["contact_id"] == "contact-42"
    assert routes.contact_update.called
    assert str(routes.contact_update.calls.last.request.url).endswith("/contacts/contact-42")
    assert not routes.contact_create.called
    assert HubSpotRoutes.body(routes.deal_create)["associations"][0]["to"]["id"] == "contact-42"


def test_duplicate_submission_does_not_create_second_deal(client, claude, hubspot):
    payload = load_fixture("intake_quote.json")
    claude.reply_classification(request_type="quote", urgency="low")

    first = post_intake(client, payload)
    second = post_intake(client, payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["deal_id"] == first.json()["deal_id"]
    assert second.json()["request_type"] == "quote"
    assert hubspot.deal_create.call_count == 1
    assert len(claude.requests) == 1  # no second LLM call either


def test_duplicate_with_cosmetic_differences_is_still_caught(client, claude, hubspot):
    payload = load_fixture("intake_quote.json")
    claude.reply_classification(request_type="quote", urgency="low")
    post_intake(client, payload)

    variant = payload | {"email": payload["email"].upper(), "message": f"  {payload['message']}\n"}
    resp = post_intake(client, variant)
    assert resp.json()["status"] == "duplicate"
    assert hubspot.deal_create.call_count == 1


def test_duplicate_found_in_hubspot_after_restart(client, claude, hubspot):
    # Local ledger is empty (e.g. new container), but HubSpot already has the deal.
    hubspot.deal_search.respond(json={"total": 1, "results": [{"id": "deal-old"}]})

    resp = post_intake(client, load_fixture("intake_quote.json"))

    assert resp.status_code == 200
    assert resp.json() | {"intake_id": None} == {
        "status": "duplicate",
        "intake_id": None,
        "request_type": None,
        "urgency": None,
        "needs_review": None,
        "contact_id": None,
        "deal_id": "deal-old",
        "note_id": None,
    }
    assert not hubspot.deal_create.called
    assert claude.requests == []


@pytest.mark.parametrize(
    "headers",
    [
        {},  # unsigned
        {"X-Timestamp": "1", "X-Signature": "sha256=deadbeef"},  # stale + wrong
        "wrong-secret",
    ],
)
def test_bad_signature_is_rejected(client, claude, hubspot, headers):
    body = json.dumps(load_fixture("intake_quote.json")).encode()
    if headers == "wrong-secret":
        headers = signed_headers(body, secret="not-the-secret")

    resp = client.post("/webhooks/intake", content=body, headers=headers)

    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid signature"}
    assert claude.requests == []
    assert not hubspot.contact_search.called


def test_signature_checked_before_payload_validation(client):
    resp = client.post("/webhooks/intake", content=b"not json at all")
    assert resp.status_code == 401


def test_invalid_payload_returns_422(client, claude, hubspot):
    resp = post_intake(client, load_fixture("intake_invalid.json"))

    assert resp.status_code == 422
    bad_fields = {err["loc"][0] for err in resp.json()["detail"]}
    assert {"name", "email", "message", "unexpected_field"} <= bad_fields
    assert "not-an-email" not in resp.text  # inputs are not echoed back
    assert claude.requests == []
    assert not hubspot.contact_search.called


def test_non_json_body_returns_422(client):
    body = b"name=Maria&email=m@example.com"
    resp = client.post("/webhooks/intake", content=body, headers=signed_headers(body))
    assert resp.status_code == 422


def test_malformed_llm_output_falls_back_and_flags_review(client, claude, hubspot):
    claude.reply(load_fixture("claude_malformed.json"))

    resp = post_intake(client, load_fixture("intake_coi.json"))

    assert resp.status_code == 201
    data = resp.json()
    assert (data["request_type"], data["urgency"], data["needs_review"]) == (
        "other",
        "normal",
        True,
    )
    props = HubSpotRoutes.body(hubspot.deal_create)["properties"]
    assert props["dealstage"] == STAGES["review"]
    assert props["hs_priority"] == "medium"
    note = HubSpotRoutes.body(hubspot.note_create)["properties"]["hs_note_body"]
    assert "Needs human review" in note and "llm_invalid_output" in note


def test_llm_api_outage_still_creates_crm_records(client, claude, hubspot):
    claude.reply({"type": "error", "error": {"type": "overloaded_error", "message": "x"}}, 529)

    resp = post_intake(client, load_fixture("intake_renewal.json"))

    assert resp.status_code == 201
    assert resp.json()["needs_review"] is True
    assert hubspot.deal_create.called and hubspot.note_create.called


def test_hubspot_429_then_success(client, claude, hubspot, sleeps):
    hubspot.contact_search.mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}, json={"message": "slow down"}),
            httpx.Response(200, json={"total": 0, "results": []}),
        ]
    )
    claude.reply_classification(request_type="quote", urgency="normal")

    resp = post_intake(client, load_fixture("intake_quote.json"))

    assert resp.status_code == 201
    assert hubspot.contact_search.call_count == 2
    assert sleeps == [1.0]
    assert hubspot.deal_create.call_count == 1


def test_hubspot_500_exhausts_retries_then_retry_succeeds(client, claude, hubspot, sleeps):
    hubspot.contact_search.respond(500, json={"message": "internal error"})
    claude.reply_classification(request_type="quote", urgency="normal")
    payload = load_fixture("intake_quote.json")

    resp = post_intake(client, payload)

    assert resp.status_code == 502
    assert resp.json()["detail"] == "CRM unavailable; safe to retry"
    assert hubspot.contact_search.call_count == 4  # 1 + 3 retries
    assert len(sleeps) == 3
    assert not hubspot.deal_create.called

    # HubSpot recovers; the sender's retry is processed normally (claim was released).
    hubspot.contact_search.respond(json={"total": 0, "results": []})
    claude.reply_classification(request_type="quote", urgency="normal")
    retry = post_intake(client, payload)
    assert retry.status_code == 201
    assert hubspot.deal_create.call_count == 1


def test_note_failure_is_resumed_without_second_deal(client, claude, hubspot):
    hubspot.note_create.respond(503)
    claude.reply_classification(request_type="coi", urgency="high")
    payload = load_fixture("intake_coi.json")

    assert post_intake(client, payload).status_code == 502

    hubspot.note_create.respond(201, json={"id": "note-late"})
    retry = post_intake(client, payload)

    assert retry.status_code == 201
    assert retry.json()["status"] == "resumed"
    assert retry.json()["note_id"] == "note-late"
    assert retry.json()["request_type"] == "coi"
    assert hubspot.deal_create.call_count == 1
    assert len(claude.requests) == 1


def test_every_run_emits_one_structured_log_line(client, claude, hubspot, caplog):
    claude.reply_classification(request_type="coi", urgency="high")
    caplog.set_level(logging.INFO, logger="intake")

    post_intake(client, load_fixture("intake_coi.json"))
    client.post("/webhooks/intake", content=b"{}")  # rejected: unsigned

    records = [r for r in caplog.records if r.getMessage() == "intake.processed"]
    assert [r.outcome for r in records] == ["created", "rejected_signature"]
    ok = records[0]
    assert ok.request_type == "coi" and ok.urgency == "high"
    assert (ok.contact_id, ok.deal_id, ok.note_id) == ("contact-new", "deal-1", "note-1")
    assert ok.latency_ms >= 0 and ok.intake_id
    assert ok.email == "d***@whitfieldroofing.com"  # PII masked


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
