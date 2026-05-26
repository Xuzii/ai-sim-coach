# pitwall — Project Status

> **Single source of truth for where this project stands.** Other docs are
> point-in-time or detail references; this file is kept current. Last updated &
> re-verified **2026-05-25**.

## TL;DR

**The pipeline works end-to-end and every component is built and tested**, including
against a real 286 MB ACC session and a one-time live drive-test. The **ingest gap is
now closed**: the `pitwall-ingest` command populates the store the server reads — live
as you drive (`--watch` keeps it running across sessions) or from a `.pwcap`
(see [Ingest command](#ingest-command-shipped) below). The remaining work is purely
**account-gated shipping** (GitHub push, PyPI upload, demo GIF, one community post) plus
the final **Claude Desktop round-trip**. After Phase 1 ships, Phase 2 (the single-agent
coach) begins.

## Phase 1 at a glance

| Milestone | Scope | State |
|---|---|---|
| **M1 — Foundation** | Canonical schema, SQLite+Parquet storage, synthetic-source ingest pipeline, FastMCP server + all 8 tools | ✅ Complete & verified |
| **M2 — ACC reader** | Shared-memory capture, struct parsing, canonical mapping, offline replay + live reader, scrub/inspect tools | ✅ Complete & **live-verified** |
| **Finish-line** | Benchmarks + harness, architecture diagram, README, git init + tag `v0.1.0`, build + `twine check` | ✅ Complete |
| **Ship (account-gated)** | GitHub release, PyPI, demo GIF, Claude Desktop round-trip, community post | ⏭️ **You run these** (see below) |

## Verified working (re-confirmed 2026-05-24)

These were run against the current tree, not just claimed:

- **`ruff check .`** → all checks passed.
- **`pytest`** → **100 passed**. The ACC reader is regression-tested in CI *without the
  game* by replaying a scrubbed fixture (`tests/fixtures/acc-nurburgring-slim.pwcap`,
  1.56 MiB) through the real pipeline; `pitwall-ingest`'s replay/live/`--watch` paths are
  covered offline too.
- **`python -m build`** → wheel + sdist in `dist/` (`pitwall_mcp-0.1.0`); `twine check`
  passes. Wheel ships only `src/pitwall` + the 5 console scripts (`pitwall`,
  `pitwall-capture`, `pitwall-ingest`, `pitwall-inspect`, `pitwall-scrub`).
- **End-to-end on the real 286 MB capture** (`acc-spa.pwcap`, Nürburgring GP / Ford
  Mustang GT3): `bench/verify_success_criteria.py` ingests it and answers the 5
  plan queries — fastest valid lap **2:04.126**, first-braking-zone brake/speed
  summary, tyre history, CSV export. The one comparison query degrades gracefully
  (this short capture has only one clean lap).
- **Performance** (full numbers in [`BENCHMARKS.md`](BENCHMARKS.md)): ingest **3.06 s /
  117× realtime**, cold-start → first tool call **1.06 s** (plan target < 3 s),
  metadata tools in µs / trace tools 4–8 ms, summary trace **~750 tokens vs ~30k** full.
- **Live ACC drive-test** (one-time, 360 s, 2 laps + deliberate off-track): confirmed
  multi-lap landing, off-track → `is_valid=False`, and the `accG` axis/sign.

## Ingest command (shipped)

**The store the server reads is now populated by `pitwall-ingest`.** It's a thin wrapper
over the tested ingest spine (`pipeline.run(detect_source(...), conn)` — live ACC *or* a
`.pwcap`), so driving → store is finally wired. Five console scripts now ship:

| Script | What it does | Populates the store? |
|---|---|---|
| `pitwall` | The MCP server Claude Desktop launches | ❌ reads only |
| `pitwall-capture` | Records raw shared-memory bytes → `.pwcap` | ❌ writes a `.pwcap`, not the store |
| **`pitwall-ingest`** | **Ingests live ACC or a `.pwcap` into the store** | ✅ **writes the store** |
| `pitwall-inspect` | Prints a decode of a `.pwcap` | ❌ |
| `pitwall-scrub` | Strips player name from a `.pwcap` | ❌ |

Three modes:
- `pitwall-ingest` (live) — capture ACC into the store as you drive, ending at session end.
- `pitwall-ingest path/to/session.pwcap` — ingest a previously captured recording.
- `pitwall-ingest --watch` — daemon: after a session ends, wait for the next and
  auto-ingest it. Start it once and just drive. It's hardened to survive a bad session
  (logs the error and keeps watching) and logs to `%LOCALAPPDATA%\pitwall\ingest.log`
  (override with `--log`, disable with `--no-log`), so it's safe to run unattended (e.g.
  a logon scheduled task that stays dormant until ACC is live).

It prints each lap as it lands and a summary line at the end. The store now runs in **WAL**
mode so the MCP server can read it while `pitwall-ingest` writes (read-while-driving)
without "database is locked"; expect `pitwall.db-wal`/`-shm` sidecars next to `pitwall.db`.

## What's left to ship Phase 1 — you run these

All remaining steps need an account or a running game, so they're yours to run.

0. ✅ **Planning docs decided (2026-05-25).** `ai-race-engineer-roadmap.md` and
   `phase1-mcp-telemetry-plan.md` (career strategy / target companies) are kept
   **local** — moved into `planning/`, which is gitignored as a whole. They will not
   appear in the public repo.
1. **Push to GitHub + cut the v0.1.0 release.** Remote is
   `https://github.com/Xuzii/ai-sim-coach`.
   ```bash
   git remote add origin https://github.com/Xuzii/ai-sim-coach.git
   git push -u origin main --tags
   ```
   Then create the GitHub release from tag `v0.1.0`.
2. **PyPI.** TestPyPI dry-run first, then real:
   ```bash
   twine upload -r testpypi dist/*     # verify the listing renders
   twine upload dist/*                 # real PyPI (needs creds)
   ```
3. **Demo GIF.** Record Claude Desktop answering a telemetry question (ScreenToGif /
   kap), drop it into the README placeholder.
4. **Claude Desktop round-trip + community.** Install pitwall, wire it into
   `claude_desktop_config.json`, **populate the store** with `pitwall-ingest`
   (`pitwall-ingest --watch` and drive, or `pitwall-ingest a-session.pwcap`), and ask the
   5 success-criteria questions for real — this is the last unproven hop (the data + query
   layer are already proven). Full walkthrough: [`docs/verifying-phase-1.md`](docs/verifying-phase-1.md).
   Then post once to r/simracing / a sim-racing Discord.

When 1–4 are done, **Phase 1 is shipped.**

## Then: Phase 2

The single-agent coach — ingest a lap and return structured + human-readable feedback.
See the roadmap (planning doc) for the full 6-phase arc.

## Where things live

- **This file** — current status, the one to keep updated.
- [`docs/verifying-phase-1.md`](docs/verifying-phase-1.md) — how to confirm the whole
  pipeline works (the runbook behind the checks above).
- [`docs/milestone-1-summary.md`](docs/milestone-1-summary.md) — detailed technical
  write-up of M1 + the M2 ACC reader (§7).
- [`BENCHMARKS.md`](BENCHMARKS.md) — performance + the token/quality tradeoff curve.
- [`docs/architecture.md`](docs/architecture.md) — full architecture diagram.
- [`docs/channel-mapping.md`](docs/channel-mapping.md) — the canonical-schema mapping.
- [`notes/`](notes/) — design-decision write-ups (why Parquet, why distance bins, …).
- `CLAUDE.md` — guidance for Claude Code; locked architecture decisions.
