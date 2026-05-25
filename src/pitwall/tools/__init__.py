"""MCP tool implementations.

Named ``tools`` (not ``mcp``) deliberately: a subpackage called ``mcp`` would
shadow the installed ``mcp`` SDK under absolute imports. ``queries`` holds pure,
SDK-free functions that are unit-testable without spinning up a server;
``formatting`` handles summary stats, detail-level downsampling, and distance
binning. The thin ``@mcp.tool()`` wrappers live in :mod:`pitwall.server`.
"""
