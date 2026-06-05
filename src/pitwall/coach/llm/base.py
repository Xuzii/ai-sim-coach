"""Provider-agnostic primitives for an LLM tool-use loop.

These neutral dataclasses and the :class:`LLMClient` ABC are the seam that keeps
the coach independent of any one vendor. ``GeminiClient`` (chunk C1) and a future
``ClaudeClient`` each translate to/from these types, so the agent loop in
``agent.py`` never imports a provider SDK and is trivially testable via
:class:`MockLLMClient`.

The loop is: the agent sends a conversation (``Message`` list) plus the available
``ToolSpec`` list to ``LLMClient.generate``; the client returns an ``LLMResponse``
that either carries free ``text`` or one or more ``ToolCall``s; the agent runs the
tools, appends their results as tool ``Message``s, and calls ``generate`` again --
until the model stops requesting tools (or calls the terminal report tool).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolSpec:
    """A tool offered to the model: its name, a description of when to use it, and
    a JSON-Schema object describing its parameters."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    """A model's request to invoke a tool with parsed arguments. ``id`` correlates
    the call with the tool-result message that answers it."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    """One conversation turn.

    - ``role="assistant"`` with ``tool_calls`` set: the model requested tools.
    - ``role="tool"`` with ``tool_call_id`` + ``name``: a tool's result (``content``
      is the JSON-encoded result dict).
    - otherwise ``content`` is plain text.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMResponse:
    """One assistant turn from an :class:`LLMClient`: free text, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient(ABC):
    """Minimal provider-agnostic chat-with-tools interface used by the agent loop."""

    @abstractmethod
    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        system: str | None = None,
    ) -> LLMResponse:
        """Produce the next assistant turn given the conversation and available tools."""
        raise NotImplementedError


@dataclass
class MockLLMClient(LLMClient):
    """A scripted ``LLMClient`` for tests: returns a pre-set ``LLMResponse`` per
    ``generate`` call, in order, with no network. Every call's ``(messages, tools,
    system)`` is recorded in :attr:`calls` so tests can assert what the agent did."""

    script: list[LLMResponse] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    _cursor: int = 0

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        system: str | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": list(tools), "system": system})
        if self._cursor >= len(self.script):
            raise AssertionError("MockLLMClient script exhausted: agent requested more turns than scripted")
        response = self.script[self._cursor]
        self._cursor += 1
        return response
