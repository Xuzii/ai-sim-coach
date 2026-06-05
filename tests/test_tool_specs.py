"""P2-C0: the shared tool source (tools/specs.py) and the server registered from it.

These guard the refactor that made specs.py the single source of tool
name/description/schema for both the MCP server and the coach -- so they can never
drift, and Claude Desktop keeps seeing exactly the tools it always did."""

from __future__ import annotations

import asyncio

from pitwall import server
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db
from pitwall.tools import specs

TOOL_NAMES = {
    "list_sessions", "list_laps", "get_lap_summary", "get_telemetry_trace",
    "compare_laps", "get_sector_analysis", "get_tire_history", "export_lap_csv",
}


def _seed():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0), conn, target_hz=50.0)
    return conn, sid


def test_specs_cover_all_eight_tools_with_descriptions():
    assert {s.name for s in specs.TOOL_SPECS} == TOOL_NAMES
    assert len(specs.TOOL_SPECS) == len(TOOL_NAMES)
    # descriptions are the prompt engineering -- they must be substantial
    assert all(len(s.description) > 60 for s in specs.TOOL_SPECS)


def test_server_registers_from_specs_without_drift():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools) == TOOL_NAMES
    # each registered description matches its spec (ignoring incidental whitespace)
    for name, tool in tools.items():
        spec = specs.TOOL_SPECS_BY_NAME[name]
        assert " ".join(tool.description.split()) == " ".join(spec.description.split())


def test_server_still_advertises_detail_level_enum():
    # Regression guard: the server has always shown Claude an enum for detail_level.
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    schema = tools["get_telemetry_trace"].inputSchema
    assert schema["properties"]["detail_level"]["enum"] == ["summary", "low", "medium", "full"]
    assert schema["required"] == ["lap_id"]


def test_parameters_schema_is_clean_and_neutral():
    by_name = {s.name: specs.parameters_schema(s) for s in specs.TOOL_SPECS}

    trace = by_name["get_telemetry_trace"]
    assert trace["type"] == "object"
    assert trace["required"] == ["lap_id"]
    assert trace["properties"]["detail_level"] == {"type": "string", "enum": ["summary", "low", "medium", "full"]}
    assert trace["properties"]["lap_id"] == {"type": "integer"}
    # optional nullable list collapses to a plain array (no anyOf/null noise)
    assert trace["properties"]["channels"] == {"type": "array", "items": {"type": "string"}}
    assert trace["properties"]["distance_range"] == {"type": "array", "items": {"type": "number"}}

    # all-optional tool has no required list
    assert "required" not in by_name["list_sessions"]
    assert by_name["list_laps"]["properties"]["valid_only"] == {"type": "boolean"}


def test_run_tool_dispatch():
    conn, sid = _seed()
    fastest = db.fastest_valid_lap(conn, sid).lap_id
    ok = specs.run_tool(conn, "get_lap_summary", {"lap_id": fastest})
    assert ok["lap_id"] == fastest and "error" not in ok
    assert "error" in specs.run_tool(conn, "no_such_tool", {})
    assert "error" in specs.run_tool(conn, "get_lap_summary", {"bogus": 1})
