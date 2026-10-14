"""OpenAI-compatible provider adapter.

Covers two cases with one impl:
  - the official API: default base_url https://api.openai.com/v1, key required
  - any OpenAI-compatible endpoint with a caller-supplied base_url — a local
    server (llama.cpp, LM Studio, an MLX server) or a hosted gateway

Wire format + streaming-delta assembly live in `_openai_compat`; this file only
owns the HTTP call (httpx) and bearer auth. Network is isolated behind
`_stream_raw` so unit tests replace it with a fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from typing import Any

from ..core.config import ProviderConfig
from ..core.errors import ProviderError, classify_provider_error
from ..core.types import Message, ToolSpec
from . import _openai_compat as oai
from .interfaces import ILLMProvider, ProviderCapabilities, ProviderChunk

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OFFICIAL_TIMEOUT_SECONDS = 120.0


class OpenAIProvider(ILLMProvider):
    name = "openai"

    def __init__(self, cfg: ProviderConfig, *, api_key: str | None = None, base_url: str | None = None) -> None:
        self._cfg = cfg
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(native_tool_calling=True, streaming=True, prompt_caching=False, vision=False)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # 로컬 OpenAI 호환 서버는 키 없이 받는다 — 빈 Bearer를 보내면 거절하는 서버가
        # 있어 키가 있을 때만 싣는다.
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _timeout(self) -> float:
        # 로컬 서버는 모델 로드와 긴 프롬프트 프리필로 첫 바이트가 늦다. 공식 API의
        # 짧은 상한 대신 설정의 로컬 추론 타임아웃을 쓴다.
        if self._base_url == _DEFAULT_BASE_URL:
            return _OFFICIAL_TIMEOUT_SECONDS
        return float(self._cfg.timeout_seconds)

    async def _stream_raw(self, payload: dict[str, Any]) -> AsyncIterable[dict[str, Any]]:
        """POST /chat/completions (SSE). Yields parsed chunk dicts; skips `[DONE]`."""
        import httpx  # noqa: PLC0415 - optional dependency by design

        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout()) as client:
            async with client.stream("POST", "/chat/completions", json=payload, headers=self._headers()) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    yield json.loads(data)

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        temperature: float = 0.7,
        response_schema: dict | None = None,
        think: bool | None = None,  # noqa: ARG002 — no portable wire knob across OpenAI-compatible servers
        max_tokens: int | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        payload: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": oai.to_oai_messages(system, messages),
            "stream": True,
            "temperature": temperature,
        }
        # 공식 API는 stream_options 없이는 usage를 절대 안 보낸다 → 요청해서 실측
        # 사용량을 받는다. 커스텀 호환 엔드포인트(vLLM/게이트웨이 등)는 미지원 필드에
        # 400을 내는 서버가 있어 보내지 않는다 — 그런 서버가 usage를 상시 싣는 경우는
        # assemble_stream이 알아서 줍는다.
        if self._base_url == _DEFAULT_BASE_URL:
            payload["stream_options"] = {"include_usage": True}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": response_schema},
            }
        if tools:
            payload["tools"] = oai.to_oai_tools(tools)

        try:
            async for chunk in oai.assemble_stream(self._stream_raw(payload)):
                yield chunk
        except ProviderError:
            raise
        except Exception as exc:  # pragma: no cover - exercised via mocks in tests
            raise classify_provider_error(exc, self.name) from exc
