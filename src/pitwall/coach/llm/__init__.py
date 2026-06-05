"""Provider-agnostic LLM seam for the coach.

:mod:`pitwall.coach.llm.base` defines the neutral message/tool types and the
``LLMClient`` ABC the agent loop talks to, plus a ``MockLLMClient`` for tests.
``GeminiClient`` (and, later, a drop-in ``ClaudeClient``) implement that ABC in
sibling modules, so no provider SDK leaks into the coaching logic.
"""
