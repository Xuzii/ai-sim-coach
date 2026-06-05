"""Persistence for coaching reports (the ``coaching_reports`` table).

The full :class:`~pitwall.coach.report.CoachingReport` is stored as JSON in
``report_json`` for lossless round-trips; the columns alongside it
(lap/session/reference ids, provider, model, schema version, timestamp,
consistency score) are denormalised for cheap listing and filtering. Reuses
``db.connect`` / ``db.apply_schema`` (so WAL + foreign keys are inherited)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pitwall import timeutil
from pitwall.coach.report import CoachingReport


@dataclass
class StoredReportRow:
    """A row from ``coaching_reports`` without the heavy JSON body -- for listings."""

    report_id: int
    lap_id: int
    session_id: int | None
    reference_lap_id: int | None
    provider: str | None
    model: str | None
    schema_version: int
    generated_at_utc: str
    consistency_score: int | None


def insert_report(conn: sqlite3.Connection, report: CoachingReport) -> int:
    """Persist a report and return its new ``report_id``. Stamps
    ``meta.generated_at_utc`` if unset so the stored column and JSON body agree."""
    if report.meta.generated_at_utc is None:
        report.meta.generated_at_utc = timeutil.to_iso_utc(timeutil.utc_now())
    m = report.meta
    cur = conn.execute(
        "INSERT INTO coaching_reports (lap_id, session_id, reference_lap_id, provider, model, "
        "schema_version, generated_at_utc, consistency_score, report_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            m.lap_id, m.session_id, m.reference_lap_id, m.provider, m.model,
            m.schema_version, m.generated_at_utc, report.consistency.score,
            json.dumps(report.to_dict()),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_report(conn: sqlite3.Connection, report_id: int) -> CoachingReport | None:
    """Load a full report by id, or None if there is no such report."""
    row = conn.execute(
        "SELECT report_json FROM coaching_reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        return None
    return CoachingReport.from_dict(json.loads(row["report_json"]))


def list_reports(
    conn: sqlite3.Connection,
    *,
    lap_id: int | None = None,
    session_id: int | None = None,
    limit: int = 50,
) -> list[StoredReportRow]:
    """List report metadata (newest first), optionally filtered by lap or session."""
    clauses, params = [], []
    if lap_id is not None:
        clauses.append("lap_id = ?")
        params.append(lap_id)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = conn.execute(
        "SELECT report_id, lap_id, session_id, reference_lap_id, provider, model, schema_version, "
        f"generated_at_utc, consistency_score FROM coaching_reports{where} "
        "ORDER BY generated_at_utc DESC, report_id DESC LIMIT ?",
        params,
    ).fetchall()
    return [StoredReportRow(**{k: r[k] for k in r.keys()}) for r in rows]
