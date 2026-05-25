# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Phase 1 in progress.** v0.1 scope is **ACC-only**; package name is **`pitwall`** (PyPI dist `pitwall-mcp`, import `pitwall`, console script `pitwall`).

### Current state — READ THIS FIRST (last updated 2026-05-24)

- ✅ **Milestone 1 (Foundation) COMPLETE & verified.** Chunks C0–C4 done: canonical schema, SQLite+Parquet storage, the synthetic-source ingest pipeline (downsample → segment → persist), and the FastMCP server with all 8 tools. ruff clean, wheel builds. The whole pipeline works against synthetic data with no game running. Full write-up: **`docs/milestone-1-summary.md`**.
- ✅ **Milestone 2 — ACC reader (chunk C3): COMPLETE & live-verified** (ruff clean, **80/80 tests pass**, wheel builds with only `src/pitwall` + all 4 console scripts). Capture tool (`shm.py`/`recording.py`/`capture.py`) plus the reader: **`structs.py`** (three ctypes pages, `_pack_=4`, sizeof+offset asserts, UTF-16-LE byte-array strings for cross-platform layout), **`mapping.py`** (pure struct→canonical: gear−1, fuel×0.745, `carCoordinates[playerCarID]`, wheel order, `g_lat = −accG[0]`), **`reader.py`** (`ACCSource` over a `FrameStream`: `RecordingFrameStream` offline replay + `LiveFrameStream` that terminates when the session ends), **`detect_source()`** in `ingest/source.py` (lazy ACC imports; iRacing v0.2 stub), plus **`scrub.py`/`inspect.py`** with `pitwall-scrub`/`pitwall-inspect`. A live 360 s drive-test (2 full laps + deliberate off-track) confirmed multi-lap landing, off-track→`is_valid=False`, and the **accG axis/sign**. CI fixture: **`tests/fixtures/acc-nurburgring-slim.pwcap`** (1.56 MiB, scrubbed, a valid lap + the off-track invalid lap). Write-up: `docs/milestone-1-summary.md` §7.
- ⏭️ **NEXT — Phase 1 finish-line (per roadmap), suggested order:**
  1. **Fix two real-data findings** before they bite: (a) sector times don't sum to lap time on real ACC data (`_sector_breakdown` vs ACC's `currentSectorIndex`/lap-counter timing); (b) `trackSPlineLength` reports 0, so add a `TRACK_LENGTHS` lookup (by `naming.py` code) so distances are metres not 0..1.
  2. **`BENCHMARKS.md`** — ingest throughput, storage/lap, MCP round-trip + token/detail-level curve.
  3. **Ship polish** — README install + Claude Desktop snippet, demo GIF, architecture diagram, `git init`, TestPyPI dry-run → PyPI, tag v0.1.0; run the 5 success-criteria queries through Claude Desktop on this real data.
  4. **Then Phase 2** — the single-agent coach (ingest a lap → structured + human-readable feedback).
- Cadence is **autonomous batches with milestone check-ins** (Kenneth's choice).

Planning documents:

- `ai-race-engineer-roadmap.md` — the full 6-phase project + career-pivot roadmap (16–18 weeks). Big-picture "why" and sequencing.
- `phase1-mcp-telemetry-plan.md` — the original Phase 1 spec (day-by-day).
- `~/.claude/plans/i-need-you-fluffy-clock.md` — the chunked execution plan (C0–C8, 3 milestones).

The end product is an AI race engineer: a Claude-powered, multi-agent coaching system for sim racing (ACC, iRacing) and HPDE track driving. Phase 1 builds the foundation: an MCP server that ingests live telemetry and exposes it to Claude as tools.

When scaffolding the codebase, follow the locked-in decisions below rather than re-deriving them — they cascade through the rest of the build, and the Phase 1 plan treats them as settled.

## Phase sequencing (from the roadmap)

1. **MCP telemetry server** (current) — publish to PyPI, works with Claude Desktop out of the box.
2. **Single-agent coach** — ingests a lap, returns structured + human-readable feedback.
3. **Multi-agent architecture** — specialist agents (Braking / Throttle / Racing Line / Tire) + a Synthesis Coach + orchestrator. This is the portfolio centerpiece.
4. **Vision pipeline** — onboard video analysis (Gemini for scene understanding, OpenCV/YOLOv8 for frame precision), fused with telemetry. Optional.
5. **Evals framework** — lap-time-delta + LLM-as-judge + consistency, calibrated against each other.
6. **Public beta + community launch.**

## Planned stack & tooling (Phase 1)

- **Python**, MCP Python SDK using the **`FastMCP`** high-level API, **stdio transport** (Claude Desktop). HTTP/SSE only matters if hosted later — not now.
- **Storage:** SQLite for metadata (sessions, laps, sector times, car/track, weather) + **Parquet via `pyarrow`** for per-lap traces (one file per lap, columnar).
- **ACC:** Shared Memory API — three memory-mapped pages (`Local\acpmf_physics` ~333Hz, `Local\acpmf_graphics` ~30Hz, `Local\acpmf_static`) parsed with Python's `struct` module. No extra software needed.
- **iRacing:** the `irsdk` package (kutu, MIT). Also parse `.ibt` post-session exports for backfill.
- **Packaging:** `pyproject.toml` with **hatchling** backend (no `setup.py`), `python -m build` + `twine upload`, TestPyPI dry-run before real PyPI, semver from `0.1.0`.

### Build / lint / test commands

Dev environment lives in `.venv` (Python 3.14 locally; package supports 3.10+). All commands run from the repo root on Windows PowerShell:

- **Install (editable, with dev deps):** `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`
- **Lint:** `.\.venv\Scripts\ruff.exe check .` (config in `ruff.toml`; line length 120, rules E/W/F/I/UP/B)
- **Test:** `.\.venv\Scripts\python.exe -m pytest` (config in `pyproject.toml [tool.pytest.ini_options]`, `testpaths = ["tests"]`)
- **Build wheel/sdist:** `.\.venv\Scripts\python.exe -m build`

Tests isolate the data store via the `data_dir` fixture (sets `PITWALL_DATA_DIR` to a tmp dir) — they never touch the real `%LOCALAPPDATA%/pitwall`.

## Locked architecture decisions — respect these

1. **Canonical channel schema is the translation layer.** ACC says `gas`, iRacing says `Throttle` — map both to ONE canonical name. Define this dict in `channels.py` before writing readers. Canonical channels: `throttle, brake, clutch, steering_angle, gear, rpm, speed_kmh, lap_distance_m, world_x/y/z, tire_temp_{fl,fr,rl,rr}, tire_pressure_{fl,fr,rl,rr}, g_lat, g_lon, fuel_kg`. Document the per-game mapping in `docs/channel-mapping.md`.

2. **Sampling rate:** ACC physics 333Hz → **downsample to 50Hz on ingest**; iRacing 60Hz → keep as-is. 50Hz (20ms) is below human-meaningful change; storing 333Hz is wasted data.

3. **Storage split is deliberate:** SQLite metadata vs Parquet traces. **Never store traces as SQLite blobs** — the core access pattern is column slicing by distance range ("brake channel between 200m and 400m"), which row-based blobs handle terribly.

4. **MCP tools are narrow, not god-tools.** 8 planned tools — `list_sessions`, `list_laps`, `get_lap_summary`, `get_telemetry_trace`, `compare_laps`, `get_sector_analysis`, `get_tire_history`, `export_lap_csv`. **Tool descriptions are the prompt engineering** — write each like a docstring for Claude stating when to use it and what it returns; this is ~60% of how well Claude uses the server.

5. **Token-budget discipline for tool responses.** Raw 50Hz traces blow the context window. Default to **summary stats** (min/max/mean/std per channel per sector). Expose a `detail_level` param (`summary` | `low`=5Hz | `medium`=10Hz | `full`=50Hz). Bin traces by **distance, not time** — that's what a coach actually wants.

## Cross-cutting constraints

- **Store all timestamps in UTC;** convert at query time.
- **Track/car name normalization across games** — ACC uses internal codes (`spa`, `nurburgring`), iRacing uses human names. Build a lookup table.
- **Handle "game not running" gracefully** — ACC shared memory only exists while ACC runs on the same machine. Clear error, never a crash; users install the server before launching the game.
- **Surface lap validity flags** (off-track invalidation, present in both games) in `get_lap_summary`.
- **License check** before publishing — verify ACC shared-memory redistribution terms (Kunos-documented); `irsdk` is MIT and safe to depend on.

## Portfolio artifacts to maintain as you build

This project doubles as a job-application portfolio, so capture receipts *while building*, not after:

- `docs/channel-mapping.md` — the canonical-schema mapping table.
- `BENCHMARKS.md` — ingestion/storage/MCP-round-trip performance and the token/quality tradeoff curve (few MCP servers publish this).
- `notes/` — blog-draft seeds capturing decisions and tradeoffs as they're made (why Parquet over SQLite blobs, why distance-binning, why narrow tools, the token-budget curve).
- Tagged GitHub releases per phase, demo GIF in README, single-page architecture diagram.

## Scope guardrail

Phase 1 is about shipping a polished, working MCP server — not feature completeness. Two well-described tools with great install UX beat eight half-broken ones. **Cut scope before you cut polish.** If running behind, ship ACC-only + `.ibt` parsing and add iRacing live in v0.2.
