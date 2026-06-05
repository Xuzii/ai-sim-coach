"""The coach agent's tool registry and dispatcher.

Builds the provider-neutral :class:`~pitwall.coach.llm.base.ToolSpec` list the agent
offers the model, and dispatches tool calls to handlers. Tool name/description/schema
come from the one shared source, :mod:`pitwall.tools.specs` (so the coach and the MCP
server never disagree), plus one coach-only tool:

- ``get_consistency`` -- the deterministic 0-100 consistency score, exposed so the
  model grounds repeatability remarks in a real number instead of guessing.

``export_lap_csv`` is intentionally **excluded**: it writes a file for the user, not
an analysis input, so offering it to the coach would only add tool-choice noise and
prompt cost.
"""

from __future__ import annotations

import sqlite3

from pitwall.coach import consistency
from pitwall.coach.llm import base
from pitwall.storage import db
from pitwall.tools import specs

GET_CONSISTENCY = "get_consistency"

# Query tools the coach does NOT offer the model (see module docstring).
_EXCLUDED = frozenset({"export_lap_csv"})
COACH_QUERY_SPECS: list[specs.QueryToolSpec] = [s for s in specs.TOOL_SPECS if s.name not in _EXCLUDED]

_CONSISTENCY_DESC = (
    "Compute a deterministic 0-100 consistency score for a session (or one stint) from the spread of "
    "lap times and per-sector times across the valid, timed laps. Higher = more repeatable. Use this to "
    "ground any comment about how consistent the driver is -- never estimate consistency yourself."
)
_CONSISTENCY_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "integer", "description": "Session to score."},
        "stint_number": {"type": "integer", "description": "Optional: limit to one stint (a run on one tyre set)."},
    },
    "required": ["session_id"],
}


def get_consistency(conn: sqlite3.Connection, *, session_id: int, stint_number: int | None = None) -> dict:
    """The ``get_consistency`` tool: load a session's laps + sectors and score them."""
    laps = db.laps_for_session(conn, session_id)
    if stint_number is not None:
        laps = [lap for lap in laps if lap.stint_number == stint_number]
    sectors_by_lap = {lap.lap_id: db.sectors_for_lap(conn, lap.lap_id) for lap in laps}
    result = consistency.consistency_score(laps, sectors_by_lap)
    result["session_id"] = session_id
    result["stint_number"] = stint_number
    return result


def agent_tools() -> list[base.ToolSpec]:
    """The provider-neutral tools the coach offers the model: the shared query tools
    (minus the excluded ones) plus ``get_consistency``."""
    tools = [base.ToolSpec(s.name, s.description, specs.parameters_schema(s)) for s in COACH_QUERY_SPECS]
    tools.append(base.ToolSpec(GET_CONSISTENCY, _CONSISTENCY_DESC, _CONSISTENCY_SCHEMA))
    return tools


_QUERY_HANDLERS = {s.name: s.handler for s in COACH_QUERY_SPECS}


def run_tool(conn: sqlite3.Connection, name: str, arguments: dict | None = None) -> dict:
    """Dispatch a coach tool call. Returns the handler's JSON-dict result, or a
    graceful ``{"error": ...}`` for an unknown tool or a bad argument set, so a stray
    model tool call never crashes the agent loop."""
    args = arguments or {}
    try:
        if name == GET_CONSISTENCY:
            return get_consistency(conn, **args)
        handler = _QUERY_HANDLERS.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        return handler(conn, **args)
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the model, don't crash the loop
        return {"error": f"{type(exc).__name__}: {exc}"}
