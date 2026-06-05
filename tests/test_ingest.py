"""C2 tests: synthetic source -> downsample -> segment -> persist, end to end."""

from __future__ import annotations

import pytest

from pitwall import channels
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db, traces

# A smaller, faster session for tests: shorter track + lower native rate keeps
# frame counts modest while still exercising downsampling and segmentation.
TEST_KWARGS = dict(track_length_m=2400.0, native_hz=150.0)


@pytest.fixture
def ingested(data_dir):
    """Ingest one synthetic session and return (conn, session_id)."""
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    source = SyntheticACCSource(seed=7, **TEST_KWARGS)
    session_id = pipeline.run(source, conn, target_hz=50.0)
    return conn, session_id


def test_run_with_no_laps_writes_no_session(data_dir):
    """A source that yields no complete lap must leave no session row behind.

    The row is created lazily on the first lap, so an empty source (ACC parked in
    the menu, where the live stream ends with zero frames) returns None and writes
    nothing -- otherwise ``--watch`` spams empty 0-lap sessions.
    """
    from collections.abc import Iterator

    from pitwall.ingest.frames import CanonicalFrame, StaticInfo
    from pitwall.ingest.source import TelemetrySource

    class _Empty(TelemetrySource):
        native_hz = 150.0

        def static_info(self) -> StaticInfo:
            return StaticInfo(game="acc", track_code="spa", car_code="mclaren_720s_gt3",
                              started_at_utc="2026-05-27T00:00:00Z")

        def frames(self) -> Iterator[CanonicalFrame]:
            return iter(())

    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(_Empty(), conn)
    assert sid is None
    assert db.find_sessions(conn) == []


def test_session_and_lap_count(ingested):
    conn, sid = ingested
    sess = db.get_session(conn, sid)
    assert sess.track_name == "Spa-Francorchamps"
    assert sess.car_name == "McLaren 720S GT3"
    assert sess.sector_count == 3
    laps = db.laps_for_session(conn, sid)
    assert [lap.lap_number for lap in laps] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_fastest_and_ordering(ingested):
    conn, sid = ingested
    assert db.fastest_valid_lap(conn, sid).lap_number == 3
    top2 = db.laps_for_session(conn, sid, top_n_fastest=2)
    assert [lap.lap_number for lap in top2] == [3, 4]


def test_invalid_lap_flagged(ingested):
    conn, sid = ingested
    by_num = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}
    assert by_num[5].is_valid is False
    assert all(by_num[n].is_valid for n in (1, 2, 3, 4, 6, 7, 8))


def test_outlap_inlap_flags(ingested):
    conn, sid = ingested
    by_num = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}
    assert by_num[1].is_outlap is True
    assert by_num[8].is_inlap is True


def test_two_stints_from_tyre_change(ingested):
    conn, sid = ingested
    by_num = {lap.lap_number: lap for lap in db.laps_for_session(conn, sid)}
    assert [by_num[n].stint_number for n in range(1, 6)] == [1, 1, 1, 1, 1]
    assert [by_num[n].stint_number for n in (6, 7, 8)] == [2, 2, 2]


def test_sectors_present_and_sum_equals_lap_time(ingested):
    conn, sid = ingested
    lap = db.fastest_valid_lap(conn, sid)
    sectors = db.sectors_for_lap(conn, lap.lap_id)
    assert [s.sector_index for s in sectors] == [0, 1, 2]
    assert all(s.sector_time_ms and s.sector_time_ms > 0 for s in sectors)
    total = sum(s.sector_time_ms for s in sectors)
    # The forward-only partition telescopes exactly to lap_time; only integer
    # rounding of the per-sector vs whole-lap ms can differ (a ms or two).
    assert abs(total - lap.lap_time_ms) <= 2


def _sector_buf(rows):
    """A LapBuffer from (t, sector_index, lap_distance_m) rows."""
    from pitwall.ingest.frames import CanonicalFrame

    frames = [
        CanonicalFrame(t=t, values={"lap_distance_m": d}, lap_count=1, sector_index=sec)
        for t, sec, d in rows
    ]
    return frames


def test_sector_breakdown_handles_stale_lead_and_early_reset():
    # The real-ACC failure mode the synthetic source can't reproduce: a lap that
    # opens still reading the PREVIOUS lap's sector (stale 2) because
    # currentSectorIndex lags the lap-counter increment, then resets to 0 early at
    # the end. The forward-only partition must still telescope to lap_time.
    from pitwall.ingest.pipeline import LapBuffer, _sector_breakdown

    frames = _sector_buf([
        (0.00, 2, 0.0),     # stale: really this lap's sector 0
        (0.10, 2, 60.0),    # still stale
        (0.20, 0, 120.0),   # now reads 0 -> still sector 0 (forward-only)
        (0.40, 1, 300.0),   # boundary 0->1
        (0.80, 2, 700.0),   # boundary 1->2
        (1.10, 0, 980.0),   # premature reset -> ignored, absorbed into sector 2
    ])
    buf = LapBuffer(frames=frames, start_t=0.00, end_t=1.20, lap_count=1)

    times, starts, ends = _sector_breakdown(buf, sector_count=3)

    assert None not in times
    assert sum(times) == 1200          # telescopes exactly to lap_time (end_t-start_t)
    assert times == [400, 400, 400]    # 0.40-0.00, 0.80-0.40, 1.20-0.80
    assert starts == [0.0, 300.0, 700.0]
    assert ends == [300.0, 700.0, 980.0]  # last end = final frame dist, not the wrap


def test_sector_breakdown_incomplete_lap_leaves_none():
    # An out-lap that never leaves sector 0: only sector 0 is closed (on end_t).
    from pitwall.ingest.pipeline import LapBuffer, _sector_breakdown

    frames = _sector_buf([(i * 0.1, 0, i * 50.0) for i in range(5)])
    buf = LapBuffer(frames=frames, start_t=0.0, end_t=0.5, lap_count=0)
    times, starts, ends = _sector_breakdown(buf, sector_count=3)
    assert times == [500, None, None]
    assert starts[1] is None and starts[2] is None


def test_trace_distance_is_monotonic(ingested):
    conn, sid = ingested
    lap = db.fastest_valid_lap(conn, sid)
    table = traces.read_lap_trace(traces.resolve_trace_path(lap.trace_path))
    dist = table.column(channels.DISTANCE_KEY).to_pylist()
    assert dist == sorted(dist)
    assert dist[0] < 100.0  # starts near the line
    assert table.num_rows == lap.point_count


def test_downsample_hits_target_rate(ingested):
    conn, sid = ingested
    lap = db.fastest_valid_lap(conn, sid)
    # point_count should be roughly lap_time(s) * 50 Hz (boundary frames add a few)
    expected = lap.lap_time_ms / 1000.0 * 50.0
    assert 0.85 * expected <= lap.point_count <= 1.20 * expected


def test_fuel_consumed_over_lap(ingested):
    conn, sid = ingested
    lap = db.fastest_valid_lap(conn, sid)
    assert lap.fuel_used_kg is not None and lap.fuel_used_kg > 0.0


def test_downsample_preserves_lap_and_sector_boundaries():
    # Pure-logic check independent of the DB: a high-rate stream decimates but
    # keeps the exact frames where lap or sector changes.
    from pitwall.ingest.frames import CanonicalFrame

    def stream():
        t = 0.0
        for lap in range(2):
            for sector in range(3):
                for _ in range(100):  # 100 frames per sector at 1 kHz
                    yield CanonicalFrame(t=t, lap_count=lap, sector_index=sector)
                    t += 0.001

    kept = list(pipeline.downsample(stream(), target_hz=50.0))
    # Every (lap, sector) transition frame must survive decimation.
    seen = {(f.lap_count, f.sector_index) for f in kept}
    assert seen == {(lap, sec) for lap in range(2) for sec in range(3)}
    # And we actually decimated (600 frames -> far fewer).
    assert len(kept) < 120
