# pitwall — Architecture

One page: how a frame of telemetry becomes an answer Claude can give. The whole
design turns on one idea — a **canonical channel schema** that every game maps into,
so storage, queries, and the MCP layer never know or care which game produced the data.

```mermaid
flowchart TD
    subgraph game["Game (same machine)"]
        ACC["ACC Shared Memory<br/>physics 333 Hz · graphics 30 Hz · static<br/>(3 mmap pages)"]
    end

    subgraph capture["Capture (optional, de-risking)"]
        PWCAP[".pwcap recording<br/>raw page bytes over time"]
    end

    subgraph reader["ingest/ — game readers"]
        STRUCTS["games/acc/structs.py<br/>ctypes pages, _pack_=4"]
        MAP["games/acc/mapping.py<br/>struct → canonical<br/>gear−1 · fuel×0.745 · accG axes"]
        SRC["detect_source()<br/>live ACC | recording replay | iRacing (v0.2)"]
    end

    SCHEMA["channels.py<br/><b>CANONICAL SCHEMA (22 channels)</b><br/>throttle, brake, speed_kmh,<br/>lap_distance_m, tire_*, g_lat/lon …"]

    subgraph pipe["ingest/pipeline.py — the spine"]
        DS["downsample<br/>333 Hz → 50 Hz<br/>(keep lap/sector boundaries)"]
        SEG["segment<br/>laps at line crossings,<br/>sectors forward-only"]
        PERSIST["persist<br/>1 SQLite row + 1 Parquet file / lap"]
    end

    subgraph store["storage/"]
        SQLITE[("SQLite<br/>sessions · laps · sectors<br/>+ distance bounds")]
        PARQUET[("Parquet (zstd)<br/>one columnar trace per lap<br/>distance-sorted")]
    end

    subgraph mcp["MCP server (stdio)"]
        QUERIES["tools/queries.py<br/>pure, SDK-free,<br/>token-sized dicts"]
        FMT["tools/formatting.py<br/>summary stats ·<br/>distance binning"]
        SERVER["server.py<br/>FastMCP · 8 @mcp.tool()<br/>descriptions = the prompt"]
    end

    CLAUDE(["Claude Desktop<br/>'brake pressure at T1<br/>on my fastest lap?'"])

    ACC --> STRUCTS
    ACC -.capture once.-> PWCAP
    PWCAP -.replay offline.-> SRC
    STRUCTS --> MAP --> SRC
    SRC --> DS
    SCHEMA -.defines.-> MAP
    SCHEMA -.defines.-> PARQUET
    DS --> SEG --> PERSIST
    PERSIST --> SQLITE
    PERSIST --> PARQUET
    SQLITE --> QUERIES
    PARQUET --> QUERIES
    QUERIES --> FMT --> SERVER
    SERVER <-->|"JSON-RPC / stdio"| CLAUDE
```

## Why it's shaped this way

- **Canonical schema is the contract** (`channels.py`, written first). ACC's `gas`
  and iRacing's `Throttle` both become `throttle` *before* anything downstream sees
  them, so the pipeline, storage, and tools are written once and stay game-agnostic.
  Adding iRacing in v0.2 is a new reader at the top — nothing below changes.

- **Capture-then-replay de-risks the binary parsing.** The riskiest code is the
  ctypes struct layout. A tiny capture tool dumps raw shared-memory bytes to a
  `.pwcap` once; the struct + mapping are then developed and regression-tested
  *offline* against it, and a scrubbed, slimmed slice is committed as a CI fixture —
  so the ACC reader stays tested forever without the game running.

- **Downsample to 50 Hz on ingest.** Human reaction is ~250 ms; 50 Hz (20 ms) is well
  below any change a coach can act on. Storing 333 Hz is 6.6× the data for zero
  coaching value — but boundary frames (lap/sector changes) are always kept so
  segmentation stays exact.

- **SQLite for metadata, Parquet for traces** — deliberately split. The core access
  pattern is "the brake channel between 200 m and 400 m on lap 7": column-slicing by
  distance, which row-based SQLite blobs handle terribly and columnar Parquet handles
  for a few ms (see [BENCHMARKS.md](../BENCHMARKS.md) §3).

- **Narrow tools, summary-first responses.** Eight small tools, each described like a
  docstring *for Claude* (the descriptions are the prompt engineering). Responses
  default to per-sector summary stats; a `detail_level` knob trades tokens for
  resolution, and traces bin by **distance, not time**, because a coach reasons about
  places on the track. A full 50 Hz trace is ~30k tokens; the summary is ~750
  (BENCHMARKS.md §4).
