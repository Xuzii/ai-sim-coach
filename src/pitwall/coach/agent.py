"""The agentic tool-use loop -- ``analyze_lap``.

Drives an :class:`~pitwall.coach.llm.base.LLMClient` through pitwall's existing
query tools until the model calls the terminal ``submit_coaching_report`` tool
whose params are the :class:`~pitwall.coach.report.CoachingReport` schema. The
captured args are validated, the report is stamped with provenance + persisted,
and the loop returns ``(report, report_id)``.

The loop is provider-agnostic: it only knows the neutral seam in
:mod:`pitwall.coach.llm.base`. ``GeminiClient`` (or a future ``ClaudeClient``)
plugs in unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from pitwall.coach import store
from pitwall.coach.config import CoachConfig
from pitwall.coach.llm.base import LLMClient, Message, ToolCall, ToolSpec
from pitwall.coach.report import CoachingReport, LapInfo, ReportMeta, report_json_schema
from pitwall.coach.tools import agent_tools, run_tool
from pitwall.storage import db

SUBMIT_TOOL = "submit_coaching_report"

SYSTEM_PROMPT = """\
You are an expert sim-racing race engineer. Your job is to analyse one ACC lap
of telemetry and produce a structured coaching report. You have read-only tools
that query a SQLite + Parquet store of lap, sector, and per-channel data.

How to work:
- Start cheap: call get_lap_summary, then get_sector_analysis vs the reference
  lap. Identify the worst sector or two by delta_ms.
- Drill into the problem zone with get_telemetry_trace at detail_level="summary"
  and a tight distance_range_m (e.g. one corner, 150-300 m wide). Never request
  detail_level="full" over a whole lap -- it blows the token budget.
- Ground any consistency remark by calling get_consistency for the session.
- Use compare_laps and get_tire_history only if a finding genuinely needs them.

Quality rules:
- Every finding.evidence must cite a real tool result you saw: a sector delta,
  a channel stat, the consistency score. Never invent figures.
- Findings must be specific and actionable: name the corner / sector, the
  channel involved (brake / throttle / steering / line), and a concrete change.
- Prefer 2-4 high-signal findings over many small ones. Rank the top 1-3 in
  `priorities` (1-based indices into the findings array).

Termination:
- When you have enough evidence, call submit_coaching_report ONCE with the
  full structured report (findings, consistency echo, priorities,
  human_summary). After that call, stop. Do not call any further tools.
"""

# Tunable: how wide a tool-result payload can be (chars) before we truncate it
# in the conversation history. Generous; a runaway full-detail trace would still
# be capped well under one Gemini context window.
_MAX_TOOL_RESULT_CHARS = 60_000


def analyze_lap(
    conn: sqlite3.Connection,
    lap_id: int,
    llm: LLMClient,
    config: CoachConfig,
) -> tuple[CoachingReport, int]:
    """Run the coaching loop on one lap and persist the report.

    Returns ``(report, report_id)``. Raises :class:`ValueError` if the model
    stalls (two consecutive text-only turns) or fails to submit a report within
    ``config.max_iterations``. Raises :class:`LookupError` if ``lap_id`` does
    not exist in the store.
    """
    meta, lap_info = _bootstrap_context(conn, lap_id, config)
    tools = _build_tool_list()
    system = config.system_prompt or SYSTEM_PROMPT

    messages: list[Message] = [_user_briefing(meta, lap_info)]
    stalled_once = False

    for _ in range(config.max_iterations):
        response = llm.generate(messages, tools, system=system)
        messages.append(
            Message(
                role="assistant",
                content=response.text,
                tool_calls=list(response.tool_calls),
            )
        )

        terminal_args = _find_terminal_call(response.tool_calls)
        if terminal_args is not None:
            return _finalize(conn, terminal_args, meta, lap_info, config)

        if response.tool_calls:
            for call in response.tool_calls:
                result = run_tool(conn, call.name, call.arguments)
                messages.append(_tool_result_message(call, result))
            stalled_once = False
            continue

        # Text-only response: nudge once, fail on the second consecutive stall.
        if stalled_once:
            raise ValueError("agent stalled without submitting a report (two text-only turns)")
        stalled_once = True
        messages.append(
            Message(
                role="user",
                content=(
                    "Please continue your analysis. When you have enough evidence, "
                    f"call {SUBMIT_TOOL} with your structured findings."
                ),
            )
        )

    raise ValueError(f"agent did not submit a report within {config.max_iterations} iterations")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bootstrap_context(
    conn: sqlite3.Connection,
    lap_id: int,
    config: CoachConfig,
) -> tuple[ReportMeta, LapInfo]:
    """Resolve the lap + session + reference lap into the meta/lap blocks the
    agent will stamp onto the final report."""
    lap = db.get_lap(conn, lap_id)
    if lap is None:
        raise LookupError(f"lap_id {lap_id} not found")
    session = db.get_session(conn, lap.session_id)
    if session is None:  # pragma: no cover -- foreign-key invariant
        raise LookupError(f"session {lap.session_id} for lap {lap_id} not found")

    reference_lap_id = config.reference_lap_id
    reference_desc: str | None = None
    delta_ms: int | None = None
    if reference_lap_id is None:
        fastest = db.fastest_valid_lap(conn, lap.session_id)
        if fastest is not None and fastest.lap_id is not None:
            reference_lap_id = fastest.lap_id
            reference_desc = "fastest valid lap in the same session"
            if fastest.lap_time_ms is not None and lap.lap_time_ms is not None:
                delta_ms = int(lap.lap_time_ms - fastest.lap_time_ms)
    else:
        ref_lap = db.get_lap(conn, reference_lap_id)
        if ref_lap is not None:
            reference_desc = f"lap_id {reference_lap_id}"
            if ref_lap.lap_time_ms is not None and lap.lap_time_ms is not None:
                delta_ms = int(lap.lap_time_ms - ref_lap.lap_time_ms)

    meta = ReportMeta(
        lap_id=lap_id,
        session_id=lap.session_id,
        track=session.track_name,
        car=session.car_name,
        reference_lap_id=reference_lap_id,
    )
    lap_info = LapInfo(
        lap_time_ms=lap.lap_time_ms,
        is_valid=lap.is_valid,
        delta_to_reference_ms=delta_ms,
        reference=reference_desc,
    )
    return meta, lap_info


def _build_tool_list() -> list[ToolSpec]:
    """The seven query tools + get_consistency, plus the terminal report tool.

    The terminal tool is NOT in :func:`pitwall.coach.tools.run_tool` -- the loop
    catches it by name and treats it as the exit condition, so adding it here
    is enough."""
    tools = list(agent_tools())
    tools.append(
        ToolSpec(
            name=SUBMIT_TOOL,
            description=(
                "Call this exactly once when your analysis is complete. Pass the full "
                "structured coaching report: a non-empty `findings` array (each with "
                "area / severity / observation / recommendation / evidence), an optional "
                "`consistency` echo of the get_consistency score you grounded any "
                "consistency remarks on, an optional 1-based `priorities` list, and a "
                "markdown `human_summary` tying it together. Do not call any further "
                "tools after this."
            ),
            parameters=report_json_schema(),
        )
    )
    return tools


def _user_briefing(meta: ReportMeta, lap_info: LapInfo) -> Message:
    parts = [
        f"Analyse lap_id={meta.lap_id}",
        f"on {meta.track or 'unknown track'} in the {meta.car or 'unknown car'}.",
    ]
    if lap_info.lap_time_ms is not None:
        parts.append(f"Lap time: {lap_info.lap_time_ms} ms.")
    if lap_info.is_valid is False:
        parts.append("This lap is INVALID (off-track) -- factor that into your analysis.")
    if meta.reference_lap_id is not None:
        delta = lap_info.delta_to_reference_ms
        delta_str = f" (delta {delta:+d} ms)" if delta is not None else ""
        ref_desc = lap_info.reference or f"lap_id {meta.reference_lap_id}"
        parts.append(f"Reference: {ref_desc}, lap_id={meta.reference_lap_id}{delta_str}.")
    parts.append(
        "Suggested order: get_lap_summary -> get_sector_analysis -> "
        "get_telemetry_trace(summary, distance_range) on the worst sector -> "
        f"get_consistency for the session -> {SUBMIT_TOOL}."
    )
    return Message(role="user", content=" ".join(parts))


def _find_terminal_call(tool_calls: list[ToolCall]) -> dict | None:
    """Return the arguments of the FIRST submit_coaching_report call, or None."""
    for call in tool_calls:
        if call.name == SUBMIT_TOOL:
            return call.arguments
    return None


def _tool_result_message(call: ToolCall, result: dict) -> Message:
    """Encode a ``run_tool`` result back into the conversation."""
    payload = json.dumps(result, default=str)
    if len(payload) > _MAX_TOOL_RESULT_CHARS:
        payload = payload[:_MAX_TOOL_RESULT_CHARS] + '..."<truncated>"'
    return Message(
        role="tool",
        content=payload,
        tool_call_id=call.id,
        name=call.name,
    )


def _finalize(
    conn: sqlite3.Connection,
    terminal_args: dict,
    meta: ReportMeta,
    lap_info: LapInfo,
    config: CoachConfig,
) -> tuple[CoachingReport, int]:
    """Validate, stamp provenance, persist."""
    report = CoachingReport.from_tool_args(terminal_args, meta=meta, lap=lap_info)
    report.meta.provider = config.provider
    report.meta.model = config.model
    report.meta.generated_at_utc = datetime.now(UTC).isoformat()
    report_id = store.insert_report(conn, report)
    return report, report_id
