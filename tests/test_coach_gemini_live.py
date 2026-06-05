r"""P2-C1: live Gemini smoke test (env-gated, skipped by default).

This is the one test that actually hits the network. It builds a real
``GeminiClient`` against ``GEMINI_API_KEY``, runs ``analyze_lap`` against a
seeded synthetic session, and asserts the round-trip works end-to-end.

Opt-in:
    $env:GEMINI_API_KEY = "..."
    $env:PITWALL_LIVE_GEMINI = "1"
    .\.venv\Scripts\python.exe -m pytest tests/test_coach_gemini_live.py -v

CI stays offline -- the test is skipped without ``PITWALL_LIVE_GEMINI``.
"""

from __future__ import annotations

import os

import pytest

from pitwall.coach import agent, store
from pitwall.coach.config import CoachConfig, load_dotenv
from pitwall.coach.llm.gemini import GeminiClient
from pitwall.coach.report import CoachingReport
from pitwall.ingest import pipeline
from pitwall.ingest.synthetic import SyntheticACCSource
from pitwall.storage import db


def _have_live_creds() -> bool:
    load_dotenv()  # so $env:GEMINI_API_KEY set in .env counts
    return bool(os.getenv("PITWALL_LIVE_GEMINI")) and bool(os.getenv("GEMINI_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _have_live_creds(),
    reason="live Gemini test; set PITWALL_LIVE_GEMINI=1 and GEMINI_API_KEY (env or .env)",
)


def test_live_gemini_round_trip():
    conn = db.connect(":memory:")
    db.apply_schema(conn)
    sid = pipeline.run(
        SyntheticACCSource(seed=7, track_length_m=2400.0, native_hz=150.0),
        conn,
        target_hz=50.0,
    )
    fastest_id = db.fastest_valid_lap(conn, sid).lap_id

    config = CoachConfig()
    client = GeminiClient.from_config(config)
    report, rid = agent.analyze_lap(conn, fastest_id, client, config)

    assert isinstance(report, CoachingReport)
    assert rid > 0
    assert report.findings, "live coach must produce at least one finding"
    for f in report.findings:
        assert f.evidence.strip(), "every finding must cite real evidence"

    # Lossless dict round-trip -- the Phase 3 hand-off contract.
    assert CoachingReport.from_dict(report.to_dict()).to_dict() == report.to_dict()
    # Persisted round-trip
    loaded = store.get_report(conn, rid)
    assert loaded is not None
    assert loaded.to_dict() == report.to_dict()
