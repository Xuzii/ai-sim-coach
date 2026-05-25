"""pitwall -- an MCP telemetry server that turns Claude into a sim-racing race engineer.

Phase 1 ingests Assetto Corsa Competizione telemetry, stores it (SQLite metadata +
Parquet per-lap traces), and exposes it to Claude Desktop through a small set of
narrow MCP tools. iRacing support lands in v0.2; the canonical channel schema in
:mod:`pitwall.channels` keeps the rest of the system game-agnostic.
"""

__version__ = "0.1.0"
