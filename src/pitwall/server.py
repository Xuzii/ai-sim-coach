"""The pitwall MCP server.

Defines the FastMCP server and registers eight narrow tools over stdio (what
Claude Desktop speaks). Each tool is a thin wrapper that opens a SQLite
connection and delegates to :mod:`pitwall.tools.queries`; the docstrings below
are the tool descriptions Claude reads to decide *when* to call each one and
*what* it returns -- they are the most important prompt engineering in the
project, so they are written for that audience.
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from mcp.server.fastmcp import FastMCP

from pitwall.storage import db
from pitwall.tools import queries

mcp = FastMCP("pitwall")

DetailLevel = Literal["summary", "low", "medium", "full"]


def _conn() -> sqlite3.Connection:
    """Open the local store, ensuring the schema exists (idempotent, cheap)."""
    conn = db.connect()
    db.apply_schema(conn)
    return conn


@mcp.tool()
def list_sessions(
    track: str | None = None,
    car: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict:
    """Find driving sessions, newest first. Start here when the user refers to a
    track, car, day, or "my last session" and you need a session_id to drill into.

    Filters (all optional): `track` and `car` match the name or internal code
    (case-insensitive substring, e.g. "spa", "720s"); `since` is an ISO-8601 UTC
    lower bound on start time (e.g. "2026-05-20T00:00:00+00:00"). Returns each
    session's id, track, car, type, start time, lap count, and best valid lap.
    """
    conn = _conn()
    try:
        return queries.list_sessions(conn, track=track, car=car, since=since, limit=limit)
    finally:
        conn.close()


@mcp.tool()
def list_laps(
    session_id: int,
    valid_only: bool = False,
    top_n_fastest: int | None = None,
) -> dict:
    """List the laps in a session. Use after `list_sessions` to pick a lap_id.

    Set `valid_only=True` to exclude laps invalidated for going off-track. Set
    `top_n_fastest=N` to get only the N quickest valid timed laps, fastest first
    (ideal for "my fastest lap" / "best two laps"). Returns per lap: lap_id,
    lap_number, lap time (ms and formatted), validity, out/in-lap flags, stint,
    and top speed.
    """
    conn = _conn()
    try:
        return queries.list_laps(
            conn, session_id=session_id, valid_only=valid_only, top_n_fastest=top_n_fastest
        )
    finally:
        conn.close()


@mcp.tool()
def get_lap_summary(lap_id: int) -> dict:
    """Summarise a single lap: overall and per-sector times, top/avg speed, fuel
    used, validity, stint, and the delta to the driver's personal best for that
    car+track. Use this for "how was lap N" or before deciding whether a deeper
    telemetry pull is worthwhile. Cheap; safe to call often.
    """
    conn = _conn()
    try:
        return queries.get_lap_summary(conn, lap_id=lap_id)
    finally:
        conn.close()


@mcp.tool()
def get_telemetry_trace(
    lap_id: int,
    channels: list[str] | None = None,
    detail_level: DetailLevel = "summary",
    distance_range: list[float] | None = None,
) -> dict:
    """Pull channel telemetry for a lap, binned by track distance.

    `channels` selects canonical channels (e.g. ["brake","throttle","speed_kmh",
    "steering_angle","rpm","gear"]); omit for a sensible default set.
    `detail_level` controls cost vs resolution:
      - "summary" (default): min/max/mean/std per channel, per sector and
        whole-lap. No raw points -- use this first; it is by far the cheapest.
      - "low"/"medium"/"full": a distance-binned series (~60/150/600 points)
        with the mean of each channel per bin.
    `distance_range` is an inclusive [start_m, end_m] window -- pass it to zoom
    into a corner (e.g. [200, 400] for "brake pressure at T1"); the bins then
    concentrate in that window for higher resolution. Avoid "full" over a whole
    lap unless the user truly needs every point -- it is the largest response.
    """
    conn = _conn()
    try:
        return queries.get_telemetry_trace(
            conn,
            lap_id=lap_id,
            channels=channels,
            detail_level=detail_level,
            distance_range=distance_range,
        )
    finally:
        conn.close()


@mcp.tool()
def compare_laps(lap_a_id: int, lap_b_id: int) -> dict:
    """Compare two laps and show where time went. Returns the overall lap-time
    delta and, per sector, each lap's time, the delta (positive = lap A slower),
    and each lap's average speed in that sector. Use for "where did I lose time
    between my fastest and second-fastest lap?".
    """
    conn = _conn()
    try:
        return queries.compare_laps(conn, lap_a_id=lap_a_id, lap_b_id=lap_b_id)
    finally:
        conn.close()


@mcp.tool()
def get_sector_analysis(lap_id: int, reference_lap_id: int | None = None) -> dict:
    """Break a lap into sectors versus a reference lap (defaults to the fastest
    valid lap in the same session) and report per-sector deltas, the total delta,
    and which sector lost the most time. Use for "which sector is costing me?".
    """
    conn = _conn()
    try:
        return queries.get_sector_analysis(conn, lap_id=lap_id, reference_lap_id=reference_lap_id)
    finally:
        conn.close()


@mcp.tool()
def get_tire_history(session_id: int, stint_number: int | None = None) -> dict:
    """Track tyre temperatures and pressures across a session, lap by lap, per
    wheel (fl/fr/rl/rr). Pass `stint_number` to focus on one stint (a stint is a
    run on one set of tyres). Use for "what were my tyre temps over the last
    stint?" or to spot a tyre overheating across a run.
    """
    conn = _conn()
    try:
        return queries.get_tire_history(conn, session_id=session_id, stint_number=stint_number)
    finally:
        conn.close()


@mcp.tool()
def export_lap_csv(lap_id: int, channels: list[str] | None = None) -> dict:
    """Export a lap's full-resolution telemetry to a CSV file on disk and return
    its path (plus row/column counts) to hand to the user. Omit `channels` for
    all channels. Use when the user asks to export, download, or save a lap, or
    wants the raw data for their own analysis.
    """
    conn = _conn()
    try:
        return queries.export_lap_csv(conn, lap_id=lap_id, channels=channels)
    finally:
        conn.close()


def main() -> None:
    """Console-script entry point: run the server over stdio for Claude Desktop."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
