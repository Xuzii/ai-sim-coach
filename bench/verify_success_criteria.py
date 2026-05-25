"""Prove the 5 Phase-1 success-criteria queries answer on a *real* ingested lap.

These are the natural-language questions from the Phase 1 plan, mapped to the
tool calls Claude would make, run against the real Nurburgring / Ford Mustang GT3
capture. It shows the data + tools actually produce the answers (the literal
Claude-Desktop round-trip is Kenneth's to film for the demo GIF).

    python bench/verify_success_criteria.py [path/to/session.pwcap]
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
os.environ["PITWALL_DATA_DIR"] = tempfile.mkdtemp(prefix="pitwall_verify_")

from pitwall.ingest import pipeline  # noqa: E402
from pitwall.ingest.source import detect_source  # noqa: E402
from pitwall.storage import db  # noqa: E402
from pitwall.tools import queries  # noqa: E402


def show(title: str, obj) -> None:
    print(f"\n### {title}")
    print(json.dumps(obj, indent=2, default=str))


def main() -> None:
    cap = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "acc-spa.pwcap"
    if not cap.exists():
        cap = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "acc-nurburgring-slim.pwcap"
    print(f"capture: {cap}")

    conn = db.connect()
    db.apply_schema(conn)
    pipeline.run(detect_source(recording_path=str(cap)), conn)  # ingest into the isolated store

    # Q1: "What was my fastest lap at Nurburgring this week?"
    sessions = queries.list_sessions(conn, track="nurburgring")
    sid = sessions["sessions"][0]["session_id"]
    fastest = queries.list_laps(conn, session_id=sid, top_n_fastest=1)
    show("Q1  fastest lap at Nurburgring  (list_sessions -> list_laps top_n_fastest=1)", {
        "session": sessions["sessions"][0],
        "fastest_lap": fastest["laps"][0] if fastest["laps"] else None,
    })
    fastest_id = fastest["laps"][0]["lap_id"]

    # Q2: "What was my brake pressure at the first corner on that lap?"
    sess = db.get_session(conn, sid)
    length = sess.track_length_m or 1.0
    t1 = [round(0.04 * length, 1), round(0.10 * length, 1)]
    brake_t1 = queries.get_telemetry_trace(
        conn, lap_id=fastest_id, channels=["brake", "speed_kmh"], detail_level="summary", distance_range=t1
    )
    show(f"Q2  brake pressure at the first braking zone (distance {t1} m)  (get_telemetry_trace summary)",
         {"distance_range_m": brake_t1["distance_range"], "whole_window": brake_t1["whole_lap"]})

    # Q3: "Compare my fastest and second-fastest lap -- where did I lose time?"
    two = queries.list_laps(conn, session_id=sid, top_n_fastest=2)
    if len(two["laps"]) >= 2:
        cmp = queries.compare_laps(conn, lap_a_id=two["laps"][1]["lap_id"], lap_b_id=two["laps"][0]["lap_id"])
        show("Q3  compare 2nd-fastest vs fastest -- where time was lost  (compare_laps)", cmp)
    else:
        show("Q3  compare laps", {"note": "only one valid timed lap in this capture; needs two to compare"})

    # Q4: "What were my tire temps over the stint?"
    tires = queries.get_tire_history(conn, session_id=sid)
    show("Q4  tyre temps across the stint  (get_tire_history)", tires)

    # Q5: "Export that lap to CSV"
    csv = queries.export_lap_csv(conn, lap_id=fastest_id)
    show("Q5  export the lap to CSV  (export_lap_csv)", csv)
    print(f"\nCSV exists on disk: {Path(csv['path']).exists()}  ({Path(csv['path']).stat().st_size} bytes)")

    conn.close()


if __name__ == "__main__":
    main()
