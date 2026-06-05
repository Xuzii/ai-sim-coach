"""The coaching report -- pitwall's structured coaching output and the Phase 3 contract.

A :class:`CoachingReport` is what the coach produces for one lap. Phase 3's
specialist agents (Braking / Throttle / Line / Tire + a Synthesis Coach) will emit
and consume this same shape, so it is deliberately stable and self-describing.

Two layers:

- The **dataclasses** (`CoachingReport`, `Finding`, `Consistency`, `ReportMeta`,
  `LapInfo`) are the in-memory model, serialisable to/from a plain dict for SQLite
  persistence (``to_dict`` / ``from_dict``, lossless round-trip).
- The **tool schema** (:func:`report_json_schema`) describes the parameters of the
  terminal ``submit_coaching_report`` tool the agent loop ends on. Only the fields
  the *model* authors live there (findings, consistency echo, priorities, summary);
  the agent stamps the rest of ``meta`` and ``lap`` from what it already knows.
  :meth:`CoachingReport.from_tool_args` parses + validates that tool call.

House style: hand-written dataclasses and JSON schema, no Pydantic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

REPORT_SCHEMA_VERSION = 1

# The coaching areas a finding can belong to. "consistency" lets the coach raise a
# finding about lap-to-lap repeatability grounded in the get_consistency score.
AREA_VALUES: tuple[str, ...] = ("braking", "throttle", "racing_line", "consistency")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    """One specific, telemetry-grounded coaching point."""

    area: str  # one of AREA_VALUES
    severity: int  # 1 (minor) .. 5 (major time loss)
    observation: str
    recommendation: str
    evidence: str  # the tool result / channel stat this rests on
    sector: int | None = None  # 1-based sector number
    distance_range_m: list[float] | None = None  # [start_m, end_m] of the zone
    estimated_gain_ms: int | None = None

    @classmethod
    def from_dict(cls, d: Any) -> Finding:
        if not isinstance(d, dict):
            raise ValueError("each finding must be an object")
        area = d.get("area")
        if area not in AREA_VALUES:
            raise ValueError(f"finding.area must be one of {AREA_VALUES}, got {area!r}")
        return cls(
            area=area,
            severity=_clamp_int(d.get("severity", 3), 1, 5),
            observation=str(d.get("observation", "")),
            recommendation=str(d.get("recommendation", "")),
            evidence=str(d.get("evidence", "")),
            sector=_opt_int(d.get("sector")),
            distance_range_m=_opt_range(d.get("distance_range_m")),
            estimated_gain_ms=_opt_int(d.get("estimated_gain_ms")),
        )


@dataclass
class Consistency:
    """The deterministic consistency score the coach grounds remarks on."""

    score: int | None  # 0-100, higher = more consistent
    basis: str = ""

    @classmethod
    def from_dict(cls, d: Any) -> Consistency:
        if not isinstance(d, dict):
            return cls(score=None, basis="")
        return cls(score=_opt_int(d.get("score")), basis=str(d.get("basis", "")))


@dataclass
class ReportMeta:
    """Provenance + identity. Stamped by the agent, not authored by the model."""

    lap_id: int
    session_id: int | None = None
    track: str | None = None
    car: str | None = None
    reference_lap_id: int | None = None
    provider: str | None = None
    model: str | None = None
    generated_at_utc: str | None = None
    schema_version: int = REPORT_SCHEMA_VERSION


@dataclass
class LapInfo:
    """The lap under analysis, relative to its reference."""

    lap_time_ms: int | None = None
    is_valid: bool | None = None
    delta_to_reference_ms: int | None = None
    reference: str | None = None  # human description of the reference lap


@dataclass
class CoachingReport:
    """The full structured coaching report for one lap."""

    meta: ReportMeta
    lap: LapInfo
    findings: list[Finding]
    consistency: Consistency
    priorities: list[int] = field(default_factory=list)  # 1-based indices into findings, top first
    human_summary: str = ""

    # -- construction from the model's terminal tool call -------------------- #
    @classmethod
    def from_tool_args(cls, args: Any, *, meta: ReportMeta, lap: LapInfo) -> CoachingReport:
        """Build + validate a report from a ``submit_coaching_report`` tool call.

        ``args`` is the model-authored payload (see :func:`report_json_schema`);
        ``meta`` and ``lap`` are supplied by the agent. Raises ``ValueError`` on a
        structurally invalid payload (missing/empty findings, bad area); leniently
        drops out-of-range priority indices rather than failing the whole report."""
        if not isinstance(args, dict):
            raise ValueError("submit_coaching_report args must be an object")
        raw_findings = args.get("findings")
        if not isinstance(raw_findings, list) or not raw_findings:
            raise ValueError("a report requires a non-empty 'findings' array")
        findings = [Finding.from_dict(f) for f in raw_findings]
        consistency = Consistency.from_dict(args.get("consistency"))
        n = len(findings)
        priorities = [
            int(i) for i in (args.get("priorities") or [])
            if isinstance(i, (int, float)) and not isinstance(i, bool) and 1 <= int(i) <= n
        ]
        return cls(
            meta=meta,
            lap=lap,
            findings=findings,
            consistency=consistency,
            priorities=priorities,
            human_summary=str(args.get("human_summary", "")),
        )

    # -- persistence round-trip --------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "meta": asdict(self.meta),
            "lap": asdict(self.lap),
            "findings": [asdict(f) for f in self.findings],
            "consistency": asdict(self.consistency),
            "priorities": list(self.priorities),
            "human_summary": self.human_summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CoachingReport:
        return cls(
            meta=ReportMeta(**d["meta"]),
            lap=LapInfo(**d["lap"]),
            findings=[Finding(**f) for f in d["findings"]],
            consistency=Consistency(**d["consistency"]),
            priorities=list(d.get("priorities", [])),
            human_summary=d.get("human_summary", ""),
        )


# --------------------------------------------------------------------------- #
# The submit_coaching_report tool schema (model-authored fields only)
# --------------------------------------------------------------------------- #
def _finding_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "area": {"type": "string", "enum": list(AREA_VALUES)},
            "sector": {"type": "integer", "description": "1-based sector number this finding is about, or null."},
            "distance_range_m": {
                "type": "array",
                "items": {"type": "number"},
                "description": "[start_m, end_m] of the corner/zone, or null if not localised.",
            },
            "severity": {"type": "integer", "description": "1 (minor) to 5 (major time loss)."},
            "observation": {"type": "string", "description": "What the data shows."},
            "recommendation": {"type": "string", "description": "Concrete, actionable advice for the driver."},
            "evidence": {
                "type": "string",
                "description": "The tool result or channel stat this rests on (e.g. 'sector 2 +0.41s vs ref; "
                "min brake speed 38 km/h higher'). Never cite figures not in the data.",
            },
            "estimated_gain_ms": {"type": "integer", "description": "Estimated time gain if fixed (ms), or null."},
        },
        "required": ["area", "severity", "observation", "recommendation", "evidence"],
    }


def report_json_schema() -> dict:
    """JSON schema for the terminal ``submit_coaching_report`` tool's parameters.

    Only the fields the model authors: the agent stamps lap/session/track/car,
    the reference lap, provider/model, and the timestamp afterwards."""
    return {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "description": "Specific, telemetry-grounded coaching findings for this lap.",
                "items": _finding_schema(),
            },
            "consistency": {
                "type": "object",
                "description": "Echo the get_consistency result you grounded any consistency remarks on.",
                "properties": {
                    "score": {"type": "integer", "description": "0-100 from get_consistency, or null."},
                    "basis": {"type": "string"},
                },
            },
            "priorities": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "1-based indices into `findings`, most important first (1-3 items).",
            },
            "human_summary": {
                "type": "string",
                "description": "A short markdown narrative for the driver tying the findings together.",
            },
        },
        "required": ["findings", "human_summary"],
    }


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _opt_int(x: Any) -> int | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _clamp_int(x: Any, lo: int, hi: int) -> int:
    v = _opt_int(x)
    if v is None:
        v = lo
    return max(lo, min(hi, v))


def _opt_range(x: Any) -> list[float] | None:
    if isinstance(x, (list, tuple)) and len(x) == 2:
        try:
            return [float(x[0]), float(x[1])]
        except (TypeError, ValueError):
            return None
    return None
