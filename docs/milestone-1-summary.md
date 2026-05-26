# Milestone 1 — Foundation: what was built and how

**Status:** complete and verified. ruff clean · **100/100 tests pass** (this doc now covers M1 *and* the M2 ACC reader in §7) · wheel builds (`pitwall_mcp-0.1.0`) with `schema.sql` bundled · all 5 console scripts installed (incl. `pitwall-ingest`) · all 8 MCP tools register. (The 43-test figure below is M1's original count and the 83 in §7 is the M2-reader snapshot; both predate the ingest command and its tests.)

**Scope of M1:** the entire ingest → storage → MCP pipeline, testable end-to-end against a *synthetic* telemetry generator — i.e. everything that does **not** require ACC to be running. The live ACC reader is Milestone 2.

---

## 1. Package layout (what each file does)

```
src/pitwall/
  __init__.py            __version__ = "0.1.0"
  channels.py            CANONICAL SCHEMA — the cross-game translation layer
  naming.py              ACC track/car internal codes -> display names
  timeutil.py            store-UTC / convert-at-query helpers
  server.py              FastMCP("pitwall") + 8 @mcp.tool() wrappers + main()
  storage/
    paths.py             data dir resolution (%LOCALAPPDATA%/pitwall, PITWALL_DATA_DIR override)
    schema.sql           SQLite DDL: sessions, laps, sectors, schema_meta
    db.py                connection, apply_schema, dataclasses, insert/query helpers
    traces.py            Parquet read/write (zstd), column projection + distance slicing
  ingest/
    frames.py            CanonicalFrame, StaticInfo (the unit + session context)
    source.py            TelemetrySource ABC (the reader contract)
    synthetic.py         SyntheticACCSource — deterministic fake session
    pipeline.py          downsample -> segment -> persist (the spine)
  tools/
    queries.py           the 8 query functions (pure, SDK-free)
    formatting.py        summary stats, distance binning, token budgeting
tests/                   conftest.py + test_channels/storage/ingest/tools (43 tests)
docs/  notes/  pyproject.toml  ruff.toml  LICENSE  README.md  .gitignore
```

---

## 2. The canonical schema (`channels.py`) — written first, on purpose

Everything maps into one ordered list of 22 channels (`CANONICAL_CHANNELS`), each a `Channel(name, dtype, unit, description)`. ACC's `gas` becomes `throttle`, iRacing's `Throttle` will too. Key exports the rest of the system uses:

- `CHANNEL_NAMES`, `CHANNEL_BY_NAME` — fast lookups.
- `DISTANCE_KEY = "lap_distance_m"` (the bin key), `TIME_KEY = "t_ms"` (pipeline-added time index).
- `trace_columns()` — the exact Parquet column order (distance, time, then channels).
- `dtype_for(name)` — pyarrow/numpy dtype per column, so storage builds schemas from one source of truth.
- `WHEEL_ORDER`, `TIRE_TEMP_CHANNELS`, `TIRE_PRESSURE_CHANNELS`.

A module-level `assert` fails the import if the schema is ever made inconsistent (duplicate name, missing distance key).

---

## 3. Storage (`storage/`) — SQLite metadata + Parquet traces

**Why split:** the core access pattern is "give me the brake channel between 200 m and 400 m on lap 7". That is column-slicing by distance — terrible for row-based SQLite blobs, ideal for columnar Parquet. So metadata lives in SQLite; each lap's time-series lives in its own Parquet file.

**`schema.sql` / `db.py`:**
- Tables: `sessions` (track/car/codes, start/end UTC, sector_count, length, weather), `laps` (times, validity, out/in-lap, stint, top/avg speed, fuel, `trace_path`, `point_count`), `sectors` (per-sector time **plus `start_distance_m`/`end_distance_m`** so sectors can be sliced out of the trace), `schema_meta` (version stamp).
- `connect()` sets `row_factory` + `PRAGMA foreign_keys=ON`. `apply_schema()` is idempotent (safe to call every connection).
- Read models are dataclasses (`Session`, `Lap`, `Sector`) with `from_row()`.
- Query helpers: `find_sessions` (track/car substring + `since`), `laps_for_session` (with `valid_only`, `top_n_fastest`), `fastest_valid_lap`, `sectors_for_lap`, `personal_best_ms` (best valid lap for a car+track across all sessions, for consistency deltas).

**`traces.py`:**
- `build_table(data)` — builds a canonical-schema Arrow table; a missing channel is null-filled so **every lap file shares one schema**.
- `write_lap_trace(path, data)` — zstd level 3, returns row count.
- `read_lap_trace(path, columns, distance_range)` — projects only requested columns (always includes the distance key) and filters an inclusive distance window via pyarrow compute. This projection/slice is the whole reason for Parquet.
- `lap_trace_relpath()` / `resolve_trace_path()` — traces are stored relative to the data dir for portability.

---

## 4. Ingest (`ingest/`) — the spine

**Contract (`source.py`):** any reader implements `TelemetrySource` with `static_info()` and `frames()`. The synthetic source and the future ACC reader both satisfy it, so the pipeline never branches on game.

**`frames.py`:** a `CanonicalFrame` carries `t` (seconds), `values` (canonical channel → value, incl. `lap_distance_m`), and the segmentation metadata: `lap_count` (completed laps so far — the line-crossing signal), `sector_index`, `lap_valid`, `in_pit`, `tyre_set`. `StaticInfo` is the once-per-session context.

**`synthetic.py` — `SyntheticACCSource`:** a seeded, parametric fake session that flows through the *same* pipeline as real data. It models a closed-loop track (default Spa-ish 7004 m, 9 corners) with believable throttle/brake/speed/steer shapes, 3 sectors, tyre warm-up across a stint, and a deliberately varied lap mix:

| Lap | character | why it exists |
|---|---|---|
| 1 | out-lap (slow, leaves pit) | tests `is_outlap` |
| 2 | normal | filler |
| 3 | **fastest** | the "fastest lap" demo answer |
| 4 | 2nd fastest (tiny delta) | `compare_laps` target |
| 5 | **invalid** (off-track) | tests validity flag |
| 6 | fresh tyres → **stint 2** | tests stint detection / tyre history |
| 7 | normal (stint 2) | filler |
| 8 | in-lap (slow, enters pit) | tests `is_inlap` |

Because it's seeded, tests assert the *exact* fastest lap, stint boundaries, etc.

**`pipeline.py` — `run(source, conn)`:** the three stages.
1. **`downsample(frames, target_hz)`** — decimates 333 Hz → 50 Hz, but always keeps any frame where the lap or sector index changes, so segmentation stays exact.
2. **`segment(frames)`** — groups frames into `LapBuffer`s at `lap_count` increments (start/finish line crossings). Lap N's number = `lap_count + 1`.
3. **persist** — per lap: `_sector_breakdown` computes sector times + distance bounds from sector-index transitions; `_trace_data` builds the column dict; the trace is written to Parquet; `db.insert_lap` + `db.insert_sectors` record metadata. Stints advance when `tyre_set` changes between laps.

So: **synthetic source → 50 Hz frames → laps/sectors → SQLite rows + one Parquet file per lap.** The ACC reader (M2) swaps in at stage 0 with zero changes downstream.

---

## 5. MCP layer (`tools/` + `server.py`)

Deliberate split for testability: **pure logic in `tools/queries.py`** (takes a SQLite connection, returns a JSON-ready dict), **thin `@mcp.tool()` wrappers in `server.py`** (open a connection, delegate, close). The wrapper docstrings are the tool descriptions Claude reads — the most important prompt engineering in the project.

> Note: the subpackage is named `tools/`, not `mcp/`, because a `mcp/` package would shadow the installed `mcp` SDK under absolute imports.

**The 8 tools:** `list_sessions`, `list_laps`, `get_lap_summary` (incl. validity + delta-to-PB), `get_telemetry_trace`, `compare_laps`, `get_sector_analysis`, `get_tire_history`, `export_lap_csv`.

**Token discipline (`formatting.py`):** a raw 50 Hz lap is tens of thousands of tokens. So:
- `detail_level="summary"` (default) returns min/max/mean/std per channel, **per sector and whole-lap** — no raw points.
- `low`/`medium`/`full` return a **distance-binned** series (60/150/600 points, mean per bin). Fixed bin counts cap the token cost regardless of lap length; a `distance_range` zoom concentrates the bins in a corner.

A test (`test_detail_level_response_sizes_are_ordered`) asserts `summary < low < medium < full` and that `summary` stays well under a context-busting size.

**Server entry point:** `pitwall = pitwall.server:main` → `mcp.run(transport="stdio")` — exactly what Claude Desktop launches.

---

## 6. Tooling / environment

- Dev venv: `.venv`, Python 3.14 (pyarrow 24.0, mcp 1.27.1 — both ship cp314 wheels). Package itself targets 3.10+.
- Commands (from repo root, PowerShell): see CLAUDE.md → "Build / lint / test commands".
- Deviations from the original plan (all intentional, recorded): `tools/` vs `mcp/`; `detail_level` as distance bins vs literal Hz; `sectors` table gained distance bounds.

---

## 7. Milestone 2 — the ACC reader (C3): BUILT & validated offline

**Status (updated 2026-05-24):** the offline reader batch is **done** — built and
validated against a real captured session, ruff clean, 83/83 tests pass. What
landed under `src/pitwall/games/acc/`:

- **`structs.py`** — the three ctypes pages (`SPageFilePhysics` 800 B,
  `SPageFileGraphic` 1588 B, `SPageFileStatic` 820 B), `_pack_=4`, with
  module-level `sizeof` + read-field-offset asserts that fail the import on any
  layout drift. **String fields are `c_ubyte*2N` UTF-16-LE byte arrays, not
  `c_wchar`** — `c_wchar` is 2 B on Windows but 4 B on Linux, which would change
  every offset and `sizeof` off-Windows; byte arrays make the layout
  platform-identical (and `wstr()` decodes them). Validated by decoding the real
  recording: track/car strings correct, channels all sane.
- **`mapping.py`** — pure struct→canonical conversions (`to_static_info`,
  `to_frame`, `lap_distance_m`): `gear-1`, `fuel × FUEL_DENSITY_KG_PER_L (0.745)`,
  wheel order FL/FR/RL/RR, `carCoordinates[playerCarID]`, accG axes.
- **`reader.py`** — `ACCSource(TelemetrySource)` fed by a `FrameStream`:
  `RecordingFrameStream` (offline replay — supersedes the "RecordingPages" name
  from the old plan; replay is a *stream* of pre-timestamped page pairs, not a
  poll-driven snapshot provider) and `LiveFrameStream` (polls `LivePages`,
  **terminates** when status leaves LIVE / `packetId` freezes / page vanishes).
- **`ingest/source.py`** gained **`detect_source()`** (recording-replay or live
  ACC; iRacing = documented `NotImplementedError` v0.2 stub; ACC imports stay
  lazy so the module imports on non-Windows).
- **`scrub.py`** (+`pitwall-scrub`) and **`inspect.py`** (+`pitwall-inspect`)
  consoles. A scrubbed, slimmed CI fixture
  `tests/fixtures/acc-nurburgring-slim.pwcap` (1.56 MiB, player name removed,
  pages truncated to struct size, windowed across a lap crossing) lets
  `test_acc_reader.py` drive the *whole* pipeline in CI without the game.

**Two learnings from the real capture** (Kenneth's Nurburgring / Ford Mustang
GT3 session): ACC reported **`trackSPlineLength == 0`**, so `lap_distance_m`
falls back to the normalised 0..1 position; and the usable capture had only
`completedLaps` 0→1 (one crossing), so the fixture is windowed to span it (2
laps). Both are documented in `docs/channel-mapping.md`.

**Live drive-test DONE (2026-05-24):** a 360 s session (2 full laps + a deliberate
off-track) confirmed the items a single recording couldn't:

- **`accG` axis & sign** — confirmed by correlation: `accG[2]` is longitudinal
  (hard braking −1.46 g, full throttle +0.38 g); `accG[0]` is lateral but signed
  opposite to steering (right −1.36 g, left +1.39 g), so `g_lat = −accG[0]`
  (positive = right). `mapping.py` and `docs/channel-mapping.md` updated.
- **Multi-lap landing** — the session persists 3 laps (lap 2 a clean valid full
  lap), so segmentation increments and writes one row per lap.
- **Off-track invalidation** — the off-track 3rd lap lands `is_valid = False`.
- **Graceful end-of-session** — the partial final lap persists (replay terminates
  at EOF; `LiveFrameStream` termination on status≠LIVE / packetId freeze is unit
  tested). The CI fixture was upgraded to a slice of this session (a valid lap +
  the invalid off-track lap).

**Two open findings — both RESOLVED (2026-05-24):**
1. **`trackSPlineLength` is 0 even in a full clean session** → distances stayed
   normalised 0..1. **Fixed** with a `TRACK_LENGTHS` lookup keyed on track code in
   `naming.py` (all ~25 ACC tracks), resolved once in
   `acc/mapping.resolve_track_length` and shared by both the session row and every
   per-frame `lap_distance_m` (the reader cached its own length, so both paths now
   route through the one resolver). `nurburgring` GP is **5148 m**, not the ~5783 m
   first guessed here — confirmed ~5152 m by integrating speed over the fixture's
   final straight. An unknown track still falls back to the 0..1 key.
2. **Sector times don't sum to lap time on real data** — ACC's
   `currentSectorIndex` doesn't reset at the exact lap-counter increment, so a lap
   could open still reading the previous lap's sector; the old `_sector_breakdown`
   dropped that sliver. **Fixed** by rewriting it as a forward-only partition of the
   exact `[start_t, end_t]` lap interval (sector 0 anchored at the line, the last
   open sector closed on `end_t`), so sector times telescope to lap time by
   construction. A direct unit test reproduces the stale-lead + premature-reset case
   the synthetic source can't; the fixture replay confirms it on real data.
   Lap/sector times stay wall-clock (not ACC's unreliable `lastSectorTime`).

### Original C3 plan (for reference)

Files added under `src/pitwall/games/acc/`:

- **`structs.py`** — `ctypes.Structure` definitions for the three ACC shared-memory pages (`SPageFilePhysics`, `SPageFileGraphic`, `SPageFileStatic`), `_pack_=4`, UTF-16 `c_wchar` strings, the **full ACC field order** (the high-risk part — fields like `isValidLap`, `currentSectorIndex`, `currentTyreSet` sit deep in the ACC-extended graphics page; truncating the layout reads garbage).
- **`reader.py`** — `ACCSource(TelemetrySource)`: opens the three `mmap` pages, snapshots each with `from_buffer_copy`, exposes `is_running()` (gates on `status` + non-zero `packetId`) and raises a clear `GameNotRunningError` instead of crashing. `frames()` polls the physics page and assembles `CanonicalFrame`s.
- **`mapping.py`** — ACC fields → canonical values: `gear - 1`, fuel litres → kg, `normalizedCarPosition × trackSPlineLength` → `lap_distance_m`, and the **`accG` axis/sign** (only confirmable by driving). Also lifts `completedLaps`, `currentSectorIndex`, `isValidLap`, `currentTyreSet` for segmentation.
- **`ingest/source.py`** gains `detect_source()` (ACC now; iRacing = documented v0.2 stub).

**Testing strategy (offline-first):** `ctypes.sizeof`/`.offset` assertions catch layout/padding/char-vs-wchar errors deterministically, and a handcrafted raw buffer round-trips through the structs. Then a **live drive-test checklist** confirms real values: sane throttle/brake/speed/gear, correct track/car decode + length, ~5 laps landing in SQLite with a deliberate off-track flagged invalid, the accG axis/sign, and graceful quit-mid-session.

---

## 8. Recommendation: develop C3 against a captured shared-memory recording

**Yes — and here's the nuance.** Live ACC isn't a "file"; it's shared memory that only exists while the game runs. But we can get the same benefit by **recording the raw bytes once**:

1. As the *first* step of C3 I write a tiny capture tool. It just copies the raw bytes of the three pages to disk — it needs **no correct struct yet**, so it can't be wrong. Ideally it records a short *sequence* of physics+graphics snapshots (not one frame), covering ~2–3 laps plus one deliberate off-track.
2. You run it once during a ~60-second ACC session. That's your only driving until the final validation.
3. I develop and validate the struct parsing + canonical mapping **offline** against that recording, iterating freely without you in the loop.
4. A slimmed copy of the recording becomes a committed **regression fixture** — C3 then stays testable forever in CI without the game (portfolio-worthy; almost no MCP server does this). Only caveat: the static page contains your player name — I'll scrub or exclude it before committing.
5. A final short live drive-test confirms axis/sign/units against the moving car.

This collapses the biggest risk (struct correctness) into an offline, deterministic loop and reduces your driving to one capture + one confirmation. (The iRacing `.ibt` file in v0.2 is the analogous idea, except it's a real on-disk export the game already produces.)
