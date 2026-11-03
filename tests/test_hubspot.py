import httpx
import pytest
import respx

from app.hubspot import HubSpotClient, HubSpotError, contact_properties
from app.schemas import IntakePayload
from tests.fakes import load_fixture

BASE = "https://hubspot.test"


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    c = HubSpotClient("test-token", BASE, max_retries=3, sleep=sleeps.append)
    yield c
    c.close()


def _payload() -> IntakePayload:
    return IntakePayload.model_validate(load_fixture("intake_quote.json"))


def test_contact_properties_skip_blank_values():
    payload = IntakePayload.model_validate(load_fixture("intake_other.json"))
    props = contact_properties(payload)
    assert props == {"email": "sam.rivera@example.com", "firstname": "Sam", "lastname": "Rivera"}


@respx.mock(base_url=BASE)
def test_upsert_creates_when_not_found(respx_mock, client):
    respx_mock.post("/crm/v3/objects/contacts/search").respond(json={"total": 0, "results": []})
    create = respx_mock.post("/crm/v3/objects/contacts").respond(201, json={"id": "101"})

    assert client.upsert_contact(_payload()) == ("101", True)
    sent = create.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-token"
    assert b'"company":"Lopez Landscaping LLC"' in sent.content


@respx.mock(base_url=BASE, assert_all_called=False)
def test_upsert_updates_existing(respx_mock, client):
    respx_mock.post("/crm/v3/objects/contacts/search").respond(
        json={"total": 1, "results": [{"id": "55"}]}
    )
    patch = respx_mock.patch("/crm/v3/objects/contacts/55").respond(json={"id": "55"})
    create = respx_mock.post("/crm/v3/objects/contacts")

    assert client.upsert_contact(_payload()) == ("55", False)
    assert patch.called and not create.called


@respx.mock(base_url=BASE)
def test_upsert_recovers_from_409_conflict(respx_mock, client):
    respx_mock.post("/crm/v3/objects/contacts/search").respond(json={"results": []})
    respx_mock.post("/crm/v3/objects/contacts").respond(
        409, json={"status": "error", "message": "Contact already exists. Existing ID: 77"}
    )
    patch = respx_mock.patch("/crm/v3/objects/contacts/77").respond(json={"id": "77"})

    assert client.upsert_contact(_payload()) == ("77", False)
    assert patch.called


@respx.mock(base_url=BASE)
def test_429_then_success_respects_retry_after(respx_mock, client, sleeps):
    route = respx_mock.post("/crm/v3/objects/contacts/search").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "rate limited"}),
            httpx.Response(200, json={"results": [{"id": "9"}]}),
        ]
    )
    assert client.find_contact_by_email("a@b.com") == "9"
    assert route.call_count == 2
    assert sleeps == [2.0]


@respx.mock(base_url=BASE)
def test_500_exhausts_retries_with_backoff(respx_mock, client, sleeps):
    route = respx_mock.post("/crm/v3/objects/contacts/search").respond(500, text="oops")
    with pytest.raises(HubSpotError) as info:
        client.find_contact_by_email("a@b.com")

    assert info.value.status_code == 500
    assert route.call_count == 4  # 1 attempt + 3 retries
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2]  # exponential


@respx.mock(base_url=BASE)
def test_network_error_is_retried(respx_mock, client, sleeps):
    respx_mock.post("/crm/v3/objects/contacts/search").mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json={"results": []})]
    )
    assert client.find_contact_by_email("a@b.com") is None
    assert len(sleeps) == 1


@respx.mock(base_url=BASE)
def test_4xx_is_not_retried(respx_mock, client, sleeps):
    route = respx_mock.post("/crm/v3/objects/deals").respond(400, json={"message": "bad stage"})
    with pytest.raises(HubSpotError) as info:
        client.create_deal({"dealname": "x"}, "1")
    assert info.value.status_code == 400
    assert route.call_count == 1 and sleeps == []


@respx.mock(base_url=BASE)
def test_create_note_associates_contact_and_deal(respx_mock, client):
    route = respx_mock.post("/crm/v3/objects/notes").respond(201, json={"id": "n1"})
    assert client.create_note("<p>hi</p>", "c1", "d1") == "n1"
    body = route.calls.last.request.read()
    assert b'"associationTypeId":202' in body and b'"associationTypeId":214' in body
