"""Pure query functions behind the MCP tools.

Each takes an open SQLite connection plus parameters and returns a plain,
JSON-serialisable, already-token-sized dict. No MCP imports here -- the thin
``@mcp.tool()`` wrappers in :mod:`pitwall.server` just manage a connection and
delegate, so all of this is unit-testable without a server.
"""

from __future__ import annotations

import sqlite3

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from pitwall import channels as CH
from pitwall.storage import db, paths, traces
from pitwall.tools import formatting


def fmt_time(ms: int | None) -> str | None:
    """Format milliseconds as ``M:SS.mmm`` (or ``SS.mmm`` under a minute)."""
    if ms is None:
        return None
    minutes, rem = divmod(int(ms), 60_000)
    seconds = rem / 1000.0
    return f"{minutes}:{seconds:06.3f}" if minutes else f"{seconds:.3f}"


def _valid_channels(names: list[str] | None, default: tuple[str, ...]) -> list[str]:
    if not names:
        return list(default)
    return [c for c in names if c in CH.CHANNEL_BY_NAME] or list(default)


def _read_trace(lap: db.Lap, names: list[str] | None, dist_range: tuple[float, float] | None) -> pa.Table:
    return traces.read_lap_trace(
        traces.resolve_trace_path(lap.trace_path), columns=names, distance_range=dist_range
    )


# --------------------------------------------------------------------------- #
def list_sessions(
    conn: sqlite3.Connection,
    *,
    track: str | None = None,
    car: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict:
    sessions = db.find_sessions(conn, track=track, car=car, since=since, limit=limit)
    rows = []
    for s in sessions:
        fastest = db.fastest_valid_lap(conn, s.session_id)
        rows.append({
            "session_id": s.session_id,
            "track": s.track_name,
            "car": s.car_name,
            "session_type": s.session_type,
            "started_at_utc": s.started_at_utc,
            "lap_count": len(db.laps_for_session(conn, s.session_id)),
            "best_lap_ms": fastest.lap_time_ms if fastest else None,
            "best_lap": fmt_time(fastest.lap_time_ms) if fastest else None,
        })
    return {"count": len(rows), "sessions": rows}


def list_laps(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    valid_only: bool = False,
    top_n_fastest: int | None = None,
) -> dict:
    laps = db.laps_for_session(conn, session_id, valid_only=valid_only, top_n_fastest=top_n_fastest)
    return {
        "session_id": session_id,
        "count": len(laps),
        "laps": [{
            "lap_id": lap.lap_id,
            "lap_number": lap.lap_number,
            "lap_time_ms": lap.lap_time_ms,
            "lap_time": fmt_time(lap.lap_time_ms),
            "is_valid": lap.is_valid,
            "is_outlap": lap.is_outlap,
            "is_inlap": lap.is_inlap,
            "stint_number": lap.stint_number,
            "top_speed_kmh": _round1(lap.top_speed_kmh),
        } for lap in laps],
    }


def get_lap_summary(conn: sqlite3.Connection, *, lap_id: int) -> dict:
    lap = db.get_lap(conn, lap_id)
    if lap is None:
        return {"error": f"no lap with id {lap_id}"}
    sess = db.get_session(conn, lap.session_id)
    sectors = db.sectors_for_lap(conn, lap_id)
    pb = db.personal_best_ms(conn, sess.track_code, sess.car_code) if sess else None
    return {
        "lap_id": lap_id,
        "lap_number": lap.lap_number,
        "session_id": lap.session_id,
        "track": sess.track_name if sess else None,
        "car": sess.car_name if sess else None,
        "lap_time_ms": lap.lap_time_ms,
        "lap_time": fmt_time(lap.lap_time_ms),
        "is_valid": lap.is_valid,
        "is_outlap": lap.is_outlap,
        "is_inlap": lap.is_inlap,
        "stint_number": lap.stint_number,
        "sectors": [{
            "sector": s.sector_index + 1,
            "time_ms": s.sector_time_ms,
            "time": fmt_time(s.sector_time_ms),
        } for s in sectors],
        "top_speed_kmh": _round1(lap.top_speed_kmh),
        "avg_speed_kmh": _round1(lap.avg_speed_kmh),
        "fuel_used_kg": _round2(lap.fuel_used_kg),
        "personal_best_ms": pb,
        "personal_best": fmt_time(pb),
        "delta_to_pb_ms": (lap.lap_time_ms - pb) if (pb and lap.lap_time_ms) else None,
    }


def get_telemetry_trace(
    conn: sqlite3.Connection,
    *,
    lap_id: int,
    channels: list[str] | None = None,
    detail_level: str = "summary",
    distance_range: list[float] | None = None,
) -> dict:
    lap = db.get_lap(conn, lap_id)
    if lap is None:
        return {"error": f"no lap with id {lap_id}"}
    names = _valid_channels(channels, formatting.DEFAULT_TRACE_CHANNELS)
    dist_range = (float(distance_range[0]), float(distance_range[1])) if distance_range else None
    table = _read_trace(lap, names, dist_range)
    result = {
        "lap_id": lap_id,
        "lap_number": lap.lap_number,
        "detail_level": detail_level,
        "channels": names,
        "units": {n: CH.CHANNEL_BY_NAME[n].unit for n in names},
        "distance_range": dist_range,
    }
    if detail_level == "summary":
        sectors = db.sectors_for_lap(conn, lap_id)
        result["by_sector"] = formatting.summary_by_sector(table, sectors, names)
        result["whole_lap"] = {n: formatting.channel_summary(table, n) for n in names}
    else:
        points = formatting.bin_by_distance(table, names, formatting.n_bins_for(detail_level))
        result["points"] = points
        result["point_count"] = len(points)
    return result


def compare_laps(conn: sqlite3.Connection, *, lap_a_id: int, lap_b_id: int) -> dict:
    a = db.get_lap(conn, lap_a_id)
    b = db.get_lap(conn, lap_b_id)
    if a is None or b is None:
        return {"error": f"no lap with id {lap_a_id if a is None else lap_b_id}"}
    sec_a = db.sectors_for_lap(conn, lap_a_id)
    sec_b = db.sectors_for_lap(conn, lap_b_id)
    speed_a = _read_trace(a, ["speed_kmh"], None)
    speed_b = _read_trace(b, ["speed_kmh"], None)

    sector_deltas = []
    for sa, sb in zip(sec_a, sec_b, strict=False):
        delta = (sa.sector_time_ms - sb.sector_time_ms) if (sa.sector_time_ms and sb.sector_time_ms) else None
        sector_deltas.append({
            "sector": sa.sector_index + 1,
            "a_ms": sa.sector_time_ms,
            "b_ms": sb.sector_time_ms,
            "delta_ms": delta,
            "a_avg_speed_kmh": _sector_mean_speed(speed_a, sa),
            "b_avg_speed_kmh": _sector_mean_speed(speed_b, sb),
        })
    total = (a.lap_time_ms - b.lap_time_ms) if (a.lap_time_ms and b.lap_time_ms) else None
    return {
        "lap_a": _lap_brief(a),
        "lap_b": _lap_brief(b),
        "lap_time_delta_ms": total,
        "sector_deltas": sector_deltas,
        "note": "positive delta_ms means lap A was slower than lap B in that sector",
    }


def get_sector_analysis(
    conn: sqlite3.Connection, *, lap_id: int, reference_lap_id: int | None = None
) -> dict:
    lap = db.get_lap(conn, lap_id)
    if lap is None:
        return {"error": f"no lap with id {lap_id}"}
    if reference_lap_id is None:
        ref = db.fastest_valid_lap(conn, lap.session_id)
        reference_lap_id = ref.lap_id if ref and ref.lap_id != lap_id else reference_lap_id
    ref_sectors = {s.sector_index: s for s in db.sectors_for_lap(conn, reference_lap_id)} if reference_lap_id else {}

    rows, total_delta, worst = [], 0, None
    for s in db.sectors_for_lap(conn, lap_id):
        ref_s = ref_sectors.get(s.sector_index)
        ref_ms = ref_s.sector_time_ms if ref_s else None
        delta = (s.sector_time_ms - ref_ms) if (s.sector_time_ms and ref_ms) else None
        if delta is not None:
            total_delta += delta
            if worst is None or delta > worst[1]:
                worst = (s.sector_index + 1, delta)
        rows.append({
            "sector": s.sector_index + 1,
            "time_ms": s.sector_time_ms,
            "time": fmt_time(s.sector_time_ms),
            "reference_ms": ref_ms,
            "delta_ms": delta,
        })
    return {
        "lap_id": lap_id,
        "reference_lap_id": reference_lap_id,
        "sectors": rows,
        "total_delta_ms": total_delta if reference_lap_id else None,
        "biggest_loss_sector": worst[0] if worst and worst[1] > 0 else None,
    }


def get_tire_history(
    conn: sqlite3.Connection, *, session_id: int, stint_number: int | None = None
) -> dict:
    laps = db.laps_for_session(conn, session_id)
    if stint_number is not None:
        laps = [lap for lap in laps if lap.stint_number == stint_number]
    cols = list(CH.TIRE_TEMP_CHANNELS) + list(CH.TIRE_PRESSURE_CHANNELS)
    rows = []
    for lap in laps:
        table = _read_trace(lap, cols, None)
        rows.append({
            "lap_id": lap.lap_id,
            "lap_number": lap.lap_number,
            "stint_number": lap.stint_number,
            "tire_temp_c": {w: _mean(table, f"tire_temp_{w}") for w in CH.WHEEL_ORDER},
            "tire_pressure_psi": {w: _mean(table, f"tire_pressure_{w}") for w in CH.WHEEL_ORDER},
        })
    return {"session_id": session_id, "stint_filter": stint_number, "lap_count": len(rows), "laps": rows}


def export_lap_csv(
    conn: sqlite3.Connection, *, lap_id: int, channels: list[str] | None = None
) -> dict:
    lap = db.get_lap(conn, lap_id)
    if lap is None:
        return {"error": f"no lap with id {lap_id}"}
    names = [c for c in channels if c in CH.CHANNEL_BY_NAME] if channels else None
    table = _read_trace(lap, names, None)
    out_path = paths.exports_dir() / f"lap_s{lap.session_id}_{lap.lap_number:03d}.csv"
    pacsv.write_csv(table, str(out_path))
    return {
        "path": str(out_path),
        "rows": table.num_rows,
        "columns": table.column_names,
    }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _round1(x: float | None) -> float | None:
    return None if x is None else round(float(x), 1)


def _round2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _mean(table: pa.Table, channel: str) -> float | None:
    col = table.column(channel)
    if col.length() == 0 or col.null_count == col.length():
        return None
    return round(float(pc.mean(col).as_py()), 1)


def _lap_brief(lap: db.Lap) -> dict:
    return {
        "lap_id": lap.lap_id,
        "lap_number": lap.lap_number,
        "lap_time_ms": lap.lap_time_ms,
        "lap_time": fmt_time(lap.lap_time_ms),
    }


def _sector_mean_speed(speed_table: pa.Table, sector: db.Sector) -> float | None:
    sub = formatting.slice_distance(speed_table, sector.start_distance_m, sector.end_distance_m)
    return _mean(sub, "speed_kmh")
