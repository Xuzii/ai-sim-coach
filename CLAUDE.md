# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Phase 1 SHIPPED (2026-05-25). Phase 2 IN PROGRESS — chunks P2-C0 + P2-C1 + P2-C2 DONE (C2 2026-06-04).**
Phase 2 is the **single-agent coach**; the full chunked, multi-session plan lives at
`~/.claude/plans/i-want-you-to-virtual-papert.md`. Locked: engine = a hand-rolled agentic
tool-use loop on **Gemini** (free tier) behind a provider-agnostic `LLMClient` seam (a Claude
backend drops in later); a new **`pitwall-coach`** CLI (the analysis is *not* an MCP tool); a
structured `CoachingReport` persisted to SQLite, which is the **Phase 3 hand-off contract**.
**P2-C0 (contract + skeleton) shipped:** new `src/pitwall/coach/` package (report, consistency,
store, llm/base + MockLLMClient, tools registry) + a single shared tool source
`src/pitwall/tools/specs.py` that **both** the MCP server and the coach build from (server now
registers its 8 tools programmatically from it — byte-identical schemas, no drift); a new
`coaching_reports` SQLite table (schema bumped to v2). No LLM cost, fully mock-tested.
**P2-C1 (Gemini client + agent loop) shipped:** `coach/agent.py` (`analyze_lap` — the tool-use
loop that drives the `LLMClient` through the query tools and terminates on the model's
`submit_coaching_report` call, then validates + persists the report), `coach/llm/gemini.py`
(`GeminiClient` — lazy-imported `google-genai`, raw-JSON-schema tool path + a key sanitiser),
`coach/config.py` (`CoachConfig` + a zero-dep `.env` loader / `resolve_api_key`), and a tracked
`.env.example`. Tests mock the LLM (live Gemini test is env-gated, skipped in CI).
**P2-C2 (`pitwall-coach` CLI) shipped:** `coach/cli.py` — the new **`pitwall-coach`** console
script with `analyze <lap_id> [--reference ID|--pb] [--model M] [--json]` (the only LLM-calling
command; runs `analyze_lap`, persists, prints a human block or `--json`), `show <report_id>`, and
`list [--session ID|--lap ID]`; a `format_report()` pretty-printer; an injectable `llm_factory`
(default `GeminiClient.from_config`) so the CLI is fully mock-tested offline; graceful exit codes
for every failure (unknown lap/report, missing key, `coach` extra absent, agent stall). Added
`db.personal_best_lap` (for `--pb`) and the 6th entry point in `pyproject.toml`.
**Next action: the Milestone B live check-in** (one real `GEMINI_API_KEY` run on an ingested lap —
review report quality + token/latency, tune the prompt), then build **chunk P2-C3** (eval framework).
**iRacing `.ibt` ingest added (2026-06-04):** `src/pitwall/games/iracing/` — a near-verbatim port of
the sibling racing-telemetry-visualiser's `.ibt` decode layer (`ibt_types`/`ibt`/`normalize`) plus a
pitwall canonical adapter (`mapping`/`reader.IRacingSource`); `pitwall-ingest file.ibt` routes by
extension; behind a new optional `iracing` extra (`pyirsdk`+`pyyaml`, lazy-imported). The canonical
pipeline is now genuinely cross-game (the coach/MCP layers work over iRacing laps unchanged). Verified
on a real 90 MB MX-5 @ Rudskogen capture → 14 laps. See STATUS.md → *iRacing `.ibt` ingest* and
`docs/channel-mapping.md`. Package name is **`pitwall`** (PyPI dist `pitwall-mcp`, import `pitwall`,
console script `pitwall`).

### Current state — READ THIS FIRST

**Canonical, always-current status lives in [`STATUS.md`](STATUS.md).** Read it before
acting on anything in this section. Summary as of **2026-06-04** (re-verified: ruff
clean, **172 passed / 1 skipped**, wheel+sdist build, `twine check` passes, end-to-end ingest of
the real 286 MB capture works):

- ✅ **M1 (Foundation)** and ✅ **M2 (ACC reader)** are complete and verified; M2's
  live drive-test passed. Detailed write-up: `docs/milestone-1-summary.md` (§7 = ACC
  reader). Benchmarks: `BENCHMARKS.md`. Both real-data findings (sector-telescoping +
  `trackSPlineLength==0`) are FIXED.
- ✅ **Phase 1 shipped:** code is public on GitHub (`main` + tag `v0.1.0` at
  https://github.com/Xuzii/ai-sim-coach), the Claude Desktop round-trip was done and
  recorded (demo at `docs/demo.gif`, embedded in the README). **Deferred / optional**
  (not blocking Phase 2): PyPI upload (shelved; `pitwall-mcp` name reserved), cutting the
  GitHub *Release* object from the tag, and one community post — drafts live in
  gitignored `planning/launch-materials.md`. See `STATUS.md` for the ship log.
- ✅ **Ingest gap closed (2026-05-25):** `pitwall-ingest` now wires driving → store
  (`src/pitwall/ingest/cli.py`, a thin wrapper over `pipeline.run(detect_source(...), conn)`).
  Three modes: `pitwall-ingest` (live, ends with the session), `pitwall-ingest file.pwcap`
  (replay), `pitwall-ingest --watch` (daemon — auto-ingest each session back-to-back). The
  5 console scripts are now `pitwall` (server, reads only), `pitwall-capture` (raw bytes →
  `.pwcap`), **`pitwall-ingest`** (writes the store), `pitwall-inspect`, `pitwall-scrub`.

**Coding gotchas (live-verified, don't re-derive):** **ACC** `g_lat = −accG[0]` (positive =
right) — but **iRacing is the OPPOSITE: `g_lat = LatAccel ÷ 9.80665` with NO negation**
(iRacing's `LatAccel` is already positive-right; verified on a real left-hander). ACC reports
`trackSPlineLength == 0` so distances come from `TRACK_LENGTHS` in `naming.py` (nurburgring GP
= 5148 m) — iRacing instead supplies track length / sector layout / display names directly in
the `.ibt` session-info YAML (no lookup tables). Other iRacing quirks: `Clutch` is inverted
(`1 − Clutch`); there's no per-frame sector channel (`sector_index` is computed from
`LapDistPct` vs `SplitTimeInfo.Sectors[].SectorStartPct`); `world_x/y/z` are None (GPS-only).
**Two concurrent `pitwall-capture` processes corrupt the recording — run exactly one**;
lap/sector times are wall-clock, not ACC's unreliable `lastSectorTime`/`iLastTime`; the store
runs in **WAL** (`db.connect` enables it for on-disk DBs only) so the server reads while
`pitwall-ingest` writes — in-memory test DBs skip WAL.

Cadence is **autonomous batches with milestone check-ins** (Kenneth's choice).

Planning documents:

- `planning/ai-race-engineer-roadmap.md` — the full 6-phase project + career-pivot roadmap (16–18 weeks). Big-picture "why" and sequencing. (Local-only; `planning/` is gitignored.)
- `planning/phase1-mcp-telemetry-plan.md` — the original Phase 1 spec (day-by-day). (Local-only.)
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
