"""P2-C0: the coach agent tool registry + dispatcher."""

from __future__ import annotations

import pytest

from pitwall.coach import tools as ctools
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db


@pytest.fixture
def seeded():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0), conn, target_hz=50.0)
    return conn, sid


def test_agent_tools_exclude_export_and_add_consistency():
    names = [t.name for t in ctools.agent_tools()]
    assert "export_lap_csv" not in names          # not a coaching input
    assert "get_consistency" in names             # coach-only grounding tool
    assert "get_lap_summary" in names and "get_sector_analysis" in names
    assert len(names) == 8                          # 7 shared query tools + get_consistency


def test_agent_tools_have_neutral_schemas():
    for tool in ctools.agent_tools():
        assert tool.description and len(tool.description) > 40
        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters


def test_run_tool_query_delegates(seeded):
    conn, sid = seeded
    fastest = db.fastest_valid_lap(conn, sid).lap_id
    res = ctools.run_tool(conn, "get_lap_summary", {"lap_id": fastest})
    assert res["lap_id"] == fastest and "error" not in res


def test_run_tool_consistency(seeded):
    conn, sid = seeded
    res = ctools.run_tool(conn, "get_consistency", {"session_id": sid})
    assert res["score"] is not None and 0 <= res["score"] <= 100
    assert res["session_id"] == sid and res["lap_count"] >= 2


def test_run_tool_graceful_errors(seeded):
    conn, _ = seeded
    assert "error" in ctools.run_tool(conn, "export_lap_csv", {"lap_id": 1})  # excluded -> unknown to coach
    assert "error" in ctools.run_tool(conn, "nope", {})
    assert "error" in ctools.run_tool(conn, "get_lap_summary", {"bad_kwarg": 1})
