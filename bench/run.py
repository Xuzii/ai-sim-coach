"""Reproducible performance benchmarks for pitwall.

Measures the four things the Phase 1 plan asks ``BENCHMARKS.md`` to publish:

1. **Ingest** -- offline-replay throughput (native frames/s), wall time, peak
   memory, and CPU of a full session ingest (parse -> downsample -> segment ->
   persist).
2. **Storage** -- bytes per lap (Parquet trace + SQLite row share) and points/lap.
3. **Query latency** -- median + p95 of each query function behind the 8 MCP
   tools, over many warm runs against the ingested store.
4. **Token budget** -- serialized-JSON bytes and an estimated token count of each
   tool response across ``detail_level`` settings (the cost/resolution curve),
   plus the stdio cold-start to first-tool-call round-trip.

Everything runs against an *isolated* temp data store (``PITWALL_DATA_DIR``), so
it never touches the real ``%LOCALAPPDATA%/pitwall``. Point it at any ``.pwcap``::

    python bench/run.py                       # auto: real capture if present, else fixture
    python bench/run.py path/to/session.pwcap

Token counts use tiktoken's ``cl100k_base`` as a documented, reproducible proxy:
Claude's tokenizer differs slightly, so treat tokens as an estimate and the byte
counts as ground truth. ``tiktoken`` and ``psutil`` install via ``pip install
-e ".[bench]"``; both degrade gracefully if absent.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Isolate the data store *before* importing anything that resolves paths.
_BENCH_DIR = Path(tempfile.mkdtemp(prefix="pitwall_bench_"))
os.environ["PITWALL_DATA_DIR"] = str(_BENCH_DIR)

from pitwall.games.acc.recording import PageKind, read_recording  # noqa: E402
from pitwall.ingest import pipeline  # noqa: E402
from pitwall.ingest.source import detect_source  # noqa: E402
from pitwall.storage import db, paths  # noqa: E402
from pitwall.tools import queries  # noqa: E402

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int | None:
        return len(_ENC.encode(text))
except Exception:  # pragma: no cover - tiktoken optional
    def count_tokens(text: str) -> int | None:
        return None

try:
    import psutil

    _PROC = psutil.Process()

    def rss_mb() -> float | None:
        return _PROC.memory_info().rss / 1e6
except Exception:  # pragma: no cover - psutil optional
    def rss_mb() -> float | None:
        return None


def find_capture(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    real = REPO_ROOT / "acc-spa.pwcap"
    if real.exists():
        return real
    return REPO_ROOT / "tests" / "fixtures" / "acc-nurburgring-slim.pwcap"


def time_calls(fn, *, iterations: int) -> dict:
    """Run ``fn`` ``iterations`` times (warm) and report median/p95 in ms."""
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(p95, 3),
        "min_ms": round(samples[0], 3),
        "iterations": iterations,
    }


def measure_payload(obj) -> dict:
    """Serialized-JSON size of a tool response: bytes (ground truth) + est. tokens."""
    text = json.dumps(obj, separators=(",", ":"), default=str)
    out = {"bytes": len(text.encode("utf-8"))}
    toks = count_tokens(text)
    if toks is not None:
        out["est_tokens"] = toks
    return out


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def benchmark_ingest(capture: Path) -> tuple[dict, int, sqlite3.Connection]:
    rec = read_recording(capture)
    n_physics = len(rec.of_kind(PageKind.PHYSICS))
    n_graphics = len(rec.of_kind(PageKind.GRAPHICS))
    span_s = (rec.records[-1].ts_micros - rec.records[0].ts_micros) / 1e6 if rec.records else 0.0

    conn = db.connect()
    db.apply_schema(conn)
    source = detect_source(recording_path=str(capture))

    gc.collect()
    rss_before = rss_mb()
    tracemalloc.start()
    cpu0 = time.process_time()
    wall0 = time.perf_counter()

    session_id = pipeline.run(source, conn)

    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    py_peak = tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()
    rss_after = rss_mb()

    result = {
        "capture_file": capture.name,
        "capture_size_mb": round(capture.stat().st_size / 1e6, 2),
        "recorded_span_s": round(span_s, 1),
        "native_physics_frames": n_physics,
        "graphics_frames": n_graphics,
        "ingest_wall_s": round(wall, 3),
        "native_frames_per_s": round(n_physics / wall, 0) if wall else None,
        "realtime_speedup_x": round(span_s / wall, 1) if wall and span_s else None,
        "cpu_s": round(cpu, 3),
        "cpu_utilisation_pct": round(100 * cpu / wall, 1) if wall else None,
        "python_peak_mb": round(py_peak, 1),
        "rss_before_mb": round(rss_before, 1) if rss_before else None,
        "rss_after_mb": round(rss_after, 1) if rss_after else None,
    }
    return result, session_id, conn


def benchmark_storage(session_id: int, conn) -> dict:
    laps = db.laps_for_session(conn, session_id)
    traces_bytes = dir_size_bytes(paths.traces_dir())
    db_bytes = paths.db_path().stat().st_size
    total_points = sum(lap.point_count for lap in laps)
    n = len(laps) or 1
    return {
        "laps": len(laps),
        "total_points": total_points,
        "avg_points_per_lap": round(total_points / n, 0),
        "parquet_total_kib": round(traces_bytes / 1024, 1),
        "parquet_per_lap_kib": round(traces_bytes / n / 1024, 1),
        "sqlite_total_kib": round(db_bytes / 1024, 1),
        "sqlite_per_lap_bytes": round(db_bytes / n, 0),
        "bytes_per_lap_total_kib": round((traces_bytes + db_bytes) / n / 1024, 1),
        "bytes_per_point": round(traces_bytes / (total_points or 1), 1),
    }


def benchmark_queries(session_id: int, conn) -> dict:
    laps = db.laps_for_session(conn, session_id, valid_only=True, top_n_fastest=2)
    fastest = laps[0].lap_id if laps else db.laps_for_session(conn, session_id)[0].lap_id
    second = laps[1].lap_id if len(laps) > 1 else fastest
    sess = db.get_session(conn, session_id)
    length = sess.track_length_m or 1.0
    corner = [round(0.05 * length, 1), round(0.15 * length, 1)]  # a ~T1 window

    cases = {
        "list_sessions": lambda: queries.list_sessions(conn, track="nurburgring"),
        "list_laps": lambda: queries.list_laps(conn, session_id=session_id),
        "list_laps_fastest": lambda: queries.list_laps(conn, session_id=session_id, top_n_fastest=3),
        "get_lap_summary": lambda: queries.get_lap_summary(conn, lap_id=fastest),
        "trace_summary": lambda: queries.get_telemetry_trace(conn, lap_id=fastest, detail_level="summary"),
        "trace_full": lambda: queries.get_telemetry_trace(conn, lap_id=fastest, detail_level="full"),
        "trace_corner_full": lambda: queries.get_telemetry_trace(
            conn, lap_id=fastest, channels=["brake", "speed_kmh"], detail_level="full", distance_range=corner
        ),
        "compare_laps": lambda: queries.compare_laps(conn, lap_a_id=fastest, lap_b_id=second),
        "get_sector_analysis": lambda: queries.get_sector_analysis(conn, lap_id=second),
        "get_tire_history": lambda: queries.get_tire_history(conn, session_id=session_id),
        "export_lap_csv": lambda: queries.export_lap_csv(conn, lap_id=fastest),
    }
    return {name: time_calls(fn, iterations=200 if "trace" not in name else 100) for name, fn in cases.items()}


def benchmark_tokens(session_id: int, conn) -> dict:
    laps = db.laps_for_session(conn, session_id, valid_only=True, top_n_fastest=2)
    fastest = laps[0].lap_id if laps else db.laps_for_session(conn, session_id)[0].lap_id
    second = laps[1].lap_id if len(laps) > 1 else fastest
    sess = db.get_session(conn, session_id)
    length = sess.track_length_m or 1.0
    corner = [round(0.05 * length, 1), round(0.15 * length, 1)]

    trace_levels = {
        level: measure_payload(queries.get_telemetry_trace(conn, lap_id=fastest, detail_level=level))
        for level in ("summary", "low", "medium", "full")
    }
    trace_levels["full_corner_2ch"] = measure_payload(
        queries.get_telemetry_trace(
            conn, lap_id=fastest, channels=["brake", "speed_kmh"], detail_level="full", distance_range=corner
        )
    )
    others = {
        "list_sessions": measure_payload(queries.list_sessions(conn, track="nurburgring")),
        "list_laps": measure_payload(queries.list_laps(conn, session_id=session_id)),
        "get_lap_summary": measure_payload(queries.get_lap_summary(conn, lap_id=fastest)),
        "compare_laps": measure_payload(queries.compare_laps(conn, lap_a_id=fastest, lap_b_id=second)),
        "get_sector_analysis": measure_payload(queries.get_sector_analysis(conn, lap_id=second)),
        "get_tire_history": measure_payload(queries.get_tire_history(conn, session_id=session_id)),
    }
    return {"get_telemetry_trace_by_detail_level": trace_levels, "other_tools": others}


def benchmark_stdio_roundtrip() -> dict:
    """Cold-start a real MCP server subprocess over stdio and time the first tool call.

    Measures process spawn + MCP initialize handshake + list_tools + one tool call
    returning -- everything pitwall controls. Excludes Claude's network + model
    latency (not pitwall's to measure). Maps to the plan's '<3 s cold start' goal.
    """
    import asyncio

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as exc:  # pragma: no cover
        return {"error": f"mcp client unavailable: {exc}"}

    async def _run() -> dict:
        env = dict(os.environ)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from pitwall.server import main; main()"],
            env=env,
        )
        t_spawn = time.perf_counter()
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                t_init = time.perf_counter()
                tools = await session.list_tools()
                t_list = time.perf_counter()
                await session.call_tool("list_sessions", {"track": "nurburgring"})
                t_call = time.perf_counter()
        return {
            "spawn_to_initialize_s": round(t_init - t_spawn, 3),
            "initialize_to_list_tools_s": round(t_list - t_init, 3),
            "list_tools_to_first_call_s": round(t_call - t_list, 3),
            "cold_start_to_first_tool_call_s": round(t_call - t_spawn, 3),
            "tools_registered": len(tools.tools),
        }

    env_src = dict(os.environ)
    env_src["PYTHONPATH"] = str(SRC) + os.pathsep + env_src.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = env_src["PYTHONPATH"]
    try:
        return asyncio.run(_run())
    except Exception as exc:  # pragma: no cover
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    capture = find_capture(sys.argv)
    if not capture.exists():
        raise SystemExit(f"capture not found: {capture}")

    print(f"# pitwall benchmarks\ncapture: {capture}\ndata_dir: {_BENCH_DIR}\n")

    ingest, session_id, conn = benchmark_ingest(capture)
    storage = benchmark_storage(session_id, conn)
    query = benchmark_queries(session_id, conn)
    tokens = benchmark_tokens(session_id, conn)
    conn.close()
    stdio = benchmark_stdio_roundtrip()

    results = {
        "python": sys.version.split()[0],
        "tiktoken": count_tokens("x") is not None,
        "psutil": rss_mb() is not None,
        "ingest": ingest,
        "storage": storage,
        "query_latency": query,
        "token_budget": tokens,
        "stdio_roundtrip": stdio,
    }
    print(json.dumps(results, indent=2))

    out = REPO_ROOT / "bench" / "results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    shutil.rmtree(_BENCH_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
