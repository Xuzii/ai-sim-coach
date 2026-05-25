"""C1 tests: SQLite metadata round-trips and Parquet trace read/slice."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pyarrow as pa
import pytest

from pitwall import channels
from pitwall.storage import db, traces


def _open() -> sqlite3.Connection:
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    return conn


def _seed_session(conn, *, track_code="spa", car_code="mclaren_720s_gt3") -> int:
    return db.insert_session(
        conn,
        game="acc",
        track_code=track_code,
        track_name="Spa-Francorchamps",
        car_code=car_code,
        car_name="McLaren 720S GT3",
        started_at_utc="2026-05-23T14:00:00+00:00",
        session_type="practice",
        sector_count=3,
        track_length_m=7004.0,
    )


# --------------------------------------------------------------------------- #
# Schema + metadata
# --------------------------------------------------------------------------- #
def test_apply_schema_is_idempotent():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    db.apply_schema(conn)  # second call must not raise or double-stamp
    rows = conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0]
    assert rows == 1


def test_session_lap_sector_roundtrip():
    conn = _open()
    sid = _seed_session(conn)
    lap_id = db.insert_lap(
        conn,
        session_id=sid,
        lap_number=1,
        started_at_utc="2026-05-23T14:01:00+00:00",
        trace_path="traces/1.parquet",
        point_count=4500,
        lap_time_ms=138_500,
        top_speed_kmh=271.3,
    )
    db.insert_sectors(conn, lap_id, [44_100, 51_200, 43_200])

    sess = db.get_session(conn, sid)
    assert sess is not None and sess.track_name == "Spa-Francorchamps"
    assert sess.started_at_utc == "2026-05-23T14:00:00+00:00"  # UTC preserved verbatim

    lap = db.get_lap(conn, lap_id)
    assert lap is not None and lap.lap_time_ms == 138_500 and lap.is_valid is True

    secs = db.sectors_for_lap(conn, lap_id)
    assert [s.sector_index for s in secs] == [0, 1, 2]
    assert [s.sector_time_ms for s in secs] == [44_100, 51_200, 43_200]


def test_foreign_key_cascade_on_session_delete():
    conn = _open()
    sid = _seed_session(conn)
    lap_id = db.insert_lap(
        conn, session_id=sid, lap_number=1, started_at_utc="2026-05-23T14:01:00+00:00",
        trace_path="t.parquet", point_count=10, lap_time_ms=100_000,
    )
    db.insert_sectors(conn, lap_id, [50_000, 50_000])
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    conn.commit()
    assert db.get_lap(conn, lap_id) is None
    assert db.sectors_for_lap(conn, lap_id) == []


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def test_find_sessions_filters():
    conn = _open()
    _seed_session(conn, track_code="spa")
    db.insert_session(
        conn, game="acc", track_code="monza", track_name="Monza", car_code="ferrari_488_gt3",
        car_name="Ferrari 488 GT3", started_at_utc="2026-05-24T10:00:00+00:00",
    )
    assert {s.track_code for s in db.find_sessions(conn)} == {"spa", "monza"}
    assert [s.track_code for s in db.find_sessions(conn, track="spa")] == ["spa"]
    assert [s.track_code for s in db.find_sessions(conn, track="Spa")] == ["spa"]  # case-insensitive
    assert [s.car_code for s in db.find_sessions(conn, car="ferrari")] == ["ferrari_488_gt3"]
    since = [s.track_code for s in db.find_sessions(conn, since="2026-05-24T00:00:00+00:00")]
    assert since == ["monza"]
    # newest first
    assert [s.track_code for s in db.find_sessions(conn)][0] == "monza"


def test_laps_for_session_valid_and_fastest():
    conn = _open()
    sid = _seed_session(conn)
    specs = [  # (lap_number, time_ms, valid)
        (1, 150_000, True),   # outlap-ish slow
        (2, 139_000, True),
        (3, 137_500, True),   # fastest valid
        (4, 130_000, False),  # invalid (off-track) -- must be excluded from fastest
    ]
    for n, t, valid in specs:
        db.insert_lap(
            conn, session_id=sid, lap_number=n, started_at_utc="2026-05-23T14:01:00+00:00",
            trace_path=f"{n}.parquet", point_count=10, lap_time_ms=t, is_valid=valid,
        )
    assert len(db.laps_for_session(conn, sid)) == 4
    assert len(db.laps_for_session(conn, sid, valid_only=True)) == 3
    top2 = db.laps_for_session(conn, sid, top_n_fastest=2)
    assert [lap.lap_number for lap in top2] == [3, 2]  # fastest valid first; invalid excluded
    assert db.fastest_valid_lap(conn, sid).lap_number == 3


def test_personal_best_across_sessions():
    conn = _open()
    s1 = _seed_session(conn)
    db.insert_lap(conn, session_id=s1, lap_number=1, started_at_utc="2026-05-23T14:01:00+00:00",
                  trace_path="a.parquet", point_count=10, lap_time_ms=139_000, is_valid=True)
    s2 = _seed_session(conn)  # same track+car, later session
    db.insert_lap(conn, session_id=s2, lap_number=1, started_at_utc="2026-05-25T14:01:00+00:00",
                  trace_path="b.parquet", point_count=10, lap_time_ms=137_000, is_valid=True)
    db.insert_lap(conn, session_id=s2, lap_number=2, started_at_utc="2026-05-25T14:03:00+00:00",
                  trace_path="c.parquet", point_count=10, lap_time_ms=135_000, is_valid=False)
    assert db.personal_best_ms(conn, "spa", "mclaren_720s_gt3") == 137_000  # invalid 135k ignored


# --------------------------------------------------------------------------- #
# Parquet traces
# --------------------------------------------------------------------------- #
def _fake_trace(n: int = 101, length: float = 1000.0) -> dict[str, list]:
    dist = [length * i / (n - 1) for i in range(n)]
    data: dict[str, list] = {c: [0.0] * n for c in channels.CHANNEL_NAMES}
    data[channels.DISTANCE_KEY] = dist
    data[channels.TIME_KEY] = [i * 20 for i in range(n)]  # 50 Hz -> 20 ms steps
    data["throttle"] = [i / (n - 1) for i in range(n)]
    data["brake"] = [1.0 - i / (n - 1) for i in range(n)]
    data["gear"] = [min(6, 1 + i // 20) for i in range(n)]
    data["rpm"] = [6000 + i * 10 for i in range(n)]
    return data


def test_trace_write_read_roundtrip(tmp_path: Path):
    path = tmp_path / "lap.parquet"
    n = traces.write_lap_trace(path, _fake_trace(n=50))
    assert n == 50
    assert path.exists() and path.stat().st_size > 0

    table = traces.read_lap_trace(path)
    assert set(table.column_names) == set(channels.trace_columns())
    assert table.num_rows == 50
    # dtypes carried through
    assert table.schema.field("gear").type == pa.int8()
    assert table.schema.field("throttle").type == pa.float32()


def test_trace_column_projection_includes_distance(tmp_path: Path):
    path = tmp_path / "lap.parquet"
    traces.write_lap_trace(path, _fake_trace())
    table = traces.read_lap_trace(path, columns=["brake"])
    assert set(table.column_names) == {channels.DISTANCE_KEY, "brake"}


def test_trace_distance_range_slice(tmp_path: Path):
    path = tmp_path / "lap.parquet"
    traces.write_lap_trace(path, _fake_trace(n=101, length=1000.0))  # 10 m spacing
    table = traces.read_lap_trace(path, columns=["throttle"], distance_range=(200.0, 400.0))
    dist = table.column(channels.DISTANCE_KEY).to_pylist()
    assert dist, "expected a non-empty slice"
    assert min(dist) >= 200.0 and max(dist) <= 400.0
    assert dist == sorted(dist)  # still monotonic
    # 200,210,...,400 inclusive = 21 points
    assert len(dist) == 21


def test_build_table_requires_distance_key():
    with pytest.raises(KeyError):
        traces.build_table({"throttle": [0.1, 0.2]})
