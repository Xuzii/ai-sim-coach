"""``GeminiClient`` -- the Google Gemini implementation of :class:`LLMClient`.

Translation only. The agent loop in :mod:`pitwall.coach.agent` is what decides
*what* to send and *when* to stop; this client just maps the neutral seam types
(:class:`Message`, :class:`ToolSpec`, :class:`ToolCall`, :class:`LLMResponse`)
to and from the ``google-genai`` SDK.

We import ``google.genai`` lazily inside the constructor so the base ``pitwall``
install (no ``coach`` extra) doesn't pay an import cost and tests using
:class:`MockLLMClient` need no SDK at all.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pitwall.coach.llm.base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec


class GeminiClient(LLMClient):
    """``LLMClient`` over the ``google-genai`` SDK.

    Construct via :meth:`from_config` (which resolves the key from ``.env`` /
    ``os.environ`` via :func:`pitwall.coach.config.resolve_api_key`) or pass an
    explicit ``api_key`` for tests.
    """

    def __init__(self, *, api_key: str, model: str, temperature: float = 0.2) -> None:
        # Lazy import so the base install (no coach extra) doesn't need google-genai.
        from google import genai
        from google.genai import types as gt

        self._types = gt
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._temperature = temperature

    # -- construction ------------------------------------------------------- #
    @classmethod
    def from_config(cls, config: Any) -> GeminiClient:
        """Build a client from a :class:`~pitwall.coach.config.CoachConfig`.

        Imported here (not at module top) to avoid a config<->llm import cycle
        if the CLI ever wants to import both up front."""
        from pitwall.coach.config import resolve_api_key

        return cls(
            api_key=resolve_api_key(config),
            model=config.model,
            temperature=config.temperature,
        )

    # -- the LLMClient contract -------------------------------------------- #
    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        system: str | None = None,
    ) -> LLMResponse:
        contents = [self._to_content(m) for m in messages]
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=self._temperature,
            tools=[self._to_tool(tools)] if tools else None,
        )
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=config,
        )
        return self._from_response(response)

    # -- tool translation -------------------------------------------------- #
    def _to_tool(self, tools: Sequence[ToolSpec]) -> Any:
        """One ``Tool`` carrying all ``FunctionDeclaration`` entries.

        We pass each spec's JSON schema as ``parameters_json_schema`` (the SDK's
        raw-JSON-schema escape hatch) rather than ``parameters`` so we never have
        to translate to the SDK's ``Schema`` class -- safer and lossless for the
        ``detail_level`` enum and any custom keys."""
        declarations = [
            self._types.FunctionDeclaration(
                name=spec.name,
                description=spec.description,
                parameters_json_schema=_clean_schema(spec.parameters),
            )
            for spec in tools
        ]
        return self._types.Tool(function_declarations=declarations)

    # -- message translation ----------------------------------------------- #
    def _to_content(self, msg: Message) -> Any:
        types = self._types
        if msg.role == "user":
            return types.Content(role="user", parts=[types.Part.from_text(text=msg.content)])
        if msg.role == "tool":
            # The Gemini SDK sends function results back as a "user" turn whose
            # part is a function_response. ``content`` is the JSON-encoded result
            # dict the agent loop produced from ``run_tool``.
            try:
                response_obj = json.loads(msg.content) if msg.content else {}
            except json.JSONDecodeError:
                response_obj = {"raw": msg.content}
            if not isinstance(response_obj, dict):
                response_obj = {"result": response_obj}
            return types.Content(
                role="user",
                parts=[types.Part.from_function_response(name=msg.name or "", response=response_obj)],
            )
        # assistant
        parts: list[Any] = []
        if msg.content:
            parts.append(types.Part.from_text(text=msg.content))
        for tc in msg.tool_calls:
            parts.append(types.Part.from_function_call(name=tc.name, args=dict(tc.arguments)))
        if not parts:
            # An empty assistant turn would 400 the API; emit an empty text part.
            parts.append(types.Part.from_text(text=""))
        return types.Content(role="model", parts=parts)

    # -- response parsing -------------------------------------------------- #
    def _from_response(self, response: Any) -> LLMResponse:
        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return LLMResponse(text="", tool_calls=[])
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for i, part in enumerate(parts):
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                tool_calls.append(
                    ToolCall(
                        id=f"call_{i}",
                        name=fc.name,
                        arguments=dict(getattr(fc, "args", None) or {}),
                    )
                )
                continue
            txt = getattr(part, "text", None)
            if txt:
                text_chunks.append(txt)
        return LLMResponse(text="".join(text_chunks), tool_calls=tool_calls)


# --------------------------------------------------------------------------- #
# Schema sanitiser
# --------------------------------------------------------------------------- #
# Keys the Gemini JSON-schema path accepts at object / property level. Anything
# else (e.g. ``$schema``, ``additionalProperties``, ``examples``) is dropped to
# avoid a 400 from the API. The list is conservative -- adding a key later is
# cheaper than debugging a server-side rejection.
_ALLOWED_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "items",
        "enum",
        "format",
        "nullable",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "anyOf",
        "oneOf",
        "default",
    }
)


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursive deep-copy that keeps only Gemini-friendly JSON-schema keys."""
    if not isinstance(schema, dict):
        return schema  # type: ignore[return-value]
    cleaned: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _ALLOWED_SCHEMA_KEYS:
            continue
        if k == "properties" and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif k in ("anyOf", "oneOf") and isinstance(v, list):
            cleaned[k] = [_clean_schema(x) for x in v]
        else:
            cleaned[k] = v
    return cleaned
