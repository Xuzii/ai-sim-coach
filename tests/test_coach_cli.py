"""P2-C2: the ``pitwall-coach`` CLI (analyze / show / list).

Drives ``cli.main`` end-to-end against an on-disk store in the ``data_dir`` tmp
dir, with the LLM provided by an injected ``llm_factory`` so no network / API key
is needed. The agent loop itself is covered in ``test_coach_agent.py``; here we
assert the CLI's seeding, flag handling, output, and graceful-error exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pitwall.coach import store
from pitwall.coach.cli import main
from pitwall.coach.config import CoachConfig
from pitwall.coach.llm.base import LLMResponse, MockLLMClient, ToolCall
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db


@pytest.fixture
def seeded(data_dir: Path) -> tuple[int, int]:
    """Seed the on-disk store (the one ``cli.main`` opens) with a 3-lap synthetic
    ACC session. Returns ``(fastest_lap_id, session_id)``."""
    conn = db.connect()
    db.apply_schema(conn)
    sid = pipeline.run(
        SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0),
        conn,
        target_hz=50.0,
    )
    lap_id = db.fastest_valid_lap(conn, sid).lap_id
    conn.close()
    return lap_id, sid


def _terminal_response(score: int | None = 88) -> LLMResponse:
    call = ToolCall(
        id="terminal",
        name="submit_coaching_report",
        arguments={
            "findings": [
                {
                    "area": "braking",
                    "sector": 2,
                    "distance_range_m": [800, 1000],
                    "severity": 3,
                    "observation": "Braking 40 m late into the apex.",
                    "recommendation": "Brake at the 100 m board instead of past it.",
                    "evidence": "sector 2 +0.41s vs ref; min brake speed 38 km/h higher",
                    "estimated_gain_ms": 410,
                }
            ],
            "consistency": {"score": score, "basis": "3 laps"},
            "priorities": [1],
            "human_summary": "**Brake earlier in the worst corner.**",
        },
    )
    return LLMResponse(text="report ready", tool_calls=[call])


def _factory(*responses: LLMResponse):
    """An ``llm_factory`` that hands back a scripted ``MockLLMClient``."""
    captured = {}

    def factory(config: CoachConfig):
        captured["config"] = config
        return MockLLMClient(script=list(responses))

    factory.captured = captured  # type: ignore[attr-defined]
    return factory


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #
def test_analyze_persists_and_prints_human_summary(seeded, capsys):
    lap_id, _ = seeded
    rc = main(["analyze", str(lap_id)], llm_factory=_factory(_terminal_response()))
    assert rc == 0
    out = capsys.readouterr()
    assert "Coaching report" in out.out
    assert "Braking" in out.out
    assert "Brake earlier" in out.out
    # The saved-report line goes to stderr so --json stdout stays clean.
    assert "Saved report #1" in out.err

    # It actually landed in the store.
    conn = db.connect()
    rows = store.list_reports(conn)
    assert len(rows) == 1 and rows[0].lap_id == lap_id
    conn.close()


def test_analyze_json_emits_parseable_report(seeded, capsys):
    lap_id, _ = seeded
    rc = main(["analyze", str(lap_id), "--json"], llm_factory=_factory(_terminal_response()))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta"]["lap_id"] == lap_id
    assert payload["findings"][0]["area"] == "braking"
    assert payload["meta"]["provider"] == "gemini"


def test_analyze_model_flag_overrides_config(seeded):
    lap_id, _ = seeded
    fac = _factory(_terminal_response())
    rc = main(["analyze", str(lap_id), "--model", "gemini-2.5-pro"], llm_factory=fac)
    assert rc == 0
    assert fac.captured["config"].model == "gemini-2.5-pro"


def test_analyze_pb_resolves_reference(seeded, capsys):
    lap_id, _ = seeded
    fac = _factory(_terminal_response())
    rc = main(["analyze", str(lap_id), "--pb"], llm_factory=fac)
    assert rc == 0
    # Single session -> the PB lap is the fastest valid lap, which exists.
    assert fac.captured["config"].reference_lap_id is not None


def test_analyze_unknown_lap_is_graceful(data_dir, capsys):
    rc = main(["analyze", "999999"], llm_factory=_factory(_terminal_response()))
    assert rc == 2
    assert "no lap with id 999999" in capsys.readouterr().err


def test_analyze_unknown_reference_is_graceful(seeded, capsys):
    lap_id, _ = seeded
    rc = main(["analyze", str(lap_id), "--reference", "999999"], llm_factory=_factory(_terminal_response()))
    assert rc == 2
    assert "reference lap 999999 not found" in capsys.readouterr().err


def test_analyze_missing_key_is_graceful(seeded, capsys):
    lap_id, _ = seeded

    def factory(config):
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to .env ...")

    rc = main(["analyze", str(lap_id)], llm_factory=factory)
    assert rc == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().err


def test_analyze_missing_sdk_is_graceful(seeded, capsys):
    lap_id, _ = seeded

    def factory(config):
        raise ImportError("No module named 'google'")

    rc = main(["analyze", str(lap_id)], llm_factory=factory)
    assert rc == 2
    assert "coach" in capsys.readouterr().err


def test_analyze_agent_stall_returns_one(seeded, capsys):
    lap_id, _ = seeded
    # Two text-only turns -> agent.analyze_lap raises ValueError("stalled").
    fac = _factory(LLMResponse(text="thinking"), LLMResponse(text="still thinking"))
    rc = main(["analyze", str(lap_id)], llm_factory=fac)
    assert rc == 1
    assert "did not produce a valid report" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# show / list
# --------------------------------------------------------------------------- #
def test_show_round_trips_a_saved_report(seeded, capsys):
    lap_id, _ = seeded
    main(["analyze", str(lap_id)], llm_factory=_factory(_terminal_response()))
    capsys.readouterr()  # drain

    rc = main(["show", "1"])
    assert rc == 0
    assert "Coaching report" in capsys.readouterr().out

    rc = main(["show", "1", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["meta"]["lap_id"] == lap_id


def test_show_unknown_report_is_graceful(data_dir, capsys):
    rc = main(["show", "42"])
    assert rc == 2
    assert "no report with id 42" in capsys.readouterr().err


def test_list_empty_store(data_dir, capsys):
    rc = main(["list"])
    assert rc == 0
    assert "no coaching reports yet" in capsys.readouterr().out


def test_list_shows_saved_report(seeded, capsys):
    lap_id, sid = seeded
    main(["analyze", str(lap_id)], llm_factory=_factory(_terminal_response()))
    capsys.readouterr()

    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(lap_id) in out
    assert "gemini" in out

    # Filter by a non-matching session -> empty.
    rc = main(["list", "--session", str(sid + 999)])
    assert rc == 0
    assert "no coaching reports yet" in capsys.readouterr().out
