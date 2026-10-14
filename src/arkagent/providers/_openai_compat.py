"""Shared OpenAI-style chat helpers.

OpenAI and compatible endpoints use the `chat/completions` wire shape
(system/user/assistant/tool messages, `tools` as function specs, and
SSE `choices[].delta` streaming). This module holds the format mapping + the
streaming-delta assembler so each provider only owns its HTTP + auth.

It is provider-internal plumbing, NOT a widening of the FROZEN `ILLMProvider`
seam — providers still expose the normalized `stream()` and `ProviderChunk`s.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from typing import Any

from ..core.types import IMAGES_METADATA_KEY, Message, ToolCall, ToolSpec
from .interfaces import ProviderChunk


def to_oai_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _image_url_parts(message: Message) -> list[dict[str, Any]]:
    """metadata["images"] 첨부(core.types 계약) → OpenAI `image_url` 콘텐츠 파트
    (base64 data URL)."""
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{item.get('mime_type') or 'image/png'};base64,{item['data_b64']}"},
        }
        for item in (message.metadata.get(IMAGES_METADATA_KEY) or ())
        if isinstance(item, dict) and item.get("data_b64")
    ]


def to_oai_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    native: list[dict[str, Any]] = []
    if system:
        native.append({"role": "system", "content": system})
    # OpenAI wire는 tool 메시지에 이미지 파트를 허용하지 않는다 — tool 결과의
    # 첨부는 그 tool 응답 블록이 **끝난 뒤** 합성 user 메시지의 image_url 파트로
    # 싣는다(블록 중간에 끼우면 "모든 tool_call_id에 tool 응답이 와야 한다"는
    # 검증이 깨진다). Ollama는 자기 wire에 native로 싣는다.
    pending_tool_images: list[dict[str, Any]] = []

    def _flush_tool_images() -> None:
        if not pending_tool_images:
            return
        native.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "[Image(s) attached from the preceding tool result(s)]"},
                    *pending_tool_images,
                ],
            }
        )
        pending_tool_images.clear()

    for message in messages:
        if message.role != "tool":
            _flush_tool_images()
        if message.role == "system":
            native.append({"role": "system", "content": message.content})
            continue
        if message.role == "user":
            image_parts = _image_url_parts(message)
            if image_parts:
                content_parts: list[dict[str, Any]] = []
                if message.content:
                    content_parts.append({"type": "text", "text": message.content})
                native.append({"role": "user", "content": content_parts + image_parts})
            else:
                native.append({"role": "user", "content": message.content})
            continue
        if message.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.tool_name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            native.append(entry)
            continue
        if message.role == "tool":
            entry = {"role": "tool", "content": message.content}
            tool_call_id = message.metadata.get("tool_call_id")
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            native.append(entry)
            pending_tool_images.extend(_image_url_parts(message))
            continue
        native.append({"role": message.role, "content": message.content})
    _flush_tool_images()
    return native


_FINISH = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


async def assemble_stream(chunks: AsyncIterable[dict[str, Any]]) -> AsyncIterator[ProviderChunk]:
    """Turn OpenAI-style streamed `chunk` dicts into normalized `ProviderChunk`s.

    Handles incremental `delta.content` (text), `delta.tool_calls[]` fragments
    (assembled by index, arguments concatenated as JSON text), and `finish_reason`.
    Ends with exactly one `kind="done"`.
    """
    # index → {"id", "name", "args"} accumulated across deltas
    pending: dict[int, dict[str, str]] = {}
    finish: str | None = None
    # usage(prompt/completion tokens)를 싣는 서버가 있으면(공식 API의
    # stream_options.include_usage, vLLM/게이트웨이의 상시 보고 등) done 청크에
    # 실어 호출부가 이 provider에서도 실측 사용량을 받게 한다.
    input_tokens: int | None = None
    output_tokens: int | None = None

    async for chunk in chunks:
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("prompt_tokens", input_tokens)
            output_tokens = usage.get("completion_tokens", output_tokens)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        content = delta.get("content")
        if content:
            yield ProviderChunk(kind="text", text=content)

        # some servers stream reasoning as `reasoning_content`/`reasoning`
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield ProviderChunk(kind="thinking", text=reasoning)

        for tc in delta.get("tool_calls") or ():
            idx = tc.get("index", 0)
            slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

        if choice.get("finish_reason"):
            finish = choice["finish_reason"]

    # flush assembled tool calls (in index order) before the done chunk
    for _idx, slot in sorted(pending.items()):
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"]) if slot["args"] else {}
        except json.JSONDecodeError:
            args = {}
        kwargs: dict[str, Any] = {"tool_name": slot["name"], "arguments": args}
        if slot["id"]:
            kwargs["id"] = slot["id"]
        yield ProviderChunk(kind="tool_call", tool_call=ToolCall(**kwargs))

    stop = _FINISH.get(finish or "", "end_turn" if finish is None else "error")
    # if tool calls were produced but finish_reason was absent/odd, prefer tool_use
    if stop != "tool_use" and pending:
        stop = "tool_use"
    yield ProviderChunk(kind="done", stop_reason=stop, input_tokens=input_tokens, output_tokens=output_tokens)
