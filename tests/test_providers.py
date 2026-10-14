"""Provider seam tests for the real adapters.

These tests stay offline by mocking network edges. The fragile part of this
module is history conversion, so the native-message mappers are tested directly in
addition to the normalized `ProviderChunk` stream contract.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from arkagent.core.config import Config, ConfigStore, ProviderConfig
from arkagent.core.errors import ProviderError
from arkagent.core.types import Message, ToolCall
from arkagent.providers._openai_compat import to_oai_messages
from arkagent.providers.echo import EchoProvider
from arkagent.providers.interfaces import ILLMProvider
from arkagent.providers.ollama import OllamaProvider, _to_ollama_messages
from arkagent.providers.openai import OpenAIProvider
from arkagent.providers.registry import ProviderCredentials, ProviderRegistry


async def _collect(stream: AsyncIterator):
    return [chunk async for chunk in stream]


def _registry(provider: ProviderConfig, credentials=None) -> ProviderRegistry:
    return ProviderRegistry(ConfigStore(Config(provider=provider)), credentials)


def test_provider_protocols_are_structural():
    assert isinstance(EchoProvider(), ILLMProvider)
    assert isinstance(OllamaProvider(ProviderConfig(kind="ollama", model="m")), ILLMProvider)
    assert isinstance(OpenAIProvider(ProviderConfig(kind="openai", model="m"), api_key="k"), ILLMProvider)


def test_ollama_history_mapping_round_trip():
    call = ToolCall(tool_name="lookup", arguments={"q": "ark"}, id="call_1")
    messages = (
        Message(role="user", content="find it"),
        Message(role="assistant", content="", tool_calls=(call,)),
        Message(role="tool", content='{"ok": true}', metadata={"tool_call_id": "call_1"}),
    )

    native = _to_ollama_messages(messages)

    assert native[0] == {"role": "user", "content": "find it"}
    assert native[1]["tool_calls"][0]["function"] == {"name": "lookup", "arguments": {"q": "ark"}}
    assert native[2] == {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_1"}


@pytest.mark.asyncio
async def test_ollama_stream_emits_text_then_one_done(monkeypatch):
    provider = OllamaProvider(ProviderConfig(kind="ollama", model="qwen", base_url="http://ollama.local"))

    async def fake_stream_chat(payload):
        assert payload["model"] == "qwen"
        yield {"message": {"content": "hello "}, "done": False}
        yield {"message": {"content": "world"}, "done": True, "done_reason": "stop"}

    monkeypatch.setattr(provider, "_stream_chat", fake_stream_chat)
    chunks = await _collect(provider.stream(system="sys", messages=(Message(role="user", content="hi"),)))

    assert [chunk.kind for chunk in chunks] == ["text", "text", "done"]
    assert chunks[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_ollama_recovers_answer_leaked_after_thinking_close_tag(monkeypatch):
    """qwen3.6 실측: native thinking 필드가 닫는 태그 뒤의 최종 답까지 삼킨다.

    태그가 스트림 청크 사이에서 갈라져도 reasoning과 본문을 올바른 ProviderChunk
    채널로 분리해야 호출부가 thinking-only 빈 응답으로 오인하지 않는다.
    """
    provider = OllamaProvider(
        ProviderConfig(kind="ollama", model="qwen3.6", base_url="http://ollama.local")
    )

    async def fake_stream_chat(payload):
        yield {"message": {"thinking": "내부 추론입니다.</think"}, "done": False}
        yield {
            "message": {"thinking": "ing>\n\n파일을 만들었습니다."},
            "done": True,
            "done_reason": "stop",
        }

    monkeypatch.setattr(provider, "_stream_chat", fake_stream_chat)
    chunks = await _collect(
        provider.stream(system="sys", messages=(Message(role="user", content="hi"),))
    )

    assert [chunk.kind for chunk in chunks] == ["thinking", "thinking", "text", "done"]
    assert "".join(chunk.text or "" for chunk in chunks if chunk.kind == "thinking") == (
        "내부 추론입니다."
    )
    assert "".join(chunk.text or "" for chunk in chunks if chunk.kind == "text") == (
        "\n\n파일을 만들었습니다."
    )


# ── OpenAI-compatible wire shape ───────────────────────────────────
def test_oai_history_mapping_round_trip():
    call = ToolCall(tool_name="lookup", arguments={"q": "ark"}, id="call_1")
    messages = (
        Message(role="user", content="find it"),
        Message(role="assistant", content="", tool_calls=(call,)),
        Message(role="tool", content='{"ok": true}', metadata={"tool_call_id": "call_1"}),
    )

    native = to_oai_messages("sys", messages)

    assert native[0] == {"role": "system", "content": "sys"}
    assert native[1] == {"role": "user", "content": "find it"}
    assert native[2]["role"] == "assistant"
    assert native[2]["tool_calls"][0]["function"]["name"] == "lookup"
    assert json.loads(native[2]["tool_calls"][0]["function"]["arguments"]) == {"q": "ark"}
    assert native[3] == {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_1"}


@pytest.mark.asyncio
async def test_openai_stream_emits_text_then_one_done(monkeypatch):
    provider = OpenAIProvider(ProviderConfig(kind="openai", model="gpt-x"), api_key="sk-x")

    async def fake_raw(payload):
        assert payload["model"] == "gpt-x"
        yield {"choices": [{"delta": {"content": "hello "}}]}
        yield {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(provider, "_stream_raw", fake_raw)
    chunks = await _collect(provider.stream(system="sys", messages=(Message(role="user", content="hi"),)))

    assert [c.kind for c in chunks] == ["text", "text", "done"]
    assert chunks[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_openai_stream_assembles_tool_call_from_fragments(monkeypatch):
    provider = OpenAIProvider(ProviderConfig(kind="openai", model="gpt-x"), api_key="sk-x")

    async def fake_raw(payload):
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "lookup", "arguments": '{"q": '}}]}}]}
        yield {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"ark"}'}}]}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

    monkeypatch.setattr(provider, "_stream_raw", fake_raw)
    chunks = await _collect(provider.stream(system="sys", messages=(Message(role="user", content="hi"),)))

    assert [c.kind for c in chunks] == ["tool_call", "done"]
    assert chunks[0].tool_call.tool_name == "lookup"
    assert chunks[0].tool_call.arguments == {"q": "ark"}
    assert chunks[0].tool_call.id == "call_9"
    assert chunks[-1].stop_reason == "tool_use"


# ── local OpenAI-compatible servers need no key ────────────────────
def test_registry_builds_keyless_openai_for_a_local_endpoint():
    cfg = ProviderConfig(kind="openai", model="local-model", base_url="http://localhost:8080/v1/", timeout_seconds=900)

    provider = _registry(cfg).get("openai")

    assert provider.name == "openai"
    assert provider._base_url == "http://localhost:8080/v1"  # noqa: SLF001 - wire contract
    assert "Authorization" not in provider._headers()  # noqa: SLF001
    assert provider._timeout() == 900.0  # noqa: SLF001 - local servers get the local-inference timeout


def test_registry_rejects_openai_with_neither_key_nor_endpoint():
    with pytest.raises(ProviderError):
        _registry(ProviderConfig(kind="openai", model="gpt-x")).get("openai")


def test_registered_key_reaches_the_official_endpoint_with_its_own_timeout():
    cfg = ProviderConfig(kind="openai", model="gpt-x", timeout_seconds=900)

    provider = _registry(cfg, lambda _model: ProviderCredentials(api_key="sk-x")).get("openai")

    assert provider._base_url == "https://api.openai.com/v1"  # noqa: SLF001
    assert provider._headers()["Authorization"] == "Bearer sk-x"  # noqa: SLF001
    assert provider._timeout() == 120.0  # noqa: SLF001


def test_registry_reuses_one_instance_until_the_config_changes():
    store = ConfigStore(Config(provider=ProviderConfig(kind="echo", model="a")))
    registry = ProviderRegistry(store)

    first = registry.get("echo")
    assert registry.get("echo") is first

    store.update(Config(provider=ProviderConfig(kind="echo", model="b")))
    assert registry.get("echo") is not first


def test_registry_rejects_unknown_kind():
    registry = _registry(ProviderConfig(kind="echo", model="m"))

    assert registry.available() == ["echo", "ollama", "openai"]
    with pytest.raises(ProviderError):
        registry.get("nope")
