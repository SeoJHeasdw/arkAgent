"""Provider registry — the one place `Config.provider.kind` maps to an impl.

Implements `IProviderRegistry`. Callers ask it for the configured provider and
never name a concrete one. New providers register here (a one-line factory entry),
keeping the swap seam closed.

Real providers are lazily imported so `echo` works with no network dependencies.

Credentials (API key, custom base_url) are not in config.yaml. The registry takes
a `credentials` lookup (model_id → ProviderCredentials) so it can build a provider
with the active model's secret at resolve time (re-evaluated on every `get`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..core.config import ConfigStore, ProviderConfig
from ..core.errors import ProviderError
from .echo import EchoProvider
from .interfaces import ILLMProvider, IProviderRegistry

if TYPE_CHECKING:
    from .ollama_lifecycle import OllamaLifecycleManager


@dataclass(frozen=True)
class ProviderCredentials:
    """Per-model secret/connection info."""

    api_key: str | None = None
    base_url: str | None = None  # OpenAI-compatible custom endpoint


CredentialLookup = Callable[[str], "ProviderCredentials | None"]


class ProviderRegistry(IProviderRegistry):
    def __init__(
        self,
        config_store: ConfigStore,
        credentials: CredentialLookup | None = None,
        *,
        ollama_lifecycle: "OllamaLifecycleManager | None" = None,
    ) -> None:
        # Holds the store (not a snapshot) so `get` builds providers with the
        # *current* provider config after a runtime settings change.
        self._config_store = config_store
        self._credentials = credentials
        self._factories: dict[str, Callable[[ProviderConfig, ProviderCredentials | None], ILLMProvider]] = {
            "echo": lambda _cfg, _creds: EchoProvider(),
            "ollama": self._make_ollama,
            "openai": self._make_openai,
        }
        # 단일 슬롯 인스턴스 캐시 — 같은 설정이면 같은 provider를 돌려준다.
        # 매번 새 인스턴스를 만들면 provider가 보유한 HTTP 커넥션 풀/SDK
        # 클라이언트가 매번 버려진다(연쇄 요청마다 TCP 재수립). 시그니처가
        # 바뀌면(모델 스위치 등) 새로 만든다 — 라이브 설정 반영은 유지된다.
        self._cache: tuple[tuple, ILLMProvider] | None = None
        # Lazily constructed because httpx/Ollama remain optional in echo-only
        # installations. Every Ollama provider shares this one residency graph.
        self._ollama_lifecycle = ollama_lifecycle

    def available(self) -> list[str]:
        return list(self._factories.keys())

    def get(self, kind: str) -> ILLMProvider:
        factory = self._factories.get(kind)
        if factory is None:
            raise ProviderError(f"unknown provider kind: {kind}")
        cfg = self._config_store.config.provider
        creds = self._credentials(cfg.model) if self._credentials else None
        signature = (
            kind, cfg.model, cfg.base_url, cfg.timeout_seconds,
            getattr(cfg, "num_ctx", None), getattr(cfg, "keep_alive", None),
            getattr(cfg, "idle_unload_seconds", None),
            (creds.api_key, creds.base_url) if creds else None,
        )
        if self._cache is not None and self._cache[0] == signature:
            return self._cache[1]
        provider = factory(cfg, creds)
        # 이전 인스턴스는 참조만 놓는다 — in-flight 스트림이 아직 쓰고 있을 수
        # 있으므로(설정 변경은 다음 호출부터) 강제로 닫지 않고 GC에 맡긴다.
        self._cache = (signature, provider)
        return provider

    def ollama_lifecycle(self) -> "OllamaLifecycleManager":
        if self._ollama_lifecycle is None:
            from .ollama_lifecycle import OllamaLifecycleManager  # noqa: PLC0415

            self._ollama_lifecycle = OllamaLifecycleManager()
        return self._ollama_lifecycle

    async def provider_changed(self, old: ProviderConfig, new: ProviderConfig) -> None:
        """Retire a switched-away local model without disturbing in-flight work."""

        from .ollama_lifecycle import OllamaModelKey  # noqa: PLC0415

        old_key = OllamaModelKey.create(old.base_url, old.model) if old.kind == "ollama" else None
        new_key = OllamaModelKey.create(new.base_url, new.model) if new.kind == "ollama" else None
        if old_key == new_key:
            return

        lifecycle = self._ollama_lifecycle
        if lifecycle is not None and old_key is not None:
            await lifecycle.retire(old_key)
        if lifecycle is not None and new_key is not None:
            await lifecycle.activate(new_key)
        # New resolutions build from the new settings. Any in-flight work retains
        # its own strong reference to the old instance until it exits its lease.
        self._cache = None

    def cancel_pending(self) -> None:
        """Synchronous fallback for shutdown paths that cannot await."""

        if self._ollama_lifecycle is not None:
            self._ollama_lifecycle.cancel_pending()

    async def shutdown(self) -> None:
        """Unload touched Ollama models and close the shared inference pool."""

        if self._ollama_lifecycle is not None:
            await self._ollama_lifecycle.shutdown()
        from .ollama import close_shared_client  # noqa: PLC0415

        await close_shared_client()

    # ── lazy real providers ──────────────────────────────────────────────────
    def _make_ollama(self, cfg: ProviderConfig, _creds: ProviderCredentials | None) -> ILLMProvider:
        try:
            from .ollama import OllamaProvider  # noqa: PLC0415 - lazy by design
        except ImportError as exc:  # pragma: no cover
            raise ProviderError("ollama provider not available") from exc
        return OllamaProvider(cfg, lifecycle=self.ollama_lifecycle())

    def _make_openai(self, cfg: ProviderConfig, creds: ProviderCredentials | None) -> ILLMProvider:
        api_key = creds.api_key if creds else None
        base_url = (creds.base_url if creds else None) or cfg.base_url
        # 공식 API는 키가 필수다. 로컬 OpenAI 호환 서버(llama.cpp·LM Studio·MLX 서버)는
        # 키 없이 받으므로 base_url이 있으면 키를 요구하지 않는다.
        if not api_key and not base_url:
            raise ProviderError(
                "openai provider needs an API key, or a base_url pointing at an OpenAI-compatible server"
            )
        from .openai import OpenAIProvider  # noqa: PLC0415 - lazy by design

        return OpenAIProvider(cfg, api_key=api_key, base_url=base_url)
