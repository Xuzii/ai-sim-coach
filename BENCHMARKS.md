# pitwall — Benchmarks

Performance of the v0.1 (ACC) pipeline, measured end-to-end against a **real**
captured session. Almost no MCP server publishes numbers like these; the point is
to show the design choices (50 Hz downsample, Parquet traces, summary-first tool
responses) actually pay off — and to be honest where they don't.

Everything here is reproducible:

```bash
pip install -e ".[bench]"        # adds tiktoken + psutil (bench-only)
python bench/run.py              # uses ./acc-spa.pwcap if present, else the slim CI fixture
python bench/run.py path/to/any.pwcap
```

`bench/run.py` writes `bench/results.json` and prints the same. The success-criteria
checks below come from `python bench/verify_success_criteria.py`.

## Test setup

| | |
|---|---|
| Machine | Intel Core i7-12700K, 32 GB RAM, Windows 11 |
| Python | 3.14.3 · pyarrow 24.0.0 · mcp 1.27.1 |
| Capture | `acc-spa.pwcap` — 286 MB, **360 s** real ACC session (mislabelled "spa"; actually **Nürburgring GP / Ford Mustang GT3**) |
| Recording | 34,815 physics frames @ ~97 Hz + 34,815 graphics frames, raw shared-memory bytes |
| Session | 3 laps: lap 1 invalid (off-track) 2:26.959 · **lap 2 valid 2:04.126** · lap 3 invalid + partial (capture ended mid-lap) |

> Token counts use tiktoken's `cl100k_base` as a **documented, reproducible
> proxy**. Claude's tokenizer differs slightly, so treat the byte counts as ground
> truth and tokens as a close estimate (they track each other at ~2.3 bytes/token
> on this JSON).

---

## 1. Ingestion

Offline replay of the full 286 MB recording through the whole pipeline
(parse → map to canonical schema → downsample 333 Hz→50 Hz → segment into
laps/sectors → write SQLite rows + one Parquet trace per lap):

| Metric | Result | Plan target |
|---|---|---|
| Ingest wall time (whole session) | **3.06 s** | stable, no drops |
| Native frames processed / s | **11,393** | — |
| Speed vs real time | **117× realtime** | — |
| CPU utilisation | 93% of one core (CPU-bound, single-threaded) | <2% live¹ |
| Peak Python working set during `pipeline.run` | **53 MB** (tracemalloc) | <100 MB |

¹ The <2% CPU / <100 MB targets in the plan are for **live** capture, where frames
arrive at the game's rate (~333 Hz) and the cost is spread over real time — i.e.
the 3.06 s of work here amortised across 360 s is well under 1% of one core. This
benchmark instead measures *offline replay throughput* (how fast we can re-ingest a
saved session), which is the harder, more interesting number. The 117× headroom is
why live capture is effectively free.

> **Memory note.** Resident set during this benchmark is inflated (~740 MB) because
> offline replay holds the entire 286 MB recording — plus parsed record objects — in
> RAM at once. That's an artifact of the replay harness, **not** live capture, which
> streams one frame at a time and never materialises the session. The 53 MB figure
> (Python allocations attributable to `pipeline.run` itself) is the representative
> working set.

---

## 2. Storage

SQLite metadata + one Parquet trace per lap (zstd level 3), measured on the 3-lap
session:

| Metric | Result |
|---|---|
| Points stored per lap (avg) | 5,804 (50 Hz × ~116 s laps) |
| **Parquet trace per lap** | **~504 KiB** |
| SQLite per lap (rows in `laps` + `sectors`) | ~17 KB amortised (52 KB DB / 3 laps) |
| **Total per lap** | **~522 KiB** |
| Bytes per stored point | **89 B** |

### The honest finding: telemetry floats barely compress

89 B/point is almost exactly the raw size of one row (≈20 × float32 + int32 + int8 ≈
89 B). **zstd is recovering almost nothing.** That's expected: real telemetry is
dense, high-entropy float data with noisy mantissas — there's little redundancy for a
general compressor to exploit.

I measured the alternatives on a real lap trace (7,112 rows × 23 cols) rather than
guessing:

| Encoding | Size | vs current |
|---|---|---|
| zstd-3 (current) | 626 KiB | — |
| zstd-9 | 626 KiB | 0% |
| zstd-19 | 547 KiB | −13% |
| zstd-3/9 + byte-stream-split | 626 KiB | 0% (no-op in pyarrow 24) |
| snappy + byte-stream-split | 722 KiB | +15% |

The best available win was ~13% (zstd-19), and byte-stream-split — the encoding
*designed* for float columns — was a no-op in this pyarrow build. At ship time that's
not worth trading a locked, tested storage decision for, so v0.1 keeps zstd-3 and
reports the real number. **In practice it's a non-issue:** at ~0.5 MiB/lap a full
season of ~500 laps is ~250 MiB, and column-projected distance-range reads (below)
stay fast regardless. Revisiting float encoding is a noted v0.2 follow-up.

---

## 3. Query latency

Each query function behind a tool, run warm against the ingested store (200 iters;
100 for the heavier trace reads). These are the pitwall-side numbers — the work the
server does per tool call, excluding Claude's model/network time.

| Tool | Median | p95 | Plan target |
|---|---|---|---|
| `list_sessions` | 0.12 ms | 0.22 ms | <50 ms |
| `list_laps` | 0.06 ms | 0.08 ms | <50 ms |
| `get_lap_summary` | 0.12 ms | 0.24 ms | <50 ms |
| `get_sector_analysis` | 0.09 ms | 0.19 ms | <50 ms |
| `get_telemetry_trace` (summary) | 4.4 ms | 5.3 ms | <100 ms |
| `get_telemetry_trace` (full, whole lap) | 8.2 ms | 9.8 ms | <100 ms |
| `get_telemetry_trace` (full, one corner, 2 ch) | 4.3 ms | 5.1 ms | <100 ms |
| `compare_laps` | 4.8 ms | 5.5 ms | <100 ms |
| `get_tire_history` (3 laps) | 6.4 ms | 7.3 ms | <100 ms |
| `export_lap_csv` (full lap → disk) | 17.6 ms | 19.3 ms | — |

Metadata-only tools answer in **microseconds** (pure SQLite). Trace tools cost a few
ms because they crack open Parquet; that's exactly the access pattern the
SQLite-vs-Parquet split was chosen for. Every tool clears its target with 5–400×
headroom.

---

## 4. Token budget — the cost/resolution curve

This is the design choice no one benchmarks. A raw 50 Hz lap dumped as JSON is tens of
thousands of tokens — one tool call would blow a big chunk of the context window. So
`get_telemetry_trace` defaults to `summary` and exposes a `detail_level` knob. Measured
response sizes for the fastest lap (default 6 channels):

| `detail_level` | Points | Bytes | Est. tokens | vs summary |
|---|---|---|---|---|
| **summary** (default) | 0 (per-sector + whole-lap stats) | 1.8 KB | **751** | 1× |
| low | ~60 | 7.5 KB | 3,130 | 4× |
| medium | ~150 | 18 KB | 7,703 | 10× |
| full | ~600 | 71 KB | **30,565** | 41× |
| full, one corner, 2 channels | ~580 | 26 KB | 10,728 | 14× |

**A full whole-lap trace costs 41× the default and ~30k tokens** — which validates
defaulting to summary and binning by distance. A coach almost never needs every point;
when they zoom into a corner, `distance_range` concentrates the bins there instead of
spending them across the whole lap.

The other tools are tiny by construction (full responses, real session):

| Tool | Bytes | Est. tokens |
|---|---|---|
| `list_sessions` | 226 B | 81 |
| `list_laps` | 519 B | 187 |
| `get_lap_summary` | 486 B | 173 |
| `get_sector_analysis` | 340 B | 108 |
| `compare_laps` | 575 B | 211 |
| `get_tire_history` (3 laps) | 548 B | 247 |

---

## 5. Cold start (stdio round-trip)

Spawning the real server as a subprocess and driving it over stdio with the MCP
client — process spawn + `initialize` handshake + `list_tools` + first tool call
returning. This is everything pitwall controls (it excludes Claude's model/network
latency, which isn't pitwall's to measure):

| Stage | Time |
|---|---|
| spawn → `initialize` | 1.051 s |
| `initialize` → `list_tools` | 0.007 s |
| `list_tools` → first tool call returns | 0.006 s |
| **cold start → first tool call** | **1.064 s** |
| Tools registered | 8 |

The plan's success criterion is **"under 3 seconds from launching the server to the
first tool call."** pitwall's side is **1.06 s**, almost all of it Python interpreter +
import startup; the MCP handshake and a real query add ~13 ms.

---

## 6. Quality — the 5 success-criteria queries on real data

The Phase 1 plan defines five natural-language questions Claude must answer from a lap
you actually drove. Mapped to the tools Claude would call and run against the real
Nürburgring session (`bench/verify_success_criteria.py`):

| # | Question | Tools | Answer from real data |
|---|---|---|---|
| 1 | "Fastest lap at Nürburgring this week?" | `list_sessions` → `list_laps(top_n_fastest=1)` | Lap 2 — **2:04.126**, valid, top speed 245 km/h |
| 2 | "Brake pressure at the first braking zone on that lap?" | `get_telemetry_trace(summary, distance_range)` | Over 206–515 m: brake mean 0.23, peaks to 1.0; speed bled 245→175 km/h |
| 3 | "Compare fastest vs 2nd-fastest — where did I lose time?" | `list_laps(top_n_fastest=2)` → `compare_laps` | This 6-min capture has only **one** clean valid lap, so the tool returns a clear "need two valid laps" note rather than fabricating. Exercised on multi-lap data by the test suite. |
| 4 | "Tyre temps over the stint?" | `get_tire_history` | Per lap, per wheel: FL ran hottest (86.7→88.1→87.8 °C); pressures 26–27 psi |
| 5 | "Export that lap to CSV" | `export_lap_csv` | 6,005 rows × 23 columns → 1.15 MB CSV on disk |

Four of five answer fully from this single short capture; #3 needs two valid laps and
degrades gracefully. The literal Claude-Desktop round-trip (and the demo GIF) is the
last manual step.

### Correctness check that came for free

The sector-time fix (Finding 2) is visible in the real data: for every **complete**
lap, the per-sector times **telescope to the lap time within ±1 ms** (rounding only) —
lap 1: Δ0 ms, lap 2: Δ1 ms — even though ACC's `currentSectorIndex` lags the lap
counter. The partial final lap (capture cut off) correctly leaves its last sector
`None` rather than inventing one.
