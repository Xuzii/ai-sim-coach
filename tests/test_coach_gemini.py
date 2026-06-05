"""P2-C1: ``GeminiClient`` translation tests -- no network, no real google-genai
call. We patch the SDK Client so we capture the request and craft fake responses
to drive the parser.

The point of these tests is to lock down the translation layer between the
neutral seam (Message / ToolSpec / ToolCall / LLMResponse) and the SDK's
Content / Part / FunctionDeclaration / Tool types, so a future SDK bump (or a
Claude provider drop-in) doesn't silently break the schema we send.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pitwall.coach.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from pitwall.coach.llm.gemini import GeminiClient, _clean_schema


@pytest.fixture
def fake_response_with_text():
    """A fake google-genai response carrying a single text part."""
    part = SimpleNamespace(text="hello", function_call=None)
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


@pytest.fixture
def fake_response_with_function_call():
    """A fake google-genai response with one text part + two function_call parts."""
    text_part = SimpleNamespace(text="thinking...", function_call=None)
    fc1 = SimpleNamespace(name="get_lap_summary", args={"lap_id": 2})
    fc2 = SimpleNamespace(name="get_sector_analysis", args={"lap_id": 2})
    call_part_1 = SimpleNamespace(text=None, function_call=fc1)
    call_part_2 = SimpleNamespace(text=None, function_call=fc2)
    content = SimpleNamespace(parts=[text_part, call_part_1, call_part_2])
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)])


@pytest.fixture
def patched_client(monkeypatch):
    """Build a GeminiClient with a fake ``google.genai.Client`` so no real
    network is touched. Returns ``(client, mock_models)`` -- assign
    ``mock_models.generate_content.return_value`` to a fake response."""
    fake_models = MagicMock()
    fake_client = MagicMock(models=fake_models)

    import google.genai as genai_module

    monkeypatch.setattr(genai_module, "Client", lambda *a, **kw: fake_client)
    client = GeminiClient(api_key="fake-key", model="gemini-2.5-flash", temperature=0.1)
    return client, fake_models


def test_generate_parses_text_only_response(patched_client, fake_response_with_text):
    client, models = patched_client
    models.generate_content.return_value = fake_response_with_text

    result = client.generate(
        messages=[Message(role="user", content="hi")],
        tools=[],
        system="be a coach",
    )

    assert isinstance(result, LLMResponse)
    assert result.text == "hello"
    assert result.tool_calls == []
    assert result.wants_tools is False


def test_generate_parses_function_calls_and_text(patched_client, fake_response_with_function_call):
    client, models = patched_client
    models.generate_content.return_value = fake_response_with_function_call

    result = client.generate(
        messages=[Message(role="user", content="analyse")],
        tools=[ToolSpec("get_lap_summary", "desc", {"type": "object", "properties": {}})],
        system=None,
    )

    assert result.text == "thinking..."
    assert [tc.name for tc in result.tool_calls] == ["get_lap_summary", "get_sector_analysis"]
    assert result.tool_calls[0].arguments == {"lap_id": 2}
    assert {tc.id for tc in result.tool_calls} == {"call_1", "call_2"}  # text part was index 0
    assert result.wants_tools is True


def test_generate_passes_system_temperature_and_tools(patched_client, fake_response_with_text):
    client, models = patched_client
    models.generate_content.return_value = fake_response_with_text

    lap_schema = {"type": "object", "properties": {"lap_id": {"type": "integer"}}, "required": ["lap_id"]}
    session_schema = {
        "type": "object",
        "properties": {"session_id": {"type": "integer"}},
        "required": ["session_id"],
    }
    tools = [
        ToolSpec("get_lap_summary", "summary tool desc", lap_schema),
        ToolSpec("get_consistency", "consistency tool desc", session_schema),
    ]
    client.generate(messages=[Message(role="user", content="x")], tools=tools, system="SYSTEM")

    kwargs = models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-2.5-flash"
    cfg = kwargs["config"]
    assert cfg.system_instruction == "SYSTEM"
    assert cfg.temperature == 0.1
    assert cfg.tools is not None and len(cfg.tools) == 1
    declarations = cfg.tools[0].function_declarations
    assert [d.name for d in declarations] == ["get_lap_summary", "get_consistency"]
    # description survives the translation
    assert declarations[0].description == "summary tool desc"


def test_generate_translates_message_roles_to_content(patched_client, fake_response_with_text):
    client, models = patched_client
    models.generate_content.return_value = fake_response_with_text

    msgs = [
        Message(role="user", content="please analyse lap 2"),
        Message(
            role="assistant",
            content="ok, calling get_lap_summary",
            tool_calls=[ToolCall(id="call_0", name="get_lap_summary", arguments={"lap_id": 2})],
        ),
        Message(role="tool", tool_call_id="call_0", name="get_lap_summary",
                content='{"lap_time": "2:04.126"}'),
    ]
    client.generate(messages=msgs, tools=[], system=None)

    contents = models.generate_content.call_args.kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    # user briefing -> one text part
    assert len(contents[0].parts) == 1
    # assistant w/ tool call -> text part + function_call part
    assert len(contents[1].parts) == 2
    fc_part = contents[1].parts[1]
    assert fc_part.function_call.name == "get_lap_summary"
    assert dict(fc_part.function_call.args) == {"lap_id": 2}
    # tool result -> function_response part
    fr_part = contents[2].parts[0]
    assert fr_part.function_response.name == "get_lap_summary"


def test_generate_handles_invalid_json_tool_result(patched_client, fake_response_with_text):
    """A tool result whose ``content`` isn't valid JSON shouldn't crash --
    we wrap it as ``{"raw": "..."}`` so the model still sees something."""
    client, models = patched_client
    models.generate_content.return_value = fake_response_with_text

    msgs = [
        Message(role="tool", tool_call_id="x", name="get_lap_summary", content="not json{"),
    ]
    client.generate(messages=msgs, tools=[], system=None)

    contents = models.generate_content.call_args.kwargs["contents"]
    fr_part = contents[0].parts[0]
    assert fr_part.function_response.response == {"raw": "not json{"}


def test_from_response_handles_empty_candidates():
    """No candidates from the API (e.g. safety block) returns an empty LLMResponse."""
    client = object.__new__(GeminiClient)  # bypass __init__ -- _from_response is pure
    fake_response = SimpleNamespace(candidates=[])
    result = client._from_response(fake_response)
    assert result.text == ""
    assert result.tool_calls == []


def test_clean_schema_drops_unsupported_keys_recursively():
    raw = {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "additionalProperties": False,
        "properties": {
            "detail_level": {"type": "string", "enum": ["summary", "low", "medium", "full"], "examples": ["summary"]},
            "channels": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "options": {"anyOf": [{"type": "string"}, {"type": "integer", "deprecated": True}]},
        },
        "required": ["detail_level"],
    }
    cleaned = _clean_schema(raw)
    assert cleaned["type"] == "object"
    assert "additionalProperties" not in cleaned
    assert "$schema" not in cleaned
    assert cleaned["properties"]["detail_level"]["enum"] == ["summary", "low", "medium", "full"]
    assert "examples" not in cleaned["properties"]["detail_level"]
    assert "minLength" not in cleaned["properties"]["channels"]["items"]
    assert "deprecated" not in cleaned["properties"]["options"]["anyOf"][1]


def test_from_config_resolves_api_key(monkeypatch):
    """``from_config`` reads the api key via the shared resolver and constructs
    the client. Tests using a fake genai.Client so no real client is built."""
    from pitwall.coach.config import CoachConfig

    monkeypatch.setenv("PITWALL_GEM_TEST", "fake-key-from-env")
    import google.genai as genai_module

    captured = {}

    def fake_client_ctor(*args, **kwargs):
        captured["api_key"] = kwargs.get("api_key")
        return MagicMock(models=MagicMock())

    monkeypatch.setattr(genai_module, "Client", fake_client_ctor)
    cfg = CoachConfig(api_key_env="PITWALL_GEM_TEST", model="gemini-2.5-flash")
    client = GeminiClient.from_config(cfg)
    assert isinstance(client, GeminiClient)
    assert captured["api_key"] == "fake-key-from-env"
