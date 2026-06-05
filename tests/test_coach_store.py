"""P2-C0: persistence of coaching reports (the coaching_reports table)."""

from __future__ import annotations

import sqlite3

import pytest

from pitwall.coach import store
from pitwall.coach.report import CoachingReport, Consistency, Finding, LapInfo, ReportMeta
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db


@pytest.fixture
def seeded():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0), conn, target_hz=50.0)
    return conn, sid


def _report(lap_id: int, session_id: int, *, score: int | None = 88) -> CoachingReport:
    return CoachingReport(
        meta=ReportMeta(lap_id=lap_id, session_id=session_id, track="Nurburgring GP",
                        car="Ford Mustang GT3", reference_lap_id=lap_id, provider="mock", model="mock-1"),
        lap=LapInfo(lap_time_ms=124_126, is_valid=True, delta_to_reference_ms=0, reference="fastest valid"),
        findings=[Finding(area="braking", severity=4, observation="late",
                          recommendation="earlier", evidence="ref +0.4s", sector=2)],
        consistency=Consistency(score=score, basis="5 laps"),
        priorities=[1],
        human_summary="**Good.**",
    )


def test_schema_version_bumped_to_2(seeded):
    conn, _ = seeded
    assert db.SCHEMA_VERSION == 2
    assert conn.execute("SELECT version FROM schema_meta").fetchone()[0] == 2


def test_insert_get_roundtrip(seeded):
    conn, sid = seeded
    fastest = db.fastest_valid_lap(conn, sid).lap_id
    report = _report(fastest, sid)
    rid = store.insert_report(conn, report)
    assert rid > 0
    loaded = store.get_report(conn, rid)
    assert loaded is not None
    assert loaded.to_dict() == report.to_dict()
    # generated_at_utc gets stamped on insert
    assert report.meta.generated_at_utc is not None


def test_get_missing_returns_none(seeded):
    conn, _ = seeded
    assert store.get_report(conn, 9999) is None


def test_list_reports_filters_and_orders(seeded):
    conn, sid = seeded
    laps = db.laps_for_session(conn, sid, valid_only=True)
    r1 = store.insert_report(conn, _report(laps[0].lap_id, sid, score=70))
    r2 = store.insert_report(conn, _report(laps[1].lap_id, sid, score=90))

    all_rows = store.list_reports(conn, session_id=sid)
    assert {r.report_id for r in all_rows} == {r1, r2}
    assert all(r.session_id == sid for r in all_rows)
    assert all_rows[0].consistency_score in (70, 90)

    only_first = store.list_reports(conn, lap_id=laps[0].lap_id)
    assert [r.report_id for r in only_first] == [r1]
    assert only_first[0].provider == "mock" and only_first[0].schema_version == 1


def test_foreign_key_rejects_unknown_lap(seeded):
    conn, sid = seeded
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_report(conn, _report(999_999, sid))
