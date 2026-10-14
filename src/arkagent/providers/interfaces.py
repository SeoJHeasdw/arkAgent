"""ILLMProvider — the model-swap seam.

This Protocol is the boundary that absorbs provider differences so the agent loop
never branches on which backend it is talking to. It normalizes three things:

  - tool calling  : each wire format (Ollama function-calling, OpenAI
                    `tool_calls`, or a prompt fallback) → one `ToolCall` shape
                    (core.types).
  - streaming     : each provider's stream → a single `ProviderChunk` sequence the
                    caller turns into its own event stream.
  - prompt caching: provider-specific (e.g. Ollama keep-alive) — expressed as a
                    capability flag; the loop/provider handle it, callers don't.

**FROZEN.** New providers implement this; they do not widen it. Adding a capability
means adding a field to `ProviderCapabilities`. `stream`'s required signature never
changes; the keyword-only optional params (`response_schema`, `think`, `max_tokens`)
are the one sanctioned widening — every implementation must *accept* them, and may
ignore what its wire cannot express (2026-07-04, 소형 로컬 모델의 무한 thinking
루프 대응: 구조화 출력은 프롬프트 지시가 아니라 디코딩 제약으로 강제한다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol, Sequence, runtime_checkable

from ..core.types import Message, ToolCall, ToolSpec


@dataclass(frozen=True)
class ProviderCapabilities:
    native_tool_calling: bool = False
    streaming: bool = True
    prompt_caching: bool = False
    vision: bool = False


@dataclass(frozen=True)
class ProviderChunk:
    """One normalized unit of a provider's streamed response.

    kind:
      "thinking"  → `text` is a reasoning delta
      "text"      → `text` is an answer delta
      "tool_call" → `tool_call` is a fully-formed request
      "done"      → end of this assistant turn; `stop_reason` explains why
    """

    kind: Literal["thinking", "text", "tool_call", "done"]
    text: str | None = None
    tool_call: ToolCall | None = None
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "error"] | None = None
    # Actual token usage, populated on the "done" chunk when the provider reports
    # it (echo does not). Lets callers record usage and correct token-budget
    # estimates from real numbers. None = unknown.
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class ILLMProvider(Protocol):
    """A model provider. Implementations live in `providers/` and are chosen
    by `registry.py` from `Config.provider` — never imported directly by the loop."""

    name: str

    def capabilities(self) -> ProviderCapabilities: ...

    def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        temperature: float = 0.7,
        response_schema: dict | None = None,
        think: bool | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream one assistant turn. Yields `ProviderChunk`s and ends with a
        single `kind="done"` chunk. Implementations that don't support native tool
        calling synthesize `tool_call` chunks from a prompt-based fallback so the
        loop sees a uniform stream regardless of provider.

        Optional per-call knobs (implementations ignore what they can't express):
          response_schema — JSON schema the output must conform to, enforced by
                            constrained decoding where supported (Ollama `format`,
                            OpenAI `response_format`). Callers still instruct the
                            shape in the prompt for providers that ignore this.
          think           — None: provider default. False: suppress reasoning
                            tokens (구조화 1회 호출 — planner 등 — 은 thinking이
                            품질 기여보다 소형 모델 무한 루프 리스크가 크다).
          max_tokens      — output token cap (Ollama num_predict). A guardrail so
                            a runaway generation is bounded by decode, not wall
                            clock."""
        ...


@runtime_checkable
class IProviderRegistry(Protocol):
    """Resolves a provider by config. The only place that maps `kind` → impl."""

    def get(self, kind: str) -> ILLMProvider: ...
    def available(self) -> list[str]: ...
