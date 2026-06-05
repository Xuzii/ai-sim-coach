"""P2-C0: the provider-agnostic LLM seam (neutral types + MockLLMClient)."""

from __future__ import annotations

import pytest

from pitwall.coach.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    MockLLMClient,
    ToolCall,
    ToolSpec,
)


def test_llm_response_wants_tools():
    assert not LLMResponse(text="done").wants_tools
    assert LLMResponse(tool_calls=[ToolCall(id="1", name="get_lap_summary", arguments={"lap_id": 2})]).wants_tools


def test_llm_client_is_abstract():
    with pytest.raises(TypeError):
        LLMClient()  # type: ignore[abstract]


def test_mock_client_returns_script_in_order_and_records_calls():
    script = [
        LLMResponse(tool_calls=[ToolCall(id="a", name="get_lap_summary", arguments={"lap_id": 2})]),
        LLMResponse(text="all done"),
    ]
    client = MockLLMClient(script=script)
    tools = [ToolSpec("get_lap_summary", "desc", {"type": "object"})]

    first = client.generate([Message(role="user", content="analyse lap 2")], tools, system="be a coach")
    assert first.wants_tools and first.tool_calls[0].name == "get_lap_summary"
    second = client.generate([Message(role="user", content="x")], tools)
    assert second.text == "all done"

    assert len(client.calls) == 2
    assert client.calls[0]["system"] == "be a coach"
    assert client.calls[0]["tools"][0].name == "get_lap_summary"


def test_mock_client_raises_when_script_exhausted():
    client = MockLLMClient(script=[LLMResponse(text="only one")])
    client.generate([], [])
    with pytest.raises(AssertionError):
        client.generate([], [])
