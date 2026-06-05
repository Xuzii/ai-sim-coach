"""The pitwall single-agent coach (Phase 2).

A provider-agnostic agentic tool-use loop that investigates one lap with the
existing pitwall query tools and emits a structured :class:`~pitwall.coach.report.CoachingReport`
plus a human-readable summary. This C0 layer is the contract + skeleton: the report
schema, the deterministic consistency score, the report store, the provider-neutral
LLM seam (with a mock for tests), and the agent tool registry -- all testable with no
network and no API key. The Gemini client and the agent loop arrive in later chunks.
"""
