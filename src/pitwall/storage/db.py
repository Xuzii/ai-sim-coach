"""SQLite metadata store: sessions, laps, sectors.

Read models are plain dataclasses; writes go through small ``insert_*`` helpers
that return the new row id. The schema lives in ``schema.sql`` (loaded as package
data) and is applied idempotently, so opening a fresh or existing database is the
same call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from pitwall import timeutil
from pitwall.storage import paths

SCHEMA_VERSION = 2  # v2 adds the coaching_reports table (Phase 2); changes are purely additive
_SCHEMA_SQL = files("pitwall.storage").joinpath("schema.sql").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Read models
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    game: str
    track_code: str
    track_name: str
    car_code: str
    car_name: str
    started_at_utc: str
    session_type: str | None = None
    ended_at_utc: str | None = None
    sector_count: int = 3
    track_length_m: float | None = None
    air_temp_c: float | None = None
    road_temp_c: float | None = None
    pitwall_version: str | None = None
    session_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Session:
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class Lap:
    session_id: int
    lap_number: int
    started_at_utc: str
    trace_path: str
    point_count: int
    stint_number: int = 1
    lap_time_ms: int | None = None
    is_valid: bool = True
    is_outlap: bool = False
    is_inlap: bool = False
    top_speed_kmh: float | None = None
    avg_speed_kmh: float | None = None
    fuel_used_kg: float | None = None
    tyre_set: int | None = None
    lap_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Lap:
        d = {k: row[k] for k in row.keys()}
        for flag in ("is_valid", "is_outlap", "is_inlap"):
            d[flag] = bool(d[flag])
        return cls(**d)


@dataclass
class Sector:
    lap_id: int
    sector_index: int
    sector_time_ms: int | None = None
    start_distance_m: float | None = None
    end_distance_m: float | None = None
    sector_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Sector:
        return cls(**{k: row[k] for k in row.keys()})


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #
def connect(db_file: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enforced.

    ``db_file=None`` uses the configured data dir (``PITWALL_DATA_DIR`` in tests).

    For on-disk databases we enable WAL journaling and a busy timeout so the MCP
    server can read the store while ``pitwall-ingest`` writes to it concurrently
    (read-while-driving) without hitting "database is locked". WAL is a persistent
    property of the database file, so setting it here is idempotent. In-memory
    databases (tests) don't support WAL and skip it.
    """
    target = str(db_file) if db_file is not None else str(paths.db_path())
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if ":memory:" not in target and "mode=memory" not in target:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")  # ms: wait out a transient lock instead of failing
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indices if absent and stamp the current schema version.

    Idempotent, and safe to re-run on an older on-disk store: schema changes are
    purely additive (``CREATE TABLE IF NOT EXISTS``), so we just upsert the single
    ``schema_meta`` row to the latest :data:`SCHEMA_VERSION` without needing a
    migration step."""
    conn.executescript(_SCHEMA_SQL)
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
    now = timeutil.to_iso_utc(timeutil.utc_now())
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (version, applied_at_utc) VALUES (?, ?)",
            (SCHEMA_VERSION, now),
        )
    elif row[0] != SCHEMA_VERSION:
        conn.execute("UPDATE schema_meta SET version = ?, applied_at_utc = ?", (SCHEMA_VERSION, now))
    conn.commit()


# --------------------------------------------------------------------------- #
# Inserts
# --------------------------------------------------------------------------- #
def insert_session(
    conn: sqlite3.Connection,
    *,
    game: str,
    track_code: str,
    track_name: str,
    car_code: str,
    car_name: str,
    started_at_utc: str,
    session_type: str | None = None,
    ended_at_utc: str | None = None,
    sector_count: int = 3,
    track_length_m: float | None = None,
    air_temp_c: float | None = None,
    road_temp_c: float | None = None,
    pitwall_version: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (game, track_code, track_name, car_code, car_name, session_type, "
        "started_at_utc, ended_at_utc, sector_count, track_length_m, air_temp_c, road_temp_c, "
        "pitwall_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (game, track_code, track_name, car_code, car_name, session_type, started_at_utc,
         ended_at_utc, sector_count, track_length_m, air_temp_c, road_temp_c, pitwall_version),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_lap(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    lap_number: int,
    started_at_utc: str,
    trace_path: str,
    point_count: int,
    stint_number: int = 1,
    lap_time_ms: int | None = None,
    is_valid: bool = True,
    is_outlap: bool = False,
    is_inlap: bool = False,
    top_speed_kmh: float | None = None,
    avg_speed_kmh: float | None = None,
    fuel_used_kg: float | None = None,
    tyre_set: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO laps (session_id, lap_number, stint_number, lap_time_ms, is_valid, is_outlap, "
        "is_inlap, top_speed_kmh, avg_speed_kmh, fuel_used_kg, tyre_set, started_at_utc, trace_path, "
        "point_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, lap_number, stint_number, lap_time_ms, int(is_valid), int(is_outlap),
         int(is_inlap), top_speed_kmh, avg_speed_kmh, fuel_used_kg, tyre_set, started_at_utc,
         trace_path, point_count),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_sectors(
    conn: sqlite3.Connection,
    lap_id: int,
    sector_times_ms: list[int | None],
    *,
    start_distances: list[float | None] | None = None,
    end_distances: list[float | None] | None = None,
) -> None:
    """Insert one row per sector for a lap, indexed 0..n-1.

    Distance bounds are optional (the synthetic/live pipeline supplies them; some
    tests pass times only) and power per-sector trace slicing in the MCP tools.
    """
    n = len(sector_times_ms)
    starts = start_distances if start_distances is not None else [None] * n
    ends = end_distances if end_distances is not None else [None] * n
    conn.executemany(
        "INSERT INTO sectors (lap_id, sector_index, sector_time_ms, start_distance_m, end_distance_m) "
        "VALUES (?, ?, ?, ?, ?)",
        [(lap_id, i, sector_times_ms[i], starts[i], ends[i]) for i in range(n)],
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def get_session(conn: sqlite3.Connection, session_id: int) -> Session | None:
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    return Session.from_row(row) if row else None


def find_sessions(
    conn: sqlite3.Connection,
    *,
    track: str | None = None,
    car: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[Session]:
    """Sessions filtered by track/car (substring, case-insensitive) and a ``since``
    ISO-8601 UTC lower bound, newest first."""
    clauses, params = [], []
    if track:
        clauses.append("(track_name LIKE ? OR track_code LIKE ?)")
        params += [f"%{track}%", f"%{track}%"]
    if car:
        clauses.append("(car_name LIKE ? OR car_code LIKE ?)")
        params += [f"%{car}%", f"%{car}%"]
    if since:
        clauses.append("started_at_utc >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM sessions{where} ORDER BY started_at_utc DESC LIMIT ?", params
    ).fetchall()
    return [Session.from_row(r) for r in rows]


def get_lap(conn: sqlite3.Connection, lap_id: int) -> Lap | None:
    row = conn.execute("SELECT * FROM laps WHERE lap_id = ?", (lap_id,)).fetchone()
    return Lap.from_row(row) if row else None


def laps_for_session(
    conn: sqlite3.Connection,
    session_id: int,
    *,
    valid_only: bool = False,
    top_n_fastest: int | None = None,
) -> list[Lap]:
    """Laps in a session. By default ordered by lap number; with ``top_n_fastest``
    returns the N quickest *timed* laps (valid only) fastest-first."""
    clauses = ["session_id = ?"]
    params: list = [session_id]
    if valid_only:
        clauses.append("is_valid = 1")
    where = " AND ".join(clauses)
    if top_n_fastest is not None:
        sql = (
            f"SELECT * FROM laps WHERE {where} AND is_valid = 1 AND lap_time_ms IS NOT NULL "
            "ORDER BY lap_time_ms ASC LIMIT ?"
        )
        params.append(top_n_fastest)
    else:
        sql = f"SELECT * FROM laps WHERE {where} ORDER BY lap_number ASC"
    return [Lap.from_row(r) for r in conn.execute(sql, params).fetchall()]


def fastest_valid_lap(conn: sqlite3.Connection, session_id: int) -> Lap | None:
    row = conn.execute(
        "SELECT * FROM laps WHERE session_id = ? AND is_valid = 1 AND lap_time_ms IS NOT NULL "
        "ORDER BY lap_time_ms ASC LIMIT 1",
        (session_id,),
    ).fetchone()
    return Lap.from_row(row) if row else None


def sectors_for_lap(conn: sqlite3.Connection, lap_id: int) -> list[Sector]:
    rows = conn.execute(
        "SELECT * FROM sectors WHERE lap_id = ? ORDER BY sector_index ASC", (lap_id,)
    ).fetchall()
    return [Sector.from_row(r) for r in rows]


def personal_best_ms(conn: sqlite3.Connection, track_code: str, car_code: str) -> int | None:
    """Fastest valid lap time (ms) for a track+car across all sessions, for
    consistency comparisons. Returns None if there is no timed valid lap."""
    row = conn.execute(
        "SELECT MIN(l.lap_time_ms) FROM laps l JOIN sessions s ON l.session_id = s.session_id "
        "WHERE s.track_code = ? AND s.car_code = ? AND l.is_valid = 1 AND l.lap_time_ms IS NOT NULL",
        (track_code, car_code),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def personal_best_lap(conn: sqlite3.Connection, track_code: str, car_code: str) -> Lap | None:
    """The actual fastest valid lap for a track+car across all sessions (the lap
    behind :func:`personal_best_ms`). Returns the :class:`Lap` so callers that need
    its ``lap_id`` -- e.g. the coach's ``--pb`` reference -- get it directly.
    Returns None if there is no timed valid lap for that track+car."""
    row = conn.execute(
        "SELECT l.* FROM laps l JOIN sessions s ON l.session_id = s.session_id "
        "WHERE s.track_code = ? AND s.car_code = ? AND l.is_valid = 1 AND l.lap_time_ms IS NOT NULL "
        "ORDER BY l.lap_time_ms ASC LIMIT 1",
        (track_code, car_code),
    ).fetchone()
    return Lap.from_row(row) if row else None
