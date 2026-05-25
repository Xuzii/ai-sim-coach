# Why the official `mcp` SDK, not standalone `fastmcp`

**Thesis:** Two packages expose a `FastMCP` class: the official `mcp` SDK
(`from mcp.server.fastmcp import FastMCP`) and a separate, heavier third-party
`fastmcp` framework. For a single local stdio server with 8 tools, the official
SDK is the right call — fewer dependencies, less version-drift surface, and it is
what Claude Desktop targets.

To capture while building:
- [ ] Dependency-tree size comparison.
- [ ] The version pin (`mcp>=1.27,<2`) and why: the SDK's v2 line was pre-alpha at
      build time.
- [ ] Note: subpackage named `tools/`, not `mcp/`, to avoid shadowing the SDK
      under absolute imports.
