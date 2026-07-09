"""``pitwall-coach`` -- the command-line interface for the single-agent coach.

Three subcommands over the local store the MCP server / ``pitwall-ingest`` populate:

* ``pitwall-coach analyze <lap_id> [--reference ID | --pb] [--model M] [--json]``
  -- run the agentic coaching loop on one lap (this is the only command that calls
  the LLM), persist the structured :class:`~pitwall.coach.report.CoachingReport`, and
  print a human-readable summary (or the raw JSON with ``--json``).
* ``pitwall-coach show <report_id> [--json]`` -- re-print a previously saved report
  from SQLite (no LLM call).
* ``pitwall-coach list [--session ID | --lap ID]`` -- list saved reports, newest first.

Only ``analyze`` needs a Gemini API key + the ``coach`` extra; ``show`` / ``list`` are
pure reads. Every failure mode the user can hit (unknown lap/report, missing key, the
``google-genai`` SDK not installed, an agent that never submits) is turned into a clear
message + a non-zero exit code rather than a traceback -- the same graceful-error
philosophy as the rest of pitwall.

The LLM client is built through an injectable ``llm_factory`` so the whole CLI is
testable offline with a :class:`~pitwall.coach.llm.base.MockLLMClient` and no key.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Callable

from pitwall.coach import agent, store
from pitwall.coach.config import CoachConfig
from pitwall.coach.llm.base import LLMClient
from pitwall.coach.report import CoachingReport
from pitwall.storage import db, paths
from pitwall.tools.queries import fmt_time

LLMFactory = Callable[[CoachConfig], LLMClient]


def _default_llm_factory(config: CoachConfig) -> LLMClient:
    """Build the real provider client from ``config``.

    Imported here (not at module top) so ``show`` / ``list`` -- and the whole base
    install without the ``coach`` extra -- never touch ``google-genai``. Raises
    ``RuntimeError`` (missing key, via ``resolve_api_key``) or ``ImportError`` (SDK
    not installed); :func:`cmd_analyze` maps both to a friendly message."""
    from pitwall.coach.llm.gemini import GeminiClient

    return GeminiClient.from_config(config)


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #
def cmd_analyze(conn: sqlite3.Connection, args: argparse.Namespace, llm_factory: LLMFactory) -> int:
    lap = db.get_lap(conn, args.lap_id)
    if lap is None:
        print(f"error: no lap with id {args.lap_id} in the store.", file=sys.stderr)
        print("       run `pitwall-ingest` first, or `pitwall-coach list` to see what's there.", file=sys.stderr)
        return 2

    reference_lap_id, ref_error = _resolve_reference(conn, lap, args)
    if ref_error is not None:
        print(f"error: {ref_error}", file=sys.stderr)
        return 2

    config = CoachConfig(reference_lap_id=reference_lap_id)
    if args.model:
        config.model = args.model
    if args.max_iterations is not None:
        config.max_iterations = args.max_iterations

    try:
        llm = llm_factory(config)
    except RuntimeError as exc:
        # Missing API key -- resolve_api_key already points at .env / .env.example.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ImportError:
        print(
            "error: the Gemini provider needs the 'coach' extra. Install it with\n"
            '       .\\.venv\\Scripts\\python.exe -m pip install -e ".[coach]"',
            file=sys.stderr,
        )
        return 2

    print(f"Analysing lap {args.lap_id} with {config.provider}/{config.model} ...", file=sys.stderr)
    try:
        report, report_id = agent.analyze_lap(conn, args.lap_id, llm, config)
    except ValueError as exc:
        # The agent stalled / never submitted a report / submitted an invalid one.
        print(f"error: the coach did not produce a valid report ({exc}).", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- surface any provider error cleanly, no traceback
        print(f"error: coaching run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
    print(f"\nSaved report #{report_id} to {paths.db_path()}", file=sys.stderr)
    return 0


def _resolve_reference(
    conn: sqlite3.Connection, lap: db.Lap, args: argparse.Namespace
) -> tuple[int | None, str | None]:
    """Turn the ``--reference`` / ``--pb`` flags into a reference lap id.

    Returns ``(reference_lap_id, error)``. ``reference_lap_id=None`` means "let the
    agent default to the fastest valid lap in the same session". A non-None ``error``
    means the requested reference couldn't be resolved (caller exits)."""
    if args.reference is not None:
        if db.get_lap(conn, args.reference) is None:
            return None, f"reference lap {args.reference} not found."
        return args.reference, None
    if args.pb:
        session = db.get_session(conn, lap.session_id)
        if session is None:  # pragma: no cover -- foreign-key invariant
            return None, f"session {lap.session_id} for lap {lap.lap_id} not found."
        pb = db.personal_best_lap(conn, session.track_code, session.car_code)
        if pb is None or pb.lap_id is None:
            return None, f"no personal-best lap on record for {session.track_name} / {session.car_name}."
        return pb.lap_id, None
    return None, None


# --------------------------------------------------------------------------- #
# show / list
# --------------------------------------------------------------------------- #
def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    report = store.get_report(conn, args.report_id)
    if report is None:
        print(f"error: no report with id {args.report_id}.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_report(report))
    return 0


def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    rows = store.list_reports(conn, lap_id=args.lap, session_id=args.session, limit=args.limit)
    if not rows:
        print("no coaching reports yet. Run `pitwall-coach analyze <lap_id>` to make one.")
        return 0
    print(f"{'ID':>4}  {'LAP':>5}  {'SESSION':>7}  {'SCORE':>5}  {'PROVIDER/MODEL':<28}  GENERATED")
    for r in rows:
        score = "--" if r.consistency_score is None else str(r.consistency_score)
        pm = f"{r.provider or '?'}/{r.model or '?'}"
        sess = "--" if r.session_id is None else str(r.session_id)
        print(f"{r.report_id:>4}  {r.lap_id:>5}  {sess:>7}  {score:>5}  {pm:<28.28}  {r.generated_at_utc}")
    return 0


# --------------------------------------------------------------------------- #
# Human-readable report formatting
# --------------------------------------------------------------------------- #
_AREA_LABELS = {
    "braking": "Braking",
    "throttle": "Throttle",
    "racing_line": "Racing line",
    "consistency": "Consistency",
}


def format_report(report: CoachingReport) -> str:
    """Render a :class:`CoachingReport` as a readable terminal block."""
    m, lap = report.meta, report.lap
    lines: list[str] = []
    where = " / ".join(x for x in (m.track, m.car) if x) or "unknown track/car"
    lines.append(f"Coaching report -- lap {m.lap_id} ({where})")
    lines.append("=" * min(len(lines[-1]), 72))

    lap_t = fmt_time(lap.lap_time_ms) or "--"
    valid = "" if lap.is_valid is None else ("  [VALID]" if lap.is_valid else "  [INVALID]")
    lines.append(f"Lap time: {lap_t}{valid}")
    if m.reference_lap_id is not None:
        delta = lap.delta_to_reference_ms
        delta_str = "" if delta is None else f"  ({delta / 1000.0:+.3f}s)"
        ref = lap.reference or f"lap {m.reference_lap_id}"
        lines.append(f"Reference: {ref} (lap {m.reference_lap_id}){delta_str}")
    if report.consistency.score is not None:
        basis = f" -- {report.consistency.basis}" if report.consistency.basis else ""
        lines.append(f"Consistency: {report.consistency.score}/100{basis}")

    lines.append("")
    lines.append(f"Findings ({len(report.findings)}):")
    prio = set(report.priorities)
    for i, f in enumerate(report.findings, start=1):
        star = " *" if i in prio else ""
        area = _AREA_LABELS.get(f.area, f.area)
        loc = f" -- sector {f.sector}" if f.sector is not None else ""
        if f.distance_range_m:
            loc += f" ({f.distance_range_m[0]:.0f}-{f.distance_range_m[1]:.0f} m)"
        gain = f"  (~{f.estimated_gain_ms} ms)" if f.estimated_gain_ms is not None else ""
        lines.append(f"  {i}. [{area} sev {f.severity}/5]{loc}{star}{gain}")
        lines.append(f"     {f.observation}")
        lines.append(f"     -> {f.recommendation}")
        lines.append(f"     evidence: {f.evidence}")
    if report.priorities:
        lines.append("")
        order = ", ".join(f"#{p}" for p in report.priorities)
        lines.append(f"Work on first (*): {order}")
    if report.human_summary:
        lines.append("")
        lines.append("Summary:")
        lines.append(report.human_summary)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pitwall-coach",
        description="Analyse a lap with the AI race-engineer coach and review saved reports.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="run the coach on one lap and save a report")
    p_analyze.add_argument("lap_id", type=int, help="the lap to analyse (see `pitwall-coach list` / the MCP tools)")
    ref = p_analyze.add_mutually_exclusive_group()
    ref.add_argument("--reference", type=int, metavar="LAP_ID", help="reference lap to compare against")
    ref.add_argument(
        "--pb", action="store_true", help="use the all-time personal-best lap for this car+track as reference"
    )
    p_analyze.add_argument("--model", default=None, help="override the LLM model (default: gemini-2.5-flash)")
    p_analyze.add_argument(
        "--max-iterations", type=int, default=None, dest="max_iterations", help="cap the agent's tool-use turns"
    )
    p_analyze.add_argument("--json", action="store_true", help="print the structured report as JSON")

    p_show = sub.add_parser("show", help="print a previously saved report")
    p_show.add_argument("report_id", type=int)
    p_show.add_argument("--json", action="store_true", help="print the structured report as JSON")

    p_list = sub.add_parser("list", help="list saved reports (newest first)")
    grp = p_list.add_mutually_exclusive_group()
    grp.add_argument("--session", type=int, default=None, help="only reports for this session")
    grp.add_argument("--lap", type=int, default=None, help="only reports for this lap")
    p_list.add_argument("--limit", type=int, default=50, help="max rows (default 50)")

    return parser


def main(argv: list[str] | None = None, *, llm_factory: LLMFactory = _default_llm_factory) -> int:
    """Console entry point for ``pitwall-coach`` / ``python -m pitwall.coach.cli``.

    ``llm_factory`` is injectable so tests drive the loop with a ``MockLLMClient``."""
    args = _build_parser().parse_args(argv)

    conn = db.connect()
    db.apply_schema(conn)
    try:
        if args.command == "analyze":
            return cmd_analyze(conn, args, llm_factory)
        if args.command == "show":
            return cmd_show(conn, args)
        if args.command == "list":
            return cmd_list(conn, args)
        return 2  # pragma: no cover -- argparse(required=True) prevents this
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
