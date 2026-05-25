"""C4 tests: the 8 query functions over synthetic data, tool registration, token budget."""

from __future__ import annotations

import asyncio
import json

import pytest

from pitwall import server
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db
from pitwall.tools import queries

TEST_KWARGS = dict(track_length_m=2400.0, native_hz=150.0)
TOOL_NAMES = {
    "list_sessions", "list_laps", "get_lap_summary", "get_telemetry_trace",
    "compare_laps", "get_sector_analysis", "get_tire_history", "export_lap_csv",
}


@pytest.fixture
def session(data_dir):
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(SyntheticACCSource(seed=7, **TEST_KWARGS), conn, target_hz=50.0)
    return conn, sid


def _fastest_id(conn, sid) -> int:
    return db.fastest_valid_lap(conn, sid).lap_id


# --------------------------------------------------------------------------- #
# Server / registration
# --------------------------------------------------------------------------- #
def test_eight_tools_registered_with_descriptions():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES
    # docstrings must flow through as descriptions -- they are the prompt engineering
    assert all(t.description and len(t.description) > 60 for t in tools)


def test_fmt_time():
    assert queries.fmt_time(None) is None
    assert queries.fmt_time(138_500) == "2:18.500"
    assert queries.fmt_time(43_210) == "43.210"


# --------------------------------------------------------------------------- #
# Queries -- the five success-criteria style questions
# --------------------------------------------------------------------------- #
def test_list_sessions_and_laps(session):
    conn, sid = session
    res = queries.list_sessions(conn, track="spa")
    assert res["count"] == 1
    assert res["sessions"][0]["track"] == "Spa-Francorchamps"
    assert res["sessions"][0]["lap_count"] == 8

    laps = queries.list_laps(conn, session_id=sid, top_n_fastest=2)
    assert [lap["lap_number"] for lap in laps["laps"]] == [3, 4]


def test_get_lap_summary_shape(session):
    conn, sid = session
    summary = queries.get_lap_summary(conn, lap_id=_fastest_id(conn, sid))
    assert summary["lap_number"] == 3
    assert summary["is_valid"] is True
    assert len(summary["sectors"]) == 3
    assert summary["lap_time"] is not None and summary["top_speed_kmh"] > 0
    # fastest lap is the personal best -> zero delta
    assert summary["delta_to_pb_ms"] == 0


def test_invalid_lap_surfaced_in_summary(session):
    conn, sid = session
    invalid = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}[5]
    summary = queries.get_lap_summary(conn, lap_id=invalid.lap_id)
    assert summary["is_valid"] is False


def test_telemetry_trace_summary_has_sectors(session):
    conn, sid = session
    res = queries.get_telemetry_trace(
        conn, lap_id=_fastest_id(conn, sid), channels=["brake", "throttle", "speed_kmh"]
    )
    assert set(res["by_sector"].keys()) == {1, 2, 3}
    assert set(res["whole_lap"].keys()) == {"brake", "throttle", "speed_kmh"}
    assert res["whole_lap"]["throttle"]["max"] <= 1.0
    assert res["units"]["speed_kmh"] == "km/h"


def test_telemetry_trace_distance_zoom(session):
    conn, sid = session
    res = queries.get_telemetry_trace(
        conn, lap_id=_fastest_id(conn, sid), channels=["brake"],
        detail_level="medium", distance_range=[200.0, 400.0],
    )
    assert res["point_count"] > 0
    assert all(200.0 <= p["distance_m"] <= 400.0 for p in res["points"])
    assert "brake" in res["points"][0]


def test_compare_laps(session):
    conn, sid = session
    laps = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}
    res = queries.compare_laps(conn, lap_a_id=laps[4].lap_id, lap_b_id=laps[3].lap_id)
    # lap 4 is slower than the fastest lap 3 -> positive overall delta
    assert res["lap_time_delta_ms"] > 0
    assert len(res["sector_deltas"]) == 3
    assert all("a_avg_speed_kmh" in d for d in res["sector_deltas"])


def test_sector_analysis_vs_fastest(session):
    conn, sid = session
    laps = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}
    res = queries.get_sector_analysis(conn, lap_id=laps[4].lap_id)
    assert res["reference_lap_id"] == laps[3].lap_id  # defaults to fastest valid
    assert res["total_delta_ms"] is not None and res["total_delta_ms"] > 0
    assert res["biggest_loss_sector"] in {1, 2, 3}


def test_tire_history_per_stint(session):
    conn, sid = session
    stint2 = queries.get_tire_history(conn, session_id=sid, stint_number=2)
    assert stint2["lap_count"] == 3  # laps 6,7,8
    rec = stint2["laps"][0]
    assert set(rec["tire_temp_c"].keys()) == {"fl", "fr", "rl", "rr"}
    assert rec["tire_temp_c"]["fl"] is not None


def test_export_lap_csv_writes_file(session):
    conn, sid = session
    res = queries.export_lap_csv(conn, lap_id=_fastest_id(conn, sid), channels=["throttle", "brake"])
    from pathlib import Path

    path = Path(res["path"])
    assert path.exists() and res["rows"] > 0
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "throttle" in header and "brake" in header


def test_unknown_lap_returns_error(session):
    conn, _ = session
    assert "error" in queries.get_lap_summary(conn, lap_id=99999)


# --------------------------------------------------------------------------- #
# Token budget
# --------------------------------------------------------------------------- #
def test_detail_level_response_sizes_are_ordered(session):
    conn, sid = session
    lap_id = _fastest_id(conn, sid)

    def size(detail):
        res = queries.get_telemetry_trace(conn, lap_id=lap_id, detail_level=detail)
        return len(json.dumps(res))

    summary = size("summary")
    low, medium, full = size("low"), size("medium"), size("full")
    # summary is the cheap default and must be well under a context-busting size
    assert summary < 4000
    # detail costs more monotonically
    assert summary < low < medium < full
