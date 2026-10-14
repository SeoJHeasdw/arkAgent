"""Shared domain contracts (DTOs) for arkagent.

`core/` is the one place every other package is allowed to import, so the shared
vocabulary lives here and the dependency graph stays a DAG:

    providers ─┐
    jobs ──────┼──> core  (leaf, depends on nothing else in arkagent)
    …  ────────┘

Nothing here knows about HTTP, storage, or any concrete module. These are plain
frozen dataclasses so they are cheap to construct, easy to test, and safe to pass
across a process boundary.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal


# ─────────────────────────────────────────────────────────────────────────────
# Identity / time helpers (kept here so every module mints ids the same way)
# ─────────────────────────────────────────────────────────────────────────────
def new_id(prefix: str = "") -> str:
    """A short, sortable-enough unique id. `prefix` aids log/debug readability."""
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw


def now_ms() -> int:
    return int(time.time() * 1000)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ─────────────────────────────────────────────────────────────────────────────
# Conversation primitives
# ─────────────────────────────────────────────────────────────────────────────
Role = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    id: str = field(default_factory=lambda: new_id("msg"))
    ts: int = field(default_factory=now_ms)
    # Tool-call bookkeeping for assistant/tool turns (provider-normalized).
    tool_calls: tuple[ToolCall, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """A tool advertised to the LLM. Provider adapters render this into their own
    function-calling schema (Ollama function-calling, OpenAI `tools`, …)."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the arguments object


@dataclass(frozen=True)
class ToolCall:
    """A request to run a tool, normalized across providers."""

    tool_name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: new_id("call"))


# ── 이미지 첨부 계약 (멀티모달 provider seam) ─────────────────────────────────
# 도구가 결과 dict에 `_images: [{"mime_type": str, "data_b64": str}]`를 실으면
# 호출부가 분리해 `ToolResult.images`로 나르고, tool Message의
# `metadata["images"]`에 부착한다. provider 어댑터는 이 metadata를 각자의 wire
# 이미지 블록으로 렌더한다(Ollama message.images / OpenAI-compat image_url 파트).
# base64가 텍스트 본문으로 새지 않는 것이 계약의 핵심.
TOOL_RESULT_IMAGES_KEY = "_images"
IMAGES_METADATA_KEY = "images"


def split_image_attachments(result: Any) -> tuple[Any, tuple[dict[str, str], ...]]:
    """도구 결과에서 `_images` 첨부를 분리한다 → (본문 결과, 검증된 첨부 튜플).

    첨부가 없거나 dict가 아니면 결과를 그대로 돌려준다. 항목 검증은 관대하게 —
    mime_type/data_b64가 둘 다 비지 않은 dict만 남긴다(잘못된 항목이 작업을 깨면
    안 된다)."""
    if not isinstance(result, dict) or TOOL_RESULT_IMAGES_KEY not in result:
        return result, ()
    raw = result.get(TOOL_RESULT_IMAGES_KEY)
    images = tuple(
        {"mime_type": str(item["mime_type"]), "data_b64": str(item["data_b64"])}
        for item in (raw if isinstance(raw, list) else ())
        if isinstance(item, dict) and item.get("mime_type") and item.get("data_b64")
    )
    cleaned = {k: v for k, v in result.items() if k != TOOL_RESULT_IMAGES_KEY}
    return cleaned, images


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    ok: bool
    result: Any = None
    error: str | None = None
    # 도구가 첨부한 이미지(위 계약). 빈 튜플 = 첨부 없음.
    images: tuple[dict[str, str], ...] = ()
