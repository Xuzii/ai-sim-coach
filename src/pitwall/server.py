"""The pitwall MCP server.

Defines the FastMCP server and registers the narrow query tools over stdio (what
Claude Desktop speaks). Tools are registered programmatically from a single source
of truth, :mod:`pitwall.tools.specs`, which the Phase 2 coach agent also consumes
-- so the server and the coach can never disagree about a tool's name, description,
or parameters. Each tool description (the prompt engineering Claude reads to decide
*when* to call each one) lives in ``specs.py``.

Each registered tool opens a SQLite connection, delegates to the spec's
connection-in handler in :mod:`pitwall.tools.queries`, and closes the connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from pitwall.storage import db
from pitwall.tools import specs

mcp = FastMCP("pitwall")


def _conn() -> sqlite3.Connection:
    """Open the local store, ensuring the schema exists (idempotent, cheap)."""
    conn = db.connect()
    db.apply_schema(conn)
    return conn


def _make_tool(spec: specs.QueryToolSpec) -> Callable[..., dict]:
    """Build the FastMCP-facing callable for a tool spec.

    The returned function accepts the tool's public arguments, opens a connection,
    delegates to the handler, and closes it. We stamp ``__signature__`` from
    :func:`specs.public_signature` so FastMCP derives the exact same input schema
    the server has always advertised (the handler's signature minus ``conn``)."""
    handler = spec.handler

    def tool_fn(**kwargs: object) -> dict:
        conn = _conn()
        try:
            return handler(conn, **kwargs)
        finally:
            conn.close()

    tool_fn.__name__ = spec.name
    tool_fn.__qualname__ = spec.name
    tool_fn.__doc__ = spec.description
    tool_fn.__signature__ = specs.public_signature(spec)  # type: ignore[attr-defined]
    return tool_fn


for _spec in specs.TOOL_SPECS:
    mcp.add_tool(_make_tool(_spec), name=_spec.name, description=_spec.description)


def main() -> None:
    """Console-script entry point: run the server over stdio for Claude Desktop."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
