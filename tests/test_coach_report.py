"""P2-C0: the CoachingReport contract -- schema, parse/validate, lossless round-trip."""

from __future__ import annotations

import pytest

from pitwall.coach.report import (
    AREA_VALUES,
    CoachingReport,
    Consistency,
    Finding,
    LapInfo,
    ReportMeta,
    report_json_schema,
)


def _meta() -> ReportMeta:
    return ReportMeta(lap_id=2, session_id=1, track="Nurburgring GP", car="Ford Mustang GT3",
                      reference_lap_id=2, provider="mock", model="mock-1")


def _lap() -> LapInfo:
    return LapInfo(lap_time_ms=124_126, is_valid=True, delta_to_reference_ms=0, reference="fastest valid lap")


def _valid_args() -> dict:
    return {
        "findings": [
            {"area": "braking", "sector": 2, "distance_range_m": [200, 400], "severity": 4,
             "observation": "Braking too late into T1", "recommendation": "Brake 10 m earlier",
             "evidence": "sector 2 +0.41s vs ref", "estimated_gain_ms": 410},
            {"area": "throttle", "severity": 2, "observation": "Early lift",
             "recommendation": "Hold throttle", "evidence": "throttle dips to 0.6 mid-corner"},
        ],
        "consistency": {"score": 88, "basis": "5 valid timed laps"},
        "priorities": [1, 2],
        "human_summary": "**Solid lap.** Focus on braking in S2.",
    }


def test_report_json_schema_shape():
    schema = report_json_schema()
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"findings", "human_summary"}
    item = schema["properties"]["findings"]["items"]
    assert item["properties"]["area"]["enum"] == list(AREA_VALUES)
    assert set(item["required"]) == {"area", "severity", "observation", "recommendation", "evidence"}


def test_from_tool_args_parses_and_validates():
    report = CoachingReport.from_tool_args(_valid_args(), meta=_meta(), lap=_lap())
    assert len(report.findings) == 2
    assert report.findings[0].area == "braking" and report.findings[0].severity == 4
    assert report.consistency.score == 88
    assert report.priorities == [1, 2]
    assert report.meta.lap_id == 2 and report.lap.lap_time_ms == 124_126


def test_from_tool_args_clamps_severity_and_filters_priorities():
    args = _valid_args()
    args["findings"][0]["severity"] = 9       # out of range -> clamp to 5
    args["findings"][1]["severity"] = 0        # -> clamp to 1
    args["priorities"] = [2, 99, 0, 1]         # 99 and 0 are out of range -> dropped
    report = CoachingReport.from_tool_args(args, meta=_meta(), lap=_lap())
    assert report.findings[0].severity == 5
    assert report.findings[1].severity == 1
    assert report.priorities == [2, 1]


def test_from_tool_args_rejects_structural_errors():
    with pytest.raises(ValueError):
        CoachingReport.from_tool_args({"human_summary": "x"}, meta=_meta(), lap=_lap())  # no findings
    with pytest.raises(ValueError):
        CoachingReport.from_tool_args({"findings": []}, meta=_meta(), lap=_lap())  # empty findings
    with pytest.raises(ValueError):
        CoachingReport.from_tool_args(
            {"findings": [{"area": "tyres", "severity": 3}]}, meta=_meta(), lap=_lap()  # bad area
        )


def test_to_dict_from_dict_roundtrips_losslessly():
    report = CoachingReport.from_tool_args(_valid_args(), meta=_meta(), lap=_lap())
    again = CoachingReport.from_dict(report.to_dict())
    assert again.to_dict() == report.to_dict()
    assert isinstance(again.findings[0], Finding)
    assert isinstance(again.consistency, Consistency)


def test_distance_range_normalised():
    report = CoachingReport.from_tool_args(_valid_args(), meta=_meta(), lap=_lap())
    assert report.findings[0].distance_range_m == [200.0, 400.0]
    # a malformed range becomes None rather than blowing up
    args = _valid_args()
    args["findings"][0]["distance_range_m"] = [1, 2, 3]
    bad = CoachingReport.from_tool_args(args, meta=_meta(), lap=_lap())
    assert bad.findings[0].distance_range_m is None
