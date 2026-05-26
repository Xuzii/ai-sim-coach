# pitwall — Project Status

> **Single source of truth for where this project stands.** Other docs are
> point-in-time or detail references; this file is kept current. Last updated &
> re-verified **2026-05-25**.

## TL;DR

**Phase 1 is shipped.** The pipeline works end-to-end, every component is built and
tested (ruff clean, 100 tests, build + `twine check` pass), it's verified against a real
286 MB ACC session and a live drive-test, and the **`pitwall-ingest` command** populates
the store the server reads — live as you drive (`--watch` across sessions) or from a
`.pwcap` (see [Ingest command](#ingest-command-shipped)). The code is **public on GitHub**
(`main` + tag `v0.1.0` at https://github.com/Xuzii/ai-sim-coach), the **Claude Desktop
round-trip is done and recorded** (demo embedded in the README), and the store is verified
to answer the five success-criteria questions. **Deferred (not blocking Phase 2):** PyPI
upload (shelved — name `pitwall-mcp` is reserved/available), cutting the GitHub *Release*
object from the tag, and the community post (draft ready in local
`planning/launch-materials.md`). **Next up: Phase 2 — the single-agent coach.**

## Phase 1 at a glance

| Milestone | Scope | State |
|---|---|---|
| **M1 — Foundation** | Canonical schema, SQLite+Parquet storage, synthetic-source ingest pipeline, FastMCP server + all 8 tools | ✅ Complete & verified |
| **M2 — ACC reader** | Shared-memory capture, struct parsing, canonical mapping, offline replay + live reader, scrub/inspect tools | ✅ Complete & **live-verified** |
| **Finish-line** | Benchmarks + harness, architecture diagram, README, git init + tag `v0.1.0`, build + `twine check` | ✅ Complete |
| **Ship** | GitHub push (`main` + `v0.1.0`), demo GIF recorded + embedded, Claude Desktop round-trip | ✅ **Done (2026-05-25)** |
| **Deferred** | PyPI upload (shelved), GitHub *Release* object, community post | ⏸️ Optional, not blocking Phase 2 |

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

## Ship log — what's done and what's deferred

Phase 1 is shipped. The done items:

0. ✅ **Planning docs decided (2026-05-25).** `ai-race-engineer-roadmap.md` and
   `phase1-mcp-telemetry-plan.md` (career strategy / target companies) are kept
   **local** — moved into `planning/`, gitignored as a whole, so they never reach the
   public repo.
1. ✅ **Pushed to GitHub (2026-05-25).** `main` + tag `v0.1.0` are live at
   `https://github.com/Xuzii/ai-sim-coach`. Pre-push audit was clean (no secrets,
   captures, or planning docs).
2. ✅ **Claude Desktop round-trip done + recorded (2026-05-25).** The five
   success-criteria questions were asked in Claude Desktop against the real store
   (1 session, Nürburgring GP / Ford Mustang GT3, fastest valid **2:04.126**). The
   recording was optimized to `docs/demo.gif` (~7.9 MB) and embedded at the top of the
   README. The raw `.mp4` is gitignored and kept local.

The deferred items (optional, **not** blocking Phase 2 — pick up anytime):

- ⏸️ **PyPI** (shelved). Name `pitwall-mcp` is available on PyPI + TestPyPI. When ready,
  TestPyPI dry-run first, then real:
  ```bash
  twine upload -r testpypi dist/*     # verify the listing renders
  twine upload dist/*                 # real PyPI (needs creds)
  ```
- ⏸️ **GitHub *Release* object.** The tag `v0.1.0` is pushed but no Release was cut.
  Notes are drafted in local `planning/launch-materials.md` §1; it's also the natural
  home for the full demo `.mp4`.
- ⏸️ **Community post.** Draft ready in `planning/launch-materials.md` §2 (r/simracing /
  a sim-racing Discord).

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
