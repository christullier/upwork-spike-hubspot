from app.classify import FALLBACK_SUMMARY, TOOL_NAME, Classifier
from app.schemas import IntakePayload, RequestType, Urgency
from tests.fakes import FakeClaude, load_fixture


def _payload(name: str = "intake_coi.json") -> IntakePayload:
    return IntakePayload.model_validate(load_fixture(name))


def test_valid_tool_call_is_parsed():
    fake = FakeClaude().reply_classification(
        request_type="coi",
        urgency="high",
        line_of_business="general liability",
        summary="Roofer needs a COI naming the GC as additional insured before Monday.",
    )
    result = Classifier(fake.client(), "claude-test").classify(_payload())

    assert result.request_type is RequestType.COI
    assert result.urgency is Urgency.HIGH
    assert result.line_of_business == "general liability"
    assert result.needs_review is False


def test_request_forces_tool_and_wraps_message_as_data():
    fake = FakeClaude().reply_classification(request_type="coi", urgency="high")
    Classifier(fake.client(), "claude-test").classify(_payload())

    sent = fake.request_json()
    assert sent["model"] == "claude-test"
    assert sent["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert "<message>" in sent["messages"][0]["content"]
    assert "Riverside Apartments" in sent["messages"][0]["content"]


def test_malformed_output_falls_back_and_flags_review():
    fake = FakeClaude().reply(load_fixture("claude_malformed.json"))
    result = Classifier(fake.client(), "claude-test").classify(_payload())

    assert result.request_type is RequestType.OTHER
    assert result.urgency is Urgency.NORMAL
    assert result.needs_review is True
    assert result.review_reason == "llm_invalid_output"
    assert result.summary == FALLBACK_SUMMARY


def test_extra_fields_from_model_are_rejected():
    fake = FakeClaude().reply_classification(
        request_type="quote", urgency="low", route_to="ceo@brokerage.com"
    )
    result = Classifier(fake.client(), "claude-test").classify(_payload())
    assert result.needs_review is True


def test_text_only_response_falls_back():
    body = load_fixture("claude_malformed.json")
    body["content"] = [{"type": "text", "text": "I think this is a COI request."}]
    body["stop_reason"] = "end_turn"
    fake = FakeClaude().reply(body)
    result = Classifier(fake.client(), "claude-test").classify(_payload())
    assert result.needs_review is True
    assert result.review_reason == "llm_invalid_output"


def test_api_error_falls_back():
    fake = FakeClaude().reply(
        {"type": "error", "error": {"type": "api_error", "message": "boom"}}, status=500
    )
    result = Classifier(fake.client(), "claude-test").classify(_payload())
    assert result.request_type is RequestType.OTHER
    assert result.needs_review is True
    assert result.review_reason.startswith("llm_api_error:")
