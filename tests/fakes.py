"""Test doubles: fixture loading and a fake Anthropic HTTP layer.

The Anthropic SDK (2.x) sends requests through ``httpx2``, which respx does not
patch, so Claude calls are mocked with ``httpx2.MockTransport`` instead. HubSpot
calls go through ``httpx`` and are mocked with respx.
"""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import httpx2

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def claude_tool_message(tool_input: dict) -> dict:
    """A Messages API response body containing one record_classification tool call."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_test",
                "name": "record_classification",
                "input": tool_input,
            }
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 40},
    }


class FakeClaude:
    """Records requests and replies with queued (status, body) responses."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses: list[tuple[int, dict]] = []

    def reply(self, body: dict, status: int = 200) -> FakeClaude:
        self._responses.append((status, body))
        return self

    def reply_classification(self, **tool_input) -> FakeClaude:
        tool_input.setdefault("line_of_business", None)
        tool_input.setdefault("summary", "Customer request.")
        return self.reply(claude_tool_message(tool_input))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("unexpected Claude API call")
        status, body = self._responses.pop(0)
        return httpx2.Response(status, json=body)

    def client(self) -> anthropic.Anthropic:
        transport = httpx2.MockTransport(self._handle)
        return anthropic.Anthropic(
            api_key="test-key",
            base_url="https://anthropic.test",
            max_retries=0,
            http_client=httpx2.Client(transport=transport),
        )

    def request_json(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)
