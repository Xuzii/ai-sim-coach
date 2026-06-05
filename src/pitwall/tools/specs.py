"""Canonical definitions of pitwall's query tools -- the single source shared by
the MCP server (Claude Desktop) and the Phase 2 coach agent.

Each :class:`QueryToolSpec` couples a tool's ``name``, the ``description`` the
model reads to decide *when* to call it (the prompt engineering), and the
underlying connection-in ``handler`` from :mod:`pitwall.tools.queries`. Both the
server and the coach build their tool surfaces from this one list, so they can
never drift: add a tool or reword a description here and both sides update.

The parameter schema is derived from the handler's own typed signature (minus the
injected ``conn``):

- the MCP server lets FastMCP introspect the synthesized signature (see
  :func:`public_signature`), so Claude Desktop sees exactly what it did before;
- the coach calls :func:`parameters_schema` for a clean, provider-neutral JSON
  schema to hand to Gemini.

The descriptions below are copied verbatim from the original ``server.py`` tool
docstrings -- they are the most important prompt engineering in the project, so
they now live in exactly one place.
"""

from __future__ import annotations

import inspect
import sqlite3  # noqa: F401  -- referenced by handler annotations under eval_str
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin

from pitwall.tools import queries

# The detail-level enum the server has always advertised to Claude. The query
# function types this argument as a plain ``str``; we tighten it to a Literal here
# so the generated schema keeps its enum without modifying Phase 1 query code.
DetailLevel = Literal["summary", "low", "medium", "full"]


@dataclass(frozen=True)
class QueryToolSpec:
    """One query tool: its public name, the model-facing description, the
    connection-in handler, and any per-parameter annotation overrides used only
    for schema generation (not for dispatch)."""

    name: str
    description: str
    handler: Callable[..., dict]
    annotation_overrides: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Descriptions (verbatim from the original server.py tool docstrings)
# --------------------------------------------------------------------------- #
_LIST_SESSIONS = """Find driving sessions, newest first. Start here when the user refers to a
track, car, day, or "my last session" and you need a session_id to drill into.

Filters (all optional): `track` and `car` match the name or internal code
(case-insensitive substring, e.g. "spa", "720s"); `since` is an ISO-8601 UTC
lower bound on start time (e.g. "2026-05-20T00:00:00+00:00"). Returns each
session's id, track, car, type, start time, lap count, and best valid lap."""

_LIST_LAPS = """List the laps in a session. Use after `list_sessions` to pick a lap_id.

Set `valid_only=True` to exclude laps invalidated for going off-track. Set
`top_n_fastest=N` to get only the N quickest valid timed laps, fastest first
(ideal for "my fastest lap" / "best two laps"). Returns per lap: lap_id,
lap_number, lap time (ms and formatted), validity, out/in-lap flags, stint,
and top speed."""

_GET_LAP_SUMMARY = """Summarise a single lap: overall and per-sector times, top/avg speed, fuel
used, validity, stint, and the delta to the driver's personal best for that
car+track. Use this for "how was lap N" or before deciding whether a deeper
telemetry pull is worthwhile. Cheap; safe to call often."""

_GET_TELEMETRY_TRACE = """Pull channel telemetry for a lap, binned by track distance.

`channels` selects canonical channels (e.g. ["brake","throttle","speed_kmh",
"steering_angle","rpm","gear"]); omit for a sensible default set.
`detail_level` controls cost vs resolution:
  - "summary" (default): min/max/mean/std per channel, per sector and
    whole-lap. No raw points -- use this first; it is by far the cheapest.
  - "low"/"medium"/"full": a distance-binned series (~60/150/600 points)
    with the mean of each channel per bin.
`distance_range` is an inclusive [start_m, end_m] window -- pass it to zoom
into a corner (e.g. [200, 400] for "brake pressure at T1"); the bins then
concentrate in that window for higher resolution. Avoid "full" over a whole
lap unless the user truly needs every point -- it is the largest response."""

_COMPARE_LAPS = """Compare two laps and show where time went. Returns the overall lap-time
delta and, per sector, each lap's time, the delta (positive = lap A slower),
and each lap's average speed in that sector. Use for "where did I lose time
between my fastest and second-fastest lap?"."""

_GET_SECTOR_ANALYSIS = """Break a lap into sectors versus a reference lap (defaults to the fastest
valid lap in the same session) and report per-sector deltas, the total delta,
and which sector lost the most time. Use for "which sector is costing me?"."""

_GET_TIRE_HISTORY = """Track tyre temperatures and pressures across a session, lap by lap, per
wheel (fl/fr/rl/rr). Pass `stint_number` to focus on one stint (a stint is a
run on one set of tyres). Use for "what were my tyre temps over the last
stint?" or to spot a tyre overheating across a run."""

_EXPORT_LAP_CSV = """Export a lap's full-resolution telemetry to a CSV file on disk and return
its path (plus row/column counts) to hand to the user. Omit `channels` for
all channels. Use when the user asks to export, download, or save a lap, or
wants the raw data for their own analysis."""


TOOL_SPECS: list[QueryToolSpec] = [
    QueryToolSpec("list_sessions", _LIST_SESSIONS, queries.list_sessions),
    QueryToolSpec("list_laps", _LIST_LAPS, queries.list_laps),
    QueryToolSpec("get_lap_summary", _GET_LAP_SUMMARY, queries.get_lap_summary),
    QueryToolSpec(
        "get_telemetry_trace",
        _GET_TELEMETRY_TRACE,
        queries.get_telemetry_trace,
        {"detail_level": DetailLevel},
    ),
    QueryToolSpec("compare_laps", _COMPARE_LAPS, queries.compare_laps),
    QueryToolSpec("get_sector_analysis", _GET_SECTOR_ANALYSIS, queries.get_sector_analysis),
    QueryToolSpec("get_tire_history", _GET_TIRE_HISTORY, queries.get_tire_history),
    QueryToolSpec("export_lap_csv", _EXPORT_LAP_CSV, queries.export_lap_csv),
]
TOOL_SPECS_BY_NAME: dict[str, QueryToolSpec] = {s.name: s for s in TOOL_SPECS}


# --------------------------------------------------------------------------- #
# Signature + schema derivation
# --------------------------------------------------------------------------- #
def public_signature(spec: QueryToolSpec) -> inspect.Signature:
    """The tool's public signature: the handler's signature with the injected
    ``conn`` removed and any ``annotation_overrides`` applied. FastMCP introspects
    this (via ``__signature__`` on the server wrapper) to build the input schema,
    so the server keeps advertising exactly the parameters it always has."""
    sig = inspect.signature(spec.handler, eval_str=True)
    params = []
    for name, param in sig.parameters.items():
        if name == "conn":
            continue
        if name in spec.annotation_overrides:
            param = param.replace(annotation=spec.annotation_overrides[name])
        params.append(param)
    return sig.replace(parameters=params, return_annotation=dict)


def parameters_schema(spec: QueryToolSpec) -> dict:
    """A clean, provider-neutral JSON-Schema object for the tool's parameters,
    derived from :func:`public_signature`. Hand-rolled (no Pydantic) so the coach
    hands Gemini a minimal ``{type, properties, required}`` shape rather than the
    ``anyOf``/``$defs`` flavour FastMCP emits. Optional params are simply absent
    from ``required`` (their nullable type is collapsed to the inner type)."""
    sig = public_signature(spec)
    props: dict[str, dict] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
        props[name] = _schema_for_annotation(annotation)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _schema_for_annotation(annotation: Any) -> dict:
    """Map a Python type annotation to a minimal JSON-Schema fragment."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        return _schema_for_annotation(non_none[0]) if non_none else {"type": "string"}
    if origin is Literal:
        return {"type": "string", "enum": list(get_args(annotation))}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is str:
        return {"type": "string"}
    if origin is list:
        args = get_args(annotation)
        item = _schema_for_annotation(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}
    return {"type": "string"}


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def run_tool(conn: sqlite3.Connection, name: str, arguments: dict | None = None) -> dict:
    """Dispatch a query tool by name. Returns the handler's JSON-dict result, or a
    graceful ``{"error": ...}`` for an unknown tool or a bad argument set -- callers
    (the agent loop) should treat any ``error`` key as a recoverable signal."""
    spec = TOOL_SPECS_BY_NAME.get(name)
    if spec is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return spec.handler(conn, **(arguments or {}))
    except Exception as exc:  # noqa: BLE001 -- surface any failure to the model, don't crash the loop
        return {"error": f"{type(exc).__name__}: {exc}"}
