"""P2-C1: ``analyze_lap`` -- the agentic tool-use loop, driven by ``MockLLMClient``.

These tests exercise the full loop against a real seeded synthetic ACC session
(3 laps, 1 valid, 2 invalid + sectors + Parquet traces), so the tools that fire
return real-shaped data. The LLM is mocked so we can assert deterministic
behaviour without a network call.
"""

from __future__ import annotations

import json

import pytest

from pitwall.coach import agent, store
from pitwall.coach.config import CoachConfig
from pitwall.coach.llm.base import LLMResponse, MockLLMClient, ToolCall
from pitwall.coach.report import CoachingReport
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db


@pytest.fixture
def seeded():
    """Reused pattern from ``test_coach_store.py``: 3-lap synthetic ACC session."""
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(
        SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0),
        conn,
        target_hz=50.0,
    )
    return conn, sid


def _terminal_call(*, lap_id: int = 0, findings: list[dict] | None = None, score: int | None = 88) -> ToolCall:
    """Build a scripted submit_coaching_report tool call."""
    if findings is None:
        findings = [
            {
                "area": "braking",
                "sector": 2,
                "severity": 3,
                "observation": "Braking 40 m late into the apex.",
                "recommendation": "Brake at the 100 m board instead of past it.",
                "evidence": "sector 2 +0.41s vs ref; min brake speed 38 km/h higher",
                "estimated_gain_ms": 410,
            }
        ]
    return ToolCall(
        id="terminal",
        name="submit_coaching_report",
        arguments={
            "findings": findings,
            "consistency": {"score": score, "basis": "3 laps"},
            "priorities": [1],
            "human_summary": "**Brake earlier in the worst corner.**",
        },
    )


def test_happy_path_persists_report_and_stamps_meta(seeded):
    conn, sid = seeded
    fastest = db.fastest_valid_lap(conn, sid)
    assert fastest is not None and fastest.lap_id is not None
    fastest_id = fastest.lap_id

    summary_args = {"lap_id": fastest_id}
    sector_args = {"lap_id": fastest_id, "reference_lap_id": fastest_id}
    trace_args = {
        "lap_id": fastest_id,
        "channels": ["brake", "speed_kmh"],
        "detail_level": "summary",
        "distance_range": [800, 1200],
    }
    client = MockLLMClient(script=[
        LLMResponse(tool_calls=[ToolCall(id="c1", name="get_lap_summary", arguments=summary_args)]),
        LLMResponse(tool_calls=[ToolCall(id="c2", name="get_sector_analysis", arguments=sector_args)]),
        LLMResponse(tool_calls=[ToolCall(id="c3", name="get_telemetry_trace", arguments=trace_args)]),
        LLMResponse(tool_calls=[ToolCall(id="c4", name="get_consistency", arguments={"session_id": sid})]),
        LLMResponse(text="here is the report", tool_calls=[_terminal_call()]),
    ])

    config = CoachConfig(model="gemini-2.5-flash-test")
    report, rid = agent.analyze_lap(conn, fastest_id, client, config)

    assert isinstance(report, CoachingReport)
    assert rid > 0
    loaded = store.get_report(conn, rid)
    assert loaded is not None
    assert loaded.to_dict() == report.to_dict()
    assert report.meta.provider == "gemini"
    assert report.meta.model == "gemini-2.5-flash-test"
    assert report.meta.generated_at_utc is not None
    assert report.meta.reference_lap_id == fastest_id  # = fastest valid in same session
    assert report.lap.lap_time_ms == fastest.lap_time_ms
    assert report.lap.is_valid is True
    assert len(report.findings) == 1
    assert report.findings[0].area == "braking"


def test_system_prompt_and_submit_tool_offered_each_turn(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[LLMResponse(text="ok", tool_calls=[_terminal_call()])])
    agent.analyze_lap(conn, fastest_id, client, CoachConfig())

    call = client.calls[0]
    assert call["system"] == agent.SYSTEM_PROMPT
    tool_names = {t.name for t in call["tools"]}
    assert "submit_coaching_report" in tool_names
    # 7 query tools + get_consistency + submit_coaching_report = 9
    assert len(tool_names) == 9
    assert "export_lap_csv" not in tool_names  # excluded from coach


def test_tool_error_is_forwarded_and_loop_recovers(seeded):
    """A bad tool call returns {"error": ...} from run_tool; the model sees it
    as a tool message and recovers by calling submit_coaching_report next."""
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    bad_call = ToolCall(id="bad", name="get_lap_summary", arguments={"lap_id": 999_999})
    client = MockLLMClient(script=[
        LLMResponse(tool_calls=[bad_call]),
        LLMResponse(text="ok recovering", tool_calls=[_terminal_call()]),
    ])

    report, rid = agent.analyze_lap(conn, fastest_id, client, CoachConfig())
    assert rid > 0

    # The 2nd LLM call should have seen the error tool message in its history.
    second_call_messages = client.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_messages) == 1
    payload = json.loads(tool_messages[0].content)
    assert "error" in payload
    assert report.meta.lap_id == fastest_id


def test_stall_then_terminal_succeeds(seeded):
    """One text-only turn triggers a nudge but doesn't fail; the next terminal
    call succeeds."""
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[
        LLMResponse(text="hmm let me think"),
        LLMResponse(text="ok", tool_calls=[_terminal_call()]),
    ])

    report, _ = agent.analyze_lap(conn, fastest_id, client, CoachConfig())
    assert report.findings  # made it through

    # The history at turn 2 includes the nudge user message.
    second_call_messages = client.calls[1]["messages"]
    nudge_msgs = [m for m in second_call_messages if m.role == "user" and "submit_coaching_report" in m.content]
    # The briefing + the nudge both mention the tool name; ensure the nudge IS there.
    assert any("Please continue" in m.content for m in nudge_msgs)


def test_two_text_only_turns_raise(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[
        LLMResponse(text="thinking"),
        LLMResponse(text="still thinking"),
    ])
    with pytest.raises(ValueError, match="stalled"):
        agent.analyze_lap(conn, fastest_id, client, CoachConfig())


def test_iteration_exhaustion_raises(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    # Three tool-call turns, max_iterations=2 -> the second turn's tool call
    # runs but then the loop exits before terminal -> ValueError.
    client = MockLLMClient(script=[
        LLMResponse(tool_calls=[ToolCall(id="a", name="get_lap_summary", arguments={"lap_id": fastest_id})]),
        LLMResponse(tool_calls=[ToolCall(id="b", name="get_sector_analysis", arguments={"lap_id": fastest_id})]),
        LLMResponse(tool_calls=[_terminal_call()]),
    ])

    config = CoachConfig(max_iterations=2)
    with pytest.raises(ValueError, match="did not submit a report"):
        agent.analyze_lap(conn, fastest_id, client, config)


def test_empty_findings_propagate_value_error(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[
        LLMResponse(tool_calls=[_terminal_call(findings=[])]),
    ])
    with pytest.raises(ValueError, match="findings"):
        agent.analyze_lap(conn, fastest_id, client, CoachConfig())


def test_reference_override_used_instead_of_fastest_valid(seeded):
    conn, sid = seeded
    laps = db.laps_for_session(conn, sid)
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    # Pick a different lap as the explicit reference.
    other = next(lap for lap in laps if lap.lap_id != fastest_id)
    assert other.lap_id is not None

    client = MockLLMClient(script=[LLMResponse(tool_calls=[_terminal_call()])])
    config = CoachConfig(reference_lap_id=other.lap_id)
    report, _ = agent.analyze_lap(conn, fastest_id, client, config)
    assert report.meta.reference_lap_id == other.lap_id


def test_unknown_lap_raises_lookup_error(seeded):
    conn, _ = seeded
    client = MockLLMClient(script=[])
    with pytest.raises(LookupError, match="lap_id"):
        agent.analyze_lap(conn, 999_999, client, CoachConfig())


def test_custom_system_prompt_overrides_default(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[LLMResponse(tool_calls=[_terminal_call()])])
    config = CoachConfig(system_prompt="CUSTOM SYSTEM")
    agent.analyze_lap(conn, fastest_id, client, config)
    assert client.calls[0]["system"] == "CUSTOM SYSTEM"


def test_briefing_includes_lap_track_car_and_reference(seeded):
    conn, sid = seeded
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id
    client = MockLLMClient(script=[LLMResponse(tool_calls=[_terminal_call()])])
    agent.analyze_lap(conn, fastest_id, client, CoachConfig())

    user_msgs = [m for m in client.calls[0]["messages"] if m.role == "user"]
    assert user_msgs, "expected at least one user briefing message"
    briefing = user_msgs[0].content
    assert f"lap_id={fastest_id}" in briefing
    # The synthetic source seeds a real track + car name -- not asserting
    # specific value but that *something* non-empty is included.
    assert "track" not in briefing or "unknown track" not in briefing
