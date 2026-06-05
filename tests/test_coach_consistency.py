"""P2-C0: the deterministic consistency score (pure function, no DB)."""

from __future__ import annotations

from pitwall.coach.consistency import consistency_score
from pitwall.storage import db


def _lap(lap_id: int, time_ms: int | None, *, valid=True, outlap=False, inlap=False) -> db.Lap:
    return db.Lap(
        session_id=1, lap_number=lap_id, started_at_utc="2026-05-25T00:00:00+00:00",
        trace_path=f"{lap_id}.parquet", point_count=10, lap_time_ms=time_ms,
        is_valid=valid, is_outlap=outlap, is_inlap=inlap, lap_id=lap_id,
    )


def _secs(lap_id: int, times: list[int]) -> list[db.Sector]:
    return [db.Sector(lap_id=lap_id, sector_index=i, sector_time_ms=t) for i, t in enumerate(times)]


def test_needs_at_least_two_laps():
    res = consistency_score([_lap(1, 100_000)], {1: _secs(1, [50_000, 50_000])})
    assert res["score"] is None
    assert res["lap_count"] == 1
    assert "at least 2" in res["basis"]


def test_identical_laps_score_100():
    laps = [_lap(i, 100_000) for i in range(1, 4)]
    secs = {i: _secs(i, [40_000, 60_000]) for i in range(1, 4)}
    res = consistency_score(laps, secs)
    assert res["score"] == 100
    assert res["lap_time_cov"] == 0.0
    assert res["lap_count"] == 3


def test_tighter_spread_scores_higher():
    tight = [_lap(1, 100_000), _lap(2, 100_100), _lap(3, 100_050)]
    loose = [_lap(1, 100_000), _lap(2, 103_000), _lap(3, 98_000)]
    secs_tight = {1: _secs(1, [50_000, 50_000]), 2: _secs(2, [50_050, 50_050]), 3: _secs(3, [50_020, 50_030])}
    secs_loose = {1: _secs(1, [50_000, 50_000]), 2: _secs(2, [51_500, 51_500]), 3: _secs(3, [49_000, 49_000])}
    high = consistency_score(tight, secs_tight)["score"]
    low = consistency_score(loose, secs_loose)["score"]
    assert 0 <= low < high <= 100


def test_excludes_invalid_out_and_in_laps():
    laps = [
        _lap(1, 90_000, outlap=True),    # excluded
        _lap(2, 100_000),
        _lap(3, 100_200),
        _lap(4, 130_000, valid=False),   # excluded (off-track)
        _lap(5, 88_000, inlap=True),     # excluded
    ]
    secs = {lap.lap_id: _secs(lap.lap_id, [50_000, 50_000]) for lap in laps}
    res = consistency_score(laps, secs)
    assert res["lap_count"] == 2  # only laps 2 and 3 are usable
    assert res["score"] is not None


def test_works_without_sector_data():
    laps = [_lap(1, 100_000), _lap(2, 101_000)]
    res = consistency_score(laps, {1: [], 2: []})
    assert res["mean_sector_cov"] is None
    assert 0 <= res["score"] <= 100
